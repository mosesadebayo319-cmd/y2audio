# Y2Audio

A mobile-friendly YouTube-to-MP3/MP4 beta, with a real Python media-processing service and a dependency-free frontend.

## Current status

- Responsive converter, clipboard paste, video preview, format/quality selection, queue progress, cancellation, downloads, refresh recovery, and FAQs are implemented.
- The backend invokes **yt-dlp, Node.js, FFmpeg, and FFprobe**. It does not substitute sample media in production.
- **25 backend tests passed**, including real FFmpeg-generated MP3/MP4 files, expiry, cancellation, restart recovery, request validation, and quota enforcement.
- A browser test completed both format flows and downloaded real generated files. It also checked invalid links, reload recovery, FAQs, 390/320-pixel layouts, and JavaScript errors. The test replaces only the YouTube network boundary; it is excluded from deployment.
- The production Docker image built successfully and passed startup checks with its read-only filesystem, non-root user, and resource limits. The container served the website and reported Node/FFmpeg/FFprobe available.
- Live YouTube extraction **could not be verified**: this workspace's outbound proxy returned `403 Forbidden` for YouTube requests. This is not evidence that a chosen production host can retrieve YouTube videos. Validate retrieval from that host before opening the public service.
- The **Sites deployment is an interface preview**. Sites serves the static frontend and cannot run the Python/FFmpeg service. Until an HTTPS backend is configured, the preview clearly says conversion is not connected. It never simulates a completed download.
- No domain, paid server, or ad-network account has been purchased or activated. `y2audio.com` was available at $11.25 registration and $11.25 renewal in the domain lookup on October 4, 2026; it has not been reserved.

## Run locally

Requirements: Linux, Python 3.12+, Node.js 22+, FFmpeg, FFprobe.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Open `http://127.0.0.1:8000`. This serves the UI and API from the same origin. `dist/config.js` should retain an empty `apiBase` for this setup. Runtime files are stored in ignored `.runtime/` by default.

Run **one Uvicorn process**. The service owns one SQLite database and one bounded queue; a file lock rejects accidental multiple-process use. `CONVERSION_WORKERS` configures media-job concurrency within that process. Restarted in-flight jobs fail explicitly; unexpired completed downloads survive a restart.

## Single-server deployment

The recommended first deployment serves both the site and API from one Linux server behind Caddy. That avoids an additional frontend hosting bill.

```bash
cp .env.example .env
docker compose up -d --build
```

The app listens on host loopback port 8000. Set the purchased domain's DNS to the server and install the supplied `Caddyfile.example` as the host Caddy configuration. Caddy handles HTTPS. The sample hostname does not establish domain ownership.

For Docker behind a host proxy, determine the Compose network's **actual gateway IP**, set `TRUSTED_PROXY_IPS` in `.env` to that address, and recreate the service. This lets Uvicorn trust client-IP headers from the known proxy so users do not all share one IP quota. With a direct local Python deployment, a proxy on `127.0.0.1` already matches Uvicorn's default. Do not expose the backend port publicly or trust arbitrary forwarding headers.

The container runs as a non-root user, with a read-only root filesystem, a writable data volume, two CPU cores, a 1 GB memory limit, a PID limit, and bounded log rotation. The Node binary is supplied from Node 24; no JavaScript runtime installation is needed at request time.

The optional `proxy_ca` Docker build secret supports managed environments with an outbound TLS proxy. An ordinary server can build without the secret. The certificate is never copied into image layers. If the runtime host uses a TLS proxy, provide its trusted certificate through the host's standard runtime trust configuration.

### Separate frontend hosting

If retaining the Sites interface:

1. Deploy the Python service at a reachable HTTPS origin.
2. Set `apiBase` in `dist/config.js` to that origin (without `/api`).
3. Set backend `ALLOWED_ORIGINS` to the exact frontend origin.
4. Republish the frontend and verify the full live flow.

Only public origin configuration belongs in `config.js`. No credentials belong in frontend files. The API uses unpredictable, temporary job IDs as download capabilities; no account or API key is required.

