"""Record the real browser walkthrough using the isolated synthetic verification stack."""

import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    output = Path(os.environ["PAOS_DEMO_RECORDING_DIR"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            base_url=os.environ["PAOS_TEST_URL"],
            record_video_dir=output,
            record_video_size={"width": 1366, "height": 930},
            viewport={"width": 1366, "height": 930},
            reduced_motion="reduce",
        )
        # Authenticate before creating a page so the recording never sees the token.
        login = context.request.post("/api/session", data={"token": os.environ["PAOS_TEST_TOKEN"]})
        if not login.ok:
            raise RuntimeError("the recording stack refused the operator session")
        session = json.dumps(login.json()["session"])
        context.add_init_script(f"sessionStorage.setItem('paos.core.session', {session})")
        page = context.new_page()
        video = page.video
        assert video is not None
        page.goto("/")
        start = page.get_by_role("button", name="Start release review", exact=True)
        expect(start).to_be_visible()
        page.wait_for_timeout(1000)
        start.click()
        approve = page.get_by_role("button", name="Approve", exact=True)
        expect(approve).to_be_visible(timeout=40_000)
        page.wait_for_timeout(1800)
        page.get_by_role("button", name="Text diff", exact=True).scroll_into_view_if_needed()
        page.wait_for_timeout(2600)
        approve.scroll_into_view_if_needed()
        page.wait_for_timeout(1800)
        approve.click()
        expect(page.get_by_test_id("task-status")).to_have_text("Succeeded", timeout=20_000)
        page.wait_for_timeout(2400)
        page.get_by_role("button", name="Governed memory", exact=True).click()
        expect(page.get_by_role("heading", name="Governed memory", exact=True)).to_be_visible()
        page.wait_for_timeout(2800)
        context.close()
        Path(video.path()).replace(output / "paos-core-demo.webm")
        browser.close()


if __name__ == "__main__":
    main()
