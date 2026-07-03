from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import fyers_auth


def test_normalize_auth_code_accepts_plain_code() -> None:
    assert fyers_auth.normalize_auth_code("abc.def.ghi") == "abc.def.ghi"


def test_normalize_auth_code_extracts_auth_code_from_redirect_url() -> None:
    url = "http://127.0.0.1:8080/?s=ok&code=200&auth_code=abc.def.ghi&state=None"
    assert fyers_auth.normalize_auth_code(url) == "abc.def.ghi"


def test_normalize_auth_code_extracts_code_when_auth_code_missing() -> None:
    url = "http://127.0.0.1:8080/?s=ok&code=abc.def.ghi&state=None"
    assert fyers_auth.normalize_auth_code(url) == "abc.def.ghi"
