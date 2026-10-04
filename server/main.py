"""Single-process, bounded conversion queue and same-origin web application."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import ConversionError, MediaEngine, folder_size, youtube_id

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("y2audio")
ACTIVE = ("queued", "downloading", "processing")


@dataclass
class Settings:
    data_dir: Path = Path(os.environ.get("DATA_DIR", str(ROOT / ".runtime")))
    workers: int = int(os.environ.get("CONVERSION_WORKERS", "2"))
    queue_limit: int = int(os.environ.get("QUEUE_LIMIT", "12"))
    max_duration: int = int(os.environ.get("MAX_DURATION_SECONDS", "600"))
    retention: int = int(os.environ.get("RETENTION_SECONDS", "1800"))
    max_job_bytes: int = int(os.environ.get("MAX_JOB_MB", "250")) * 1_000_000
    max_storage_bytes: int = int(os.environ.get("MAX_STORAGE_MB", "2048")) * 1_000_000
    monthly_download_bytes: int = int(os.environ.get("MONTHLY_DOWNLOAD_GB", "300")) * 1_000_000_000
    daily_jobs: int = int(os.environ.get("DAILY_JOB_LIMIT", "100"))
    jobs_per_hour: int = int(os.environ.get("JOBS_PER_IP_PER_HOUR", "6"))
    allowed_origins: tuple = tuple(s.strip().rstrip("/") for s in os.environ.get("ALLOWED_ORIGINS", "").split(",") if s.strip())


class PreviewInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class JobInput(BaseModel):
    preview_id: str = Field(min_length=20, max_length=80, pattern=r"^[\w-]+$")
    format: Literal["mp3", "mp4"]
    quality: int


class Service:
    def __init__(self, settings, engine):
        self.settings, self.engine = settings, engine
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs_dir = settings.data_dir / "files"
        self.jobs_dir.mkdir(exist_ok=True, mode=0o700)
        self.lockfile = (settings.data_dir / "service.lock").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            raise RuntimeError("Run one Uvicorn process. CONVERSION_WORKERS controls conversion concurrency.")
        self.db = sqlite3.connect(settings.data_dir / "jobs.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, preview_id TEXT NOT NULL,
            video TEXT NOT NULL, format TEXT NOT NULL, quality INTEGER NOT NULL,
            status TEXT NOT NULL, progress REAL, created_at REAL NOT NULL,
            expires_at REAL, size INTEGER, error TEXT
          );
          CREATE INDEX IF NOT EXISTS jobs_expiry ON jobs(expires_at);
          CREATE INDEX IF NOT EXISTS jobs_owner_status ON jobs(owner, status);
          CREATE TABLE IF NOT EXISTS counters (
            key TEXT PRIMARY KEY, value INTEGER NOT NULL, expires_at REAL NOT NULL
          );
        """)
        self.db.execute("UPDATE jobs SET status='failed', error=?, expires_at=? WHERE status IN ('queued','downloading','processing')",
                        ("The service restarted during this conversion. Please try again.", time.time() + settings.retention))
        self.db.commit()
        self.db.execute("PRAGMA optimize")
        self.queue = asyncio.Queue(maxsize=settings.queue_limit)
        self.previews = {}
        self.cancellations = {}
        self.tasks = []
        self.inspect_count = 0
        salt_file = settings.data_dir / "rate-key"
        if not salt_file.exists():
            salt_file.write_bytes(secrets.token_bytes(32))
            salt_file.chmod(0o600)
        self.salt = salt_file.read_bytes()
        self.clean()
        # Recover files left by an interrupted process; retain unexpired successes.
        keep = {row["id"] for row in self.db.execute("SELECT id FROM jobs WHERE status='completed'")}
        for directory in self.jobs_dir.iterdir():
            if directory.is_dir() and directory.name not in keep:
                shutil.rmtree(directory, ignore_errors=True)

    def ready(self):
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("node") and importlib.util.find_spec("yt_dlp"))

    def identity(self, request):
        # Uvicorn only accepts forwarding headers from explicitly trusted proxies.
        ip = request.client.host if request.client else "unknown"
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return hmac.new(self.salt, f"{day}:{ip}".encode(), hashlib.sha256).hexdigest()

    def consume(self, key, amount, maximum, expires):
        row = self.db.execute("SELECT value FROM counters WHERE key=?", (key,)).fetchone()
        if (row[0] if row else 0) + amount > maximum:
            raise ConversionError("The usage limit has been reached. Please try again later.", "usage_limit", 429)
        self.db.execute("INSERT INTO counters(key,value,expires_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (key, amount, expires))
        self.db.commit()

    def rate_limit(self, owner, action, limit):
        hour = int(time.time() // 3600)
        self.consume(f"rate:{owner}:{action}:{hour}", 1, limit, (hour + 2) * 3600)

    def job(self, job_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", job_id):
            raise ConversionError("This download link wasn’t found.", "not_found", 404)
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ConversionError("This download link expired or wasn’t found.", "not_found", 404)
        result = dict(row)
        if result["expires_at"] and result["expires_at"] <= time.time():
            result["status"] = "expired"
        return result

    def public_job(self, job_id):
        job = self.job(job_id)
        result = {key: job[key] for key in ("id", "format", "quality", "status", "progress", "created_at", "expires_at", "size", "error")}
        if job["status"] == "queued":
            result["position"] = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND created_at<=?", (job["created_at"],)).fetchone()[0]
        return result

    def update(self, job_id, **fields):
        allowed = {"status", "progress", "expires_at", "size", "error"}
        assert set(fields) <= allowed
        assignments = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))
        self.db.commit()

    def clean(self):
        now = time.time()
        for row in self.db.execute("SELECT id FROM jobs WHERE expires_at<=?", (now,)).fetchall():
            shutil.rmtree(self.jobs_dir / row["id"], ignore_errors=True)
        self.db.execute("DELETE FROM jobs WHERE expires_at<=?", (now,))
        self.db.execute("DELETE FROM counters WHERE expires_at<=?", (now,))
        self.db.commit()
        self.previews = {key: item for key, item in self.previews.items() if item["expires_at"] > now}

    async def maintenance(self):
        while True:
            await asyncio.sleep(30)
            self.clean()

    async def worker(self):
        while True:
            job_id = await self.queue.get()
            directory = self.jobs_dir / job_id
            try:
                job = self.job(job_id)
                if job["status"] != "queued":
                    continue
                event = self.cancellations[job_id]
                directory.mkdir(mode=0o700)
                self.update(job_id, status="downloading")

                def progress(status, percentage):
                    if not event.is_set():
                        self.update(job_id, status=status, progress=percentage)

                target = await self.engine.convert(json.loads(job["video"]), job["format"], job["quality"], directory, event, progress)
                if event.is_set():
                    raise ConversionError("Conversion cancelled.", "cancelled", 409)
                self.update(job_id, status="completed", progress=100, size=target.stat().st_size,
                            expires_at=time.time() + self.settings.retention)
            except ConversionError as exc:
                self.update(job_id, status="cancelled" if exc.code == "cancelled" else "failed", error=exc.message,
                            expires_at=time.time() + self.settings.retention)
                shutil.rmtree(directory, ignore_errors=True)
            except asyncio.CancelledError:
                self.update(job_id, status="failed", error="The service restarted. Please try again.",
                            expires_at=time.time() + self.settings.retention)
                shutil.rmtree(directory, ignore_errors=True)
                raise
            except Exception:
                log.exception("Conversion failed: %s", job_id)
                self.update(job_id, status="failed", error="We couldn’t complete this conversion. Please try again.",
                            expires_at=time.time() + self.settings.retention)
                shutil.rmtree(directory, ignore_errors=True)
            finally:
                self.cancellations.pop(job_id, None)
                self.queue.task_done()


def create_app(settings=None, engine=None):
    settings = settings or Settings()
    engine = engine or MediaEngine(settings.max_duration, settings.max_job_bytes)
    if not 1 <= settings.workers <= 4 or not 1 <= settings.queue_limit <= 100:
        raise ValueError("Set 1–4 conversion workers and a queue limit of 1–100.")

    @asynccontextmanager
    async def lifespan(app):
        service = Service(settings, engine)
        app.state.service = service
        service.tasks = [asyncio.create_task(service.worker()) for _ in range(settings.workers)]
        service.tasks.append(asyncio.create_task(service.maintenance()))
        try:
            yield
        finally:
            for task in service.tasks:
                task.cancel()
            await asyncio.gather(*service.tasks, return_exceptions=True)
            service.db.close()
            fcntl.flock(service.lockfile, fcntl.LOCK_UN)
            service.lockfile.close()

    app = FastAPI(title="Y2Audio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    if settings.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                           allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "Accept"])

    @app.exception_handler(ConversionError)
    async def conversion_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status,
                            headers={"Retry-After": "60"} if exc.status in (429, 503) else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Please check the video link and format, then try again."}}, status_code=400)

    @app.middleware("http")
    async def request_guards(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in ("POST", "DELETE"):
            origin = request.headers.get("origin")
            own_origin = f"{request.url.scheme}://{request.url.netloc}"
            if origin and origin not in (own_origin, *settings.allowed_origins):
                return JSONResponse({"error": {"message": "This origin is not allowed."}}, status_code=403)
            if request.method == "POST":
                if not request.headers.get("content-type", "").lower().startswith("application/json"):
                    return JSONResponse({"error": {"message": "Send a JSON request."}}, status_code=415)
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 4096:
                        return JSONResponse({"error": {"message": "The request is too large."}}, status_code=413)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    async def health():
        service = app.state.service
        return {"ready": service.ready(), "max_duration": settings.max_duration, "retention": settings.retention}

    @app.post("/api/preview")
    async def preview(body: PreviewInput, request: Request):
        service = app.state.service
        video_id = youtube_id(body.url)
        if not service.ready():
            raise ConversionError("The converter is temporarily unavailable. Please try again later.", "service_unavailable", 503)
        owner = service.identity(request)
        service.rate_limit(owner, "preview", 20)
        if service.inspect_count >= 2:
            raise ConversionError("The converter is busy. Please try again in a moment.", "busy", 503)
        service.clean()
        if len(service.previews) >= 1000:
            raise ConversionError("The converter is busy. Please try again later.", "busy", 503)
        service.inspect_count += 1
        try:
            video = await service.engine.inspect(video_id)
        finally:
            service.inspect_count -= 1
        preview_id = secrets.token_urlsafe(24)
        service.previews[preview_id] = {"video": video, "owner": owner, "expires_at": time.time() + 900}
        return {"preview_id": preview_id, "video": video}

    @app.post("/api/jobs", status_code=202)
    async def create_job(body: JobInput, request: Request):
        service = app.state.service
        owner = service.identity(request)
        item = service.previews.get(body.preview_id)
        if not item or item["expires_at"] <= time.time() or item["owner"] != owner:
            raise ConversionError("This video preview expired. Find the video again to continue.", "preview_expired", 410)
        allowed = (128, 192) if body.format == "mp3" else item["video"]["video_qualities"]
        if body.quality not in allowed:
            raise ConversionError("Choose an available quality for this format.", "invalid_quality", 400)
        # Retrying a request with the same preview/options returns the same job.
        existing = service.db.execute("SELECT id FROM jobs WHERE owner=? AND preview_id=? AND format=? AND quality=? AND status IN ('queued','downloading','processing','completed') ORDER BY created_at DESC LIMIT 1",
                                      (owner, body.preview_id, body.format, body.quality)).fetchone()
        if existing and service.job(existing[0])["status"] != "expired":
            return service.public_job(existing[0])
        if service.queue.full():
            raise ConversionError("The queue is full. Please try again in a few minutes.", "queue_full", 503)
        active = service.db.execute("SELECT COUNT(*) FROM jobs WHERE owner=? AND status IN ('queued','downloading','processing')", (owner,)).fetchone()[0]
        if active >= 2:
            raise ConversionError("Please wait for your current conversions to finish.", "concurrent_limit", 429)
        reserved = service.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0] * settings.max_job_bytes
        if folder_size(service.jobs_dir) + reserved + settings.max_job_bytes > settings.max_storage_bytes:
            raise ConversionError("There isn’t a free conversion slot right now. Please try again later.", "storage_limit", 503)
        if shutil.disk_usage(settings.data_dir).free < reserved + settings.max_job_bytes + 100_000_000:
            raise ConversionError("The converter is temporarily at capacity. Please try again later.", "storage_limit", 503)
        service.rate_limit(owner, "jobs", settings.jobs_per_hour)
        day = int(time.time() // 86400)
        service.consume(f"jobs:{day}", 1, settings.daily_jobs, (day + 2) * 86400)
        job_id = secrets.token_urlsafe(24)
        service.db.execute("INSERT INTO jobs(id,owner,preview_id,video,format,quality,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (job_id, owner, body.preview_id, json.dumps(item["video"]), body.format, body.quality, "queued", time.time()))
        service.db.commit()
        service.cancellations[job_id] = asyncio.Event()
        service.queue.put_nowait(job_id)
        return service.public_job(job_id)

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        return app.state.service.public_job(job_id)

    @app.delete("/api/jobs/{job_id}")
    async def cancel_job(job_id: str):
        service = app.state.service
        job = service.job(job_id)
        if job["status"] in ACTIVE:
            if event := service.cancellations.get(job_id):
                event.set()
            service.update(job_id, status="cancelled", expires_at=time.time() + settings.retention)
        return service.public_job(job_id)

    @app.get("/api/jobs/{job_id}/download")
    async def download(job_id: str, request: Request):
        service = app.state.service
        job = service.job(job_id)
        if job["status"] == "expired":
            raise ConversionError("This download has expired. Convert the video again.", "expired", 410)
        if job["status"] != "completed":
            raise ConversionError("This file is not ready to download.", "not_ready", 409)
        path = service.jobs_dir / job_id / f"media.{job['format']}"
        if not path.is_file():
            raise ConversionError("This file is no longer available. Convert the video again.", "expired", 410)
        owner = service.identity(request)
        service.rate_limit(owner, "downloads", 30)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        # Reserve the complete file size for each request, including Range requests.
        # This intentionally overcounts partial/retried transfers to cap delivery cost.
        service.consume(f"egress:{month}", path.stat().st_size, settings.monthly_download_bytes, time.time() + 40 * 86400)
        title = json.loads(job["video"])["title"]
        filename = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", title).strip(" .")[:110] or "YouTube video"
        return FileResponse(path, media_type="audio/mpeg" if job["format"] == "mp3" else "video/mp4", filename=f"{filename}.{job['format']}")

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
    async def unknown_api(path: str):
        raise ConversionError("This request wasn’t found.", "not_found", 404)

    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="website")
    return app


app = create_app()
