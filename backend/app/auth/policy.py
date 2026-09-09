"""Authentication policy helpers shared by public signup endpoints."""

from __future__ import annotations

from collections.abc import Iterable

from email_validator import EmailNotValidError, validate_email


SIGNUP_EMAIL_DOMAIN_MESSAGE = "Kindly use your Moneda Technologies company email address."
SIGNUP_PASSWORD_POLICY_MESSAGE = "Password must be at least 8 characters and include 1 uppercase letter, 1 lowercase letter, and 1 number."


def allowed_signup_domains(configured: object) -> set[str]:
    if isinstance(configured, str):
        values: Iterable[object] = configured.split(",")
    elif isinstance(configured, Iterable):
        values = configured
    else:
        values = ()
    return {str(value).strip().lower() for value in values if str(value).strip()}


def normalize_signup_email(email: object) -> str | None:
    try:
        return str(validate_email(str(email or "").strip(), check_deliverability=False).normalized).lower()
    except (EmailNotValidError, ValueError, TypeError):
        return None


def is_allowed_signup_email(email: object, configured_domains: object) -> bool:
    normalized = normalize_signup_email(email)
    if not normalized or "@" not in normalized:
        return False
    return normalized.rsplit("@", 1)[1] in allowed_signup_domains(configured_domains)


def is_valid_signup_password(password: object) -> bool:
    value = str(password or "")
    return (
        len(value) >= 8
        and any(character.isupper() for character in value)
        and any(character.islower() for character in value)
        and any(character.isdigit() for character in value)
    )
