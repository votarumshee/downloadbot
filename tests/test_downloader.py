import io
import json
import socket
from pathlib import Path

import pytest

from downloadbot import download_process, downloader
from downloadbot.download_process import _public_ip, classify_error, install_network_guard
from downloadbot.downloader import DownloadError, YtDlpDownloader


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.1", "172.16.1.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
    "224.0.0.1", "ff02::1", "not-an-ip",
    "64:ff9b::7f00:1", "64:ff9b:1::c0a8:1", "2002:7f00:1::", "2001::1",
])
def test_network_policy_rejects_nonpublic_addresses(ip):
    assert not _public_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_network_policy_accepts_public_addresses(ip):
    assert _public_ip(ip)


def test_dns_guard_rejects_mixed_public_private_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    # Register the original methods with monkeypatch so teardown restores all.
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    install_network_guard()
    with pytest.raises(OSError, match="not public"):
        socket.getaddrinfo("example.org", 443)
    with socket.socket() as sock:
        with pytest.raises(OSError, match="not public"):
            sock.connect(("127.0.0.1", 443))
        with pytest.raises(OSError, match="not public"):
            sock.connect_ex(("169.254.169.254", 80))


def test_dns_guard_pins_resolved_public_address(monkeypatch):
    connected = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    monkeypatch.setattr(socket.socket, "connect", lambda self, address: connected.append(address))
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    install_network_guard()
    with socket.socket() as sock:
        sock.connect(("example.org", 443))
    assert connected == [("8.8.8.8", 443)]


@pytest.mark.parametrize(("message", "code"), [
    ("HTTP Error 429 secret-token", "rate_limited"),
    ("Sign in to confirm you're not a bot token=SECRET", "auth_required"),
    ("Private video", "auth_required"),
    ("HTTP Error 404", "unavailable"),
    ("HTTP Error 401: Unauthorized", "auth_required"),
    ("HTTP Error 403: Forbidden", "unavailable"),
    ("Connection timed out https://example.test?token=SECRET", "network_error"),
    ("Unexpected value: SECRET", "download_failed"),
    ("getaddrinfo failed", "network_error"),
])
def test_error_classification_is_sanitized(message, code):
    assert classify_error(Exception(message)) == code
    error = downloader.public_error(code)
    assert "SECRET" not in str(error)
    assert "token" not in str(error)


class FakeProcess:
    def __init__(self, args, *, stdin, stdout, stderr, env, **kwargs):
        self.args = args
        self.stdin = io.BytesIO()
        self.stdout_file = stdout
        self.returncode = None
        self.pid = 123456
        self.stopped = False
        self.env = env

    def poll(self):
        return self.returncode


