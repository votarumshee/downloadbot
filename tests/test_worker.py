import os
import time
import uuid

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql

from downloadbot.config import Settings
from downloadbot.downloader import DownloadError, DownloadResult
from downloadbot.store import Store
from downloadbot.worker import Worker, remove_directory


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_key="test-api-key-that-is-at-least-24-characters",
        encryption_key=Fernet.generate_key().decode(),
        database_url="postgresql://unused/unused",
        data_dir=tmp_path,
        max_file_bytes=1024,
        min_free_disk_bytes=0,
        _env_file=None,
    )


class FakeStore:
    def __init__(self, job=None):
        self.job = job or {
            "id": "job-123", "attempt": 1, "url": "https://www.youtube.com/watch?v=BaW_jenozKc",
            "max_height": 720, "account_id": None, "status": "running",
        }
        self.account = None
        self.completed = []
        self.failed = []
        self.released = []
        self.heartbeat_live = True
        self.accept_completion = True
        self.expired_jobs = []
        self.claimed = False

    def initialize(self):
        pass

    def worker_ping(self, worker_id):
        pass

    def claim(self, worker_id):
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    def get_job(self, job_id):
        return self.job if self.job and job_id == self.job["id"] else None

    def heartbeat(self, job_id, worker_id):
        return self.heartbeat_live

    def get_account(self, account_id):
        return self.account

    def finish(self, job_id, worker_id, **result):
        self.completed.append(result)
        return self.accept_completion

    def fail(self, job_id, worker_id, code, message, retryable=False):
        self.failed.append((code, message, retryable))

    def release(self, job_id, worker_id):
        self.released.append(job_id)

    def expire(self):
        return self.expired_jobs


class FakeDownloader:
    def __init__(self, callback):
        self.callback = callback

    def download(self, **kwargs):
        return self.callback(**kwargs)


def successful_download(**kwargs):
    output = kwargs["output_dir"]
    output.mkdir(parents=True, exist_ok=True)
    path = output / "media.mp4"
    path.write_bytes(b"test media")
    return DownloadResult(path, "A video", "mp4", path.stat().st_size, 1.0)


def make_worker(settings, callback=successful_download):
    worker = Worker(settings, FakeDownloader(callback))
    worker.store = FakeStore()
    worker.initialize()
    return worker


def with_account(worker):
    cookies = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-cookie-value\n"
    worker.store.job["account_id"] = "youtube-session"
    worker.store.account = {"status": "active", "encrypted_cookies": worker.vault.encrypt("youtube", cookies)}
    return cookies


def test_cookies_are_temporary_and_never_published_with_artifact(settings, caplog):
    cookie_paths = []

    def download(**kwargs):
        path = kwargs["cookie_file"]
        cookie_paths.append(path)
        assert path.read_text() == cookies
        assert not path.is_relative_to(kwargs["output_dir"])
        if os.name != "nt":
            assert path.stat().st_mode & 0o077 == 0
        return successful_download(**kwargs)

    worker = make_worker(settings, download)
    cookies = with_account(worker)
    assert worker.run_once()
    assert not worker.store.failed
    result = worker.store.completed[0]
    assert result["artifact_path"] == "job-123/attempt1/media.mp4"
    assert (settings.files_dir / result["artifact_path"]).is_file()
    assert all(not path.exists() for path in cookie_paths)
    assert not list(settings.temp_dir.iterdir())
    assert "private-cookie-value" not in caplog.text
    assert not worker.run_once()


def test_failed_download_removes_partial_files_and_cookie_file(settings):
    def download(**kwargs):
        successful_download(**kwargs)
        assert kwargs["cookie_file"].is_file()
        raise DownloadError("auth_required", "Credentials must be refreshed")

    worker = make_worker(settings, download)
    with_account(worker)
    assert worker.run_once()
    assert worker.store.failed == [("auth_required", "Credentials must be refreshed", False)]
    assert not worker.store.completed
    assert not (settings.files_dir / "job-123" / "attempt1").exists()
    assert not list(settings.temp_dir.iterdir())


def test_unexpected_library_errors_cannot_leak_credentials(settings, caplog):
    def download(**kwargs):
        successful_download(**kwargs)
        raise RuntimeError("private-cookie-value signed-url-secret api-key-secret")

    worker = make_worker(settings, download)
    with_account(worker)
    assert worker.run_once()
    assert worker.store.failed == [("internal_error", "An internal worker error occurred", False)]
    for secret in ("private-cookie-value", "signed-url-secret", "api-key-secret"):
        assert secret not in caplog.text
        assert secret not in str(worker.store.failed)
    assert not list(settings.temp_dir.iterdir())


