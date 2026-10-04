"""Bounded subprocess-based YouTube extraction and conversion."""
import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class ConversionError(Exception):
    def __init__(self, message: str, code: str = "conversion_failed", status: int = 422):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def youtube_id(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        host = (parsed.hostname or "").lower()
        video_id = ""
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            else:
                match = re.fullmatch(r"/(?:shorts|embed)/([\w-]{11})/?", parsed.path, flags=re.ASCII)
                video_id = match.group(1) if match else ""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
            raise ValueError()
        return video_id
    except (ValueError, TypeError):
        raise ConversionError("Enter a link to a single YouTube video or Short.", "invalid_url", 400)


def folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except FileNotFoundError:
            pass
    return total


def public_error(stderr: str) -> ConversionError:
    message = stderr.lower()
    if any(s in message for s in ("confirm you’re not a bot", "confirm you're not a bot", "sign in", "cookies", "http error 429")):
        return ConversionError("YouTube is restricting access to this video right now. Please try again later.", "source_restricted", 503)
    if any(s in message for s in ("private video", "video unavailable", "not available", "removed", "members-only", "age-restricted")):
        return ConversionError("This video is unavailable or requires signing in. Try a public video.", "video_unavailable")
    if "requested format" in message:
        return ConversionError("This video doesn’t offer that format. Try another quality or choose MP3.", "format_unavailable")
    if "max-filesize" in message or "larger than max" in message:
        return ConversionError("This video exceeds the file-size limit. Try a shorter video.", "file_too_large")
    return ConversionError("We couldn’t retrieve this video. Please try again later or use another link.", "source_error", 502)


class MediaEngine:
    def __init__(self, max_duration=600, max_job_bytes=250_000_000, timeout=240):
        self.max_duration = max_duration
        self.max_job_bytes = max_job_bytes
        self.timeout = timeout

    def base_args(self):
        return [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-warnings",
                "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
                "--js-runtimes", "node", "--no-cache-dir"]

    async def run(self, args, *, timeout, workdir=None, cancelled=None, progress=None):
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        output, errors = bytearray(), bytearray()

        async def collect(stream, target, cap, report=False):
            line_buffer = ""
            while chunk := await stream.read(16384):
                if len(target) + len(chunk) > cap:
                    raise ConversionError("The video response was too large to process.", "response_too_large")
                target.extend(chunk)
                if report and progress:
                    line_buffer += chunk.decode("utf-8", errors="replace")
                    lines = line_buffer.split("\n")
                    line_buffer = lines.pop()[-2048:]
                    for line in lines:
                        match = re.search(r"Y2PROGRESS:\s*([\d.]+)%", line)
                        if match:
                            progress("downloading", min(100, float(match.group(1))))
                        if "[ExtractAudio]" in line or "[Merger]" in line:
                            progress("processing", None)

        readers = [asyncio.create_task(collect(process.stdout, output, 4_000_000, True)),
                   asyncio.create_task(collect(process.stderr, errors, 128_000))]
        started = time.monotonic()
        try:
            while process.returncode is None:
                if cancelled and cancelled.is_set():
                    raise ConversionError("Conversion cancelled.", "cancelled", 409)
                if time.monotonic() - started > timeout:
                    raise ConversionError("The conversion took too long. Please try a shorter video.", "timeout", 504)
                if workdir and folder_size(workdir) > self.max_job_bytes:
                    raise ConversionError("This video exceeds the file-size limit. Try a shorter video.", "file_too_large")
                for task in readers:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()
                await asyncio.sleep(0.15)
            await asyncio.gather(*readers)
            if process.returncode:
                raise public_error(errors.decode("utf-8", errors="replace"))
            return output.decode("utf-8", errors="replace")
        finally:
            # Killing the process group also stops ffmpeg children on timeout/cancel.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 3)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def inspect(self, video_id):
        raw = await self.run(self.base_args() + ["--dump-single-json", "--skip-download", "--", f"https://www.youtube.com/watch?v={video_id}"], timeout=40)
        try:
            info = json.loads(raw)
        except (ValueError, TypeError):
            raise ConversionError("We couldn’t read this video’s details. Please try again.", "invalid_metadata", 502)
        duration = info.get("duration")
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise ConversionError("Live streams aren’t supported. Choose an uploaded video.", "live_video")
        if not isinstance(duration, (int, float)) or not 0 < duration <= self.max_duration:
            raise ConversionError(f"Choose a video up to {self.max_duration // 60} minutes long.", "duration_limit")
        formats = info.get("formats") or []
        if not any(f.get("acodec") not in (None, "none") for f in formats):
            raise ConversionError("No downloadable audio was found for this video.", "format_unavailable")
        heights = [f.get("height", 0) for f in formats if f.get("ext") == "mp4" and str(f.get("vcodec", "")).startswith("avc1") and f.get("height")]
        qualities = [level for level in (360, 720) if any(height <= level for height in heights)]
        return {"id": video_id, "title": str(info.get("title") or "YouTube video")[:200],
                "author": str(info.get("uploader") or "YouTube")[:120], "duration": duration,
                "video_qualities": qualities}

    async def convert(self, video, output_format, quality, directory, cancelled, progress):
        args = self.base_args() + ["--newline", "--progress", "--progress-template", "download:Y2PROGRESS:%(progress._percent_str)s",
                                  "--max-filesize", str(self.max_job_bytes // 2),
                                  "--match-filters", f"!is_live & duration <= {self.max_duration}",
                                  "--concurrent-fragments", "1", "--no-keep-video", "--restrict-filenames",
                                  "--paths", str(directory), "--output", "media.%(ext)s",
                                  "--postprocessor-args", "ffmpeg:-threads 1"]
        if output_format == "mp3":
            args += ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", f"{quality}K"]
        else:
            selector = f"bestvideo[ext=mp4][vcodec^=avc1][height<={quality}]+bestaudio[ext=m4a]/best[ext=mp4][vcodec^=avc1][height<={quality}]"
            args += ["-f", selector, "--merge-output-format", "mp4", "--remux-video", "mp4"]
        args += ["--", f"https://www.youtube.com/watch?v={video['id']}"]
        await self.run(args, timeout=self.timeout, workdir=directory, cancelled=cancelled, progress=progress)
        target = directory / f"media.{output_format}"
        if not target.is_file() or target.stat().st_size == 0:
            raise ConversionError("The video couldn’t be converted to that format. Try a different quality.", "missing_output")
        if folder_size(directory) > self.max_job_bytes:
            raise ConversionError("This video exceeds the file-size limit. Try a shorter video.", "file_too_large")
        progress("processing", None)
        details = json.loads(await self.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,height", "-of", "json", str(target)], timeout=15, cancelled=cancelled))
        duration = float(details.get("format", {}).get("duration", 0))
        if not 0 < duration <= self.max_duration + 1:
            raise ConversionError("The converted file didn’t pass the duration check.", "invalid_output")
        streams = details.get("streams", [])
        if not any(s.get("codec_type") == "audio" for s in streams):
            raise ConversionError("The converted file has no audio.", "invalid_output")
        if output_format == "mp4" and not any(s.get("codec_type") == "video" and 0 < s.get("height", 0) <= quality for s in streams):
            raise ConversionError("The converted file didn’t match the chosen video quality.", "invalid_output")
        for item in directory.iterdir():
            if item != target and item.is_file():
                item.unlink(missing_ok=True)
        return target
