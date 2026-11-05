import time

import pytest
from conftest import ADMIN_KEY, ALICE_KEY, BOB_KEY, COOKIE_VALUE, YOUTUBE_COOKIES
from fastapi.testclient import TestClient

from downloadbot.api import create_app

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


def auth(key=ALICE_KEY):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def submit(client, key=ALICE_KEY, **kwargs):
    return client.post("/v1/jobs", headers=auth(key), json={"url": URL, **kwargs})


def account(client):
    return client.post("/v1/accounts", headers=auth(ADMIN_KEY), json={
        "id": "youtube-account", "platform": "youtube", "cookies": YOUTUBE_COOKIES,
    })


def finished_file(client, settings, store):
    job = submit(client).json()
    claimed = store.claim("worker")
    assert claimed["id"] == job["id"]
    artifact = settings.files_dir / job["id"] / "attempt1" / "media.mp4"
    artifact.parent.mkdir(parents=True)
    content = b"synthetic-video-content"
    artifact.write_bytes(content)
    assert store.finish(job["id"], "worker", artifact_path=artifact.relative_to(settings.files_dir).as_posix(),
                        title="Synthetic video", ext="mp4", size_bytes=len(content), duration=2.0)
    return job, artifact, content


@pytest.mark.parametrize("path", ["/v1/jobs", "/v1/accounts", "/v1/platforms"])
def test_authentication_required(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth("invalid-key")).status_code == 401
    assert client.get(path, headers={"Authorization": f"Basic {ADMIN_KEY}"}).status_code == 401


def test_health_separates_liveness_from_worker_readiness(client, store):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    store.worker_ping("test-worker")
    assert client.get("/readyz").status_code == 200
    with store.connect() as db:
        db.execute("UPDATE workers SET heartbeat_at=0")
    assert client.get("/readyz").status_code == 503


def test_job_isolation_applies_to_list_get_file_and_cancel(client):
    job = submit(client).json()
    identifier = job["id"]
    assert client.get(f"/v1/jobs/{identifier}", headers=auth()).status_code == 200
    assert client.get(f"/v1/jobs/{identifier}", headers=auth(ADMIN_KEY)).status_code == 200
    assert client.get("/v1/jobs", headers=auth(BOB_KEY)).json() == {"items": []}
    assert len(client.get("/v1/jobs", headers=auth()).json()["items"]) == 1
    for suffix in ("", "/file"):
        assert client.get(f"/v1/jobs/{identifier}{suffix}", headers=auth(BOB_KEY)).status_code == 404
    assert client.post(f"/v1/jobs/{identifier}/cancel", headers=auth(BOB_KEY)).status_code == 404


def test_idempotency_reuses_job_and_rejects_changed_payload(client):
    headers = {**auth(), "Idempotency-Key": "same-submission"}
    first = client.post("/v1/jobs", headers=headers, json={"url": URL})
    second = client.post("/v1/jobs", headers=headers, json={"url": URL})
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.headers["Location"] == f"/v1/jobs/{first.json()['id']}"
    changed = client.post("/v1/jobs", headers=headers, json={"url": URL, "max_height": 1080})
    assert changed.status_code == 409
    independent = client.post("/v1/jobs", headers={**auth(BOB_KEY), "Idempotency-Key": "same-submission"},
                              json={"url": URL})
    assert independent.status_code == 202 and independent.json()["id"] != first.json()["id"]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://127.0.0.1/video", "https://localhost/video",
    "https://169.254.169.254/latest/meta-data/", "https://www.youtube.com.evil.invalid/watch?v=BaW_jenozKc",
    "https://user:password@www.youtube.com/watch?v=BaW_jenozKc",
    "https://www.youtube.com:8080/watch?v=BaW_jenozKc", "https://www.youtube.com/playlist?list=test",
])
def test_rejects_unsafe_or_out_of_scope_urls(client, store, url):
    response = submit(client, url=url)
    assert response.status_code == 422
    assert store.list_jobs(None) == []


def test_canonical_url_removes_tracking_and_extractor_smuggling(client, store):
    response = submit(client, url=URL + "&tracking=untrusted#__youtubedl_smuggle=secret")
    assert response.status_code == 202
    assert response.json()["url"] == URL
    assert store.get_job(response.json()["id"])["url"] == URL


def test_public_job_does_not_implicitly_borrow_account(client, store):
    assert account(client).status_code == 201
    response = submit(client)
    assert response.status_code == 202
    assert response.json()["account_id"] is None
    assert store.get_job(response.json()["id"])["account_id"] is None