def test_graceful_stop_releases_job_and_cleans_partial_output(settings):
    def download(**kwargs):
        successful_download(**kwargs)
        worker.stop.set()
        assert kwargs["cancelled"]()
        raise DownloadError("cancelled", "The download was cancelled")

    worker = make_worker(settings, download)
    assert worker.run_once()
    assert worker.store.released == ["job-123"]
    assert not worker.store.failed
    assert not (settings.files_dir / "job-123" / "attempt1").exists()


def test_rejected_completion_removes_local_attempt(settings):
    worker = make_worker(settings)
    worker.store.accept_completion = False
    assert worker.run_once()
    assert not (settings.files_dir / "job-123" / "attempt1").exists()


def test_disabled_account_is_not_given_to_downloader(settings):
    def download(**kwargs):
        pytest.fail("Disabled account must not reach downloader")

    worker = make_worker(settings, download)
    with_account(worker)
    worker.store.account["status"] = "disabled"
    assert worker.run_once()
    assert worker.store.failed[0][0] == "account_unavailable"
    assert not list(settings.temp_dir.iterdir())


def prepare_attempts(settings, count=2):
    root = settings.files_dir / "job-123"
    for attempt in range(1, count + 1):
        directory = root / f"attempt{attempt}"
        directory.mkdir(parents=True)
        (directory / "media.mp4").write_bytes(b"media")
    old = time.time() - settings.lease_seconds * 3
    os.utime(root, (old, old))
    return root


def test_cleanup_stale_queued_snapshot_preserves_newly_claimed_attempt(settings):
    worker = make_worker(settings)
    worker.store.job.update(status="queued", attempt=1)
    root = prepare_attempts(settings)
    worker.cleanup()
    assert not (root / "attempt1").exists()
    assert (root / "attempt2" / "media.mp4").is_file()


def test_cleanup_running_retry_preserves_current_attempt_only(settings):
    worker = make_worker(settings)
    worker.store.job.update(status="running", attempt=2)
    root = prepare_attempts(settings)
    worker.cleanup()
    assert not (root / "attempt1").exists()
    assert (root / "attempt2" / "media.mp4").is_file()


def test_cleanup_expired_artifacts_and_old_cookies_preserves_recent_cookie(settings):
    worker = make_worker(settings)
    worker.store.job["status"] = "expired"
    worker.store.expired_jobs = [worker.store.job]
    root = prepare_attempts(settings)
    old_cookie = settings.temp_dir / "crashed.cookies"
    fresh_cookie = settings.temp_dir / "active.cookies"
    old_cookie.write_text("old secret")
    fresh_cookie.write_text("active secret")
    old = time.time() - settings.download_timeout_seconds - settings.lease_seconds * 3
    os.utime(old_cookie, (old, old))
    worker.cleanup()
    assert not root.exists()
    assert not old_cookie.exists()
    assert fresh_cookie.exists()


def test_remove_directory_never_removes_storage_root_or_outside(settings, tmp_path):
    root = settings.files_dir
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    remove_directory(root, root)
    remove_directory(outside, root)
    assert root.is_dir()
    assert outside.is_dir()


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [("api_key", "short-secret"), ("encryption_key", "invalid-encryption-secret"),
     ("clients_json", "invalid-client-secret")],
)
def test_startup_validation_error_does_not_expose_secret_inputs(settings, invalid_field, invalid_value):
    values = {
        "api_key": "private-api-secret-that-is-long-enough",
        "encryption_key": settings.encryption_key.get_secret_value(),
        "database_url": "postgresql://user:private-db-password@localhost/db",
        "clients_json": "[]",
    }
    values[invalid_field] = invalid_value
    with pytest.raises(ValueError) as failure:
        Settings(_env_file=None, **values)
    error_text = str(failure.value)
    for secret in values.values():
        if secret != "[]":
            assert secret not in error_text
    assert "private-db-password" not in error_text


