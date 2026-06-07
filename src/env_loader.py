#!/usr/bin/env python3
"""
BTC Genius — tiny .env loader (stdlib only).

Loads KEY=VALUE pairs from the project-root .env into os.environ at import
time of the calling script. Real environment variables always win (we use
setdefault), so launchd EnvironmentVariables / shell exports can override.

Usage:  from env_loader import load_env; load_env()
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"  # src/ -> project root


def load_env(path=ENV_FILE):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))
