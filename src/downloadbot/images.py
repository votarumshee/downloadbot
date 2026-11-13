"""Photo-only posts, bounded image downloads, and a portable album artifact."""

import math
import re
import warnings
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from .downloader import public_error

MAX_POST_IMAGES = 100
MAX_PHOTO_BYTES = 10_000_000
MAX_IMAGE_PIXELS = 40_000_000


class ImagePost(Exception):
    def __init__(self, urls: list[str], title: str):
        super().__init__("Photo-only post")
        self.urls = urls
        self.title = title


def _instagram_photos(product: dict) -> list[str]:
    items = product.get("carousel_media") or [product]
    # A missing video rendition never turns a video's thumbnail into a photo.
    if any(item.get("media_type") == 2 or item.get("video_versions")
           or item.get("video_dash_manifest") or item.get("is_video") for item in items):
        return []
    urls = []
    for item in items:
        candidates = (item.get("image_versions2") or {}).get("candidates") or []
        candidates = [candidate for candidate in candidates if isinstance(candidate.get("url"), str)]
        if not candidates:
            return []
        def area(candidate):
            width, height = candidate.get("width"), candidate.get("height")
            return width * height if isinstance(width, int) and isinstance(height, int) else 0
        urls.append(max(candidates, key=area)["url"])
    return urls


def post_extractor(platform: str, base):
    """Extend the installed extractor, retaining its authenticated access checks."""
    if platform == "x":
        class TwitterPhotosIE(base):
            @classmethod
            def ie_key(cls):
                return base.ie_key()

            def _extract_status(self, post_id):
                status = super()._extract_status(post_id)
                media = (status.get("extended_entities") or {}).get("media") or []
                quoted = ((status.get("quoted_status") or {}).get("extended_entities") or {}).get("media") or []
                if media and all(item.get("type") == "photo" for item in media) and not any(
                    item.get("type") in {"video", "animated_gif"} for item in quoted
                ):
                    from yt_dlp.utils import update_url_query
                    urls = [item.get("media_url_https") or item.get("media_url") for item in media]
                    if all(isinstance(url, str) and url for url in urls):
                        raise ImagePost([update_url_query(url, {"name": "orig"}) for url in urls],
                                        str(status.get("full_text") or status.get("text") or "Post images"))
                return status
        return TwitterPhotosIE
    if platform == "instagram":
        class InstagramPhotosIE(base):
            @classmethod
            def ie_key(cls):
                return base.ie_key()

            def _extract_product(self, product_info, *args, **kwargs):
                urls = _instagram_photos(product_info)
                if urls:
                    caption = product_info.get("caption") or {}
                    raise ImagePost(urls, str(caption.get("text") or "Post images"))
                return super()._extract_product(product_info, *args, **kwargs)
        return InstagramPhotosIE
    return base


def _image_url_allowed(url: str, platform: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port:
            return False
        if platform == "x":
            return host == "pbs.twimg.com"
        if platform == "instagram":
            return any(host == suffix or host.endswith("." + suffix) for suffix in ("cdninstagram.com", "fbcdn.net"))
    except ValueError:
        pass
    return False


def _prepare_photo(source: Path, target: Path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=["JPEG", "PNG", "WEBP", "AVIF"]) as original:
                if original.width * original.height > MAX_IMAGE_PIXELS:
                    raise public_error("size_limit")
                image = ImageOps.exif_transpose(original)
                image.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    image = Image.new("RGB", rgba.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = image.convert("RGB")
                # Telegram photos require width+height <= 10000 and ratio <= 20.
                minimum = max(1, math.ceil(max(image.size) / 20))
                image = ImageOps.pad(image, (max(image.width, minimum), max(image.height, minimum)), color="white")
                image.save(target, "JPEG", quality=90, optimize=True)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise public_error("size_limit") from None
    except (OSError, ValueError, UnidentifiedImageError):
        raise public_error("download_failed") from None
    if target.stat().st_size > MAX_PHOTO_BYTES:
        raise public_error("size_limit")


def download_album(ydl, post: ImagePost, *, platform: str, output_dir: Path, max_size: int) -> dict:
    from yt_dlp.networking import Request

    if not 1 <= len(post.urls) <= MAX_POST_IMAGES:
        raise public_error("size_limit")
    if not all(_image_url_allowed(url, platform) for url in post.urls):
        raise public_error("unavailable")
    downloaded = 0
    prepared = 0
    photos = []
    for index, url in enumerate(post.urls, 1):
        source = output_dir / f"image-{index:03}.source"
        target = source.with_suffix(".jpg")
        headers = {"Referer": "https://www.instagram.com/"} if platform == "instagram" else {}
        # The same urllib handler, cookie jar and socket guard protect redirects.
        with ydl.urlopen(Request(url, headers=headers)) as response, source.open("wb") as output:
            for chunk in iter(lambda: response.read(64 * 1024), b""):
                downloaded += len(chunk)
                if downloaded > max_size or output.tell() + len(chunk) > min(max_size, 20_000_000):
                    raise public_error("size_limit")
                output.write(chunk)
        _prepare_photo(source, target)
        prepared += target.stat().st_size
        if prepared > max_size:
            raise public_error("size_limit")
        source.unlink()
        photos.append(target)
    archive_path = output_dir / "media.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for photo in photos:
            archive.write(photo, photo.name)
            if archive_path.stat().st_size > max_size:
                raise public_error("size_limit")
    if archive_path.stat().st_size > max_size:
        raise public_error("size_limit")
    for photo in photos:
        photo.unlink()
    title = "".join(char for char in post.title if ord(char) >= 32 and ord(char) != 127)[:512]
    return {"ok": True, "filename": "media.zip", "title": title, "ext": "zip", "duration": None}


def album_members(archive: zipfile.ZipFile, max_bytes: int) -> list[zipfile.ZipInfo]:
    """Validate without extracting: no paths, encryption or compressed zip bombs."""
    members = archive.infolist()
    if not 1 <= len(members) <= MAX_POST_IMAGES:
        raise ValueError("Invalid image count")
    total = 0
    for index, item in enumerate(members, 1):
        if item.filename != f"image-{index:03}.jpg" or not re.fullmatch(r"image-\d{3}\.jpg", item.filename):
            raise ValueError("Invalid image filename")
        if item.compress_type != zipfile.ZIP_STORED or item.flag_bits & 1 or not 0 < item.file_size <= MAX_PHOTO_BYTES:
            raise ValueError("Invalid image entry")
        total += item.file_size
        if total > max_bytes:
            raise ValueError("Album exceeds size limit")
    return members
