"""Internal child process. Its stdout is a small, sanitized JSON result only."""

import contextlib
import ipaddress
import json
import math
import os
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .downloader import _MEDIA_EXTENSIONS, DownloadError, public_error
from .url_policy import validate_url


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if isinstance(ip, ipaddress.IPv6Address) and any(ip in network for network in (
            ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
            ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"),
        )):
            # Translation/tunnel ranges can hide a private IPv4 destination.
            return False
        return ip.is_global and not ip.is_multicast and not ip.is_unspecified
    except ValueError:
        return False


def install_network_guard() -> None:
    """Validate every DNS answer and pin every connection to its checked IP.

    Only Python's urllib HTTP handler is enabled below, so native libraries
    cannot bypass these socket hooks. Proxy use and network ffmpeg are disabled.
    An infrastructure egress firewall is still recommended as defense in depth.
    """
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        results = original_getaddrinfo(host, port, *args, **kwargs)
        if not results or any(not _public_ip(result[4][0]) for result in results):
            raise OSError("Network destination is not public.")
        return results

    def checked_address(sock, address):
        if sock.family not in {socket.AF_INET, socket.AF_INET6} or not isinstance(address, tuple):
            raise OSError("Unsupported network destination.")
        host, port = address[:2]
        # Numeric addresses are checked again at connect time to prevent a
        # changed resolver answer or a direct connection bypassing DNS checks.
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
        except (ValueError, AttributeError):
            results = guarded_getaddrinfo(host, port, sock.family, sock.type)
            return results[0][4]
        if not _public_ip(host):
            raise OSError("Network destination is not public.")
        return address

    def guarded_connect(sock, address):
        return original_connect(sock, checked_address(sock, address))

    def guarded_connect_ex(sock, address):
        return original_connect_ex(sock, checked_address(sock, address))

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex


class QuietLogger:
    def debug(self, message, *args, **kwargs):
        pass

    info = debug
    warning = debug
    error = debug


def classify_error(error: Exception) -> str:
    """Inspect diagnostics locally; never return their text or request URLs."""
    if isinstance(error, DownloadError):
        return error.code
    message = str(error).lower()[:16384]
    if any(word in message for word in ("429", "too many requests", "rate limit")):
        return "rate_limited"
    if any(word in message for word in ("sign in", "sign-in", "login", "log in", "cookies", "authentication", "401", "unauthorized", "private video", "members-only", "confirm your age", "not a bot")):
        return "auth_required"
    if any(word in message for word in ("timed out", "timeout", "network", "connection", "resolve", "getaddrinfo", "certificate", "ssl", "destination is not public")):
        return "network_error"
    if any(word in message for word in ("unavailable", "not available", "removed", "403", "404", "forbidden", "not found", "copyright", "geo restricted", "country", "no video", "unsupported url", "requested format", "drm")):
        return "unavailable"
    return "download_failed"