def test_technical_accounts_require_explicit_permission(client):
    assert account(client).status_code == 201
    assert submit(client, account_id="youtube-account").status_code == 202
    assert submit(client, BOB_KEY, account_id="youtube-account").status_code == 403
    assert submit(client, ADMIN_KEY, account_id="missing").status_code == 400
    wrong_platform = submit(client, account_id="youtube-account", url="https://x.com/test/status/123456789")
    assert wrong_platform.status_code == 400
    assert client.get("/v1/accounts", headers=auth()).status_code == 403
    assert client.post("/v1/accounts", headers=auth(), json={
        "id": "new", "platform": "youtube", "cookies": YOUTUBE_COOKIES,
    }).status_code == 403
    assert client.put("/v1/accounts/youtube-account/cookies", headers=auth(),
                      json={"cookies": YOUTUBE_COOKIES}).status_code == 403
    assert client.delete("/v1/accounts/youtube-account", headers=auth()).status_code == 403


def test_cookie_secrets_encrypted_and_never_returned(client, store):
    created = account(client)
    assert created.status_code == 201
    saved = store.get_account("youtube-account")
    ciphertext = saved["encrypted_cookies"]
    assert COOKIE_VALUE not in ciphertext
    assert client.app.state.vault.decrypt(ciphertext) == YOUTUBE_COOKIES
    rotated = client.put("/v1/accounts/youtube-account/cookies", headers=auth(ADMIN_KEY),
                         json={"cookies": YOUTUBE_COOKIES.replace(COOKIE_VALUE, "new-synthetic-cookie")})
    assert rotated.status_code == 200
    for response in [created, rotated, client.get("/v1/accounts", headers=auth(ADMIN_KEY))]:
        assert COOKIE_VALUE not in response.text
        assert ciphertext not in response.text
        assert "encrypted_cookies" not in response.text and '"cookies"' not in response.text
    deleted = client.delete("/v1/accounts/youtube-account", headers=auth(ADMIN_KEY))
    assert deleted.status_code == 204
    assert store.get_account("youtube-account")["encrypted_cookies"] == ""
    assert submit(client, account_id="youtube-account").status_code == 400


def test_cookie_validation_does_not_echo_input(client):
    secret = "cookie-contents-must-never-be-echoed"
    malformed = client.post("/v1/accounts", headers=auth(ADMIN_KEY), json={
        "id": "bad", "platform": "youtube", "cookies": {"secret": secret},
    })
    assert malformed.status_code == 422 and secret not in malformed.text
    wrong_domain = client.post("/v1/accounts", headers=auth(ADMIN_KEY), json={
        "id": "bad", "platform": "youtube",
        "cookies": YOUTUBE_COOKIES.replace("youtube.com", "evil.invalid"),
    })
    assert wrong_domain.status_code == 422 and COOKIE_VALUE not in wrong_domain.text
    oversized = client.post("/v1/accounts", headers=auth(ADMIN_KEY), content=b"x" * (301 * 1024))
    assert oversized.status_code == 413


def test_queue_limit_and_cancellation(client, settings):
    settings.max_active_jobs_per_client = 1
    first = submit(client)
    assert first.status_code == 202
    rejected = submit(client)
    assert rejected.status_code == 429 and rejected.headers["Retry-After"] == "10"
    cancelled = client.post(f"/v1/jobs/{first.json()['id']}/cancel", headers=auth())
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert submit(client).status_code == 202


def test_authenticated_result_download_and_expiry(client, settings, store):
    queued = submit(client).json()
    assert client.get(f"/v1/jobs/{queued['id']}/file", headers=auth()).status_code == 409
    store.request_cancel(queued["id"])
    job, artifact, content = finished_file(client, settings, store)
    endpoint = f"/v1/jobs/{job['id']}/file"
    assert client.get(endpoint).status_code == 401
    assert client.get(endpoint, headers=auth(BOB_KEY)).status_code == 404
    downloaded = client.get(endpoint, headers=auth())
    assert downloaded.status_code == 200 and downloaded.content == content
    assert downloaded.headers["Cache-Control"] == "private, no-store"
    public = client.get(f"/v1/jobs/{job['id']}", headers=auth()).json()
    assert public["file_url"] == endpoint and "artifact_path" not in public
    assert str(artifact) not in str(public)
    with store.connect() as db:
        db.execute("UPDATE jobs SET expires_at=%s WHERE id=%s", (time.time() - 1, job["id"]))
    assert client.get(endpoint, headers=auth()).status_code == 410
    expired = client.get(f"/v1/jobs/{job['id']}", headers=auth()).json()
    assert expired["status"] == "expired" and expired["file_url"] is None


def test_result_path_cannot_escape_own_job_directory(client, settings, store):
    job, _, _ = finished_file(client, settings, store)
    outside = settings.data_dir / "private-secret.txt"
    outside.write_text("private-content", encoding="utf-8")
    with store.connect() as db:
        db.execute("UPDATE jobs SET artifact_path=%s WHERE id=%s", (str(outside), job["id"]))
    denied = client.get(f"/v1/jobs/{job['id']}/file", headers=auth())
    assert denied.status_code == 410 and "private-content" not in denied.text


def test_non_ascii_invalid_bearer_is_rejected_without_server_error(client):
    response = client.get("/v1/jobs", headers=[(b"authorization", b"Bearer invalid-\xe9-token")])
    assert response.status_code == 401
