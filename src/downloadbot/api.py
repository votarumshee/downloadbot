import hmac
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from downloadbot.config import Principal, Settings
from downloadbot.store import AccountUnavailable, IdempotencyConflict, QueueFull, Store
from downloadbot.url_policy import validate_url
from downloadbot.vault import CookieVault


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=10, max_length=2048)
    account_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    max_height: int = Field(default=720, ge=144, le=2160)


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    platform: Literal["youtube", "x", "instagram", "vimeo"]
    cookies: SecretStr = Field(min_length=1, max_length=262144)


class CookieUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookies: SecretStr = Field(min_length=1, max_length=262144)


def timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def job_public(job: dict) -> dict:
    status = job["status"]
    if status == "succeeded" and job["expires_at"] <= time.time():
        status = "expired"
    return {
        "id": job["id"], "url": job["url"], "platform": job["platform"],
        "account_id": job["account_id"], "max_height": job["max_height"], "status": status,
        "attempt": job["attempt"], "cancel_requested": job["cancel_requested"],
        "created_at": timestamp(job["created_at"]), "updated_at": timestamp(job["updated_at"]),
        "started_at": timestamp(job["started_at"]), "finished_at": timestamp(job["finished_at"]),
        "expires_at": timestamp(job["expires_at"]), "title": job["title"],
        "size_bytes": job["size_bytes"], "duration": job["duration"], "ext": job["ext"],
        "error": {"code": job["error_code"], "message": job["error_message"]} if job["error_code"] else None,
        "file_url": f"/v1/jobs/{job['id']}/file" if status == "succeeded" else None,
    }


