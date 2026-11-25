# This software is designed to automate the renewal process of books at the Regina Public Library.
# It uses Selenium WebDriver to interact with the library's website and perform actions such as logging in, navigating to the 'Due Soon' materials, and clicking the 'Renew' button for each book.
# The software also includes error handling to manage cases where no books are found to be renewed.
#
# Runs both locally (credentials from a .env file) and on GitHub Actions
# (credentials from repository secrets exposed as environment variables).
#
# Optional environment variables (handy for testing, no code edits needed):
#   RPL_URL            Page to read the books from. Default: the 'overdue-soon' page.
#                      For testing use https://www.reginalibrary.ca/myaccount/currently-borrowed/
#   RENEW_WITHIN_DAYS  Also renew books due within this many days. Default: 0 (due today or overdue only).

import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Loads environment variables if running on a machine, but has no effect on GitHub Actions.
load_dotenv()

# GitHub Actions always sets CI=true. Locally it is unset, so the browser opens
# visibly exactly as before.
IS_CI = os.getenv("CI", "").lower() == "true"

# Production page. To test with books that are not due soon, set RPL_URL to
# https://www.reginalibrary.ca/myaccount/currently-borrowed/ (all borrowed books).
RPL_URL = os.getenv("RPL_URL") or "https://www.reginalibrary.ca/myaccount/currently-borrowed/overdue-soon"

# 0 = renew books that are due today or already overdue.
RENEW_WITHIN_DAYS = int(os.getenv("RENEW_WITHIN_DAYS") or 0)

barcode = str(os.getenv("BARCODE"))
password = str(os.getenv("PASSWORD"))
SMTP_SERVER = "smtp.gmail.com"
sender_email = str(os.getenv("EMAIL"))
sender_password = str(os.getenv("SENDER_PASSWORD"))
receiver_email = sender_email

# --- Page locations -------------------------------------------------------------------
# I could not inspect the logged-in page, so these keep the exact structure of the XPaths
# that already work for you. The only change: book details are now read *relative to each
# book card*, so one set of paths works for every book (no more div[{i}] indexing).
LOGIN_XPATHS = {
    "Go to Login": "/html/body/div[1]/div[3]/a[1]",
    "Username Input": "/html/body/div[1]/header/div[2]/div/div[2]/div[2]/div/div/li/div/form/div[1]/input",
    "Password Input": "/html/body/div[1]/header/div[2]/div/div[2]/div[2]/div/div/li/div/form/div[2]/input",
    "Login Button": "/html/body/div[1]/header/div[2]/div/div[2]/div[2]/div/div/li/div/form/input[1]",
}

# Only entries with the title and due-date structure of a book card.
CARDS_XPATH = (
    "/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[2]/div/div"
    "/div[div[2]/div/div[1]/a and div[3]/div/span]"
)
CARD_XPATHS = {  # relative to one book card
    "Book's name": "./div[2]/div/div[1]/a",
    "Book's due date": "./div[3]/div/span",
    "'Renew' button": "./div[3]/div[2]/form/input[1]",
    "Renewals left": "./div[3]/div[1]/p/span",
}

# --- Dates ----------------------------------------------------------------------------
# Saskatchewan does not observe daylight saving time, so the fallback is a fixed UTC-6.
try:
    REGINA = ZoneInfo("America/Regina")
except ZoneInfoNotFoundError:  # e.g. Windows without the 'tzdata' package
    REGINA = timezone(timedelta(hours=-6), "CST")

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DUE_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)


def today_in_regina() -> date:
    return datetime.now(REGINA).date()


def parse_due_date(text: str, today: date) -> date | None:
    """Turn page text like 'Due Dec 5' into a date. Returns None if it can't be read.

    The page shows no year, so pick the year that puts the date closest to today
    (this handles Dec -> Jan rollovers and days without a leading zero).
    """
    m = DUE_DATE_RE.search(text)
    if not m:
        return None
    month, day = MONTHS[m.group(1).lower()], int(m.group(2))
    if m.group(3):
        try:
            return date(int(m.group(3)), month, day)
        except ValueError:
            return None
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:  # e.g. Feb 29 in a non-leap year
            pass
    return min(candidates, key=lambda d: abs((d - today).days), default=None)


def fmt(d: date | None) -> str:
    return d.strftime("%b %d, %Y") if d else "unknown date"


# --- Report ---------------------------------------------------------------------------
@dataclass
class Entry:
    status: str  # RENEWED / NOT RENEWED / NOT DUE / INFO / ERROR
    text: str