## Limits and operating budget

| Setting | Default |
| --- | --- |
| Video duration | 600 seconds |
| MP3 output | 128 or 192 kbps |
| MP4 output | H.264/AAC, up to 360p or 720p when available |
| Concurrent conversions | 2 |
| Pending queue | 12 |
| Video lookups | 20 per IP per hour; 2 simultaneous globally |
| New conversions | 6 per IP per hour; 2 active per IP |
| Global daily conversions | 100 |
| Downloads | 30 requests per IP per hour |
| Per-job temporary storage | 250 MB, with each source limited to 125 MB |
| Total reserved + used job storage | 2,048 MB |
| Monthly download allowance | 300 GB |
| Conversion timeout | 240 seconds |
| Metadata timeout | 40 seconds |
| File retention | 30 minutes after completion |
| Cleanup sweep | Every 30 seconds |

These are **beta limits, not a capacity guarantee or dollar-based billing cap**. Download accounting reserves the whole file size for every request, including partial/retried requests. It deliberately overcounts rather than permitting unlimited retries. Configure the allowance below the chosen host's included transfer, leaving room for other traffic. Source retrieval, page traffic, and provider fees are outside this download counter.

The agreed operating ceiling is **$100/month**. Plan approximately $50 for a server with included bandwidth, $10 for the domain allowance/basic monitoring, and retain $40 for contingencies. These are budget allocations, not vendor quotes. Choose the actual server only after testing YouTube access and conversion resource use there. Use provider billing alerts and any supported hard spending controls.

The monthly transfer counter and daily job counter persist across restarts. A restarted service fails interrupted jobs rather than silently spending resources retrying them.

## Ads and analytics

The UI contains an advertisement slot, hidden until configured. No ad script, analytics tracker, or external font is installed. The app can run without those services. Before enabling ads, choose an ad network that accepts the product, configure its actual account/slot, and add the disclosures and consent behavior required for that integration. Do not label an ad as a download control.

## API

| Route | Behavior |
| --- | --- |
| `GET /api/health` | Required local executables are present; does not promise upstream availability |
| `POST /api/preview` | `{ "url": "https://youtu.be/..." }` → expiring preview token and sanitized video metadata |
| `POST /api/jobs` | `{ "preview_id": "...", "format": "mp3", "quality": 192 }` → job; repeated preview/options reuse the existing active/completed job |
| `GET /api/jobs/:id` | Status, progress, expiry, and final size |
| `DELETE /api/jobs/:id` | Cancel a queued/running job |
| `GET /api/jobs/:id/download` | Attachment response, with expiry and transfer quota enforced |

Only exact YouTube hostnames and validated 11-character video IDs are accepted. URL paths, options, file names, and arbitrary external hosts are never forwarded from user input. Process arguments are passed directly without shell execution. Preview tokens are bound to a daily HMAC of the client IP; raw IPs are not stored by the application. Reverse proxies may maintain their own logs.

Video titles and job records expire with their files. Preview metadata is in memory for up to 15 minutes. One job ID is saved in browser session storage to recover after refresh; it is cleared when the user starts another video. Thumbnails are requested directly from YouTube's image host after a video lookup.

## Verification

```bash
.venv/bin/pip install pytest httpx playwright
.venv/bin/python -m pytest tests -q
.venv/bin/python tests/browser_smoke.py
```

The browser smoke script expects Chromium at `/usr/bin/chromium`, starts its test-only service on port 8001, and terminates it afterward. Test fixtures are not shipped in the production image or the Sites static archive. A development sandbox may need network permission for the test client's inter-thread event-loop sockets.

## Next launch checks

Connect a chosen server; verify live extraction and downloads there; register and point the selected domain; set real provider transfer alerts; then enable the selected ad integration. Conversion quality cannot exceed source quality. Keep yt-dlp and its JavaScript helpers updated when upstream formats change, and rerun the tests after upgrades.
