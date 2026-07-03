"""Headless Fyers login: TOTP + PIN flow, no browser, no ``input()``.

Uses the community-standard undocumented ``api-t2.fyers.in/vagator`` endpoints
to obtain an auth code non-interactively, then exchanges it through the
official ``fyers_apiv3`` SessionModel for a day access token.

Tokens are cached in the ``auth_tokens`` table and reused while fresh.
SECURITY: access tokens, PINs and OTPs are never logged or journalled.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import os
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp

from algobot.core.exceptions import AuthError
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import AuthTokenRow, EventLogRow

log = logging.getLogger(__name__)

_TIMEOUT = 15.0
_URL_SEND_OTP = "https://api-t2.fyers.in/vagator/v2/send_login_otp_v2"
_URL_VERIFY_OTP = "https://api-t2.fyers.in/vagator/v2/verify_otp"
_URL_VERIFY_PIN = "https://api-t2.fyers.in/vagator/v2/verify_pin_v2"
_URL_TOKEN = "https://api-t1.fyers.in/api/v3/token"

_REQUIRED_ENV = ("client_id", "secret_key", "redirect_uri", "FY_ID", "TOTP_KEY", "PIN")


# --------------------------------------------------------------------------- helpers
def _journal_error(step: str, message: str, detail: dict | None = None) -> None:
    """Write an auth failure to event_log. Never raises; never includes secrets."""
    try:
        with session_scope() as s:
            s.add(EventLogRow(level="error", source="fyers_auth",
                              message=f"{step}: {message}", detail_json=detail or {}))
    except Exception:  # journalling must never mask the real failure
        log.exception("failed to journal fyers_auth error for step %s", step)


def _env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise AuthError(
            f"Missing environment variable '{key}'. Copy .env.example to .env and "
            f"fill in the Fyers credentials ({', '.join(_REQUIRED_ENV)}).")
    return val


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def _post_json(step: str, url: str, payload: dict,
               headers: dict[str, str] | None = None) -> dict[str, Any]:
    """POST json with a 15s timeout; journal + raise AuthError on any failure."""
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        _journal_error(step, f"network error: {type(exc).__name__}")
        raise AuthError(
            f"Fyers login step '{step}' failed with a network error ({exc}). "
            "Check internet connectivity / proxy and retry.") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or (isinstance(body, dict) and body.get("s") == "error"):
        msg = body.get("message", "") if isinstance(body, dict) else ""
        _journal_error(step, f"HTTP {resp.status_code}: {msg}",
                       {"status_code": resp.status_code})
        raise AuthError(
            f"Fyers login step '{step}' failed (HTTP {resp.status_code}): {msg or 'no detail'}. "
            "Verify FY_ID / TOTP_KEY / PIN / client_id in .env are current.")
    return body


# --------------------------------------------------------------------------- flow steps
def _send_login_otp(fy_id: str) -> str:
    """Step a: request an OTP session -> request_key."""
    body = _post_json("send_login_otp", _URL_SEND_OTP,
                      {"fy_id": _b64(fy_id), "app_id": "2"})
    request_key = body.get("request_key")
    if not request_key:
        _journal_error("send_login_otp", "no request_key in response")
        raise AuthError("Fyers send_login_otp returned no request_key. "
                        "Check FY_ID in .env (format XX00000).")
    return request_key


def _verify_totp(request_key: str, totp_key: str) -> str:
    """Step b: submit the current TOTP -> request_key (retry once next window)."""
    totp = pyotp.TOTP(totp_key)
    try:
        body = _post_json("verify_otp", _URL_VERIFY_OTP,
                          {"request_key": request_key, "otp": totp.now()})
    except AuthError:
        # OTP may have expired mid-flight: wait for the next 30s window and retry once.
        wait = 30 - (time.time() % 30) + 1
        log.info("TOTP verify failed; retrying in %.0fs with next OTP window", wait)
        time.sleep(wait)
        body = _post_json("verify_otp(retry)", _URL_VERIFY_OTP,
                          {"request_key": request_key, "otp": totp.now()})
    request_key = body.get("request_key")
    if not request_key:
        _journal_error("verify_otp", "no request_key in response")
        raise AuthError("Fyers verify_otp returned no request_key. "
                        "Check TOTP_KEY (base32 secret from Fyers 2FA setup).")
    return request_key


def _verify_pin(request_key: str, pin: str) -> str:
    """Step c: submit the trading PIN -> temporary access token."""
    body = _post_json("verify_pin", _URL_VERIFY_PIN,
                      {"request_key": request_key, "identity_type": "pin",
                       "identifier": _b64(pin)})
    data = body.get("data", body)  # handle both {"data": {...}} and top-level shapes
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        _journal_error("verify_pin", "no access_token in response")
        raise AuthError("Fyers verify_pin returned no access token. "
                        "Check PIN in .env (4-digit trading pin).")
    return token


def _get_auth_code(temp_token: str, fy_id: str, client_id: str, redirect_uri: str) -> str:
    """Step d: trade the temp token for an OAuth auth_code."""
    app_id, _, app_type = client_id.partition("-")
    payload = {
        "fyers_id": fy_id,
        "app_id": app_id,
        "redirect_uri": redirect_uri,
        "appType": app_type,
        "code_challenge": "",
        "state": "None",
        "scope": "",
        "nonce": "",
        "response_type": "code",
        "create_cookie": True,
    }
    body = _post_json("token", _URL_TOKEN, payload,
                      headers={"Authorization": f"Bearer {temp_token}"})
    url = body.get("Url") or body.get("url") or ""
    auth_code = (parse_qs(urlparse(url).query).get("auth_code") or [None])[0]
    if not auth_code:
        _journal_error("token", "no auth_code in redirect Url")
        raise AuthError("Fyers /api/v3/token returned no auth_code. Check that "
                        "client_id and redirect_uri in .env match your API app "
                        "at https://myapi.fyers.in.")
    return auth_code


def _exchange_auth_code(auth_code: str, client_id: str, secret_key: str,
                        redirect_uri: str) -> str:
    """Step e: SessionModel auth-code -> final access token."""
    from fyers_apiv3.fyersModel import SessionModel

    session = SessionModel(client_id=client_id, secret_key=secret_key,
                           redirect_uri=redirect_uri, response_type="code",
                           grant_type="authorization_code")
    session.set_token(auth_code)
    try:
        resp = session.generate_token()
    except Exception as exc:
        _journal_error("generate_token", f"exception: {type(exc).__name__}")
        raise AuthError(f"Fyers generate_token raised {type(exc).__name__}: {exc}. "
                        "Verify secret_key in .env.") from exc
    token = (resp or {}).get("access_token")
    if not token:
        _journal_error("generate_token", str((resp or {}).get("message", "no access_token")))
        raise AuthError("Fyers generate_token returned no access_token: "
                        f"{(resp or {}).get('message', 'no detail')}. "
                        "Verify secret_key / redirect_uri in .env.")
    return token


# --------------------------------------------------------------------------- public API
def login() -> str:
    """Run the full headless login flow and return a fresh access token."""
    client_id = _env("client_id")
    fy_id = _env("FY_ID")

    request_key = _send_login_otp(fy_id)
    request_key = _verify_totp(request_key, _env("TOTP_KEY"))
    temp_token = _verify_pin(request_key, _env("PIN"))
    auth_code = _get_auth_code(temp_token, fy_id, client_id, _env("redirect_uri"))
    token = _exchange_auth_code(auth_code, client_id, _env("secret_key"),
                                _env("redirect_uri"))
    log.info("Fyers headless login succeeded for app %s", client_id.partition("-")[0])
    return token


def get_access_token(max_age_hours: int = 12) -> str:
    """Return a cached access token if fresh, else login and persist a new one."""
    init_db()
    now = dt.datetime.utcnow()
    with session_scope() as s:
        row = s.get(AuthTokenRow, "fyers")
        if row is not None and row.issued_at is not None:
            age = now - row.issued_at
            if age < dt.timedelta(hours=max_age_hours):
                log.debug("Reusing cached Fyers token (age %.1fh)",
                          age.total_seconds() / 3600)
                return row.access_token

    token = login()
    with session_scope() as s:
        row = s.get(AuthTokenRow, "fyers")
        expires = now + dt.timedelta(hours=max_age_hours)
        if row is None:
            s.add(AuthTokenRow(broker="fyers", access_token=token,
                               issued_at=now, expires_at=expires))
        else:
            row.access_token = token
            row.issued_at = now
            row.expires_at = expires
    return token


def get_fyers_client():
    """Authenticated synchronous ``FyersModel`` client."""
    from fyers_apiv3.fyersModel import FyersModel

    return FyersModel(client_id=_env("client_id"), token=get_access_token(),
                      is_async=False, log_path="")