def account_public(account: dict) -> dict:
    return {key: account[key] for key in ("id", "platform", "status", "last_error_code")} | {
        "created_at": timestamp(account["created_at"]), "updated_at": timestamp(account["updated_at"]),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings)
    vault = CookieVault(settings.encryption_key.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        store.initialize()
        yield

    app = FastAPI(title="DownloadBot API", version="0.1.0", lifespan=lifespan,
                  description="Asynchronous video downloads with explicitly authorized technical accounts.")
    app.state.store, app.state.settings, app.state.vault = store, settings, vault
    bearer = HTTPBearer(auto_error=False)
    principals = settings.principals()

    # Reject oversized bodies before JSON parsing, including chunked requests.
    class BodyLimit:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
                return await self.app(scope, receive, send)
            chunks, total = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                total += len(message.get("body", b""))
                if total > 300 * 1024:
                    return await JSONResponse({"detail": "Request body too large"}, 413)(scope, receive, send)
                chunks.append(message)
                if not message.get("more_body", False):
                    break
            async def replay():
                return chunks.pop(0) if chunks else await receive()
            await self.app(scope, replay, send)

    app.add_middleware(BodyLimit)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Pydantic's default input field can contain raw cookies or passwords.
        return JSONResponse({"detail": [{"loc": e["loc"], "type": e["type"], "msg": e["msg"]}
                                        for e in exc.errors()]}, status_code=422)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Principal:
        if credentials and credentials.scheme.lower() == "bearer":
            for principal in principals:
                if hmac.compare_digest(credentials.credentials.encode("utf-8"), principal.api_key.encode("utf-8")):
                    return principal
        raise HTTPException(401, "Valid bearer API key required", headers={"WWW-Authenticate": "Bearer"})

    def admin(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if not principal.is_admin:
            raise HTTPException(403, "Administrator access required")
        return principal

    def owned_job(job_id: str, principal: Principal) -> dict:
        job = store.get_job(job_id)
        if not job or (job["owner"] != principal.id and not principal.is_admin):
            raise HTTPException(404, "Job not found")
        return job

    @app.get("/healthz", tags=["health"])
    def health():
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    def ready():
        try:
            if store.ready():
                return {"status": "ready"}
        except Exception:
            pass
        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.get("/v1/platforms", tags=["downloads"])
    def platforms(principal: Annotated[Principal, Depends(authenticate)]):
        return {"platforms": ["youtube", "x", "instagram", "vimeo"],
                "max_file_bytes": settings.max_file_bytes,
                "max_duration_seconds": settings.max_duration_seconds}

    @app.post("/v1/jobs", status_code=202, tags=["downloads"])
    def submit(body: JobCreate, response: Response, principal: Annotated[Principal, Depends(authenticate)],
               idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None):
        try:
            url, platform = validate_url(body.url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if body.account_id and not principal.is_admin and body.account_id not in principal.allowed_account_ids:
            raise HTTPException(403, "This client cannot use the requested account")
        if shutil.disk_usage(settings.files_dir).free < settings.min_free_disk_bytes + settings.max_file_bytes * 3:
            raise HTTPException(507, "Insufficient download storage")
        try:
            job, created = store.create_job(owner=principal.id, url=url, platform=platform,
                                           account_id=body.account_id, max_height=body.max_height,
                                           idempotency_key=idempotency_key)
        except QueueFull:
            raise HTTPException(429, "Active job limit reached", headers={"Retry-After": "10"}) from None
        except IdempotencyConflict:
            raise HTTPException(409, "Idempotency-Key already used with different parameters") from None
        except AccountUnavailable:
            raise HTTPException(400, "Account is unknown, disabled, requires new cookies, or belongs to another platform") from None
        response.status_code = 202 if created else 200
        response.headers["Location"] = f"/v1/jobs/{job['id']}"
        return job_public(job)

    @app.get("/v1/jobs", tags=["downloads"])
    def jobs(principal: Annotated[Principal, Depends(authenticate)], limit: int = Query(default=50, ge=1, le=100)):
        return {"items": [job_public(j) for j in store.list_jobs(None if principal.is_admin else principal.id, limit)]}

    @app.get("/v1/jobs/{job_id}", tags=["downloads"])
    def job(job_id: str, principal: Annotated[Principal, Depends(authenticate)]):
        return job_public(owned_job(job_id, principal))

    @app.post("/v1/jobs/{job_id}/cancel", tags=["downloads"])
    def cancel(job_id: str, principal: Annotated[Principal, Depends(authenticate)]):
        owned_job(job_id, principal)
        return job_public(store.request_cancel(job_id))

    @app.get("/v1/jobs/{job_id}/file", tags=["downloads"])
    def file(job_id: str, principal: Annotated[Principal, Depends(authenticate)]):
        job = owned_job(job_id, principal)
        if job_public(job)["status"] == "expired":
            raise HTTPException(410, "Download has expired")
        if job["status"] != "succeeded":
            raise HTTPException(409, "Download is not ready")
        path = (settings.files_dir / (job["artifact_path"] or "")).resolve()
        if not path.is_relative_to(settings.files_dir / job_id) or not path.is_file():
            raise HTTPException(410, "Download is no longer available")
        return FileResponse(path, filename=f"video-{job_id}.{job['ext']}", media_type="application/octet-stream",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/accounts", tags=["accounts"])
    def accounts(principal: Annotated[Principal, Depends(admin)]):
        return {"items": [account_public(a) for a in store.list_accounts()]}

    @app.post("/v1/accounts", status_code=201, tags=["accounts"])
    def account_create(body: AccountCreate, principal: Annotated[Principal, Depends(admin)]):
        try:
            encrypted = vault.encrypt(body.platform, body.cookies.get_secret_value())
            return account_public(store.save_account(body.id, body.platform, encrypted))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.put("/v1/accounts/{account_id}/cookies", tags=["accounts"])
    def account_rotate(account_id: str, body: CookieUpdate, principal: Annotated[Principal, Depends(admin)]):
        account = store.get_account(account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            encrypted = vault.encrypt(account["platform"], body.cookies.get_secret_value())
            return account_public(store.save_account(account_id, account["platform"], encrypted))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete("/v1/accounts/{account_id}", status_code=204, tags=["accounts"])
    def account_disable(account_id: str, principal: Annotated[Principal, Depends(admin)]):
        if not store.get_account(account_id):
            raise HTTPException(404, "Account not found")
        store.disable_account(account_id)
        return Response(status_code=204)

    return app
