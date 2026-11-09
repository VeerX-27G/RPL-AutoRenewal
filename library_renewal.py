# This software is designed to automate the renewal process of books at the Regina Public Library.
# It uses Selenium WebDriver to interact with the library's website and perform actions such as logging in, navigating to the 'Due Soon' materials, and clicking the 'Renew' button for each book.
# The software also includes error handling to manage cases where no books are found to be renewed.
#
# Runs both locally (credentials from a .env file) and on GitHub Actions
# (credentials from repository secrets exposed as environment variables).

import os
lazy import sys
import smtplib
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from email.message import EmailMessage
lazy from datetime import datetime as dt
from dotenv import load_dotenv

# Loads environment variables if running on a machine, but has not affect on GitHub Actions.
load_dotenv()

# GitHub Actions always sets CI=true. Locally it is unset, so the browser opens
# visibly exactly as before.
IS_CI = os.getenv("CI", "").lower() == "true"

# Try https://www.reginalibrary.ca/myaccount/currently-borrowed for testing the software below but add '/overdue-soon' in production.
RPL_url = "https://www.reginalibrary.ca/myaccount/currently-borrowed/overdue-soon"

barcode = str(os.getenv("BARCODE"))
password = str(os.getenv("PASSWORD"))
SMTP_server = "smtp.gmail.com"
sender_email = str(os.getenv("EMAIL"))
sender_password = str(os.getenv("SENDER_PASSWORD"))
receiver_email = sender_email

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

elements_to_be_scraped = {
    "Go to Login": "/html/body/div[1]/div[3]/a[1]",
    "Username Input": "/html/body/div[1]/header/div[2]/div/div[2]/div[2]/div/div/li/div/form/div[1]/input",
    "Password Input": "/html/body/div[1]/header/div[2]/div/div[2]/div[2]/div/div/li/div/form/div[2]/input",
    "Login Button": "/html/body/div[1]/header/div[2]/div/div[2]/div[2]/div/div/li/div/form/input[1]",
    "Book's name": "/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[2]/div/div/div/div[2]/div/div[1]/a",
    "Book's due date": "/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[2]/div/div/div/div[3]/div/span",
    "'Renew' button": "/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[2]/div/div/div/div[3]/div[2]/form/input[1]",

}

today = dt.today().strftime("%d/%m/%Y")
months = {
    'Jan': '01',
    'Feb': '02',
    'Mar': '03',
    'Apr': '04',
    'May': '05',
    'Jun': '06',
    'Jul': '07',
    'Aug': '08',
    'Sep': '09',
    'Oct': '10',
    'Nov': '11',
    'Dec': '12',
}

# Modify the book's renewal date format to math regular expression (RegEx) of Python's date format
def modify_date(date) -> str:
   return f"{date[2]}/{months[date[1]]}/{dt.now().year}"

def build_driver():
    options = webdriver.ChromeOptions()
    if IS_CI:
        # A CI runner has no display, so Chrome must run headless.
        # No window-size / resolution flags are set on purpose.
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")  # required in the runner's container-like environment
        options.add_argument("--disable-dev-shm-usage")  # /dev/shm is small on runners
    drv = webdriver.Chrome(options=options)
    if IS_CI:
        # Headless Chrome announces itself as "HeadlessChrome" in its user agent,
        # which some sites reject. Report it as regular Chrome instead.
        ua = drv.execute_cdp_cmd("Browser.getVersion", {})["userAgent"]
        drv.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua.replace("HeadlessChrome", "Chrome")})
    return drv


driver = build_driver()

# Create email message early
msg = EmailMessage()
msg["Subject"] = "Hello from Python!"
msg["From"] = sender_email
msg["To"] = receiver_email
msg.set_content("Initialized message")

