"""Strict entry-point policy; media CDN URLs are handled inside the worker."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_POST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def validate_url(url: str) -> tuple[str, str]:
    """Return a canonical HTTPS video URL and its supported platform.

    Tracking parameters and fragments are discarded. Playlist parameters are
    rejected instead of silently changing a request for a collection.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise ValueError("Provide a supported HTTPS video URL (maximum 2048 characters).")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise ValueError("The video URL contains invalid characters.")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        raise ValueError("The video URL is invalid.") from None
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        raise ValueError("Only HTTPS video URLs without credentials are allowed.")
    if port is not None or not host or host.endswith(".") or ":" in parts.netloc:
        raise ValueError("Custom ports and IP addresses are not allowed.")
    query = parse_qs(parts.query, keep_blank_values=True)
    if "list" in query or "playlist" in query:
        raise ValueError("Playlists are not supported; provide one video URL.")
    path = parts.path.rstrip("/")
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}:
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = path.removeprefix("/")
        elif path == "/watch" and len(query.get("v", [])) == 1:
            video_id = query["v"][0]
        else:
            match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]+)", path)
            video_id = match[1] if match else ""
        if _YOUTUBE_ID.fullmatch(video_id):
            return f"https://www.youtube.com/watch?{urlencode({'v': video_id})}", "youtube"
    elif host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "mobile.x.com"}:
        match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/([0-9]{1,25})(?:/video/[1-9][0-9]?)?", path)
        if match:
            return f"https://x.com/{match[1]}/status/{match[2]}", "x"
        match = re.fullmatch(r"/i/(?:web/)?status/([0-9]{1,25})", path)
        if match:
            return f"https://x.com/i/status/{match[1]}", "x"
    elif host in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        match = re.fullmatch(r"/(p|reel|tv)/([^/]+)", path)
        if match and _POST_ID.fullmatch(match[2]):
            return f"https://www.instagram.com/{match[1]}/{match[2]}/", "instagram"
    elif host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}:
        pattern = r"/video/([0-9]{1,15})" if host == "player.vimeo.com" else r"/([0-9]{1,15})(?:/([a-fA-F0-9]{10}))?"
        match = re.fullmatch(pattern, path)
        if match:
            # Vimeo's unlisted hash is part of the access URL, not tracking.
            video_hash = match[2] if host != "player.vimeo.com" else None
            if host == "player.vimeo.com" and len(query.get("h", [])) == 1:
                candidate = query["h"][0]
                if not re.fullmatch(r"[a-fA-F0-9]{10}", candidate):
                    raise ValueError("The Vimeo video access hash is invalid.")
                video_hash = candidate
            if host == "player.vimeo.com":
                suffix = f"?h={video_hash}" if video_hash else ""
                return f"https://player.vimeo.com/video/{match[1]}{suffix}", "vimeo"
            suffix = f"/{video_hash}" if video_hash else ""
            return f"https://vimeo.com/{match[1]}{suffix}", "vimeo"
    raise ValueError("Use a single YouTube, X, Instagram or Vimeo video/post URL.")
