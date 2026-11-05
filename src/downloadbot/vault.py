from __future__ import annotations

import time

from cryptography.fernet import Fernet, InvalidToken

COOKIE_DOMAINS = {
    "youtube": ("youtube.com", "google.com"),
    "x": ("x.com", "twitter.com"),
    "instagram": ("instagram.com",),
    "vimeo": ("vimeo.com",),
}


class CookieVault:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode("ascii"))

    def encrypt(self, platform: str, cookies: str) -> str:
        if platform not in COOKIE_DOMAINS:
            raise ValueError("Unsupported account platform")
        if len(cookies.encode("utf-8")) > 256 * 1024 or "\x00" in cookies:
            raise ValueError("Invalid cookie file size or encoding")
        if not cookies.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
            raise ValueError("Expected a Netscape cookies.txt file")
        count, valid = 0, 0
        for line in cookies.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line.removeprefix("#HttpOnly_")
            elif not line.strip() or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != 7:
                raise ValueError("Invalid Netscape cookie row")
            domain, include_subdomains, path, secure, expires, name, value = fields
            domain = domain.lstrip(".").lower()
            if not any(domain == d or domain.endswith("." + d) for d in COOKIE_DOMAINS[platform]):
                raise ValueError("Cookie domain does not belong to this platform")
            if include_subdomains not in {"TRUE", "FALSE"} or secure not in {"TRUE", "FALSE"}:
                raise ValueError("Invalid cookie flags")
            if not path.startswith("/") or not name:
                raise ValueError("Invalid cookie row")
            try:
                expiry = int(expires or "0")
            except ValueError:
                raise ValueError("Invalid cookie expiry") from None
            count += 1
            valid += expiry == 0 or expiry > time.time()
        if not count or not valid:
            raise ValueError("Cookie file is empty or all cookies have expired")
        normalized = cookies.replace("\r\n", "\n").rstrip("\r\n") + "\n"
        return self._fernet.encrypt(normalized.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            raise ValueError("Account credentials cannot be decrypted; verify the encryption key") from None
