"""Telegram adapter tests use only MockTransport; no messages leave this process."""

import asyncio
import json
import os
import time
import uuid
from dataclasses import replace

import httpx
import pytest

from downloadbot import telegram_bot
from downloadbot.telegram_bot import BotConfig, BotState, TelegramAdapter, TelegramError


class MemoryState:
    """Adapter unit-test double; PostgreSQL persistence is checked separately."""

    def __init__(self):
        self.jobs = {}
        self.offset = 0

    def bind_bot(self, bot_id):
        self.bot_id = bot_id

    def acknowledge(self, update_id):
        self.offset = max(self.offset, update_id + 1)

    def add_job(self, job_id, update_id, user_id, chat_id):
        self.jobs[job_id] = dict(
            id=job_id, update_id=update_id, user_id=user_id, chat_id=chat_id, completed=0, checked_at=0
        )

    def job_for_update(self, update_id):
        return next((row for row in self.jobs.values() if row["update_id"] == update_id), None)

    def owns(self, job_id, user_id):
        return job_id in self.jobs and self.jobs[job_id]["user_id"] == user_id

    def pending_count(self, user_id=None):
        return sum(
            not row["completed"] and (user_id is None or row["user_id"] == user_id)
            for row in self.jobs.values()
        )

    def pending(self, limit):
        return sorted(
            (row for row in self.jobs.values() if not row["completed"]),
            key=lambda row: row["checked_at"],
        )[:limit]

    def checked(self, job_id):
        self.jobs[job_id]["checked_at"] = time.time()

    def finish(self, job_id):
        self.jobs[job_id]["completed"] = 1


def update(text="https://www.youtube.com/watch?v=BaW_jenozKc", user_id=101, chat_type="private", update_id=8):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False},
            "text": text,
        },
    }


@pytest.fixture
def harness():
    api_requests = []
    telegram_requests = []
    def api_handler(request):
        return httpx.Response(202, json={"id": "job-123", "status": "queued"})

    def telegram_handler(request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_transport(request):
        api_requests.append(request)
        return api_handler(request)

    def telegram_transport(request):
        telegram_requests.append(request)
        return telegram_handler(request)

    api = httpx.AsyncClient(transport=httpx.MockTransport(api_transport))
    telegram = httpx.AsyncClient(transport=httpx.MockTransport(telegram_transport))
    config = BotConfig(
        token="123:test-token",
        allowed_user_ids=frozenset({101, 202}),
        api_key="private-api-key",
        database_url="postgresql://unused/unused",
    )
    adapter = TelegramAdapter(config, MemoryState(), api, telegram)

    def set_api(handler):
        nonlocal api_handler
        api_handler = handler

    def set_telegram(handler):
        nonlocal telegram_handler
        telegram_handler = handler

    yield adapter, api_requests, telegram_requests, set_api, set_telegram
    asyncio.run(api.aclose())
    asyncio.run(telegram.aclose())


def valid_env():
    return {
        "TELEGRAM_BOT_TOKEN": "123:example",
        "TELEGRAM_ALLOWED_USER_IDS": "101,202",
        "DOWNLOADBOT_API_KEY": "secret",
        "DOWNLOADBOT_DATABASE_URL": "postgresql://user:password@127.0.0.1/downloadbot",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TELEGRAM_ALLOWED_USER_IDS", ""),
        ("TELEGRAM_ALLOWED_USER_IDS", "0"),
        ("TELEGRAM_ALLOWED_USER_IDS", "101,garbage"),
        ("TELEGRAM_BOT_TOKEN", "123:token/path"),
        ("DOWNLOADBOT_API_KEY", ""),
        ("DOWNLOADBOT_DATABASE_URL", "sqlite:///data.db"),
        ("DOWNLOADBOT_API_URL", "https://user:password@example.com"),
        ("TELEGRAM_MAX_PENDING_TOTAL", "0"),
        ("TELEGRAM_BOT_INSTANCE", "bad/name"),
    ],
)
def test_configuration_fails_closed(key, value):
    env = valid_env()
    env[key] = value
    with pytest.raises(ValueError):
        BotConfig.from_env(env)


def test_configuration_reads_explicit_allowed_users():
    config = BotConfig.from_env(valid_env())
    assert config.allowed_user_ids == {101, 202}
    assert config.bot_id == "123"
    assert config.instance == "default"


