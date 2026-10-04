import asyncio
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from server.engine import ConversionError, MediaEngine, youtube_id
from server.main import Settings, create_app


class FixtureEngine(MediaEngine):
    """Only the YouTube network boundary is replaced. ffmpeg/ffprobe are real."""
    async def inspect(self, video_id):
        return {"id": video_id, "title": 'Test <video> / audio', "author": "Test fixture", "duration": 1, "video_qualities": [360, 720]}

    async def run(self, args, **kwargs):
        if "yt_dlp" in args:
            directory = Path(args[args.index("--paths") + 1])
            mp3 = "--audio-format" in args
            output = directory / ("media.mp3" if mp3 else "media.mp4")
            ffmpeg = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
            if not mp3:
                ffmpeg += ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=15:duration=1", "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-shortest"]
            else:
                ffmpeg += ["-c:a", "libmp3lame", "-b:a", "192k"]
            ffmpeg += [str(output)]
            return await super().run(ffmpeg, **kwargs)
        return await super().run(args, **kwargs)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, workers=1), FixtureEngine())) as client:
        yield client


def preview(client):
    response = client.post("/api/preview", json={"url": "https://youtu.be/BaW_jenozKc"})
    assert response.status_code == 200, response.text
    return response.json()["preview_id"]


def wait_for_job(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "downloading", "processing"):
            return job
        time.sleep(0.03)
    pytest.fail("Job did not complete")


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.example/watch?v=BaW_jenozKc",
    "http://127.0.0.1/private", "file:///etc/passwd", "https://youtube.com:8443/watch?v=BaW_jenozKc",
    "https://evil@youtube.com/watch?v=BaW_jenozKc", "https://youtube.com/playlist?list=abc",
    "https://youtu.be/BaW_jenozKc/anything", "--exec=anything", "https://youtu.be/not-an-id",
])
def test_rejects_non_video_sources(url):
    with pytest.raises(ConversionError):
        youtube_id(url)


@pytest.mark.parametrize("url", ["youtu.be/BaW_jenozKc?t=4", "https://www.youtube.com/watch?v=BaW_jenozKc&list=anything", "https://youtube.com/shorts/BaW_jenozKc", "https://m.youtube.com/watch?v=BaW_jenozKc"])
def test_canonical_youtube_sources(url):
    assert youtube_id(url) == "BaW_jenozKc"


@pytest.mark.parametrize("format,quality,mimetype", [("mp3", 192, "audio/mpeg"), ("mp4", 720, "video/mp4")])
def test_conversion_download_and_expiry(client, format, quality, mimetype):
    payload = {"preview_id": preview(client), "format": format, "quality": quality}
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    # Retries cannot accidentally create duplicate work.
    assert client.post("/api/jobs", json=payload).json()["id"] == job_id
    job = wait_for_job(client, job_id)
    assert job["status"] == "completed", job
    download = client.get(f"/api/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == mimetype
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["cache-control"] == "no-store"
    assert len(download.content) == job["size"] > 1000
    assert (download.content.startswith(b"ID3") if format == "mp3" else b"ftyp" in download.content[:32])
    service = client.app.state.service
    service.update(job_id, expires_at=time.time() - 1)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "expired"
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 410
    service.clean()
    assert not (service.jobs_dir / job_id).exists()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_invalid_quality_and_fake_preview(client):
    token = preview(client)
    assert client.post("/api/jobs", json={"preview_id": token, "format": "mp3", "quality": 999}).status_code == 400
    assert client.post("/api/jobs", json={"preview_id": "A" * 32, "format": "mp3", "quality": 192}).status_code == 410


def test_request_boundaries(client):
    assert client.post("/api/preview", json={"url": "https://127.0.0.1"}).status_code == 400
    assert client.post("/api/preview", json={"url": "https://youtu.be/BaW_jenozKc"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/preview", content='{"url":"' + 'x' * 5000 + '"}', headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/api/preview", content="something").status_code == 415
    assert client.get("/api/jobs/guessable").status_code == 404


def test_bandwidth_cap(client):
    client.app.state.service.settings.monthly_download_bytes = 1
    job = client.post("/api/jobs", json={"preview_id": preview(client), "format": "mp3", "quality": 128}).json()
    assert wait_for_job(client, job["id"])["status"] == "completed"
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 429


def test_daily_job_cap(client):
    client.app.state.service.settings.daily_jobs = 0
    response = client.post("/api/jobs", json={"preview_id": preview(client), "format": "mp3", "quality": 192})
    assert response.status_code == 429
    assert client.app.state.service.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


class SlowEngine(FixtureEngine):
    async def convert(self, video, output_format, quality, directory, cancelled, progress):
        await cancelled.wait()
        raise ConversionError("Conversion cancelled.", "cancelled", 409)


def test_cancellation(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, workers=1), SlowEngine())) as client:
        job_id = client.post("/api/jobs", json={"preview_id": preview(client), "format": "mp3", "quality": 192}).json()["id"]
        assert client.delete(f"/api/jobs/{job_id}").json()["status"] == "cancelled"
        assert wait_for_job(client, job_id)["status"] == "cancelled"
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 409


def test_restart_preserves_completed_files(tmp_path):
    settings = Settings(data_dir=tmp_path, workers=1)
    with TestClient(create_app(settings, FixtureEngine())) as client:
        job_id = client.post("/api/jobs", json={"preview_id": preview(client), "format": "mp3", "quality": 192}).json()["id"]
        assert wait_for_job(client, job_id)["status"] == "completed"
    with TestClient(create_app(settings, FixtureEngine())) as client:
        assert client.get(f"/api/jobs/{job_id}/download").status_code == 200


def test_process_timeout():
    import sys
    async def run():
        with pytest.raises(ConversionError, match="took too long"):
            await MediaEngine().run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
    asyncio.run(run())


@pytest.mark.parametrize("duration,is_live", [(601, False), (30, True), (None, False)])
def test_rejects_long_live_and_unknown_duration(duration, is_live):
    class MetadataEngine(MediaEngine):
        async def run(self, *args, **kwargs):
            return json.dumps({"duration": duration, "is_live": is_live})
    async def run():
        with pytest.raises(ConversionError):
            await MetadataEngine().inspect("BaW_jenozKc")
    asyncio.run(run())
