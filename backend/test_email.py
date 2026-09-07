"""Safe Zoho Mail OAuth/API diagnostics and optional real test delivery."""

from __future__ import annotations

import argparse

from email_validator import EmailNotValidError, validate_email

from app import create_app
from app.communication.email import EmailDeliveryError


def main(recipient: str | None) -> int:
    app = create_app()
    with app.app_context():
        status = app.extensions["zoho_oauth"].status()
        print("EMAIL PROVIDER: zoho_mail_api")
        print(f"CONFIGURATION: {'VALID' if status['configured'] else 'INVALID'}")
        print(f"OAUTH: {'CONNECTED' if status['connected'] else 'NOT CONNECTED'}")
        print(f"ACCOUNT: {status.get('account_email') or 'unknown'}")
        print(f"ACCOUNT ID: {'CONFIGURED' if status['account_id_configured'] else 'UNKNOWN'}")
        print(f"API DOMAIN: {status.get('api_domain') or 'UNKNOWN'}")
        if not recipient:
            print("TEST EMAIL: NOT REQUESTED")
            return 0 if status["configured"] else 1
        try:
            normalized = validate_email(recipient, check_deliverability=False).normalized
        except EmailNotValidError:
            print("TEST EMAIL: FAILED")
            print("STAGE: recipient_validation")
            return 2
        if not status["connected"]:
            print("TEST EMAIL: FAILED")
            print("STAGE: oauth_configuration")
            print("ERROR: OAUTH_NOT_CONNECTED")
            return 3
        try:
            result = app.extensions["email_service"].send_test_email(
                to=[normalized], request_id="zoho-cli-test",
            )
        except EmailDeliveryError as exc:
            print("TEST EMAIL: FAILED")
            print(f"STAGE: {exc.stage}")
            print(f"ERROR: {exc.error_code}")
            print(f"DIAGNOSTIC ID: {exc.diagnostic_id}")
            return 4
        if result.get("provider") == "recording":
            print("TEST EMAIL: RECORDED IN TEST MODE (NOT EXTERNALLY DELIVERED)")
        else:
            print("TEST EMAIL: SUBMITTED TO ZOHO MAIL API")
        for check in result.get("checks", []):
            print(f"{check['stage']}: {check['result']}")
        print(f"DIAGNOSTIC ID: {result.get('diagnostic_id') or 'not-applicable'}")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose Zoho Mail OAuth without printing credentials")
    parser.add_argument("--to", dest="recipient", help="Send exactly one real test message through the Zoho Mail API")
    args = parser.parse_args()
    raise SystemExit(main(args.recipient))