@pytest.mark.parametrize("message", [update(user_id=303), update(chat_type="group"), update(chat_type="channel")])
def test_unauthorized_chat_cannot_create_jobs_or_receive_messages(harness, message):
    adapter, api_requests, telegram_requests, _, _ = harness
    asyncio.run(adapter.handle_update(message))
    assert not api_requests
    assert not telegram_requests


def test_download_replay_reuses_job_with_stable_idempotency_key(harness):
    adapter, api_requests, telegram_requests, _, _ = harness

    async def scenario():
        await adapter.handle_update(update())
        await adapter.handle_update(update())

    asyncio.run(scenario())
    assert len(api_requests) == 1
    request = api_requests[0]
    assert request.headers["Authorization"] == "Bearer private-api-key"
    assert request.headers["Idempotency-Key"] == "telegram:123:8"
    assert json.loads(request.content) == {"url": update()["message"]["text"], "max_height": 720}
    assert adapter.state.owns("job-123", 101)
    assert len(telegram_requests) == 2


def test_foreign_job_is_denied_before_api_request(harness):
    adapter, api_requests, telegram_requests, _, _ = harness
    adapter.state.add_job("someone-elses-job", 1, 101, 101)

    async def scenario():
        await adapter.handle_update(update("/status someone-elses-job", user_id=202))
        await adapter.handle_update(update("/cancel someone-elses-job", user_id=202))
        await adapter.handle_update(update("/status ../../accounts", user_id=202))

    asyncio.run(scenario())
    assert not api_requests
    assert len(telegram_requests) == 3
    assert all("не найдено" in json.loads(request.content)["text"] for request in telegram_requests)


def test_owner_can_check_and_cancel_job(harness):
    adapter, api_requests, _, _, _ = harness
    adapter.state.add_job("my-job", 1, 101, 101)

    async def scenario():
        await adapter.handle_update(update("/status my-job"))
        await adapter.handle_update(update("/cancel my-job"))

    asyncio.run(scenario())
    assert [(req.method, req.url.path) for req in api_requests] == [
        ("GET", "/v1/jobs/my-job"), ("POST", "/v1/jobs/my-job/cancel")
    ]


def test_pending_limit_prevents_additional_job(harness):
    adapter, api_requests, telegram_requests, _, _ = harness
    adapter.config = replace(adapter.config, max_pending_per_user=1)
    adapter.state.add_job("pending", 1, 101, 101)
    asyncio.run(adapter.handle_update(update()))
    assert not api_requests
    assert "лимит" in json.loads(telegram_requests[0].content)["text"]


def test_poll_does_not_acknowledge_failed_reply_and_can_recover(harness):
    adapter, api_requests, _, _, set_telegram = harness

    def failing(request):
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [update()]})
        return httpx.Response(503, json={"ok": False})

    set_telegram(failing)
    with pytest.raises(TelegramError):
        asyncio.run(adapter.poll_once())
    assert adapter.state.offset == 0
    assert adapter.state.owns("job-123", 101)

    def recovered(request):
        return httpx.Response(
            200, json={"ok": True, "result": [update()] if request.url.path.endswith("getUpdates") else {}}
        )

    set_telegram(recovered)
    asyncio.run(adapter.poll_once())
    assert adapter.state.offset == 9
    assert len(api_requests) == 1


def test_completed_file_is_uploaded_without_forwarding_api_credentials(harness):
    adapter, api_requests, telegram_requests, set_api, _ = harness
    adapter.state.add_job("ready", 1, 101, 101)

    def handler(request):
        if request.url.path.endswith("/file"):
            return httpx.Response(
                200,
                content=b"test video content",
                headers={"Content-Disposition": 'attachment; filename="../../video.mp4"'},
            )
        return httpx.Response(200, json={"status": "succeeded", "title": "Video", "size_bytes": 18})

    set_api(handler)
    asyncio.run(adapter.monitor_once())
    assert adapter.state.pending_count() == 0
    assert len(api_requests) == 2
    assert all(request.headers["Authorization"] == "Bearer private-api-key" for request in api_requests)
    document = telegram_requests[0]
    assert document.url.path.endswith("/sendDocument")
    assert "Authorization" not in document.headers
    assert b'filename="video.mp4"' in document.content
    assert b"test video content" in document.content
    assert b"private-api-key" not in document.content


