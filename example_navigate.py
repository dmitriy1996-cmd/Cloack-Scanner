"""
Example: start Octo profile, connect via CDP (OctoAutomator), navigate, extract HTML, disconnect, stop.

Prerequisites:
  pip install playwright
  python -m playwright install chromium

  Octo Browser running, Local API at http://127.0.0.1:58888 (default).

Run:
  python example_navigate.py
  python example_navigate.py --api-key YOUR_KEY
  set OCTO_API_KEY=YOUR_KEY && python example_navigate.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from octo_client import (
    OctoAPIError,
    OctoAutomationError,
    OctoAutomator,
    OctoClient,
    StartedProfile,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser(description="Example: Octo profile + CDP navigate")
    ap.add_argument("--api-base", default="http://127.0.0.1:58888", help="Octo Local API URL")
    ap.add_argument("--api-key", default="", help="Octo API key (X-Octo-Api-Token). Or set OCTO_API_KEY.")
    args = ap.parse_args()

    api_key = (args.api_key or os.environ.get("OCTO_API_KEY") or "").strip() or None
    base = args.api_base.rstrip("/")
    octo = OctoClient(base_url=base, timeout_s=30.0, api_key=api_key)

    uuid = octo.create_profile(title="OctoScanner Example Navigate", os_name="win")
    log.info("Created profile uuid=%s", uuid)

    try:
        started = octo.start_profile(
            uuid,
            headless=False,
            start_pages=["about:blank"],
        )
        ws_preview = (started.ws_endpoint[:80] + "...") if (started.ws_endpoint and len(started.ws_endpoint) > 80) else started.ws_endpoint
        log.info("Started profile debug_port=%s ws_endpoint=%s", started.debug_port, ws_preview)

        auto = OctoAutomator(started)
        auto.connect()
        try:
            auto.goto("https://example.com", wait_until="domcontentloaded", timeout_ms=60_000)
            auto.wait_for("body", state="visible", timeout_ms=30_000)
            title = auto.get_title()
            html = auto.get_html()
            log.info("Title: %s", title)
            print("Title:", title)
            print("HTML (first 200 chars):", (html or "")[:200])
        finally:
            auto.disconnect()

        octo.stop_profile(uuid)
        log.info("Stopped profile")
    finally:
        octo.delete_profiles([uuid])
        log.info("Deleted profile")


if __name__ == "__main__":
    try:
        main()
    except (OctoAPIError, OctoAutomationError) as e:
        log.error("%s", e)
        sys.exit(1)