def _duration(info: dict) -> float | None:
    value = info.get("duration")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def perform_download(config: dict) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.external import FFmpegFD
    from yt_dlp.extractor import get_info_extractor
    from yt_dlp.globals import all_plugins_loaded, plugin_dirs
    from yt_dlp.networking._urllib import UrllibRH

    # Local plugin discovery must not load arbitrary code into the worker.
    all_plugins_loaded.value = True
    plugin_dirs.value = []
    url, platform = validate_url(config["url"])
    if platform == "vimeo" and not config.get("cookie_file") and urlsplit(url).hostname == "vimeo.com":
        # Vimeo's website API currently requires login even for public videos;
        # its public embed player remains a supported anonymous entry point.
        parts = urlsplit(url).path.strip("/").split("/")
        suffix = f"?h={parts[1]}" if len(parts) > 1 else ""
        url = f"https://player.vimeo.com/video/{parts[0]}{suffix}"
    output_dir = Path(config["output_dir"]).resolve()
    max_size = int(config["max_file_bytes"])
    max_duration = int(config["max_duration_seconds"])
    max_height = int(config["max_height"])

    def no_network_ffmpeg(*args, **kwargs):
        raise public_error("unavailable")

    FFmpegFD.real_download = no_network_ffmpeg

    def check_metadata(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise public_error("unavailable")
        if info.get("has_drm"):
            raise public_error("unavailable")
        duration = _duration(info)
        if duration is not None and duration > max_duration:
            raise public_error("duration_limit")
        known_size = info.get("filesize") or info.get("filesize_approx")
        if isinstance(known_size, (int, float)) and known_size > max_size:
            raise public_error("size_limit")
        return None

    def progress(event):
        if (event.get("downloaded_bytes") or 0) > max_size or (event.get("total_bytes") or 0) > max_size:
            raise public_error("size_limit")
        # All stream parts count toward the limit; merge can temporarily double
        # storage and is independently capped by the parent process.
        if event.get("status") == "downloading":
            total = sum(path.stat().st_size for path in output_dir.iterdir() if path.is_file())
            if total > max_size:
                raise public_error("size_limit")

    class RestrictedYoutubeDL(YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            return super().build_request_director([UrllibRH], [])

        def extract_info(self, target_url, *args, **kwargs):
            # Re-check transparent extractor redirects as well as entry URLs.
            target_url, target_platform = validate_url(target_url)
            if target_platform != platform:
                raise public_error("unavailable")
            ie_key = kwargs.get("ie_key")
            if ie_key is not None and ie_key.lower() != extractor_key.lower():
                raise public_error("unavailable")
            return super().extract_info(target_url, *args, **kwargs)

        def process_ie_result(self, result, *args, **kwargs):
            if result.get("_type", "video") in {"playlist", "multi_video"} or "entries" in result:
                raise public_error("unavailable")
            check_metadata(result)
            formats = [
                item for item in result.get("formats", [])
                if item.get("height") is None or item["height"] <= max_height
            ]
            if not ffmpeg_present and formats:
                has_video = any(item.get("vcodec") != "none" and item.get("acodec") == "none" for item in formats)
                has_audio = any(item.get("vcodec") == "none" and item.get("acodec") != "none" for item in formats)
                has_combined = any(item.get("vcodec") != "none" and item.get("acodec") != "none" for item in formats)
                if has_video and has_audio and not has_combined:
                    raise public_error("ffmpeg_missing")
            return super().process_ie_result(result, *args, **kwargs)

        def dl(self, name, info, *args, **kwargs):
            # Also cover yt-dlp's small format-probing downloads, which run
            # before process_info and must obey the same downloader policy.
            for stream in info.get("requested_formats") or [info]:
                protocol = stream.get("protocol", "")
                if protocol not in {"http", "https", "m3u8", "m3u8_native", "http_dash_segments"}:
                    raise public_error("unavailable")
                if stream.get("ext") not in _MEDIA_EXTENSIONS | {"m4a", "opus", "ogg", "aac", "mp3"}:
                    raise public_error("unavailable")
            return super().dl(name, info, *args, **kwargs)

        def process_info(self, info):
            check_metadata(info)
            return super().process_info(info)

    ffmpeg_location = config.get("ffmpeg_location")
    if ffmpeg_location:
        ffmpeg_path = Path(ffmpeg_location)
        ffmpeg_present = ffmpeg_path.is_file() or any((ffmpeg_path / name).is_file() for name in ("ffmpeg", "ffmpeg.exe"))
    else:
        ffmpeg_present = bool(shutil.which("ffmpeg"))
    format_selector = f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best[height<=?{max_height}]" if ffmpeg_present else f"best[height<={max_height}]/best[height<=?{max_height}]"
    params = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": QuietLogger(), "cachedir": False, "proxy": "",
        "enable_file_urls": False, "socket_timeout": 20,
        "retries": 1, "fragment_retries": 1, "extractor_retries": 1,
        "concurrent_fragment_downloads": 1, "noplaylist": True,
        "format": format_selector, "outtmpl": str(output_dir / "media.%(ext)s"),
        "paths": {"temp": str(output_dir)}, "check_formats": "selected",
        "restrictfilenames": True, "overwrites": True, "continuedl": False,
        "max_filesize": max_size, "match_filter": check_metadata,
        "progress_hooks": [progress], "hls_prefer_native": True,
        "skip_unavailable_fragments": False, "fixup": "never",
        "merge_output_format": "mkv", "ffmpeg_location": ffmpeg_location,
        # ffmpeg only sees already downloaded local files, including any nested
        # references in a crafted media container.
        "postprocessor_args": {"ffmpeg_i": ["-protocol_whitelist", "file,pipe"]},
        "cookiefile": config.get("cookie_file"), "usenetrc": False,
        "js_runtimes": {"deno": {}} if shutil.which("deno") else ({"node": {}} if shutil.which("node") else {}),
        "remote_components": set(),
    }
    extractor_key = {"youtube": "Youtube", "x": "Twitter", "instagram": "Instagram", "vimeo": "Vimeo"}[platform]
    with RestrictedYoutubeDL(params, auto_init=False) as ydl:
        ydl.add_info_extractor(get_info_extractor(extractor_key)())
        info = ydl.extract_info(url, download=True)
    if not isinstance(info, dict):
        raise public_error("unavailable")
    candidates = [path for path in output_dir.iterdir() if path.is_file() and path.name == f"media.{path.suffix.lstrip('.')}" and path.suffix.lstrip(".") in _MEDIA_EXTENSIONS]
    if len(candidates) != 1:
        raise public_error("download_failed")
    path = candidates[0]
    if path.stat().st_size > max_size:
        raise public_error("size_limit")
    if not path.stat().st_size:
        raise public_error("download_failed")
    title = "".join(char for char in str(info.get("title") or "Video") if ord(char) >= 32 and ord(char) != 127)[:512]
    return {"ok": True, "filename": path.name, "title": title, "ext": path.suffix[1:], "duration": _duration(info)}


def main() -> int:
    try:
        config_raw = sys.stdin.buffer.read(16385)
        if len(config_raw) > 16384:
            raise ValueError("Invalid worker configuration")
        config = json.loads(config_raw)
        install_network_guard()
        # Extractors sometimes print directly rather than use their logger.
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = perform_download(config)
        exit_code = 0
    except Exception as error:
        result = {"ok": False, "code": classify_error(error)}
        exit_code = 1
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
