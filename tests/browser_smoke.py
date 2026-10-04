"""Browser verification against a test-only service with generated media."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import httpx
from playwright.async_api import async_playwright


async def main():
    with tempfile.TemporaryDirectory(prefix="y2audio-browser-") as directory:
        environment = {**os.environ, "Y2AUDIO_TEST_DATA": directory}
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.browser_fixture:app", "--host", "127.0.0.1", "--port", "8001", "--no-access-log"], env=environment, stdout=subprocess.DEVNULL)
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(40):
                    try:
                        if (await client.get("http://127.0.0.1:8001/api/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.15)
                else:
                    raise RuntimeError("Test service did not start")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
                page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                # Block only external thumbnails in this fixture test.
                await page.route("https://i.ytimg.com/**", lambda route: route.fulfill(status=204))
                await page.goto("http://127.0.0.1:8001")
                await page.get_by_role("button", name="Find video").click()
                assert await page.locator("#form-error").is_visible()
                await page.locator("#video-url").fill("https://example.org/video")
                await page.get_by_role("button", name="Find video").click()
                assert await page.locator("#video-url").get_attribute("aria-invalid") == "true"
                for output_format in ("MP3", "MP4"):
                    if output_format == "MP4":
                        await page.get_by_role("button", name="Video MP4").click()
                    await page.locator("#video-url").fill("https://youtu.be/BaW_jenozKc")
                    await page.get_by_role("button", name="Find video").click()
                    await page.locator("#video-preview").wait_for(state="visible")
                    assert await page.locator("#video-title").inner_text() == "Test <video> / audio"
                    await page.get_by_role("button", name=f"Convert to {output_format}").click()
                    await page.locator("#download-button").wait_for(state="visible")
                    async with page.expect_download() as download_info:
                        await page.locator("#download-button").click()
                    download = await download_info.value
                    path = await download.path()
                    assert Path(path).stat().st_size > 1000
                    assert download.suggested_filename.endswith("." + output_format.lower())
                    await page.reload()
                    await page.locator("#download-button").wait_for(state="visible")
                    await page.get_by_role("button", name="Convert another video").click()
                await page.locator("summary").first.click()
                assert await page.locator("details").first.get_attribute("open") is not None
                for width in (390, 320):
                    await page.set_viewport_size({"width": width, "height": 844})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
                    assert await page.locator("#convert-button").is_visible()
                assert not errors, errors
                await browser.close()
                print("PASS: invalid links, both format flows, real downloaded files, reload recovery, FAQ, mobile widths 390/320, no JavaScript errors")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
