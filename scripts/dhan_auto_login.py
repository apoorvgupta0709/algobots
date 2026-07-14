#!/usr/bin/env python3
"""
DhanHQ Auto Login — headless JWT renewal via PIN + TOTP.

Usage:
  # One-shot login (generates a fresh token)
  uv run python scripts/dhan_auto_login.py

  # Check expiry without logging in
  uv run python scripts/dhan_auto_login.py --check

  # Force renewal even if token is still fresh
  uv run python scripts/dhan_auto_login.py --force

Requirements (in .env):
  DHAN_CLIENT_ID      — Dhan client ID (e.g. 1112485968)
  DHAN_PIN            — Dhan 4-digit trading PIN
  DHAN_TOTP_SECRET    — Base32 TOTP secret from authenticator app
  DHAN_API_TOKEN      — (auto-populated by this script)

How TOTP works:
  - You provide the BASE32 secret (from Google Authenticator / Authy).
  - The script derives the current 6-digit OTP using ``pyotp.TOTP()``.
  - No manual OTP entry needed at cron time.

Environment (loaded from .env in repo root, or from OS env vars):
  DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET — required.
  Optionally: DHAN_API_TOKEN (existing token to check expiry).

Failures:
  - Journaled to stderr (syslog-friendly) with a high-exit code for cron alerts.
  - Never logs the actual token, PIN, TOTP, or secret to stdout/stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

# --------------------------------------------------------------------------- helpers

HERE = Path(__file__).parent.resolve()
REPO = HERE.parent

# Dhan's token-renew endpoint (works up to ~6h before expiry)
RENEW_URL = "https://api.dhan.co/v2/RenewToken"


def _warn(msg: str) -> None:
    print(f"[dhan_auto_login] WARNING: {msg}", file=sys.stderr)


def _die(msg: str, exit_code: int = 1) -> None:
    print(f"[dhan_auto_login] ERROR: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def _load_env() -> dict[str, str]:
    """Read required vars from .env or os.environ."""
    env_file = REPO / ".env"
    env: dict[str, str] = {}

    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()

    # OS env overrides .env
    for key in ("DHAN_CLIENT_ID", "DHAN_API_TOKEN", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        os_val = os.environ.get(key)
        if os_val:
            env[key] = os_val

    return env


def _decode_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload without verification (we only need exp/iat)."""
    try:
        payload_b64 = token.split(".")[1]
        # Pad for base64
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(__import__("base64").urlsafe_b64decode(payload_b64))
        return payload
    except Exception:
        return None


def _get_token_expiry(token: str) -> float | None:
    """Return expiry UNIX timestamp from JWT, or None if unreadable."""
    payload = _decode_jwt_payload(token)
    if payload and "exp" in payload:
        return float(payload["exp"])
    return None


def _write_dotenv(key: str, value: str) -> None:
    """Write or update a single key=value in .env (no secrets in output)."""
    env_file = REPO / ".env"
    lines = env_file.read_text().splitlines(keepends=True) if env_file.exists() else []

    # Separate key=value lines from everything else
    new_lines: list[str] = []
    updated = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"{key}={value}\n")

    env_file.write_text("".join(new_lines))


# --------------------------------------------------------------------------- main flow

def check_token_health(token: str, min_hours: float = 2.0) -> bool:
    """Return True if token is valid and has >= min_hours remaining."""
    exp_ts = _get_token_expiry(token)
    if exp_ts is None:
        _warn("Cannot read token expiry — will re-authenticate.")
        return False
    remaining = exp_ts - time.time()
    if remaining <= 0:
        _warn(f"DHAN_API_TOKEN expired {abs(remaining)/60:.0f}m ago — needs renewal.")
        return False
    if remaining < min_hours * 3600:
        _warn(f"DHAN_API_TOKEN expires in {remaining/3600:.1f}h (< {min_hours}h threshold) — will renew.")
        return False
    print(f"[dhan_auto_login] Token OK: expires in {remaining/3600:.1f}h.")
    return True


