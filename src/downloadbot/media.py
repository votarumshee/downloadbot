"""Prepare locally downloaded media for Telegram's inline video player."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .downloader import public_error


def _tool(name: str, location: str | None) -> str | None:
    if location:
        candidate = Path(location)
        directory = candidate if candidate.is_dir() else candidate.parent
        for suffix in ("", ".exe"):
            executable = directory / f"{name}{suffix}"
            if executable.is_file():
                return str(executable.resolve())
        if name == "ffmpeg" and candidate.is_file():
            return str(candidate.resolve())
    return shutil.which(name)


def prepare_mp4(path: Path, *, max_file_bytes: int, ffmpeg_location: str | None = None) -> Path:
    """Remux H.264/AAC without quality loss, transcode other codecs when needed.

    Runs inside the bounded download child: its parent enforces the shared time,
    disk and cancellation limits and terminates the entire process tree.
    FFmpeg and FFprobe may read local media only, never network protocols.
    """
    ffmpeg = _tool("ffmpeg", ffmpeg_location)
    if not ffmpeg:
        raise public_error("ffmpeg_missing")
    ffprobe = _tool("ffprobe", ffmpeg_location)
    video_copy = audio_copy = False
    output = path.parent / "prepared.mp4"
    try:
        if ffprobe:
            with tempfile.TemporaryFile() as probe_output:
                subprocess.run(
                    [ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                     "-show_entries", "stream=codec_type,codec_name,pix_fmt", "-of", "json", str(path)],
                    stdin=subprocess.DEVNULL, stdout=probe_output, stderr=subprocess.DEVNULL,
                    check=True, timeout=20,
                )
                probe_output.seek(0)
                raw = probe_output.read(65537)
            if len(raw) > 65536:
                raise ValueError("Invalid media metadata")
            streams = json.loads(raw)["streams"]
            video = next(stream for stream in streams if stream.get("codec_type") == "video")
            audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
            video_copy = video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
            audio_copy = audio is None or audio.get("codec_name") == "aac"
        # Without FFprobe, encoding both streams gives the same compatibility
        # guarantee. Production images include both executables.
        video_args = ["-c:v", "copy"] if video_copy else [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-threads", "2",
        ]
        audio_args = ["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "128k"]
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
             "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
             *video_args, *audio_args, "-movflags", "+faststart", str(output)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=True, timeout=300,
        )
        if not output.is_file() or not output.stat().st_size:
            raise ValueError("Empty prepared media")
        if output.stat().st_size > max_file_bytes:
            raise public_error("size_limit")
        destination = path.with_suffix(".mp4")
        output.replace(destination)
        if path != destination:
            path.unlink()
        return destination
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        raise public_error("download_failed") from None
    finally:
        output.unlink(missing_ok=True)