@dataclass
class Book:
    name: str
    due_text: str
    due: date | None


class LoginError(Exception):
    pass


def describe(e: Exception) -> str:
    """Short error text for the EMAIL only. Logs get just the error type: workflow logs can be public."""
    first_line = (str(e).strip().splitlines() or [""])[0][:300]
    return f"{type(e).__name__}: {first_line}" if first_line else type(e).__name__


def build_subject(entries: list[Entry]) -> str:
    if any(e.status == "ERROR" for e in entries):
        return "RPL Auto-Renewer: ERROR - please check"
    if any(e.status == "NOT RENEWED" for e in entries):
        return "RPL Auto-Renewer: ACTION NEEDED - return a book today"
    renewed = sum(e.status == "RENEWED" for e in entries)
    if renewed:
        return f"RPL Auto-Renewer: {renewed} book{'s' if renewed != 1 else ''} renewed"
    return "RPL Auto-Renewer: nothing to renew today"


def send_report(entries: list[Entry]) -> bool:
    msg = EmailMessage()
    msg["Subject"] = build_subject(entries)
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.set_content("\n".join(f"{e.status}: {e.text}" for e in entries))
    try:
        with smtplib.SMTP(SMTP_SERVER, 587, timeout=30) as server:
            server.ehlo()  # Identify yourself to the server
            server.starttls()  # Secure the connection
            server.ehlo()  # Re-identify as an encrypted connection
            server.login(sender_email, sender_password)
            server.send_message(msg)
    except Exception as e:
        print(f"❌ Could not send the report email: {type(e).__name__}")
        return False
    print("🚀 Email report sent successfully.")
    return True


# --- Browser --------------------------------------------------------------------------
def build_driver():
    options = webdriver.ChromeOptions()
    if IS_CI:
        # A CI runner has no display, so Chrome must run headless.
        # No window-size / resolution flags are set on purpose.
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")  # required in the runner's container-like environment
        options.add_argument("--disable-dev-shm-usage")  # /dev/shm is small on runners
    drv = webdriver.Chrome(options=options)
    drv.set_page_load_timeout(60)  # never hang forever on a stuck page
    if IS_CI:
        # Headless Chrome announces itself as "HeadlessChrome" in its user agent,
        # which some sites reject. Report it as regular Chrome instead.
        ua = drv.execute_cdp_cmd("Browser.getVersion", {})["userAgent"]
        drv.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
    return drv


def login(driver, wait) -> None:
    driver.get(RPL_URL)

    login_page = wait.until(EC.element_to_be_clickable((By.XPATH, LOGIN_XPATHS["Go to Login"])))
    login_page.click()

    # Type your username and password
    uname_input = wait.until(EC.visibility_of_element_located((By.XPATH, LOGIN_XPATHS["Username Input"])))
    uname_input.send_keys(barcode)
    pwd_input = driver.find_element(By.XPATH, LOGIN_XPATHS["Password Input"])
    pwd_input.send_keys(password)
    login_button = driver.find_element(By.XPATH, LOGIN_XPATHS["Login Button"])
    login_button.click()

    # Don't read the page until it has reacted to the login. Logged in means the login form is
    # gone (or book cards are showing). If the form is still there, the login did not work -
    # without this check a wrong PIN looked exactly like "no books due".
    def login_finished(d):
        return d.find_elements(By.XPATH, CARDS_XPATH) or not d.find_elements(By.XPATH, LOGIN_XPATHS["Username Input"])

    try:
        wait.until(login_finished)
    except TimeoutException:
        raise LoginError("the login form is still showing after submitting") from None


# --- Books ----------------------------------------------------------------------------
def read_books(driver, today: date) -> list[Book]:
    books = []
    for card in driver.find_elements(By.XPATH, CARDS_XPATH):
        name = card.find_element(By.XPATH, CARD_XPATHS["Book's name"]).text.strip()
        due_text = card.find_element(By.XPATH, CARD_XPATHS["Book's due date"]).text.strip()
        books.append(Book(name, due_text, parse_due_date(due_text, today)))
    return books


def find_card(driver, book: Book):
    """Find a book's card again (cards go stale after a refresh). Name + original due date identify it."""
    for card in driver.find_elements(By.XPATH, CARDS_XPATH):
        name = card.find_element(By.XPATH, CARD_XPATHS["Book's name"]).text.strip()
        due_text = card.find_element(By.XPATH, CARD_XPATHS["Book's due date"]).text.strip()
        if name == book.name and due_text == book.due_text:
            return card
    return None


