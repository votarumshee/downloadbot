"""Real FFmpeg conversions: container, codecs, faststart and failure handling."""

import os
import shutil
import subprocess

import pytest

from downloadbot.downloader import DownloadError
from downloadbot.media import prepare_mp4

FFMPEG = os.environ.get("DOWNLOADBOT_TEST_FFMPEG") or shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="requires FFmpeg")


def fixture_video(path, *, compatible=False, audio=True):
    command = [FFMPEG, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x48:r=10:d=1"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    command += ["-c:v", "libx264" if compatible else "mpeg4", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac" if compatible else "pcm_s16le"]
    subprocess.run(command + [str(path)], check=True, capture_output=True, timeout=20)


@pytest.mark.parametrize(("compatible", "audio"), [(False, True), (True, True), (False, False)])
def test_mp4_is_playable_with_compatible_codecs_and_faststart(tmp_path, compatible, audio):
    source = tmp_path / "media.mkv"
    fixture_video(source, compatible=compatible, audio=audio)
    output = prepare_mp4(source, max_file_bytes=1_000_000, ffmpeg_location=FFMPEG)
    assert output == tmp_path / "media.mp4"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["media.mp4"]
    data = output.read_bytes()
    assert data[4:8] == b"ftyp"
    assert 0 < data.index(b"moov") < data.index(b"mdat")
    decoded = subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-protocol_whitelist", "file,pipe",
         "-i", str(output), "-f", "null", "-"],
        capture_output=True, check=True, timeout=20,
    ).stderr.decode("utf-8", errors="replace")
    assert "Video: h264" in decoded and "yuv420p" in decoded
    assert ("Audio: aac" in decoded) == audio
    assert "frame=   10" in decoded or "frame=10" in decoded


def test_conversion_checks_final_size_and_keeps_source_on_failure(tmp_path):
    source = tmp_path / "media.mkv"
    fixture_video(source)
    with pytest.raises(DownloadError) as error:
        prepare_mp4(source, max_file_bytes=100, ffmpeg_location=FFMPEG)
    assert error.value.code == "size_limit"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["media.mkv"]


def test_invalid_media_fails_without_leaving_partial_mp4(tmp_path):
    source = tmp_path / "media.mkv"
    source.write_bytes(b"invalid video with private metadata")
    with pytest.raises(DownloadError) as error:
        prepare_mp4(source, max_file_bytes=1_000_000, ffmpeg_location=FFMPEG)
    assert error.value.code == "download_failed"
    assert "private metadata" not in str(error.value)
    assert list(tmp_path.iterdir()) == [source]


def test_existing_mp4_is_replaced_safely(tmp_path):
    source = tmp_path / "media.mp4"
    fixture_video(source, compatible=True)
    result = prepare_mp4(source, max_file_bytes=1_000_000, ffmpeg_location=FFMPEG)
    assert result == source and source.stat().st_size > 0
    assert list(tmp_path.iterdir()) == [source]
