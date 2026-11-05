import pytest

from downloadbot.url_policy import validate_url


@pytest.mark.parametrize(("url", "normalized", "platform"), [
    ("https://youtu.be/BaW_jenozKc?si=tracking", "https://www.youtube.com/watch?v=BaW_jenozKc", "youtube"),
    ("https://www.youtube.com/shorts/BaW_jenozKc", "https://www.youtube.com/watch?v=BaW_jenozKc", "youtube"),
    ("https://www.youtube.com/watch?v=BaW_jenozKc&t=10#fragment", "https://www.youtube.com/watch?v=BaW_jenozKc", "youtube"),
    ("https://twitter.com/user/status/123/video/1?s=20", "https://x.com/user/status/123", "x"),
    ("https://x.com/i/web/status/123", "https://x.com/i/status/123", "x"),
    ("https://instagram.com/reel/Chunk8-jurw/?igsh=tracking", "https://www.instagram.com/reel/Chunk8-jurw/", "instagram"),
    ("https://vimeo.com/12345", "https://vimeo.com/12345", "vimeo"),
    ("https://player.vimeo.com/video/12345?h=abcdef0123", "https://player.vimeo.com/video/12345?h=abcdef0123", "vimeo"),
    ("https://vimeo.com/12345/abcdef0123", "https://vimeo.com/12345/abcdef0123", "vimeo"),
])
def test_supported_video_urls(url, normalized, platform):
    assert validate_url(url) == (normalized, platform)


@pytest.mark.parametrize("url", [
    "http://youtu.be/BaW_jenozKc", "file:///etc/passwd", "https://127.0.0.1/video",
    "https://[::1]/video", "https://youtube.com.evil.test/watch?v=BaW_jenozKc",
    "https://user:secret@youtube.com/watch?v=BaW_jenozKc",
    "https://youtube.com:443/watch?v=BaW_jenozKc", "https://youtube.com./watch?v=BaW_jenozKc",
    "https://youtube.com/watch?v=BaW_jenozKc&list=PL123",
    "https://youtube.com/watch?v=BaW_jenozKc&v=AAAAAAAAAAA",
    "https://youtube.com/playlist?list=123", "https://youtube.com/@user",
    "https://youtu.be/short", "https://instagram.com/profile/", "https://x.com/user",
    "https://vimeo.com/channels/test", "https://youtube.com\\@evil.test/watch?v=BaW_jenozKc",
    " https://youtu.be/BaW_jenozKc", "https://youtu.be/BaW_jenozKc\n", "https://youtu.be/%42aW_jenozKc",
    "https://player.vimeo.com/video/12345?h=invalid", "", "x" * 2049,
])
def test_rejects_non_video_and_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)
