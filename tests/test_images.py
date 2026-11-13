import io
import zipfile

import pytest
from PIL import Image

from downloadbot import download_process
from downloadbot.downloader import DownloadError
from downloadbot.images import (
    ImagePost,
    _image_url_allowed,
    _instagram_photos,
    album_members,
    download_album,
    post_extractor,
)


def photo_bytes(color="red", size=(80, 60)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


@pytest.mark.parametrize(("platform", "key"), [("x", "Twitter"), ("instagram", "Instagram")])
def test_installed_extractors_instantiate_the_photo_extension(platform, key):
    from yt_dlp.extractor import get_info_extractor
    from yt_dlp.extractor.common import InfoExtractor

    extended = post_extractor(platform, get_info_extractor(key))
    extractor = extended()
    assert isinstance(extractor, InfoExtractor) and type(extractor) is extended
    assert extractor.ie_key() == key


def test_photo_post_uses_real_ytdlp_pipeline_and_preserves_all_images(monkeypatch, tmp_path):
    import yt_dlp.extractor
    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.external import FFmpegFD
    from yt_dlp.extractor.common import InfoExtractor

    class FixtureIE(InfoExtractor):
        _VALID_URL = r"https://x\.com/test/status/(?P<id>\d+)"

        def _extract_status(self, post_id):
            return {"full_text": "Album\nTitle", "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/one.png"},
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/two.png"},
            ]}}

        def _real_extract(self, url):
            self._extract_status(self._match_id(url))
            raise AssertionError("Photo post must be intercepted before video processing")

    requests = []
    def urlopen(self, request):
        requests.append(request.url)
        return io.BytesIO(photo_bytes("red" if "one" in request.url else "blue"))

    monkeypatch.setattr(yt_dlp.extractor, "get_info_extractor", lambda name: FixtureIE)
    monkeypatch.setattr(YoutubeDL, "urlopen", urlopen)
    monkeypatch.setattr(FFmpegFD, "real_download", FFmpegFD.real_download)
    result = download_process.perform_download({
        "url": "https://x.com/test/status/123", "output_dir": str(tmp_path),
        "max_file_bytes": 1_000_000, "max_duration_seconds": 60, "max_height": 720,
    })
    assert result == {"ok": True, "filename": "media.zip", "title": "AlbumTitle", "ext": "zip", "duration": None}
    assert all("name=orig" in url for url in requests) and len(requests) == 2
    assert [path.name for path in tmp_path.iterdir()] == ["media.zip"]
    with zipfile.ZipFile(tmp_path / "media.zip") as archive:
        members = album_members(archive, 1_000_000)
        assert len(members) == 2
        for item, expected_channel in zip(members, [0, 2]):
            with Image.open(io.BytesIO(archive.read(item))) as image:
                assert image.format == "JPEG" and image.size == (80, 60)
                assert image.getpixel((0, 0))[expected_channel] > 240


@pytest.mark.parametrize("quoted", [False, True])
def test_x_video_prevents_photo_fallback(quoted):
    photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/one.jpg"}
    video = {"type": "video"}
    status = {"extended_entities": {"media": [photo] if quoted else [photo, video]}}
    if quoted:
        status["quoted_status"] = {"extended_entities": {"media": [video]}}
    class Base:
        def _extract_status(self, post_id):
            return status
    assert post_extractor("x", Base)()._extract_status("123") == status


def test_instagram_carousel_selects_largest_image_for_each_slide():
    def slide(name):
        return {"media_type": 1, "image_versions2": {"candidates": [
            {"url": f"https://s.cdninstagram.com/{name}-small.jpg", "width": 10, "height": 10},
            {"url": f"https://s.cdninstagram.com/{name}.jpg", "width": 100, "height": 100},
        ]}}
    product = {"carousel_media": [slide("one"), slide("two")]}
    assert _instagram_photos(product) == ["https://s.cdninstagram.com/one.jpg", "https://s.cdninstagram.com/two.jpg"]
    product["carousel_media"].append({"media_type": 2, "image_versions2": {"candidates": [{"url": "thumbnail"}]}})
    assert not _instagram_photos(product)


def test_instagram_photo_hook_keeps_video_and_authentication_paths():
    class Base:
        def _extract_product(self, product, *args, **kwargs):
            return "video"
    extractor = post_extractor("instagram", Base)()
    with pytest.raises(ImagePost) as post:
        extractor._extract_product({"media_type": 1, "image_versions2": {"candidates": [
            {"url": "https://s.cdninstagram.com/photo.jpg"},
        ]}, "caption": {"text": "Photo caption"}})
    assert post.value.title == "Photo caption"
    assert extractor._extract_product({"media_type": 2}) == "video"


@pytest.mark.parametrize("url", [
    "http://pbs.twimg.com/a.jpg", "https://pbs.twimg.com.evil.test/a.jpg",
    "https://127.0.0.1/a.jpg", "https://user:secret@pbs.twimg.com/a.jpg",
    "https://pbs.twimg.com:123/a.jpg", "file:///etc/passwd",
])
def test_image_cdn_policy_rejects_unsafe_urls(url):
    assert not _image_url_allowed(url, "x")


def test_downloaded_images_are_validated_and_limited(tmp_path):
    class Client:
        def __init__(self, data):
            self.data = data
        def urlopen(self, request):
            return io.BytesIO(self.data)
    post = ImagePost(["https://pbs.twimg.com/media/a.jpg"], "Photo")
    with pytest.raises(DownloadError) as error:
        download_album(Client(b"not an image"), post, platform="x", output_dir=tmp_path, max_size=1000)
    assert error.value.code == "download_failed"
    with pytest.raises(DownloadError) as error:
        download_album(Client(b"x" * 2000), post, platform="x", output_dir=tmp_path, max_size=1000)
    assert error.value.code == "size_limit"


def test_telegram_photo_dimensions_are_bounded_without_losing_panorama(tmp_path):
    class Client:
        def urlopen(self, request):
            return io.BytesIO(photo_bytes(size=(6000, 100)))
    download_album(Client(), ImagePost(["https://pbs.twimg.com/media/a.jpg"], "Photo"),
                   platform="x", output_dir=tmp_path, max_size=1_000_000)
    with zipfile.ZipFile(tmp_path / "media.zip") as archive:
        with Image.open(io.BytesIO(archive.read("image-001.jpg"))) as image:
            assert max(image.size) <= 2560 and max(image.size) / min(image.size) <= 20


@pytest.mark.parametrize("filename,compression", [
    ("../secret.jpg", zipfile.ZIP_STORED), ("image-001.jpg", zipfile.ZIP_DEFLATED),
    ("image-002.jpg", zipfile.ZIP_STORED),
])
def test_album_rejects_traversal_compression_and_missing_slides(filename, compression):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr(filename, b"photo")
    with zipfile.ZipFile(output) as archive, pytest.raises(ValueError):
        album_members(archive, 1_000_000)
