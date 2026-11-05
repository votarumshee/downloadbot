import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from downloadbot.store import AccountUnavailable, IdempotencyConflict, QueueFull, Store

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


def enqueue(store, **kwargs):
    return store.create_job(owner="alice", url=URL, platform="youtube", **kwargs)[0]


def complete(store, job, worker="worker"):
    return store.finish(job["id"], worker, artifact_path=f"{job['id']}/attempt1/media.mp4",
                        title="Test video", ext="mp4", size_bytes=42, duration=3.0)


def test_jobs_persist_across_store_instances(settings, store):
    job = enqueue(store)
    restarted = Store(settings)
    restarted.initialize()
    assert restarted.get_job(job["id"])["status"] == "queued"
    assert restarted.claim("new-worker")["id"] == job["id"]


def test_idempotency_is_atomic_and_scoped_to_owner(store):
    barrier = Barrier(8)

    def submit(_):
        barrier.wait()
        return store.create_job(owner="alice", url=URL, platform="youtube", idempotency_key="submission")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert len({job["id"] for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    other, created = store.create_job(owner="bob", url=URL, platform="youtube", idempotency_key="submission")
    assert created and other["id"] != results[0][0]["id"]
    with pytest.raises(IdempotencyConflict):
        store.create_job(owner="alice", url=URL, platform="youtube", max_height=1080,
                         idempotency_key="submission")


def test_concurrent_claims_never_duplicate_a_job(store):
    pending = {enqueue(store)["id"] for _ in range(8)}
    barrier = Barrier(8)

    def claim(index):
        barrier.wait()
        return store.claim(f"worker-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    claimed = [job["id"] for job in results if job]
    assert len(claimed) == len(set(claimed))
    # Claims lock up to 20 candidate rows; subsequent polling must still obtain all jobs.
    while job := store.claim("drain-worker"):
        claimed.append(job["id"])
    assert set(claimed) == pending
    assert len(claimed) == len(pending)


def test_account_is_never_used_concurrently(store):
    store.save_account("youtube-account", "youtube", "encrypted-test-value")
    enqueue(store, account_id="youtube-account")
    enqueue(store, account_id="youtube-account")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.claim, ["one", "two"]))
    assert sum(job is not None for job in results) == 1
    running = next(job for job in results if job)
    assert complete(store, running, running["worker_id"])
    assert store.claim("three")["account_id"] == "youtube-account"


def test_stale_lease_recovery_fences_previous_worker(store):
    job = enqueue(store)
    assert store.claim("old-worker")["id"] == job["id"]
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=%s WHERE id=%s", (time.time() - 1, job["id"]))
    assert not store.heartbeat(job["id"], "old-worker")
    assert not complete(store, job, "old-worker")
    recovered = store.claim("new-worker")
    assert recovered["id"] == job["id"] and recovered["attempt"] == 2
    assert not complete(store, job, "old-worker")
    assert complete(store, job, "new-worker")


def test_crashed_worker_exhausts_attempt_budget(store):
    job = enqueue(store)
    store.claim("worker")
    with store.connect() as db:
        db.execute("UPDATE jobs SET attempt=%s,lease_until=%s WHERE id=%s",
                   (store.settings.max_attempts, time.time() - 1, job["id"]))
    assert store.claim("replacement") is None
    finished = store.get_job(job["id"])
    assert finished["status"] == "failed" and finished["error_code"] == "worker_lost"


def test_cancellation_wins_completion_race(store):
    queued = enqueue(store)
    assert store.request_cancel(queued["id"])["status"] == "cancelled"
    assert store.claim("worker") is None
    running = enqueue(store)
    store.claim("worker")
    assert store.request_cancel(running["id"])["cancel_requested"]
    assert not store.heartbeat(running["id"], "worker")
    assert not complete(store, running)
    store.fail(running["id"], "worker", "cancelled", "Cancelled")
    assert store.get_job(running["id"])["status"] == "cancelled"


def test_retry_backoff_and_max_attempts(store):
    job = enqueue(store)
    store.claim("one")
    store.fail(job["id"], "one", "network_error", "Temporary failure", retryable=True)
    assert store.get_job(job["id"])["status"] == "queued"
    assert store.claim("two") is None
    with store.connect() as db:
        db.execute("UPDATE jobs SET available_at=0 WHERE id=%s", (job["id"],))
    assert store.claim("two")["attempt"] == 2
    store.fail(job["id"], "two", "network_error", "Temporary failure", retryable=True)
    assert store.get_job(job["id"])["status"] == "failed"


def test_quota_cannot_be_exceeded_by_concurrent_submissions(store):
    store.settings.max_active_jobs_per_client = 2

    def submit(_):
        try:
            return enqueue(store)["id"]
        except QueueFull:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(job is not None for job in results) == 2
    assert len(store.list_jobs("alice")) == 2


def test_account_disable_revokes_credentials_and_pending_work(store):
    store.save_account("youtube-account", "youtube", "synthetic-encrypted-cookie")
    running = enqueue(store, account_id="youtube-account")
    store.claim("worker")
    pending = enqueue(store, account_id="youtube-account")
    store.disable_account("youtube-account")
    assert store.get_account("youtube-account")["encrypted_cookies"] == ""
    assert store.get_job(running["id"])["cancel_requested"]
    assert store.claim("other-worker") is None
    assert store.get_job(pending["id"])["error_code"] == "account_unavailable"
    with pytest.raises(AccountUnavailable):
        enqueue(store, account_id="youtube-account")


def test_auth_failure_marks_account_for_refresh_but_preserves_rotation(store):
    store.save_account("youtube-account", "youtube", "old-cookies")
    job = enqueue(store, account_id="youtube-account")
    store.claim("worker")
    store.fail(job["id"], "worker", "auth_required", "Refresh account cookies")
    assert store.get_account("youtube-account")["status"] == "needs_refresh"
    store.save_account("youtube-account", "youtube", "rotated-cookies")
    next_job = enqueue(store, account_id="youtube-account")
    store.claim("next-worker")
    store.save_account("youtube-account", "youtube", "newer-cookies")
    store.fail(next_job["id"], "next-worker", "auth_required", "Old session failed")
    account = store.get_account("youtube-account")
    assert account["status"] == "active" and account["encrypted_cookies"] == "newer-cookies"


def test_retention_expires_only_completed_jobs(store):
    job = enqueue(store)
    store.claim("worker")
    complete(store, job)
    pending = enqueue(store)
    with store.connect() as db:
        db.execute("UPDATE jobs SET expires_at=%s WHERE id=%s", (time.time() - 1, job["id"]))
    assert [item["id"] for item in store.expire()] == [job["id"]]
    assert store.get_job(job["id"])["status"] == "expired"
    assert store.get_job(pending["id"])["status"] == "queued"
    assert store.expire() == []
