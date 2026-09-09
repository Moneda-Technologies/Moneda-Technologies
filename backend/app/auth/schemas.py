from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class OtpRequest(BaseModel):
    email: EmailStr
    purpose: str = Field(pattern="^(login|signup|reset)$")


class OtpVerify(BaseModel):
    email: EmailStr
    purpose: str = Field(pattern="^(login|signup|reset)$")
    code: str = Field(pattern=r"^\d{6}$")


class PasswordLogin(BaseModel):
    """Credentials accepted by the workspace sign-in form.

    ``identifier`` intentionally covers both a human-friendly username and a
    persisted user id so the API can support admin-created accounts without
    forcing email addresses to be used as the login name.
    """

    identifier: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=200)


class Registration(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=5, max_length=30)
    password: str = Field(min_length=10, max_length=200)


class SignupStart(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    email: EmailStr
    previous_pending_signup_id: str | None = Field(default=None, max_length=120)


class SignupVerifyEmail(BaseModel):
    pending_signup_id: str = Field(min_length=1, max_length=120)
    otp: str = Field(pattern=r"^\d{6}$")


class SignupComplete(BaseModel):
    pending_signup_id: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=200)
    confirm_password: str = Field(min_length=8, max_length=200)
