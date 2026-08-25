"""Audit log per design.md §4."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("crawler.audit")


class AuditLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self.found_passwords: dict[str, dict] = {}  # password -> first-seen info
        # §4.1: tally postponed-vector signal hits across the whole crawl, so
        # a completed run can report which postponed vectors are worth
        # promoting (nonzero hits) vs which never fired (zero hits).
        self.postponed_signal_counts: Counter[str] = Counter()
        # Which active §3 vectors actually produced a password match, and
        # how many times — useful alongside the postponed tally to see where
        # the real hits came from.
        self.active_vector_hit_counts: Counter[str] = Counter()
        # Discovered-but-never-fetched URLs leave no trace in `record()`'s
        # rows (those are only written for resources actually fetched), so
        # §2.4 trial-cap rejections were previously unauditable after the
        # fact — you'd have to reproduce the crawl to find out what got
        # skipped and why. Tallied by reason, with a bounded per-reason
        # sample of which URLs, so the log stays cheap even on a big crawl.
        self.skip_counts: Counter[str] = Counter()
        self._skip_samples: dict[str, list[str]] = {}
        self._SKIP_SAMPLE_LIMIT = 20

    def record(
        self,
        *,
        resource: str,
        vector: str,
        status,
        password_found: str = "",
        postponed_signals: list[str] | None = None,
    ) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource": resource,
            "vector": vector,
            "url_path": urlsplit(resource).path or "/",
            "status": status,
            "password_found": password_found,
            "postponed_signals": postponed_signals or [],
        }
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()
        for sig in row["postponed_signals"]:
            self.postponed_signal_counts[sig] += 1
        if password_found:
            if vector:
                self.active_vector_hit_counts[vector] += 1
            if password_found not in self.found_passwords:
                self.found_passwords[password_found] = {
                    "resource": resource,
                    "vector": vector,
                }
                logger.info("FOUND %s  (vector %s @ %s)", password_found, vector, resource)

    def record_many(
        self,
        resource: str,
        hits: list[tuple[str, list[str]]],  # [(vector, [passwords]), ...]
        status,
        postponed_signals: list[str] | None = None,
    ) -> None:
        if not hits:
            self.record(
                resource=resource, vector="", status=status,
                postponed_signals=postponed_signals,
            )
            return
        for vector, passwords in hits:
            if not passwords:
                self.record(
                    resource=resource, vector=vector, status=status,
                    postponed_signals=postponed_signals,
                )
            for pw in passwords:
                self.record(
                    resource=resource, vector=vector, status=status,
                    password_found=pw, postponed_signals=postponed_signals,
                )

    def record_skip(self, url: str, reason: str) -> None:
        """A discovered URL that was deliberately never fetched — e.g. §2.4's
        trial-cap rejection, off-origin, or already visited/enqueued under a
        different literal form. One compact row per skip (not one row per
        thing that *would* have been scanned), keyed by reason so a later
        pass can answer "how many, and which, links did §2.4 actually cut"
        without re-running the crawl.
        """
        self.skip_counts[reason] += 1
        samples = self._skip_samples.setdefault(reason, [])
        if len(samples) < self._SKIP_SAMPLE_LIMIT:
            samples.append(url)
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "skipped",
            "resource": url,
            "url_path": urlsplit(url).path or "/",
            "reason": reason,
        }
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def dump_remaining_queue(self, queue) -> None:
        """§2.3 — on hitting the visit cap, dump the remaining queue to the log."""
        remaining = list(queue)
        logger.warning("Visit cap reached — %d URLs left unvisited in queue", len(remaining))
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "visit_cap_reached",
            "remaining_queue_size": len(remaining),
            "remaining_queue": remaining,
        }
        self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def render_summary(
        self,
        *,
        visited_count: int,
        max_visits: int,
        section_counts: "Counter[str]",
        expected_password_count: int | None = None,
    ) -> str:
        """Human-readable end-of-run report: found passwords, per-section
        breakdown, and the §4.1 postponed-vector promotion tally."""
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append("CRAWL COMPLETE")
        lines.append("=" * 78)
        lines.append(f"Pages visited: {visited_count}/{max_visits}")
        found_n = len(self.found_passwords)
        expected = f"/{expected_password_count}" if expected_password_count else ""
        lines.append(f"Passwords found: {found_n}{expected}")
        for pw, info in self.found_passwords.items():
            lines.append(f"  {pw}  <- vector {info['vector']} @ {info['resource']}")
        if expected_password_count and found_n < expected_password_count:
            lines.append(
                f"[!] {expected_password_count - found_n} password(s) still missing — "
                "see the SECTION BREAKDOWN below for under-crawled sections, and "
                "the POSTPONED-VECTOR TALLY for candidates worth promoting."
            )

        lines.append("-" * 78)
        lines.append("SECTION BREAKDOWN — pages visited per top-level path segment")
        lines.append("-" * 78)
        for section, n in section_counts.most_common():
            lines.append(f"  {section:<20} {n}")

        lines.append("-" * 78)
        lines.append("ACTIVE-VECTOR HITS (§3) — which vectors actually produced a password")
        lines.append("-" * 78)
        if self.active_vector_hit_counts:
            for vector, n in sorted(self.active_vector_hit_counts.items()):
                lines.append(f"  {vector:<8} {n} hit(s)")
        else:
            lines.append("  (none)")

        lines.append("-" * 78)
        lines.append("SKIPPED LINKS — discovered but deliberately never fetched, by reason")
        lines.append("-" * 78)
        if self.skip_counts:
            for reason, n in self.skip_counts.most_common():
                lines.append(f"  {reason:<24} {n}")
                for sample_url in self._skip_samples.get(reason, [])[:3]:
                    lines.append(f"      e.g. {sample_url}")
        else:
            lines.append("  (nothing skipped)")

        lines.append("-" * 78)
        lines.append("POSTPONED-VECTOR TALLY (§4.1) — promote nonzero, drop zero")
        lines.append("-" * 78)
        if self.postponed_signal_counts:
            for vector, n in sorted(self.postponed_signal_counts.items()):
                lines.append(f"  {vector:<8} {n} signal hit(s)")
        else:
            lines.append("  (no postponed-vector signals observed)")
        lines.append("=" * 78)
        return "\n".join(lines)
