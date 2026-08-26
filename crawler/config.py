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

# §2.4 — pagination-style params get only this many trial values.
PAGINATION_TRIALS = 2

# Named pagination-style params get their own dedicated trial budget,
# isolated from decorative/tracking params (see PaginationLimiter's
# docstring) so neither can starve the other.
PAGINATION_PARAM_NAMES = {"page", "p", "pg", "offset"}

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

# A `<select>` in a form previously only ever got submitted with its first
# option (dummy_value() picked one value and stopped there) — any page only
# reachable via a *different* option value was silently never visited. Mirror
# §2.4's pagination-trial-cap spirit: try more than one value, but cap it so
# a form with several dropdowns can't explode into a huge submission fan-out.
MAX_SELECT_OPTION_TRIALS = 6

# §3.33 — GeoIP-gated pages. Confirmed (via a live proxy test) to do a real
# GeoIP lookup against the actual TCP source IP — not a client-supplied
# header, so there's no generic detection heuristic worth writing (a 403
# with region-flavored body text is easy to misidentify on other sites).
# Hardcoded by URL path on purpose: this is a known, specific dead end on
# *this* target, not a general capability the crawler should try everywhere.
GEO_BYPASS_PATHS = {"/status/eu-region/"}
GEO_BYPASS_PROXY = os.environ.get("GEO_BYPASS_PROXY", "").strip()

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