try:
    driver.get(RPL_url)
    wait = WebDriverWait(driver, 20 if IS_CI else 10)  # runners are a bit slower than a laptop

    # Try to log in
    login_page = wait.until(
        EC.element_to_be_clickable((By.XPATH, elements_to_be_scraped["Go to Login"]))
    )
    login_page.click()

    # Type your username and password
    uname_input = wait.until(
        EC.visibility_of_element_located((By.XPATH, elements_to_be_scraped["Username Input"]))
    )
    uname_input.send_keys(barcode)
    pwd_input = driver.find_element(By.XPATH, elements_to_be_scraped["Password Input"])
    pwd_input.send_keys(password)
    # Click login
    login_button = driver.find_element(By.XPATH, elements_to_be_scraped["Login Button"])
    login_button.click()

    # Send that message
    with smtplib.SMTP(SMTP_server, 587) as server:
        server.ehlo()       # Identify yourself to the server
        server.starttls()   # Secure the connection
        server.ehlo()       # Re-identify as an encrypted connection

        server.login(sender_email, sender_password)
        # Click 'Renew' button for each book if due date is today:
        # VERY IMPORTANT: First, check number of books:
        # TODO: Try to inspect "/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[1]/div[6]/button" = 'Renew All' button if available when you have at most one book borrowed.
        i: int = 1  # Block of the first book's XPath.
        while True:
            try:
                driver.find_element(By.XPATH, f"/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[2]/div/div/div[{i}]")
            except Exception as e:
                print(f"Error: {e}\n")
                if i > 1:       # If you have more than one book borrowed, this line is VERY important because it prevents the program from trying to renew a non-existent book.
                    i -= 1
                    if i < 2:
                        print(f"There is {i} book in your account. So the software will try to renew it.")
                    else:
                        print(f"There are {i} books in your account. So the software will try to renew them all.")
                    break       # To avoid the below 'if' statement work if 'i' was 2.

                if i == 1:      # NOTE: 'i' will be still be 1 even if there are no books in the account.
                    print(f"There are no books that will be overdue soon. So exit the program.")
                    msg.set_content("No more books to be renewed today")
                    server.send_message(msg)
                    print("❌ No more books to renew.")
                    driver.quit()
                    sys.exit()

            i += 1

        # Renew 'i' number of books:
        for j in range(1, i+1):
            try:
                book_name = driver.find_element(By.XPATH, elements_to_be_scraped["Book's name"]).text
                # If today's date matches book's renewal date, renew it and send an email
                due_date = modify_date(driver.find_element(By.XPATH, elements_to_be_scraped["Book's due date"]).text.split())
                print(f"Due date: {due_date} of book '{book_name}'")
                # Try due_date != today if you are testing this software but not in production.
                if due_date == today:
                    try:
                        renewal_button = wait.until(
                            EC.element_to_be_clickable((By.XPATH, elements_to_be_scraped["'Renew' button"]))
                        )
                        renewal_button.click()
                        msg.set_content(f"Book '{book_name}' renewed successfully.")
                        server.send_message(msg)
                        # Book titles are deliberately not printed: workflow logs can be public.
                        print("🚀 Email sent successfully! -1")

                        driver.refresh()    # VERY IMPORTANT: This is necessary to ensure that the book's status is updated after renewal.
                    except Exception as e:
                        print(f"❌ An error occurred - 1: {type(e).__name__}: {e}")
                        # If a book cannot be renewed, check if renewals are left:
                        try:
                            renewals_left = int(driver.find_element(By.XPATH, f"/html/body/div[1]/div[1]/main/div[2]/div/div/div[2]/div/div[2]/div[2]/div/div/div/div[3]/div/p/span").text)
                            if renewals_left == 0:
                                msg.set_content(f"Book '{book_name}' cannot be renewed because you have no renewals left. Please return the book to a library associated with Regina Public Library today.")
                                server.send_message(msg)
                                print("🚀 Email sent successfully! -2")
                        except Exception as e:
                            print(f"❌ An error occurred - 2: {type(e).__name__}: {e}")
            except Exception as e:
                print(f"❌ An error occurred - 3: {type(e).__name__}: {e}")
                msg.set_content("No more books to be renewed today")
                server.send_message(msg)
                print("❌ No more books to renew.")
except Exception as e:
    msg.set_content("There is some problem or change in Regina Public Library's website. Sorry, I couldn't renew your books today. Please manually renew it.")
    with smtplib.SMTP(SMTP_server, 587) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender_email, sender_password)
        server.send_message(msg)

    print(f"❌ An error occurred: {type(e).__name__}: {e}")

    # URL and title only. Screenshots / page source are not saved because
    # they would contain your account page and logs/artifacts may be public.
    print(f"Current URL: {driver.current_url}")
    print(f"Page title: {driver.title}")
finally:
    driver.quit()

print("Task completed")