def test_parent_timeout_terminates_child(monkeypatch, tmp_path):
    processes = []
    monkeypatch.setenv("DOWNLOADBOT_ENCRYPTION_KEY", "SECRET")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET")
    monkeypatch.setenv("HTTPS_PROXY", "http://secret.invalid")

    def spawn(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(downloader.subprocess, "Popen", spawn)
    monkeypatch.setattr(downloader, "_stop_process", lambda p: setattr(p, "stopped", True))
    times = iter([0.0, 2.0])
    monkeypatch.setattr(downloader.time, "monotonic", lambda: next(times))
    with pytest.raises(DownloadError) as error:
        YtDlpDownloader(timeout_seconds=1).download(
            url="https://youtu.be/BaW_jenozKc", output_dir=tmp_path / "output",
            cookie_file=None, max_height=720, cancelled=lambda: False,
        )
    assert error.value.code == "timeout"
    assert processes[0].stopped
    assert processes[0].args[-1] == "downloadbot.download_process"
    assert len(processes[0].args) == 3  # Config and account paths never in argv.
    assert "DOWNLOADBOT_ENCRYPTION_KEY" not in processes[0].env
    assert "TELEGRAM_BOT_TOKEN" not in processes[0].env
    assert "HTTPS_PROXY" not in processes[0].env


def test_parent_cancellation_terminates_child(monkeypatch, tmp_path):
    processes = []

    def spawn(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(downloader.subprocess, "Popen", spawn)
    monkeypatch.setattr(downloader, "_stop_process", lambda p: setattr(p, "stopped", True))
    states = iter([False, True])
    with pytest.raises(DownloadError) as error:
        YtDlpDownloader().download(
            url="https://youtu.be/BaW_jenozKc", output_dir=tmp_path / "output",
            cookie_file=None, max_height=720, cancelled=lambda: next(states),
        )
    assert error.value.code == "cancelled"
    assert processes[0].stopped


def test_parent_rejects_untrusted_child_filename(monkeypatch, tmp_path):
    class CompletedProcess(FakeProcess):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stdout_file.write(json.dumps({"ok": True, "filename": "../secret.mp4", "ext": "mp4"}).encode())
            self.returncode = 0

    monkeypatch.setattr(downloader.subprocess, "Popen", CompletedProcess)
    with pytest.raises(DownloadError) as error:
        YtDlpDownloader().download(
            url="https://youtu.be/BaW_jenozKc", output_dir=tmp_path / "output",
            cookie_file=None, max_height=720, cancelled=lambda: False,
        )
    assert error.value.code == "download_failed"


def test_cookie_file_cannot_be_inside_served_directory(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    cookie = output / "cookies.txt"
    cookie.write_text("secret")
    with pytest.raises(ValueError):
        YtDlpDownloader().download(
            url="https://youtu.be/BaW_jenozKc", output_dir=output,
            cookie_file=cookie, max_height=720, cancelled=lambda: False,
        )


def test_duration_parser_excludes_nonfinite_values():
    assert download_process._duration({"duration": 2}) == 2.0
    assert download_process._duration({"duration": float("nan")}) is None
    assert download_process._duration({"duration": -1}) is None


def _fake_extractor(monkeypatch, result):
    import yt_dlp.extractor
    from yt_dlp.downloader.external import FFmpegFD
    from yt_dlp.extractor.common import InfoExtractor

    class FixtureIE(InfoExtractor):
        _VALID_URL = r"https://www\.youtube\.com/watch\?v=(?P<id>[\w-]+)"

        def _real_extract(self, url):
            return result.copy()

    monkeypatch.setattr(yt_dlp.extractor, "get_info_extractor", lambda name: FixtureIE)
    monkeypatch.setattr(FFmpegFD, "real_download", FFmpegFD.real_download)
    monkeypatch.setattr(download_process.shutil, "which", lambda name: None)


def _child_config(tmp_path):
    return {
        "url": "https://youtu.be/BaW_jenozKc", "output_dir": str(tmp_path),
        "max_file_bytes": 1024, "max_duration_seconds": 60, "max_height": 720,
        "cookie_file": None, "ffmpeg_location": None,
    }


@pytest.mark.parametrize(("extra", "code"), [
    ({"_type": "playlist", "entries": []}, "unavailable"),
    ({"is_live": True}, "unavailable"),
    ({"has_drm": True}, "unavailable"),
    ({"duration": 61}, "duration_limit"),
    ({"filesize": 1025}, "size_limit"),
    ({"formats": [
        {"height": 360, "vcodec": "h264", "acodec": "none"},
        {"vcodec": "none", "acodec": "aac"},
    ]}, "ffmpeg_missing"),
])
def test_child_rejects_metadata_limits_before_downloading(monkeypatch, tmp_path, extra, code):
    _fake_extractor(monkeypatch, {"id": "BaW_jenozKc", "title": "Fixture", **extra})
    with pytest.raises(DownloadError) as error:
        download_process.perform_download(_child_config(tmp_path))
    assert error.value.code == code
    assert not list(tmp_path.iterdir())


def test_child_downloads_with_real_ytdlp_pipeline_and_fixed_filename(monkeypatch, tmp_path):
    from yt_dlp.downloader.http import HttpFD

    _fake_extractor(monkeypatch, {
        "id": "BaW_jenozKc", "title": "Unsafe/name\nTitle", "duration": 1,
        "url": "https://cdn.example.test/movie.mp4?secret=private",
        "ext": "mp4", "height": 360, "vcodec": "h264", "acodec": "aac",
    })

    def download_fixture(self, filename, info_dict):
        Path(filename).write_bytes(b"video-content")
        self._hook_progress({"status": "finished", "downloaded_bytes": 13, "filename": filename}, info_dict)
        return True

    monkeypatch.setattr(HttpFD, "real_download", download_fixture)
    result = download_process.perform_download(_child_config(tmp_path))
    assert result == {"ok": True, "filename": "media.mp4", "title": "Unsafe/nameTitle", "ext": "mp4", "duration": 1.0}
    assert [path.name for path in tmp_path.iterdir()] == ["media.mp4"]
    assert (tmp_path / "media.mp4").read_bytes() == b"video-content"