def renewals_left(card) -> int | None:
    found = card.find_elements(By.XPATH, CARD_XPATHS["Renewals left"])
    match = re.search(r"\d+", found[0].text) if found else None
    return int(match.group()) if match else None


def renew_book(driver, wait, book: Book) -> Entry:
    label = f"{book.name} (due {fmt(book.due)})"
    try:
        card = find_card(driver, book)
        if card is None:
            return Entry("ERROR", f"Could not find {label} on the page again. Please check your account.")

        # find_elements (not a wait): no 'Renew' button is an answer, not something to time out on.
        buttons = card.find_elements(By.XPATH, CARD_XPATHS["'Renew' button"])
        if buttons:
            wait.until(EC.element_to_be_clickable(buttons[0])).click()
            driver.refresh()  # Refresh to ensure that the book's status is updated after renewal.
            return Entry("RENEWED", f"{label}.")

        left = renewals_left(card)
        if left == 0:
            return Entry("NOT RENEWED", f"{label}; no renewals remain. Please return the book to an RPL library today.")
        if left is None:
            return Entry(
                "NOT RENEWED",
                f"{label}; there is no Renew button and the renewals-left information could not be found. "
                "Please return the book to an RPL library today.",
            )
        return Entry("ERROR", f'Could not renew "{book.name}" (due {fmt(book.due)}) despite {left} renewals remaining.')
    except Exception as e:
        print(f"❌ Could not renew a book: {type(e).__name__}")
        return Entry("ERROR", f"Could not renew {label} ({describe(e)}).")


def run(driver, wait, today: date, entries: list[Entry]) -> None:
    """Does the work. Results go into `entries` as they happen, so a crash part-way keeps them."""
    login(driver, wait)
    books = read_books(driver, today)
    if not books:
        print("There are no books that will be overdue soon.")
        entries.append(Entry("INFO", "No borrowed books are due for renewal."))
        return

    print(f"Found {len(books)} book{'s' if len(books) != 1 else ''}.")
    renew_until = today + timedelta(days=RENEW_WITHIN_DAYS)
    for i, book in enumerate(books, start=1):
        if book.due is None:
            entries.append(Entry("ERROR", f"Could not read the due date ('{book.due_text}') of {book.name}."))
            outcome = "due date unreadable"
        elif book.due > renew_until:
            entries.append(Entry("NOT DUE", f"{book.name} (due {fmt(book.due)}); no renewal attempted."))
            outcome = "not due yet"
        else:
            entry = renew_book(driver, wait, book)
            entries.append(entry)
            outcome = entry.status.lower()
        print(f"Book {i}/{len(books)}: {outcome}")  # titles are deliberately not printed: logs can be public


def main() -> int:
    # Fail fast with a clear message if a secret is missing or misnamed
    missing = [
        name for name, value in (
            ("BARCODE", barcode),
            ("PASSWORD", password),
            ("EMAIL", sender_email),
            ("SENDER_PASSWORD", sender_password),
        ) if not value
    ]
    if missing:
        sys.exit(f"❌ Missing required environment variables / secrets: {', '.join(missing)}")

    entries: list[Entry] = []
    driver = None
    try:
        driver = build_driver()
        wait = WebDriverWait(driver, 20 if IS_CI else 10)  # runners are a bit slower than a laptop
        run(driver, wait, today_in_regina(), entries)
    except LoginError as e:
        print("❌ Login failed.")
        entries.append(Entry(
            "ERROR",
            f"Could not log in ({e}). Check that your library card number and PIN are still correct "
            "(BARCODE / PASSWORD), or the RPL login page may have changed. No books were renewed; "
            "please renew manually if needed.",
        ))
    except Exception as e:
        print(f"❌ An error occurred: {type(e).__name__}")
        entries.append(Entry(
            "ERROR",
            "A problem occurred with the Regina Public Library website. "
            "The renewal may not have completed; please check your account and renew books manually if needed. "
            f"Details: {describe(e)}",
        ))
        # URL and title only. Screenshots / page source are not saved because
        # they would contain your account page and logs/artifacts may be public.
        try:
            print(f"Current URL: {driver.current_url}")
            print(f"Page title: {driver.title}")
        except Exception:
            print("(The browser is no longer reachable.)")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    # Always report, even after a crash, so you are never left guessing.
    sent = send_report(entries)
    print("Task completed")
    had_errors = any(e.status == "ERROR" for e in entries)
    # A non-zero exit makes the GitHub Actions run show red (and GitHub emails you), even if Gmail fails.
    return 1 if (had_errors or not sent) else 0


if __name__ == "__main__":
    sys.exit(main())