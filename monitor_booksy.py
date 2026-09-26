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
    ALERT_EMAIL_TO    - where to send the alert (e.g. dayrbakker@gmail.com)
    SMTP_HOST         - e.g. smtp.gmail.com
    SMTP_PORT         - e.g. 587
    SMTP_USER         - the sending email address
    SMTP_PASSWORD     - an app password for that address (not your login password)
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

NO_AVAILABILITY_TEXT = "No availability at this time"
STATE_FILE = Path("state.json")


def check_availability(url: str):
    """Returns (is_available: bool, debug_text: str)."""
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

        # Click "Book" to open the booking flow. Do this up to twice, since
        # some businesses show a service list first (each with its own
        # "Book" button) before the slot picker appears.
        for _ in range(2):
            try:
                page.get_by_text("Book", exact=True).first.click(timeout=8000)
                page.wait_for_timeout(2000)
            except Exception:
                break

        page.wait_for_timeout(3000)  # let the slot picker finish rendering

        body_text = page.inner_text("body")
        browser.close()

        is_available = NO_AVAILABILITY_TEXT.lower() not in body_text.lower()
        return is_available, body_text[:2000]


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

    is_available, debug_text = check_availability(url)
    last_state = load_last_state()

    print(f"Available: {is_available}")

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
