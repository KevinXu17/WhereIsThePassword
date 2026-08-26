#!/usr/bin/env python3
"""Entry point for the design.md crawler.

Usage:
    python main.py
    python main.py --max-visits 10 --output-dir output/smoke_test
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from urllib.parse import urlsplit

from crawler.config import (
    TARGET_URL, AUTH_USERNAME, OUTPUT_DIR, PAGINATION_TRIALS,
    EXPECTED_PASSWORD_COUNT, MAX_VISITS,
)
from crawler.crawl import Crawler
from crawler.patterns import PASSWORD_RE


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-visits", type=int, default=None,
        help="Override design.md §2.2's 500-visit cap (useful for smoke tests).",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Write audit_log.jsonl / crawl.log / found_passwords.json here. "
             "Default: a fresh timestamped folder under ./output/runs/ every "
             "time, so past runs are never overwritten and stay around for "
             "debugging/review.",
    )
    return parser.parse_args()


def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Windows consoles often default to a legacy codepage that can't encode
    # the §/—/etc. characters used throughout our log messages, silently
    # mangling them to "?" — force UTF-8 on stdout so console output matches
    # what actually lands in crawl.log.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(output_dir / "crawl.log", encoding="utf-8"),
        ],
        force=True,
    )


def _fresh_run_dir() -> Path:
    """A new, never-before-used folder per run — so rerunning the crawl
    never clobbers a previous run's audit_log.jsonl/crawl.log, which are
    needed intact for debugging and reviewing past results."""
    base = OUTPUT_DIR / "runs"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = base / stamp
    suffix = 2
    while candidate.exists():  # same-second reruns get -2, -3, ...
        candidate = base / f"{stamp}-{suffix}"
        suffix += 1
    return candidate


async def main() -> None:
    args = parse_args()
    output_dir = args.output_dir or _fresh_run_dir()
    setup_logging(output_dir)
    logger = logging.getLogger("main")
    effective_max_visits = args.max_visits if args.max_visits is not None else MAX_VISITS

    logger.info("Starting crawl of %s  (scope: %s)", TARGET_URL, urlsplit(TARGET_URL).netloc)
    logger.info("Auth: %s / (basic auth configured on browser context)", AUTH_USERNAME)
    logger.info("Target pattern: %s", PASSWORD_RE.pattern)
    logger.info(
        "Visit cap: %d  |  query-string trial cap: %d distinct variant(s) per path, "
        "shared across all params (page/list/p/v/ref/... alike)",
        effective_max_visits, PAGINATION_TRIALS,
    )

    crawler = Crawler(
        max_visits=args.max_visits,
        audit_log_path=output_dir / "audit_log.jsonl",
    )
    await crawler.run()

    summary = crawler.audit.render_summary(
        visited_count=len(crawler.visited),
        max_visits=crawler.max_visits,
        section_counts=crawler.section_counts,
        expected_password_count=EXPECTED_PASSWORD_COUNT,
    )
    for line in summary.splitlines():
        logger.info(line)

    found_passwords_path = output_dir / "found_passwords.json"
    found_passwords_path.write_text(
        json.dumps(crawler.audit.found_passwords, indent=2), encoding="utf-8"
    )
    logger.info("Audit log: %s", output_dir / "audit_log.jsonl")
    logger.info("Found-passwords summary: %s", found_passwords_path)

    # Convenience pointer to the most recent run — doesn't replace any run's
    # own files, just makes "where did that last run go" a one-line lookup.
    try:
        (OUTPUT_DIR / "latest.txt").write_text(str(output_dir.resolve()), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
