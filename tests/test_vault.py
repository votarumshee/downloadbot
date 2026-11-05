import time

import pytest
from cryptography.fernet import Fernet

from downloadbot.vault import CookieVault


def cookie_file(domain=".youtube.com", expiry="0", value="session-secret", prefix=""):
    return f"# Netscape HTTP Cookie File\n{prefix}{domain}\tTRUE\t/\tTRUE\t{expiry}\tSID\t{value}\n"


@pytest.fixture
def vault():
    return CookieVault(Fernet.generate_key().decode())


def test_authenticated_encryption_roundtrip_and_randomized_ciphertext(vault):
    cookies = cookie_file(prefix="#HttpOnly_").replace("\n", "\r\n")
    first = vault.encrypt("youtube", cookies)
    second = vault.encrypt("youtube", cookies)
    assert first != second
    assert "session-secret" not in first
    assert "youtube.com" not in first
    assert vault.decrypt(first) == cookies.replace("\r\n", "\n")


@pytest.mark.parametrize(
    ("platform", "domain"),
    [("youtube", ".google.com"), ("x", ".twitter.com"), ("x", ".x.com"),
     ("instagram", ".instagram.com"), ("vimeo", ".vimeo.com")],
)
def test_allowed_platform_cookie_domains_roundtrip(vault, platform, domain):
    cookies = cookie_file(domain=domain)
    assert vault.decrypt(vault.encrypt(platform, cookies)) == cookies


@pytest.mark.parametrize(
    "domain", ["youtube.com.evil.test", "evilyoutube.com", ".instagram.com", "127.0.0.1", "localhost"]
)
def test_foreign_cookie_domains_are_rejected_without_echoing_secrets(vault, domain):
    with pytest.raises(ValueError) as failure:
        vault.encrypt("youtube", cookie_file(domain=domain))
    assert "session-secret" not in str(failure.value)
    assert domain not in str(failure.value)


@pytest.mark.parametrize(
    "cookies",
    [
        "SID=session-secret",
        "# Netscape HTTP Cookie File\n",
        cookie_file(expiry="1"),
        cookie_file(expiry="bad-secret-expiry"),
        cookie_file().replace("\tTRUE\t/", "\tbad-secret-flag\t/"),
        cookie_file().replace("\t/\t", "\tnot-a-path\t"),
        cookie_file(value="secret\x00value"),
        cookie_file(value="secret\tvalue"),
        cookie_file(value="x" * (256 * 1024)),
    ],
)
def test_malformed_or_expired_cookies_fail_closed(vault, cookies):
    with pytest.raises(ValueError) as failure:
        vault.encrypt("youtube", cookies)
    assert "session-secret" not in str(failure.value)
    assert "bad-secret" not in str(failure.value)


def test_expired_rows_are_allowed_only_with_a_live_cookie(vault):
    cookies = cookie_file(expiry="1") + cookie_file(expiry=str(int(time.time()) + 3600)).split("\n")[1] + "\n"
    assert vault.decrypt(vault.encrypt("youtube", cookies)) == cookies


def test_empty_cookie_values_in_browser_exports_are_valid(vault):
    cookies = cookie_file(value="")
    assert vault.decrypt(vault.encrypt("youtube", cookies)) == cookies


def test_wrong_key_and_tampering_produce_sanitized_errors(vault):
    ciphertext = vault.encrypt("youtube", cookie_file())
    other = CookieVault(Fernet.generate_key().decode())
    tampered = ciphertext[:20] + ("a" if ciphertext[20] != "a" else "b") + ciphertext[21:]
    for target, value in [(other, ciphertext), (vault, tampered), (vault, "secret-не-ascii")]:
        with pytest.raises(ValueError, match="cannot be decrypted") as failure:
            target.decrypt(value)
        assert value not in str(failure.value)
        assert "session-secret" not in str(failure.value)