@pytest.mark.parametrize("declared_size", [100, 0])
def test_oversized_file_never_reaches_telegram(harness, monkeypatch, declared_size):
    adapter, _, telegram_requests, set_api, _ = harness
    monkeypatch.setattr(telegram_bot, "TELEGRAM_MAX_FILE_BYTES", 20)
    adapter.state.add_job("large", 1, 101, 101)

    class UnknownLengthStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 15
            yield b"x" * 15

    def handler(request):
        if request.url.path.endswith("/file"):
            return httpx.Response(200, stream=UnknownLengthStream())
        return httpx.Response(200, json={"status": "succeeded", "size_bytes": declared_size})

    set_api(handler)
    asyncio.run(adapter.monitor_once())
    assert adapter.state.pending_count() == 0
    assert len(telegram_requests) == 1
    assert telegram_requests[0].url.path.endswith("/sendMessage")
    assert "50 МБ" in json.loads(telegram_requests[0].content)["text"]


def test_failure_message_does_not_forward_backend_secrets(harness):
    adapter, _, telegram_requests, set_api, _ = harness
    adapter.state.add_job("failed", 1, 101, 101)
    set_api(lambda request: httpx.Response(200, json={
        "status": "failed",
        "error": {"code": "authentication_required", "message": "upstream cookie secret-value"},
    }))
    asyncio.run(adapter.monitor_once())
    text = json.loads(telegram_requests[0].content)["text"]
    assert "authentication_required" in text
    assert "secret-value" not in text


def test_revoked_user_gets_no_automatic_delivery(harness):
    adapter, api_requests, telegram_requests, _, _ = harness
    adapter.state.add_job("old-job", 1, 303, 303)
    asyncio.run(adapter.monitor_once())
    assert not api_requests
    assert not telegram_requests
    assert adapter.state.pending_count() == 0


def test_monitor_rotates_a_bounded_batch(harness):
    adapter, api_requests, _, set_api, _ = harness
    adapter.config = replace(adapter.config, monitor_concurrency=2)
    for number in range(5):
        adapter.state.add_job(f"job-{number}", number, 101, 101)
    set_api(lambda request: httpx.Response(200, json={"status": "running"}))

    async def scenario():
        await adapter.monitor_once()
        await adapter.monitor_once()

    asyncio.run(scenario())
    assert len(api_requests) == 4
    assert len({request.url.path for request in api_requests}) == 4


@pytest.mark.skipif(not os.environ.get("DOWNLOADBOT_TEST_DATABASE_URL"), reason="requires test PostgreSQL")
def test_postgres_state_survives_restart_and_isolates_instances():
    database_url = os.environ["DOWNLOADBOT_TEST_DATABASE_URL"]
    instance = f"test-{uuid.uuid4().hex}"
    other_instance = f"test-{uuid.uuid4().hex}"
    state = BotState(database_url, instance)
    other = None
    try:
        state.bind_bot("123")
        state.add_job("job-123", 8, 5_000_000_001, 5_000_000_001)
        state.acknowledge(8)
        state.acknowledge(3)
        state.close()
        state = BotState(database_url, instance)
        state.bind_bot("123")
        assert state.offset == 9
        assert state.owns("job-123", 5_000_000_001)
        assert not state.owns("job-123", 101)
        assert state.pending_count() == 1
        assert state.pending_count(101) == 0
        assert state.job_for_update(8)["id"] == "job-123"
        with pytest.raises(ValueError, match="Another Telegram adapter"):
            BotState(database_url, instance)
        state.checked("job-123")
        assert state.pending(1)[0]["checked_at"] > 0
        other = BotState(database_url, other_instance)
        other.bind_bot("456")
        assert other.offset == 0
        assert not other.owns("job-123", 5_000_000_001)
        with pytest.raises(ValueError):
            state.bind_bot("456")
        state.finish("job-123")
        assert state.pending_count() == 0
    finally:
        if other:
            other.close()
        state.db.execute("DELETE FROM bot_jobs WHERE instance IN (%s, %s)", (instance, other_instance))
        state.db.execute("DELETE FROM bot_metadata WHERE instance IN (%s, %s)", (instance, other_instance))
        state.close()
