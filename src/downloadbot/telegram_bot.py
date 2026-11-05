"""Optional Telegram adapter. Run with ``python -m downloadbot.telegram_bot``.

Only private chats from explicitly allowed Telegram users are accepted. PostgreSQL
retains update offsets and job ownership across restarts. Run a single adapter
instance per bot token/TELEGRAM_BOT_INSTANCE. Telegram delivery is at
least once: a crash immediately after upload can result in a repeated document.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from email.message import Message
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

LOG = logging.getLogger(__name__)
# Hosted Bot API currently permits sendDocument multipart uploads up to 50 MB.
# https://core.telegram.org/bots/api#senddocument
TELEGRAM_MAX_FILE_BYTES = 50_000_000
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})
HELP = (
    "Пришлите ссылку на видео или /download URL.\n"
    "/status ID — статус вашего задания.\n"
    "/cancel ID — отменить ваше задание.\n"
    "Готовый файл придёт автоматически, если он не превышает 50 МБ. "
    "Скачивайте только материалы, к которым у вас есть разрешённый доступ."
)


@dataclass(frozen=True)
class BotConfig:
    token: str
    allowed_user_ids: frozenset[int]
    api_key: str
    database_url: str
    api_url: str = "http://127.0.0.1:8000"
    instance: str = "default"
    max_pending_per_user: int = 3
    max_pending_total: int = 100
    monitor_concurrency: int = 4
    poll_interval: float = 3.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BotConfig:
        env = os.environ if env is None else env
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("TELEGRAM_BOT_TOKEN must contain a valid bot token")
        raw_ids = env.get("TELEGRAM_ALLOWED_USER_IDS", "")
        try:
            allowed = frozenset(int(item.strip()) for item in raw_ids.split(","))
        except ValueError:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must list numeric user IDs") from None
        if not allowed or any(item <= 0 for item in allowed):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must list positive user IDs")
        api_key = env.get("DOWNLOADBOT_API_KEY", "").strip()
        if not api_key:
            raise ValueError("DOWNLOADBOT_API_KEY is required")
        database_url = env.get("DOWNLOADBOT_DATABASE_URL", "").strip()
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DOWNLOADBOT_DATABASE_URL must be a PostgreSQL connection URL")
        instance = env.get("TELEGRAM_BOT_INSTANCE", "default").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instance):
            raise ValueError("TELEGRAM_BOT_INSTANCE must contain 1-64 letters, digits, underscores or hyphens")
        api_url = env.get("DOWNLOADBOT_API_URL", "http://127.0.0.1:8000").rstrip("/")
        parsed = urlsplit(api_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DOWNLOADBOT_API_URL must be an HTTP(S) URL without credentials")
        try:
            per_user = int(env.get("TELEGRAM_MAX_PENDING_PER_USER", "3"))
            total = int(env.get("TELEGRAM_MAX_PENDING_TOTAL", "100"))
        except ValueError:
            raise ValueError("Telegram pending limits must be positive integers") from None
        if per_user < 1 or total < 1:
            raise ValueError("Telegram pending limits must be positive integers")
        return cls(
            token=token,
            allowed_user_ids=allowed,
            api_key=api_key,
            database_url=database_url,
            api_url=api_url,
            instance=instance,
            max_pending_per_user=per_user,
            max_pending_total=total,
        )

    @property
    def bot_id(self) -> str:
        return self.token.split(":", 1)[0]


class BotState:
    """Small durable state store; all access occurs on the adapter event loop."""

    def __init__(self, database_url: str, instance: str = "default"):
        self.instance = instance
        self.db = psycopg.connect(database_url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        lock = self.db.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
            (f"downloadbot:telegram:{instance}",),
        ).fetchone()
        if not lock["acquired"]:
            self.db.close()
            raise ValueError("Another Telegram adapter already uses this TELEGRAM_BOT_INSTANCE")
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_metadata (
                instance TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(instance, key)
            );
            CREATE TABLE IF NOT EXISTS bot_jobs (
                instance TEXT NOT NULL,
                id TEXT NOT NULL,
                update_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                chat_id BIGINT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                checked_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                PRIMARY KEY(instance, id),
                UNIQUE(instance, update_id)
            );
            CREATE INDEX IF NOT EXISTS bot_pending_jobs ON bot_jobs(instance, completed, checked_at);
            """
        )

    def close(self) -> None:
        self.db.close()

    def bind_bot(self, bot_id: str) -> None:
        row = self.db.execute(
            "SELECT value FROM bot_metadata WHERE instance = %s AND key = 'bot_id'", (self.instance,)
        ).fetchone()
        if row and row["value"] != bot_id:
            raise ValueError("TELEGRAM_BOT_INSTANCE belongs to another bot; use a separate instance name")
        self.db.execute(
            "INSERT INTO bot_metadata VALUES (%s, 'bot_id', %s) ON CONFLICT DO NOTHING",
            (self.instance, bot_id),
        )

    @property
    def offset(self) -> int:
        row = self.db.execute(
            "SELECT value FROM bot_metadata WHERE instance = %s AND key = 'offset'", (self.instance,)
        ).fetchone()
        return int(row["value"]) if row else 0

    def acknowledge(self, update_id: int) -> None:
        self.db.execute(
            "INSERT INTO bot_metadata VALUES (%s, 'offset', %s) "
            "ON CONFLICT(instance, key) DO UPDATE SET value = "
            "GREATEST(bot_metadata.value::bigint, excluded.value::bigint)::text",
            (self.instance, str(update_id + 1)),
        )

    def add_job(self, job_id: str, update_id: int, user_id: int, chat_id: int) -> None:
        self.db.execute(
            "INSERT INTO bot_jobs (instance, id, update_id, user_id, chat_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (self.instance, job_id, update_id, user_id, chat_id),
        )

    def job_for_update(self, update_id: int) -> dict[str, Any] | None:
        return self.db.execute(
            "SELECT * FROM bot_jobs WHERE instance = %s AND update_id = %s", (self.instance, update_id)
        ).fetchone()

    def owns(self, job_id: str, user_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM bot_jobs WHERE instance = %s AND id = %s AND user_id = %s",
            (self.instance, job_id, user_id),
        ).fetchone() is not None

    def pending_count(self, user_id: int | None = None) -> int:
        if user_id is None:
            row = self.db.execute(
                "SELECT count(*) AS total FROM bot_jobs WHERE instance = %s AND completed = 0",
                (self.instance,),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT count(*) AS total FROM bot_jobs WHERE instance = %s AND completed = 0 AND user_id = %s",
                (self.instance, user_id),
            ).fetchone()
        return int(row["total"])

    def pending(self, limit: int) -> list[dict[str, Any]]:
        return self.db.execute(
            "SELECT * FROM bot_jobs WHERE instance = %s AND completed = 0 ORDER BY checked_at, id LIMIT %s",
            (self.instance, limit),
        ).fetchall()

    def checked(self, job_id: str) -> None:
        self.db.execute(
            "UPDATE bot_jobs SET checked_at = %s WHERE instance = %s AND id = %s",
            (time.time(), self.instance, job_id),
        )

    def finish(self, job_id: str) -> None:
        self.db.execute(
            "UPDATE bot_jobs SET completed = 1 WHERE instance = %s AND id = %s", (self.instance, job_id)
        )


