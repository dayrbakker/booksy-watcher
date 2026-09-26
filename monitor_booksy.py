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

Click path (matches the real UI, row by row):
    1. Load the business page.
    2. Click the caret to expand "Default Category" under "Services".
    3. In the expanded list, find the row for SERVICE_NAME and click the
       blue "Book" button in that row.
    4. On the new screen this opens, click the blue "book" link next to
       SERVICE_NAME in the first matching row.
    5. Check the resulting screen's text for "No availability at this time".

Steps 3 and 4 use Playwright's layout-aware ":right-of()" selector to find
the button/link sitting next to the service's name, without needing to know
the exact CSS classes Booksy uses.

Debug screenshots are saved after each step (debug_1_landing.png,
debug_2_expanded.png, debug_3_after_first_book.png, debug_4_slots.png) on
every run, success or failure. In GitHub Actions these get uploaded as a
downloadable "debug-screenshots" artifact on the run's summary page.
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

NO_AVAILABILITY_TEXT = "no availability at this time"
# If any of these show up on the final screen, we got bounced to the
# generic Booksy homepage instead of reaching the slot picker (this is
# what happened on an earlier run) — never treat that as "available".
HOMEPAGE_MARKERS = [
    "discover and book beauty & wellness professionals",
    "grow my business",
    "cut the phone tag",
]
DEFAULT_SERVICE_NAME = "Integrative Massage One hour"
STATE_FILE = Path("state.json")


def _click_near(page, anchor_text: str, tag: str = "*", timeout: int = 8000) -> bool:
    """Click the nearest clickable element positioned to the right of the
    element containing anchor_text (e.g. the "Book" button on the same row
    as a service name). Tries a couple of tag/text combinations since we
    don't know Booksy's exact markup. Returns True if a click succeeded."""
    candidates = [
        f'button:right-of(:text("{anchor_text}"))',
        f'a:right-of(:text("{anchor_text}"))',
        f':text("Book"):right-of(:text("{anchor_text}"))',
        f':text("book"):right-of(:text("{anchor_text}"))',
    ]
    for selector in candidates:
        try:
            page.locator(selector).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


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

        # Dismiss the Cookiebot consent banner, if present — it sits on top
        # of the page and blocks every click underneath it until dismissed.
        try:
            page.get_by_role("button", name="OK", exact=True).click(timeout=5000)
            page.wait_for_timeout(500)
        except Exception:
            try:
                page.locator('button:has-text("OK")').first.click(timeout=3000)
                page.wait_for_timeout(500)
            except Exception:
                pass  # no banner this time, or it used different wording

        page.screenshot(path="debug_1_landing.png", full_page=True)

        steps_ok = {"expand": False, "first_book": False, "second_book": False}

        # Step 1: click the caret/header to expand "Default Category".
        try:
            page.get_by_text("Default Category", exact=False).first.click(timeout=8000)
            steps_ok["expand"] = True
        except Exception as e:
            print(f"Could not expand 'Default Category': {e}")
        page.wait_for_timeout(1500)
        page.screenshot(path="debug_2_expanded.png", full_page=True)

        # Step 2: click the blue "Book" button in the row for our service.
        if steps_ok["expand"]:
            steps_ok["first_book"] = _click_near(page, service_name)
            if not steps_ok["first_book"]:
                print(f"Could not find a 'Book' button next to '{service_name}' after expanding.")
        page.wait_for_timeout(2500)
        page.screenshot(path="debug_3_after_first_book.png", full_page=True)

        # Step 3: on the new screen, click the "book" link next to the
        # service in the first matching row.
        if steps_ok["first_book"]:
            steps_ok["second_book"] = _click_near(page, service_name)
            if not steps_ok["second_book"]:
                print(f"Could not find a second 'book' link next to '{service_name}'.")
        page.wait_for_timeout(3000)  # let the slot picker finish rendering
        page.screenshot(path="debug_4_slots.png", full_page=True)

        body_text = page.inner_text("body")
        current_url = page.url
        browser.close()

        body_lower = body_text.lower()
        redirected_home = any(marker in body_lower for marker in HOMEPAGE_MARKERS)
        reached_slot_picker = (
            steps_ok["expand"] and steps_ok["first_book"] and steps_ok["second_book"] and not redirected_home
        )

        if reached_slot_picker:
            is_available = NO_AVAILABILITY_TEXT not in body_lower
        else:
            # Something in the click path failed, or we got bounced to the
            # homepage — treat as "not available" rather than risk a false
            # positive.
            is_available = False

        debug_text = (
            f"Final URL: {current_url}\n"
            f"Steps -> expanded: {steps_ok['expand']}, first Book click: {steps_ok['first_book']}, "
            f"second book click: {steps_ok['second_book']}, redirected to homepage: {redirected_home}\n\n"
            f"{body_text[:2500]}"
        )
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
