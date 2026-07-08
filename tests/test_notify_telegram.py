"""notify_telegram.sh must be a safe no-op when unconfigured — a dead-man's
switch that itself fails would take the cron wrapper down with it."""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "notify_telegram.sh"


def test_notify_is_noop_without_config(tmp_path):
    # No TELEGRAM_* env and a repo-less cwd copy of the script would still try
    # ../.env; run with an env that has neither var and assert clean exit 0.
    result = subprocess.run(
        ["bash", str(SCRIPT), "test message"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
