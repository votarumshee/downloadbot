from __future__ import annotations

import logging
import os
import shutil
import signal
import tempfile
import threading
import time
import uuid
from pathlib import Path

from downloadbot.config import Settings
from downloadbot.downloader import DownloadError, YtDlpDownloader
from downloadbot.store import Store
from downloadbot.vault import CookieVault

logger = logging.getLogger(__name__)


def remove_directory(path: Path, root: Path):
    path, root = path.resolve(), root.resolve()
    if path != root and path.is_relative_to(root) and path.is_dir():
        shutil.rmtree(path)


class Worker:
    def __init__(self, settings: Settings, downloader=None):
        self.settings = settings
        self.store = Store(settings)
        self.vault = CookieVault(settings.encryption_key.get_secret_value())
        self.id = str(uuid.uuid4())
        self.stop = threading.Event()
        self.downloader = downloader or YtDlpDownloader(
            max_file_bytes=settings.max_file_bytes,
            max_duration_seconds=settings.max_duration_seconds,
            timeout_seconds=settings.download_timeout_seconds,
            ffmpeg_location=settings.ffmpeg_location,
        )

    def initialize(self):
        self.store.initialize()
        self.settings.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def cleanup(self):
        for job in self.store.expire():
            remove_directory(self.settings.files_dir / job["id"], self.settings.files_dir)
        # Also retry failed deletes and remove crash leftovers after their lease is gone.
        for path in self.settings.files_dir.iterdir():
            if not path.is_dir() or time.time() - path.stat().st_mtime < self.settings.lease_seconds * 2:
                continue
            job = self.store.get_job(path.name)
            if not job or job["status"] in {"failed", "cancelled", "expired"}:
                remove_directory(path, self.settings.files_dir)
            elif job["status"] in {"succeeded", "queued", "running"}:
                for attempt_path in path.iterdir():
                    try:
                        attempt = int(attempt_path.name.removeprefix("attempt"))
                    except ValueError:
                        continue
                    if attempt < job["attempt"] or (job["status"] == "queued" and attempt == job["attempt"]):
                        remove_directory(attempt_path, self.settings.files_dir)
        for path in self.settings.temp_dir.iterdir():
            if path.is_file() and path.name.endswith(".cookies"):
                # Cookies from a crashed child must not survive indefinitely.
                if time.time() - path.stat().st_mtime > self.settings.download_timeout_seconds + self.settings.lease_seconds * 2:
                    path.unlink(missing_ok=True)

    def run_once(self) -> bool:
        self.store.worker_ping(self.id)
        job = self.store.claim(self.id)
        if not job:
            return False
        job_id = job["id"]
        output_dir = self.settings.files_dir / job_id / f"attempt{job['attempt']}"
        cookie_path = None
        last_heartbeat = 0.0

        def cancelled():
            nonlocal last_heartbeat
            if self.stop.is_set():
                return True
            if time.monotonic() - last_heartbeat >= min(2, self.settings.lease_seconds / 4):
                last_heartbeat = time.monotonic()
                self.store.worker_ping(self.id)
                return not self.store.heartbeat(job_id, self.id)
            return False

        try:
            if shutil.disk_usage(self.settings.files_dir).free < self.settings.min_free_disk_bytes + self.settings.max_file_bytes * 3:
                raise DownloadError("storage_full", "Insufficient download storage")
            if job["account_id"]:
                account = self.store.get_account(job["account_id"])
                if not account or account["status"] != "active":
                    raise DownloadError("account_unavailable", "Account is disabled or requires new cookies")
                try:
                    cookies = self.vault.decrypt(account["encrypted_cookies"])
                except ValueError:
                    raise DownloadError("account_unavailable", "Account credentials cannot be decrypted") from None
                fd, name = tempfile.mkstemp(prefix=f"{job_id}-", suffix=".cookies", dir=self.settings.temp_dir)
                cookie_path = Path(name)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(cookies)
            result = self.downloader.download(url=job["url"], output_dir=output_dir, cookie_file=cookie_path,
                                              max_height=job["max_height"], cancelled=cancelled)
            relative = result.path.resolve().relative_to(self.settings.files_dir).as_posix()
            if not self.store.finish(job_id, self.id, artifact_path=relative, title=result.title, ext=result.ext,
                                     size_bytes=result.size_bytes, duration=result.duration):
                self.store.fail(job_id, self.id, "cancelled", "The download was cancelled")
                remove_directory(output_dir, self.settings.files_dir)
            logger.info("Job %s completed", job_id)
        except DownloadError as exc:
            if self.stop.is_set():
                self.store.release(job_id, self.id)
            else:
                self.store.fail(job_id, self.id, exc.code, exc.message, exc.retryable)
            remove_directory(output_dir, self.settings.files_dir)
            logger.info("Job %s ended: %s", job_id, exc.code)
        except Exception:
            # Exceptions from libraries can contain signed URLs or cookies: never log their text.
            self.store.fail(job_id, self.id, "internal_error", "An internal worker error occurred")
            remove_directory(output_dir, self.settings.files_dir)
            logger.error("Job %s failed with an internal error", job_id)
        finally:
            if cookie_path:
                cookie_path.unlink(missing_ok=True)
        return True

    def run(self):
        self.initialize()
        last_cleanup = 0.0
        while not self.stop.is_set():
            try:
                if time.monotonic() - last_cleanup > 30:
                    self.cleanup()
                    last_cleanup = time.monotonic()
                if not self.run_once():
                    self.stop.wait(self.settings.worker_poll_seconds)
            except Exception:
                logger.error("Worker database/storage unavailable; retrying")
                self.stop.wait(5)


def run_worker(settings: Settings):
    worker = Worker(settings)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.run()
