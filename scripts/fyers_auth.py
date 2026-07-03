#!/usr/bin/env python3
"""Generate a FYERS v3 auth URL or exchange an auth code for an access token.

Usage:
  python scripts/fyers_auth.py auth-url
  python scripts/fyers_auth.py token --auth-code '<code or full redirect URL>'

Store the returned access token in .env as FYERS_ACCESS_TOKEN.
Never paste the token into chat.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from pprint import pprint
from typing import Any
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

load_dotenv()


def session() -> fyersModel.SessionModel:
    client_id = os.getenv("FYERS_CLIENT_ID")
    secret_key = os.getenv("FYERS_SECRET_KEY")
    redirect_uri = os.getenv("FYERS_REDIRECT_URI")
    missing = [k for k, v in {
        "FYERS_CLIENT_ID": client_id,
        "FYERS_SECRET_KEY": secret_key,
        "FYERS_REDIRECT_URI": redirect_uri,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing required env vars in .env: {', '.join(missing)}")
    return fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )


def mask(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def sanitized(obj: Any) -> Any:
    """Return a printable copy with token-like fields masked."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if "token" in key.lower() or key.lower() in {"secret", "secret_key"}:
                out[key] = mask(str(value)) if value else value
            else:
                out[key] = sanitized(value)
        return out
    if isinstance(obj, list):
        return [sanitized(value) for value in obj]
    return obj


def normalize_auth_code(value: str) -> str:
    """Accept either a raw FYERS auth code or the full localhost redirect URL."""
    stripped = value.strip()
    parsed = urlparse(stripped)
    if parsed.query:
        params = parse_qs(parsed.query)
        for key in ("auth_code", "code"):
            candidate = params.get(key, [None])[0]
            if candidate and candidate != "200":
                return candidate.strip()
    return stripped


def upsert_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    updated: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            updated.append(f"{key}={value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-url")
    token_p = sub.add_parser("token")
    auth_group = token_p.add_mutually_exclusive_group(required=True)
    auth_group.add_argument("--auth-code")
    auth_group.add_argument(
        "--auth-code-stdin",
        action="store_true",
        help="Read auth code from stdin so it is not exposed in shell history or process args",
    )
    token_p.add_argument(
        "--write-env",
        action="store_true",
        help="Write the returned access token to .env without printing it",
    )
    token_p.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    s = session()
    if args.cmd == "auth-url":
        print(s.generate_authcode())
    elif args.cmd == "token":
        auth_code = normalize_auth_code(input().strip() if args.auth_code_stdin else args.auth_code)
        s.set_token(auth_code)
        response = s.generate_token()
        access_token = response.get("access_token") if isinstance(response, dict) else None
        if args.write_env:
            if not access_token:
                print("No access_token returned; sanitized response follows:")
                pprint(sanitized(response))
                raise SystemExit(1)
            upsert_env_value(Path(args.env_file), "FYERS_ACCESS_TOKEN", str(access_token))
            print(
                f"FYERS_ACCESS_TOKEN updated in {args.env_file} "
                f"(masked={mask(str(access_token))}, len={len(str(access_token))})"
            )
        else:
            print("Sanitized FYERS token response; full token suppressed. Use --write-env to update .env safely.")
            pprint(sanitized(response))


if __name__ == "__main__":
    main()