def try_renew_token(client_id: str, token: str) -> str | None:
    """Try the /RenewToken endpoint. Returns new token or None if unsupported."""
    req = Request(
        RENEW_URL,
        headers={
            "access-token": token,
            "dhanClientId": client_id,
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        new_token = body.get("accessToken") or body.get("data", {}).get("accessToken")
        if new_token:
            print(f"[dhan_auto_login] Renewed via /RenewToken endpoint.")
            return new_token
        _warn(f"RenewToken returned OK but no accessToken in: {body}")
        return None
    except Exception as exc:
        _warn(f"RenewToken failed ({exc}) — falling back to PIN+TOTP login.")
    return None


def login_via_pin_totp(client_id: str, pin: str, totp_secret: str) -> str:
    """
    Headless login via dhanhq's DhanLogin.generate_token.
    Falls back to raw HTTP if dhanhq is not installed.
    """
    # Prefer dhanhq library if available
    try:
        import dhanhq
        login = dhanhq.DhanLogin(client_id)
        import pyotp
        totp_code = pyotp.TOTP(totp_secret).now()
        result = login.generate_token(pin=pin, totp=totp_code)
        new_token = result.get("accessToken")
        if new_token:
            return new_token
        _warn(f"generate_token returned but missing accessToken: {result}")
        raise ValueError("No accessToken in response")
    except ImportError:
        _warn("dhanhq not installed, using raw HTTP fallback.")
    except Exception as exc:
        _warn(f"dhanhq login failed ({exc}), falling back to raw HTTP.")

    # Raw HTTP fallback (same endpoint, no dhanhq dependency)
    import pyotp
    import urllib.request

    totp_code = pyotp.TOTP(totp_secret).now()
    params = f"dhanClientId={client_id}&pin={pin}&totp={totp_code}"
    url = f"https://auth.dhan.co/app/generateAccessToken?{params}"

    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        _die(f"PIN+TOTP login failed: {exc}")

    new_token = body.get("accessToken")
    if not new_token:
        _die(f"PIN+TOTP response missing accessToken: {body}")
    return new_token


def main() -> None:
    env = _load_env()
    client_id = env.get("DHAN_CLIENT_ID")
    pin = env.get("DHAN_PIN")
    totp_secret = env.get("DHAN_TOTP_SECRET")
    existing_token = env.get("DHAN_API_TOKEN", "")

    # Validate required fields
    if not client_id:
        _die("DHAN_CLIENT_ID is not set in .env")
    if not pin:
        _die("DHAN_PIN is not set in .env — add your 4-digit Dhan trading PIN")
    if not totp_secret:
        _die("DHAN_TOTP_SECRET is not set in .env — add your base32 TOTP seed")

    # CLI flags
    args = set(sys.argv[1:])
    check_only = "--check" in args
    force = "--force" in args

    if check_only:
        if existing_token:
            exp_ts = _get_token_expiry(existing_token)
            if exp_ts:
                remaining = exp_ts - time.time()
                if remaining > 0:
                    print(f"Token expires at {datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ({remaining/3600:.1f}h left)")
                    return
                print("Token has EXPIRED.")
                return
            print("Cannot decode token expiry.")
            return
        print("No token found in .env.")
        return

    # Step 1: try renewal first (if token exists and not forced)
    new_token = None
    if existing_token and not force:
        if check_token_health(existing_token, min_hours=2.0):
            print("[dhan_auto_login] Token still healthy — no action needed.")
            return
        new_token = try_renew_token(client_id, existing_token)

    # Step 2: if renewal didn't work, use PIN+TOTP
    if not new_token:
        new_token = login_via_pin_totp(client_id, pin, totp_secret)

    # Step 3: write token to .env and verify
    _write_dotenv("DHAN_API_TOKEN", new_token)

    # Verify
    exp_ts = _get_token_expiry(new_token)
    if exp_ts:
        remaining = exp_ts - time.time()
        expires_str = datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"[dhan_auto_login] ✅ Token renewed — expires {expires_str} ({remaining/3600:.1f}h).")
    else:
        print("[dhan_auto_login] ✅ Token renewed (expiry unreadable from JWT).")

    # Step 4: quick smoke-test the new token (optional)
    try:
        req = Request(
            "https://api.dhan.co/v2/profile",
            headers={"access-token": new_token, "dhanClientId": client_id},
            method="GET",
        )
        with urlopen(req, timeout=10) as resp:
            profile = json.loads(resp.read().decode())
        print(f"[dhan_auto_login] ✅ Token verified — profile: {profile.get('name', 'OK')}")
    except Exception as exc:
        _warn(f"Token smoke-test failed: {exc}")


if __name__ == "__main__":
    main()