"""Stateless FastAPI adapter for Vercel, backed by Upstash Redis and private Blob."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .engine import ConversionError, MediaEngine, youtube_id
from .main import JobInput, PreviewInput, Settings

log = logging.getLogger("y2audio.vercel")
JOB_TTL = 86400
WORKER_TTL = 330
MAX_CONCURRENT_CONVERSIONS = 3


class Redis:
    def __init__(self):
        self.url = os.environ["KV_REST_API_URL"].rstrip("/")
        self.token = os.environ["KV_REST_API_TOKEN"]
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), headers={"Authorization": f"Bearer {self.token}"})

    async def command(self, *args):
        response = await self.http.post(self.url, json=list(args))
        response.raise_for_status()
        result = response.json()
        if result.get("error"):
            raise RuntimeError("Upstash command failed")
        return result.get("result")

    async def get_json(self, key):
        value = await self.command("GET", key)
        return json.loads(value) if value else None

    async def set_json(self, key, value, ttl=JOB_TTL):
        await self.command("SET", key, json.dumps(value, separators=(",", ":")), "EX", ttl)

    async def count(self, key, limit, ttl, amount=1):
        value = int(await self.command("INCRBY", key, amount))
        if value == 1:
            await self.command("EXPIRE", key, ttl)
        if value > limit:
            raise ConversionError("The usage limit has been reached. Please try again later.", "usage_limit", 429)

    async def close(self):
        await self.http.aclose()


def create_vercel_app():
    settings = Settings()
    # Keep transfer exposure below the original 300 GB local-server allowance.
    settings.monthly_download_bytes = min(settings.monthly_download_bytes, 100_000_000_000)
    engine = MediaEngine(settings.max_duration, settings.max_job_bytes, timeout=210)

    @asynccontextmanager
    async def lifespan(app):
        app.state.redis = Redis()
        from vercel.blob import AsyncBlobClient
        app.state.blob = AsyncBlobClient()
        yield
        await app.state.redis.close()

    app = FastAPI(title="Y2Audio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(ConversionError)
    async def conversion_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status,
                            headers={"Retry-After": "60"} if exc.status in (429, 503) else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Please check the video link and format, then try again."}}, status_code=400)

    @app.middleware("http")
    async def request_guards(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and request.method in ("POST", "DELETE"):
            origin = request.headers.get("origin")
            own_origin = f"{request.url.scheme}://{request.url.netloc}"
            if origin and origin != own_origin:
                return JSONResponse({"error": {"message": "This origin is not allowed."}}, status_code=403)
            if request.method == "POST" and path not in ("/api/jobs/run",) and not request.headers.get("content-type", "").lower().startswith("application/json"):
                return JSONResponse({"error": {"message": "Send a JSON request."}}, status_code=415)
            if request.method == "POST" and request.headers.get("content-type", "").lower().startswith("application/json"):
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
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def redis(request):
        return request.app.state.redis

    def owner_key(request):
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown")
        secret = os.environ.get("KV_REST_API_TOKEN", "y2audio")
        return hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()

    def job_key(job_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", job_id):
            raise ConversionError("This download link wasn’t found.", "not_found", 404)
        return f"y2a:job:{job_id}"

    async def get_job(request, job_id):
        job = await redis(request).get_json(job_key(job_id))
        if not job:
            raise ConversionError("This download link expired or wasn’t found.", "not_found", 404)
        if job.get("expires_at") and job["expires_at"] <= time.time():
            job["status"] = "expired"
        return job

    def public_job(job):
        fields = ("id", "format", "quality", "status", "progress", "created_at", "expires_at", "size", "error")
        result = {key: job.get(key) for key in fields}
        if job["status"] == "queued":
            result["position"] = 1
        return result

    async def save_job(request, job):
        await redis(request).set_json(job_key(job["id"]), job)

    async def release_owner(redis_client, owner, job_id):
        if await redis_client.command("GET", f"y2a:owner-active:{owner}") == job_id:
            await redis_client.command("DEL", f"y2a:owner-active:{owner}")

    async def release(redis_client, owner, job_id, slot=None):
        # Locks have a short TTL as a fallback if a function is terminated mid-request.
        await release_owner(redis_client, owner, job_id)
        await redis_client.command("DEL", f"y2a:worker:{job_id}")
        if slot is not None:
            await redis_client.command("DEL", f"y2a:slot:{slot}")

    @app.get("/api/health")
    async def health():
        checks = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe", "node")}
        checks["blob"] = bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
        try:
            checks["redis"] = await app.state.redis.command("PING") == "PONG"
        except Exception:
            checks["redis"] = False
        return {"ready": all(checks.values()), "checks": checks,
                "max_duration": settings.max_duration, "retention": settings.retention}

    @app.post("/api/preview")
    async def preview(body: PreviewInput, request: Request):
        client = redis(request)
        if not await health_ready(request):
            raise ConversionError("The converter is temporarily unavailable. Please try again later.", "service_unavailable", 503)
        owner = owner_key(request)
        await client.count(f"y2a:rate:preview:{owner}:{int(time.time() // 3600)}", 20, 7200)
        lock_key = f"y2a:preview-lock:{owner}"
        if not await client.command("SET", lock_key, "1", "NX", "EX", 60):
            raise ConversionError("The converter is busy. Please try again in a moment.", "busy", 503)
        try:
            video = await engine.inspect(youtube_id(body.url))
        finally:
            await client.command("DEL", lock_key)
        preview_id = secrets.token_urlsafe(24)
        await client.set_json(f"y2a:preview:{preview_id}", {"video": video, "owner": owner}, 900)
        return {"preview_id": preview_id, "video": video}

    async def health_ready(request):
        if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe", "node")):
            return False
        try:
            return await redis(request).command("PING") == "PONG" and bool(os.environ.get("BLOB_READ_WRITE_TOKEN"))
        except Exception:
            return False

    @app.post("/api/jobs", status_code=202)
    async def create_job(body: JobInput, request: Request):
        client = redis(request)
        owner = owner_key(request)
        item = await client.get_json(f"y2a:preview:{body.preview_id}")
        if not item or item["owner"] != owner:
            raise ConversionError("This video preview expired. Find the video again to continue.", "preview_expired", 410)
        allowed = (128, 192) if body.format == "mp3" else item["video"]["video_qualities"]
        if body.quality not in allowed:
            raise ConversionError("Choose an available quality for this format.", "invalid_quality", 400)
        idem = f"y2a:idem:{owner}:{body.preview_id}:{body.format}:{body.quality}"
        existing_id = await client.command("GET", idem)
        if existing_id:
            existing = await client.get_json(job_key(existing_id))
            if existing:
                return public_job(existing)
        await client.count(f"y2a:rate:jobs:{owner}:{int(time.time() // 3600)}", settings.jobs_per_hour, 7200)
        day = int(time.time() // 86400)
        await client.count(f"y2a:jobs:{day}", settings.daily_jobs, 172800)
        job_id = secrets.token_urlsafe(24)
        if not await client.command("SET", f"y2a:owner-active:{owner}", job_id, "NX", "EX", WORKER_TTL):
            raise ConversionError("Please wait for your current conversion to finish.", "concurrent_limit", 429)
        job = {"id": job_id, "owner": owner, "preview_id": body.preview_id, "video": item["video"],
               "format": body.format, "quality": body.quality, "status": "queued", "progress": None,
               "created_at": time.time(), "expires_at": None, "size": None, "error": None, "attempt": 0}
        await save_job(request, job)
        await client.command("SET", idem, job_id, "EX", 900)
        return public_job(job)

    @app.post("/api/jobs/run")
    async def run_next_job(request: Request):
        """Browser-poked runner: long work stays inside a bounded invocation."""
        client = redis(request)
        owner = owner_key(request)
        # A job id is supplied in the request body only by the internal browser client.
        try:
            payload = await request.json()
            job_id = str(payload.get("job_id", ""))
        except Exception:
            job_id = ""
        job = await get_job(request, job_id)
        if job["owner"] != owner:
            raise ConversionError("This conversion wasn’t found.", "not_found", 404)
        if job["status"] not in ("queued", "downloading", "processing"):
            return public_job(job)
        worker_key = f"y2a:worker:{job_id}"
        if not await client.command("SET", worker_key, secrets.token_urlsafe(10), "NX", "EX", WORKER_TTL):
            return public_job(job)
        slot = None
        for index in range(MAX_CONCURRENT_CONVERSIONS):
            if await client.command("SET", f"y2a:slot:{index}", job_id, "NX", "EX", WORKER_TTL):
                slot = index
                break
        if slot is None:
            await client.command("DEL", worker_key)
            job["status"] = "queued"
            return public_job(job)
        cancel_event = asyncio.Event()
        latest = {"status": "downloading", "progress": None}
        async def watch_cancel():
            while not cancel_event.is_set():
                if await client.command("GET", f"y2a:cancel:{job_id}"):
                    cancel_event.set()
                    break
                await asyncio.sleep(1)
        async def publish_progress():
            while not cancel_event.is_set():
                await asyncio.sleep(1.5)
                current = await get_job(request, job_id)
                if current["status"] in ("cancelled", "failed"):
                    cancel_event.set()
                    break
                current.update(latest)
                current["updated_at"] = time.time()
                await save_job(request, current)
        watcher = asyncio.create_task(watch_cancel())
        publisher = asyncio.create_task(publish_progress())
        tmp = None
        try:
            job["status"] = "downloading"
            job["attempt"] = int(job.get("attempt", 0)) + 1
            job["updated_at"] = time.time()
            await save_job(request, job)
            tmp = tempfile.TemporaryDirectory(prefix="y2audio-", dir="/tmp")
            directory = Path(tmp.name)
            def progress(status, percentage):
                latest["status"] = status
                latest["progress"] = percentage
            target = await engine.convert(job["video"], job["format"], job["quality"], directory, cancel_event, progress)
            if cancel_event.is_set():
                raise ConversionError("Conversion cancelled.", "cancelled", 409)
            async def chunks():
                with target.open("rb") as source:
                    while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
                        yield chunk
            pathname = f"y2audio/{job_id}/media.{job['format']}"
            await request.app.state.blob.put(pathname, chunks(), access="private",
                content_type="audio/mpeg" if job["format"] == "mp3" else "video/mp4", multipart=True)
            job.update(status="completed", progress=100, size=target.stat().st_size,
                       blob_path=pathname, expires_at=time.time() + settings.retention, updated_at=time.time(), error=None)
            await save_job(request, job)
            await client.command("ZADD", "y2a:expiry", int(job["expires_at"]), job_id)
            await release_owner(client, owner, job_id)
            return public_job(job)
        except ConversionError as exc:
            job.update(status="cancelled" if exc.code == "cancelled" else "failed", error=exc.message,
                       expires_at=time.time() + settings.retention, updated_at=time.time())
            await save_job(request, job)
            await release_owner(client, owner, job_id)
            return public_job(job)
        except Exception:
            log.exception("Vercel conversion failed: %s", job_id)
            job.update(status="failed", error="We couldn’t complete this conversion. Please try again.",
                       expires_at=time.time() + settings.retention, updated_at=time.time())
            await save_job(request, job)
            await release_owner(client, owner, job_id)
            return public_job(job)
        finally:
            cancel_event.set()
            watcher.cancel(); publisher.cancel()
            await asyncio.gather(watcher, publisher, return_exceptions=True)
            if tmp:
                tmp.cleanup()
            await release(client, owner, job_id, slot)

    @app.get("/api/jobs/{job_id}")
    async def status(job_id: str, request: Request):
        return public_job(await get_job(request, job_id))

    @app.delete("/api/jobs/{job_id}")
    async def cancel(job_id: str, request: Request):
        client = redis(request)
        job = await get_job(request, job_id)
        if job["owner"] != owner_key(request):
            raise ConversionError("This conversion wasn’t found.", "not_found", 404)
        if job["status"] in ("queued", "downloading", "processing"):
            await client.command("SET", f"y2a:cancel:{job_id}", "1", "EX", 600)
            job.update(status="cancelled", error="Conversion cancelled.", expires_at=time.time() + settings.retention)
            await save_job(request, job)
            await release_owner(client, job["owner"], job_id)
        return public_job(job)

    @app.get("/api/jobs/{job_id}/download")
    async def download(job_id: str, request: Request):
        client = redis(request)
        job = await get_job(request, job_id)
        if job["status"] == "expired":
            raise ConversionError("This download has expired. Convert the video again.", "expired", 410)
        if job["status"] != "completed" or not job.get("blob_path"):
            raise ConversionError("This file is not ready to download.", "not_ready", 409)
        owner = owner_key(request)
        await client.count(f"y2a:rate:download:{owner}:{int(time.time() // 3600)}", 30, 7200)
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        await client.count(f"y2a:egress:{month}", settings.monthly_download_bytes, 40 * 86400, int(job.get("size") or 0))
        result = await request.app.state.blob.get(job["blob_path"], access="private")
        if not result or result.status_code != 200 or not result.stream:
            raise ConversionError("This file is no longer available. Convert the video again.", "expired", 410)
        title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", job["video"].get("title", "YouTube video")).strip(" .")[:110] or "YouTube video"
        filename = f"{title}.{job['format']}"
        media_type = "audio/mpeg" if job["format"] == "mp3" else "video/mp4"
        headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
        if job.get("size"):
            headers["Content-Length"] = str(job["size"])
        return StreamingResponse(result.stream, media_type=media_type, headers=headers)

    @app.get("/api/cron/cleanup")
    async def cleanup(request: Request):
        if not os.environ.get("CRON_SECRET") or request.headers.get("authorization") != f"Bearer {os.environ['CRON_SECRET']}":
            raise ConversionError("This request wasn’t found.", "not_found", 404)
        client = redis(request)
        expired = await client.command("ZRANGEBYSCORE", "y2a:expiry", "-inf", int(time.time()), "LIMIT", 0, 100)
        removed = 0
        for job_id in expired or []:
            job = await client.get_json(job_key(job_id))
            if job and job.get("blob_path"):
                await request.app.state.blob.delete(job["blob_path"])
            await client.command("ZREM", "y2a:expiry", job_id)
            await client.command("DEL", job_key(job_id))
            removed += 1
        return {"removed": removed}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
    async def unknown_api(path: str):
        raise ConversionError("This request wasn’t found.", "not_found", 404)

    app.frontend("/", directory=str(Path(__file__).resolve().parent.parent / "dist"), fallback="index.html")
    return app
