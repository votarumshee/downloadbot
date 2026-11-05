"""Integration fixtures use a dedicated PostgreSQL database and real transactions."""

import json
import os
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from downloadbot.config import Settings
from downloadbot.store import Store

ADMIN_KEY = "admin-test-key-with-at-least-24-characters"
ALICE_KEY = "alice-test-key-with-at-least-24-characters"
BOB_KEY = "bob-test-key-with-at-least-24-characters"
COOKIE_VALUE = "only-a-synthetic-cookie-not-a-real-secret"
YOUTUBE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    f".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{COOKIE_VALUE}\n"
)


@pytest.fixture
def settings(tmp_path):
    dsn = os.environ.get("DOWNLOADBOT_TEST_DATABASE_URL")
    if not dsn:
        local = Path(__file__).resolve().parents[1] / ".runtime" / "db.env"
        dsn = dotenv_values(local).get("DOWNLOADBOT_TEST_DATABASE_URL") if local.is_file() else None
    if not dsn:
        pytest.skip("Set DOWNLOADBOT_TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test")
    # Never clean the development/production database, even if supplied accidentally.
    if not conninfo_to_dict(dsn).get("dbname", "").endswith("_test"):
        pytest.fail("Integration database name must end in _test; its jobs/accounts/workers are cleared")
    result = Settings(
        _env_file=None,
        api_key=ADMIN_KEY,
        encryption_key=Fernet.generate_key().decode(),
        database_url=dsn,
        data_dir=tmp_path,
        clients_json=json.dumps([
            {"id": "alice", "api_key": ALICE_KEY, "allowed_account_ids": ["youtube-account"]},
            {"id": "bob", "api_key": BOB_KEY, "allowed_account_ids": []},
        ]),
        account_cooldown_seconds=0,
        min_free_disk_bytes=0,
    )
    store = Store(result)
    store.initialize()
    with psycopg.connect(dsn) as db:
        db.execute("TRUNCATE jobs, accounts, workers")
    yield result


@pytest.fixture
def store(settings):
    return Store(settings)
