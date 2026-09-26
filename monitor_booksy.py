#!/usr/bin/env python3
"""
Booksy availability watcher.

Loads the Canyon Massage and Bodywork Booksy page, opens the booking flow,
and checks whether the "No availability at this time" message is showing.
If that message is gone (meaning a slot/cancellation opened up in the next
60 days), it emails an alert.

IMPORTANT: Booksy is a JavaScript app, and the exact click path to reach the
slot picker can vary by business/service. The selectors below are a
best-effort guess based on the screenshot you shared. Before you rely on
this, run it once locally with `headless=False` (see the commented line
below) and watch it click through — adjust the `page.get_by_text(...)`
calls if it doesn't land on the same screen as your screenshot.

Environment variables (set as GitHub Actions secrets, or in a local .env
loaded some other way):
    BOOKSY_URL        - the booking page URL to check
    SERVICE_NAME      - the exact (or partial) service name to click, e.g.
                        "Integrative Massage One hour". Defaults to that
                        value below if not set.
    ALERT_EMAIL_TO    - where to send the alert (e.g. dayrbakker@gmail.com)
    SMTP_HOST         - e.g. smtp.gmail.com
    SMTP_PORT         - e.g. 587
    SMTP_USER         - the sending email address
    SMTP_PASSWORD     - an app password for that address (not your login password)

Debug screenshots are saved to debug_1_landing.png, debug_2_services.png,
debug_3_service_selected.png, debug_4_slots.png on every run (whether or not
it succeeds). In GitHub Actions these get uploaded as a downloadable
"debug-screenshots" artifact on the run's summary page — open those if a
run behaves unexpectedly, so we can see exactly where the click path landed.
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

NO_AVAILABILITY_TEXT = "no availability at this time"
# Markers that mean we're still on the business listing page (services list,
# reviews, etc.) rather than inside the actual slot picker. If these are
# still present after we've tried to click through, we did NOT reach the
# slot picker, and must not report "available" — that would be a false
# positive, just like what happened on the first real run.
LANDING_PAGE_MARKERS = ["default category", "how reviews work", "based on"]
DEFAULT_SERVICE_NAME = "Integrative Massage One hour"
STATE_FILE = Path("state.json")


def check_availability(url: str, service_name: str):
    """Returns (is_available: bool, reached_slot_picker: bool, debug_text: str)."""
    with sync_playwright() as p:
        # Flip headless to False when testing locally so you can watch it
        # click through and confirm it lands on the right screen.
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.screenshot(path="debug_1_landing.png", full_page=True)

        # Step 1: click the "Book" button under the Services / Default
        # Category section, which should expand or reveal the list of
        # individual services.
        try:
            page.get_by_role("button", name="Book", exact=True).first.click(timeout=8000)
        except Exception:
            try:
                page.get_by_text("Book", exact=True).first.click(timeout=5000)
            except Exception:
                pass
        page.wait_for_timeout(2000)
        page.screenshot(path="debug_2_services.png", full_page=True)

        # Step 2: click the specific service by name.
        try:
            page.get_by_text(service_name, exact=False).first.click(timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        page.screenshot(path="debug_3_service_selected.png", full_page=True)

        # Step 3: click "Book" again — this is the service-specific CTA that
        # should load (or reveal) the actual date/time slot picker.
        try:
            page.get_by_role("button", name="Book", exact=True).first.click(timeout=8000)
        except Exception:
            try:
                page.get_by_text("Book", exact=True).first.click(timeout=5000)
            except Exception:
                pass
        page.wait_for_timeout(3000)  # let the slot picker finish rendering
        page.screenshot(path="debug_4_slots.png", full_page=True)

        body_text = page.inner_text("body")
        current_url = page.url
        browser.close()

        body_lower = body_text.lower()
        reached_slot_picker = not any(marker in body_lower for marker in LANDING_PAGE_MARKERS)

        if reached_slot_picker:
            is_available = NO_AVAILABILITY_TEXT not in body_lower
        else:
            # We never actually left the listing page — treat as "not
            # available" rather than risk a false positive.
            is_available = False

        debug_text = f"Final URL: {current_url}\n\n{body_text[:2500]}"
        return is_available, reached_slot_picker, debug_text


def send_email(subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["ALERT_EMAIL_TO"]

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(msg["From"], [msg["To"]], msg.as_string())


def load_last_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("available")
    return None


def save_state(available: bool):
    STATE_FILE.write_text(json.dumps({"available": available}))


def main():
    url = os.environ.get("BOOKSY_URL")
    if not url:
        print("BOOKSY_URL not set", file=sys.stderr)
        sys.exit(1)
    service_name = os.environ.get("SERVICE_NAME", DEFAULT_SERVICE_NAME)

    is_available, reached_slot_picker, debug_text = check_availability(url, service_name)
    last_state = load_last_state()

    print(f"Reached slot picker: {reached_slot_picker}")
    print(f"Available: {is_available}")

    if not reached_slot_picker:
        print(
            "WARNING: click path did not reach the slot picker (still looks like "
            "the listing page). Check the debug screenshots. Not sending an alert.",
            file=sys.stderr,
        )

    # Only email on the flip from unavailable -> available, so you don't get
    # a new email every 15 minutes while a slot stays open.
    if is_available and last_state is not True:
        send_email(
            subject="Canyon Massage: an opening just appeared!",
            body=f"A slot looks open in the next 60 days. Book now:\n{url}\n\n---\nPage text snippet:\n{debug_text}",
        )
        print("Alert email sent.")

    save_state(is_available)


if __name__ == "__main__":
    main()
