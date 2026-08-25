"""Configuration and constants, loaded from .env (see design.md §5)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TARGET_URL = os.environ.get("TARGET_URL", "").rstrip("/")
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

if not TARGET_URL:
    raise SystemExit("TARGET_URL is not set — copy .env.example to .env and fill it in.")

# §2.2 — cap total visits to avoid unbounded/infinite crawling.
MAX_VISITS = 500

# §2.4 — pagination-style params get only this many trial values. Applied to
# every query param generically (see PaginationLimiter), not just literal
# pagination names — see its docstring for why.
PAGINATION_TRIALS = 2

# Only used for the human-readable end-of-run summary ("N/8 passwords
# found") — the challenge homepage states this count outright. Not used for
# any control flow; crawling always continues to the visit cap regardless.
EXPECTED_PASSWORD_COUNT = 8

# Navigation / interaction timeouts (ms).
NAV_TIMEOUT_MS = 20_000
ACTION_TIMEOUT_MS = 5_000
POST_LOAD_SETTLE_MS = 400
MAX_SCROLL_ROUNDS = 6
MAX_CLICK_CANDIDATES_PER_PAGE = 25
MAX_FORMS_PER_PAGE = 15

OUTPUT_DIR = ROOT / "output"
AUDIT_LOG_PATH = OUTPUT_DIR / "audit_log.jsonl"
FOUND_PASSWORDS_PATH = OUTPUT_DIR / "found_passwords.json"
SCREENSHOTS_DIR = OUTPUT_DIR / "screenshots"

import base64

AUTH_HEADER = {
    "Authorization": "Basic "
    + base64.b64encode(f"{AUTH_USERNAME}:{AUTH_PASSWORD}".encode()).decode()
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WhereIsThePasswordCrawler/1.0 "
    "(authorized security assessment; contact: kevinfortsui@gmail.com)"
)