@pytest.fixture
def postgres_worker_store(settings):
    database_url = os.environ.get("DOWNLOADBOT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("requires test PostgreSQL")
    schema = f"worker_test_{uuid.uuid4().hex}"
    settings = settings.model_copy(update={"database_url": type(settings.database_url)(database_url)})
    with psycopg.connect(database_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    class ScopedStore(Store):
        def connect(self):
            connection = super().connect()
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            return connection

    store = ScopedStore(settings)
    store.initialize()
    try:
        yield settings, store
    finally:
        with psycopg.connect(database_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def submit(store):
    return store.create_job(owner="worker-test", url="https://www.youtube.com/watch?v=BaW_jenozKc", platform="youtube")[0]


def test_postgres_worker_retries_transient_failure_then_publishes(postgres_worker_store):
    settings, store = postgres_worker_store
    calls = []

    def download(**kwargs):
        calls.append(kwargs["output_dir"])
        result = successful_download(**kwargs)
        if len(calls) == 1:
            raise DownloadError("network_error", "Network temporarily unavailable", retryable=True)
        return result

    worker = Worker(settings, FakeDownloader(download))
    worker.store = store
    worker.initialize()
    job = submit(store)
    assert worker.run_once()
    first = store.get_job(job["id"])
    assert first["status"] == "queued"
    assert first["attempt"] == 1
    assert not calls[0].exists()
    with store.connect() as db:
        db.execute("UPDATE jobs SET available_at = 0 WHERE id = %s", (job["id"],))
    assert worker.run_once()
    second = store.get_job(job["id"])
    assert second["status"] == "succeeded"
    assert second["attempt"] == 2
    assert second["error_code"] is None
    assert (settings.files_dir / second["artifact_path"]).is_file()


def test_postgres_cancel_during_download_prevents_publication(postgres_worker_store):
    settings, store = postgres_worker_store

    def download(**kwargs):
        successful_download(**kwargs)
        store.request_cancel(job["id"])
        assert kwargs["cancelled"]()
        raise DownloadError("cancelled", "The download was cancelled")

    worker = Worker(settings, FakeDownloader(download))
    worker.store = store
    worker.initialize()
    job = submit(store)
    assert worker.run_once()
    result = store.get_job(job["id"])
    assert result["status"] == "cancelled"
    assert result["artifact_path"] is None
    assert not (settings.files_dir / job["id"] / "attempt1").exists()


def test_postgres_expired_lease_is_recovered_not_falsely_cancelled(postgres_worker_store):
    settings, store = postgres_worker_store

    def download(**kwargs):
        result = successful_download(**kwargs)
        with store.connect() as db:
            db.execute("UPDATE jobs SET lease_until = 0 WHERE id = %s", (job["id"],))
        return result

    worker = Worker(settings, FakeDownloader(download))
    worker.store = store
    worker.initialize()
    job = submit(store)
    assert worker.run_once()
    stale = store.get_job(job["id"])
    assert stale["status"] != "cancelled"
    assert stale["artifact_path"] is None
    assert not (settings.files_dir / job["id"] / "attempt1").exists()
    successor = Worker(settings, FakeDownloader(successful_download))
    successor.store = store
    assert successor.run_once()
    recovered = store.get_job(job["id"])
    assert recovered["status"] == "succeeded"
    assert recovered["attempt"] == 2


@pytest.mark.parametrize("rotate_during_download", [False, True])
def test_postgres_account_refresh_tracks_cookies_used_by_current_attempt(
    postgres_worker_store, rotate_during_download
):
    settings, store = postgres_worker_store
    calls = []
    worker = Worker(settings, FakeDownloader(lambda **kwargs: None))
    worker.store = store
    worker.initialize()
    cookie_header = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t"
    first_cookies = cookie_header + "initial-session\n"
    current_cookies = cookie_header + "current-session\n"
    next_cookies = cookie_header + "next-session\n"
    store.save_account("technical-account", "youtube", worker.vault.encrypt("youtube", first_cookies))
    job = store.create_job(
        owner="worker-test", url="https://www.youtube.com/watch?v=BaW_jenozKc",
        platform="youtube", account_id="technical-account",
    )[0]

    def download(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DownloadError("network_error", "Temporary network failure", retryable=True)
        assert kwargs["cookie_file"].read_text() == current_cookies
        if rotate_during_download:
            store.save_account("technical-account", "youtube", worker.vault.encrypt("youtube", next_cookies))
        raise DownloadError("auth_required", "Account cookies must be refreshed")

    worker.downloader = FakeDownloader(download)
    assert worker.run_once()
    store.save_account("technical-account", "youtube", worker.vault.encrypt("youtube", current_cookies))
    with store.connect() as db:
        db.execute("UPDATE jobs SET available_at = 0 WHERE id = %s", (job["id"],))
        db.execute("UPDATE accounts SET last_used_at = 0 WHERE id = 'technical-account'")
    assert worker.run_once()
    account = store.get_account("technical-account")
    assert account["status"] == ("active" if rotate_during_download else "needs_refresh")
    assert store.get_job(job["id"])["status"] == "failed"
    assert not list(settings.temp_dir.iterdir())
