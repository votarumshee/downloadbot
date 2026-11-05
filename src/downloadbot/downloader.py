"""Bounded subprocess wrapper around yt-dlp. No account secrets enter argv."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .url_policy import validate_url


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    title: str
    ext: str
    size_bytes: int
    duration: float | None


class DownloadError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


_PUBLIC_MESSAGES = {
    "auth_required": "This video requires an account with access or refreshed account cookies.",
    "rate_limited": "The video service is rate limiting requests; try again later.",
    "unavailable": "This video is unavailable, unsupported, live, or contains multiple videos.",
    "size_limit": "The video exceeds the configured file size limit.",
    "duration_limit": "The video exceeds the configured duration limit.",
    "network_error": "The video service could not be reached safely; try again later.",
    "timeout": "The download exceeded the configured time limit.",
    "cancelled": "The download was cancelled.",
    "download_failed": "The video could not be downloaded.",
    "ffmpeg_missing": "This video has separate audio and video streams; install ffmpeg on the server.",
}
_RETRYABLE = {"rate_limited", "network_error", "timeout"}
_MEDIA_EXTENSIONS = {"mp4", "webm", "mkv", "mov", "m4v", "3gp", "flv", "ts"}


def public_error(code: str) -> DownloadError:
    code = code if isinstance(code, str) and code in _PUBLIC_MESSAGES else "download_failed"
    return DownloadError(code, _PUBLIC_MESSAGES[code], code in _RETRYABLE)


def _stop_process(process: subprocess.Popen) -> None:
    """Terminate the worker and its local ffmpeg/JS children."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class YtDlpDownloader:
    def __init__(
        self, max_file_bytes: int = 500 * 1024 * 1024,
        max_duration_seconds: int = 3600, timeout_seconds: int = 600,
        ffmpeg_location: str | None = None,
    ):
        if min(max_file_bytes, max_duration_seconds, timeout_seconds) <= 0:
            raise ValueError("Download limits must be positive.")
        self.max_file_bytes = max_file_bytes
        self.max_duration_seconds = max_duration_seconds
        self.timeout_seconds = timeout_seconds
        self.ffmpeg_location = ffmpeg_location

    def download(
        self, *, url: str, output_dir: Path, cookie_file: Path | None,
        max_height: int, cancelled: Callable[[], bool],
    ) -> DownloadResult:
        url, platform = validate_url(url)
        if max_height < 144 or max_height > 4320:
            raise ValueError("Maximum height must be between 144 and 4320.")
        if cancelled():
            raise public_error("cancelled")
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.iterdir()):
            raise ValueError("Download output directory must be empty.")
        if cookie_file is not None:
            cookie_file = Path(cookie_file).resolve(strict=True)
            if cookie_file.is_relative_to(output_dir):
                raise ValueError("Account cookies must be outside the download directory.")
        config = {
            "url": url, "platform": platform, "output_dir": str(output_dir),
            "cookie_file": str(cookie_file) if cookie_file else None,
            "max_file_bytes": self.max_file_bytes,
            "max_duration_seconds": self.max_duration_seconds,
            "max_height": max_height, "ffmpeg_location": self.ffmpeg_location,
        }
        # The worker (and its JS/ffmpeg children) needs runtime paths, never the
        # API token, encryption key, database URL or Telegram bot credentials.
        allowed_environment = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
            "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "LC_CTYPE",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed_environment}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env["PYTHONUTF8"] = "1"
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        with tempfile.TemporaryFile() as result_file:
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "downloadbot.download_process"],
                    stdin=subprocess.PIPE, stdout=result_file, stderr=subprocess.DEVNULL,
                    env=env, **kwargs,
                )
            except OSError:
                raise public_error("download_failed") from None
            start = time.monotonic()
            try:
                try:
                    process.stdin.write(json.dumps(config).encode("utf-8"))
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    raise public_error("download_failed") from None
                while process.poll() is None:
                    if cancelled():
                        raise public_error("cancelled")
                    if time.monotonic() - start >= self.timeout_seconds:
                        raise public_error("timeout")
                    # Bound temporary media (video + audio + merge output) even
                    # while a postprocessor has no yt-dlp progress callbacks.
                    total = 0
                    for partial in output_dir.iterdir():
                        try:
                            if partial.is_file():
                                total += partial.stat().st_size
                        except FileNotFoundError:
                            # ffmpeg/yt-dlp may finish renaming a part between
                            # directory enumeration and the size check.
                            continue
                    if total > self.max_file_bytes * 3 + 1024 * 1024:
                        raise public_error("size_limit")
                    time.sleep(0.1)
                if cancelled():
                    raise public_error("cancelled")
            finally:
                _stop_process(process)
            result_file.seek(0)
            raw = result_file.read(16385)
        try:
            if len(raw) > 16384:
                raise ValueError("oversized result")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("invalid result")
        except (ValueError, UnicodeDecodeError):
            raise public_error("download_failed") from None
        if process.returncode != 0 or not result.get("ok"):
            raise public_error(result.get("code", "download_failed"))
        filename = result.get("filename", "")
        ext = result.get("ext", "")
        if ext not in _MEDIA_EXTENSIONS or filename != f"media.{ext}":
            raise public_error("download_failed")
        path = output_dir / filename
        if path.is_symlink() or not path.is_file() or path.resolve().parent != output_dir:
            raise public_error("download_failed")
        size = path.stat().st_size
        if size > self.max_file_bytes:
            raise public_error("size_limit")
        if size == 0:
            raise public_error("download_failed")
        duration = result.get("duration")
        return DownloadResult(path, str(result.get("title") or "Video")[:512], ext, size, duration)
