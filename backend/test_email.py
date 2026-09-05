"""Safe Zoho SMTP diagnostics and optional development test email.

Run from the project root:
    python -m backend.test_email
    python -m backend.test_email --to test@example.com

Credentials and OTP/session values are never printed.
"""

from __future__ import annotations

import argparse
import socket
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Config  # noqa: E402


def safe_error(error: BaseException) -> str:
    message = str(error) or error.__class__.__name__
    for secret in (Config.MAIL_PASSWORD, Config.MAIL_USERNAME):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def run(recipient: str | None = None) -> int:
    host, port = Config.MAIL_HOST, Config.MAIL_PORT
    print(f"SMTP HOST: {host}")
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        print(f"DNS RESOLUTION: OK ({len(addresses)} address(es))")
    except OSError as error:
        print(f"DNS RESOLUTION: FAILED\nReason: {safe_error(error)}")
        return 1

    smtp: smtplib.SMTP | None = None
    authenticated = False
    try:
        socket.create_connection((host, port), timeout=15).close()
        print("TCP CONNECTION: OK")
        smtp = smtplib.SMTP(host, port, timeout=15)
        smtp.ehlo()
        print("SMTP EHLO: OK")
        if Config.MAIL_USE_TLS:
            smtp.starttls()
            smtp.ehlo()
            print("STARTTLS: OK")
        smtp.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD)
        authenticated = True
        print("AUTHENTICATION: OK")
        if recipient:
            message = EmailMessage()
            message["Subject"] = "Moneda SMTP Test"
            message["From"] = f"{Config.MAIL_FROM_NAME} <{Config.MAIL_GENERAL_FROM}>"
            message["To"] = recipient
            message.set_content("This is a test email from Moneda Technologies.")
            smtp.send_message(message)
            print("MESSAGE SUBMISSION: OK")
            print("Email sent successfully")
        else:
            print("MESSAGE SUBMISSION: SKIPPED (pass --to test@example.com to send)")
        return 0
    except (OSError, smtplib.SMTPException, ValueError) as error:
        stage = "MESSAGE SUBMISSION" if recipient and authenticated else "SMTP SESSION"
        print(f"{stage}: FAILED\nReason: {safe_error(error)}\nEmail failed")
        return 1
    finally:
        if smtp:
            try:
                smtp.quit()
            except OSError:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose Zoho SMTP without printing credentials")
    parser.add_argument("--to", dest="recipient", help="Optional recipient for a real Moneda SMTP Test email")
    args = parser.parse_args()
    raise SystemExit(run(args.recipient))