class TelegramError(RuntimeError):
    """Deliberately excludes request URLs, tokens and server response bodies."""


class OversizeFile(ValueError):
    pass


class TelegramAdapter:
    def __init__(
        self,
        config: BotConfig,
        state: BotState,
        api_client: httpx.AsyncClient,
        telegram_client: httpx.AsyncClient,
    ):
        self.config = config
        self.state = state
        self.api = api_client
        self.telegram = telegram_client
        self.state.bind_bot(config.bot_id)

    def api_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"}

    def api_endpoint(self, path: str) -> str:
        return f"{self.config.api_url}/v1{path}"

    async def telegram_call(self, method: str, **kwargs: Any) -> Any:
        response = await self.telegram.post(
            f"https://api.telegram.org/bot{self.config.token}/{method}", **kwargs
        )
        if response.status_code >= 400:
            raise TelegramError(f"Telegram request failed with HTTP {response.status_code}")
        body = response.json()
        if not body.get("ok"):
            raise TelegramError("Telegram rejected the request")
        return body.get("result")

    async def send_text(self, chat_id: int, text: str) -> None:
        await self.telegram_call("sendMessage", json={"chat_id": chat_id, "text": text[:4000]})

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if (
            chat.get("type") != "private"
            or sender.get("is_bot")
            or user_id not in self.config.allowed_user_ids
            or not isinstance(chat_id, int)
        ):
            return
        text = message.get("text", "").strip()
        if not text:
            return
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await self.send_text(chat_id, HELP)
            return
        if command in {"/status", "/cancel"}:
            if len(parts) != 2:
                await self.send_text(chat_id, f"Формат: {command} ID")
                return
            job_id = parts[1]
            if not self.state.owns(job_id, user_id):
                await self.send_text(chat_id, "Задание не найдено среди ваших заданий.")
                return
            if command == "/cancel":
                response = await self.api.post(
                    self.api_endpoint(f"/jobs/{job_id}/cancel"), headers=self.api_headers()
                )
            else:
                response = await self.api.get(
                    self.api_endpoint(f"/jobs/{job_id}"), headers=self.api_headers()
                )
            if response.status_code >= 500 or response.status_code == 429:
                response.raise_for_status()
            if response.is_error:
                await self.send_text(chat_id, f"API отклонил запрос (HTTP {response.status_code}).")
                return
            result = response.json()
            await self.send_text(chat_id, f"Задание {job_id}: {result.get('status', 'unknown')}.")
            return
        if command == "/download":
            url = parts[1] if len(parts) == 2 else ""
        elif len(parts) == 1 and not command.startswith("/"):
            url = text
        else:
            url = ""
        try:
            parsed = urlsplit(url)
            valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            await self.send_text(chat_id, HELP)
            return
        update_id = int(update["update_id"])
        # A restarted poll or failed acknowledgment reuses the durable mapping.
        existing = self.state.job_for_update(update_id)
        if existing:
            job_id = existing["id"]
        else:
            if (
                self.state.pending_count(user_id) >= self.config.max_pending_per_user
                or self.state.pending_count() >= self.config.max_pending_total
            ):
                await self.send_text(chat_id, "Достигнут лимит активных заданий. Дождитесь завершения.")
                return
            headers = self.api_headers()
            headers["Idempotency-Key"] = f"telegram:{self.config.bot_id}:{update_id}"
            response = await self.api.post(
                self.api_endpoint("/jobs"), json={"url": url, "max_height": 720}, headers=headers
            )
            if response.status_code >= 500 or response.status_code == 429:
                response.raise_for_status()
            if response.is_error:
                await self.send_text(chat_id, f"API отклонил загрузку (HTTP {response.status_code}).")
                return
            job_id = str(response.json()["id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
                raise ValueError("API returned an invalid job ID")
            self.state.add_job(job_id, update_id, user_id, chat_id)
        await self.send_text(chat_id, f"Задание создано: {job_id}.\n/status {job_id}")

    async def poll_once(self) -> None:
        updates = await self.telegram_call(
            "getUpdates",
            json={"offset": self.state.offset, "timeout": 25, "limit": 100, "allowed_updates": ["message"]},
            timeout=35,
        )
        for update in updates or []:
            if int(update["update_id"]) < self.state.offset:
                continue
            await self.handle_update(update)
            # Do not confirm the Telegram update until the local state is durable.
            self.state.acknowledge(int(update["update_id"]))

    async def deliver_file(self, job_id: str, chat_id: int, title: str) -> None:
        async with self.api.stream(
            "GET", self.api_endpoint(f"/jobs/{job_id}/file"), headers=self.api_headers()
        ) as response:
            response.raise_for_status()
            size_header = response.headers.get("content-length")
            if size_header and int(size_header) > TELEGRAM_MAX_FILE_BYTES:
                raise OversizeFile()
            header = Message()
            header["content-disposition"] = response.headers.get("content-disposition", "")
            filename = header.get_filename() or f"{job_id}.mp4"
            filename = re.split(r"[/\\]", filename)[-1]
            filename = re.sub(r"[^\w. ()-]", "_", filename)[:150] or "video.mp4"
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as output:
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > TELEGRAM_MAX_FILE_BYTES:
                        raise OversizeFile()
                    output.write(chunk)
                output.seek(0)
                await self.telegram_call(
                    "sendDocument",
                    data={"chat_id": str(chat_id), "caption": (title or f"Задание {job_id}")[:1024]},
                    files={"document": (filename, output, "application/octet-stream")},
                    timeout=httpx.Timeout(180, connect=10),
                )

    async def check_job(self, row: Mapping[str, Any]) -> None:
        job_id, chat_id = row["id"], row["chat_id"]
        self.state.checked(job_id)
        # Revoking an allowed user also revokes automatic document delivery.
        if row["user_id"] not in self.config.allowed_user_ids:
            self.state.finish(job_id)
            return
        response = await self.api.get(self.api_endpoint(f"/jobs/{job_id}"), headers=self.api_headers())
        if response.status_code in {404, 410}:
            await self.send_text(chat_id, f"Задание {job_id} больше не доступно.")
            self.state.finish(job_id)
            return
        response.raise_for_status()
        job = response.json()
        status = job.get("status")
        if status not in TERMINAL_STATUSES:
            return
        if status == "succeeded":
            try:
                if int(job.get("size_bytes") or 0) > TELEGRAM_MAX_FILE_BYTES:
                    raise OversizeFile()
                await self.deliver_file(job_id, chat_id, str(job.get("title") or ""))
            except OversizeFile:
                await self.send_text(
                    chat_id,
                    f"Задание {job_id} готово, но файл превышает лимит Telegram 50 МБ. "
                    "Обратитесь к администратору сервиса за файлом.",
                )
        else:
            # Backend exception text may include upstream URLs or authentication details.
            error = job.get("error") or {}
            code = str(error.get("code") or "")
            safe_code = f" ({code})" if re.fullmatch(r"[a-zA-Z_]{1,60}", code) else ""
            await self.send_text(chat_id, f"Задание {job_id}: {status}{safe_code}.")
        self.state.finish(job_id)

    async def monitor_once(self) -> None:
        async def guarded(row: Mapping[str, Any]) -> None:
            try:
                await self.check_job(row)
            except (httpx.HTTPError, TelegramError, ValueError) as exc:
                LOG.warning("Job monitoring will retry (%s)", type(exc).__name__)

        await asyncio.gather(*(guarded(row) for row in self.state.pending(self.config.monitor_concurrency)))

    async def run(self) -> None:
        async def poll_loop() -> None:
            while True:
                try:
                    await self.poll_once()
                except (httpx.HTTPError, TelegramError, ValueError) as exc:
                    LOG.warning("Telegram polling will retry (%s)", type(exc).__name__)
                    await asyncio.sleep(self.config.poll_interval)

        async def monitor_loop() -> None:
            while True:
                await self.monitor_once()
                await asyncio.sleep(self.config.poll_interval)

        # File transfers run independently so a large upload cannot block getUpdates.
        async with asyncio.TaskGroup() as group:
            group.create_task(poll_loop())
            group.create_task(monitor_loop())


async def async_main(config: BotConfig) -> None:
    state = BotState(config.database_url, config.instance)
    try:
        async with (
            httpx.AsyncClient(timeout=30, follow_redirects=False) as api,
            httpx.AsyncClient(timeout=35, follow_redirects=False) as telegram,
        ):
            await TelegramAdapter(config, state, api, telegram).run()
    finally:
        state.close()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx INFO logs contain the Bot API token embedded in its URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = BotConfig.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
