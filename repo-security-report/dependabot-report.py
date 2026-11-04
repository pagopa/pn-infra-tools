#!/usr/bin/env python3
"""Generate a GitHub security report for a list of repositories.

Covers:
  - Dependabot vulnerability alerts (grouped by severity)
  - Code scanning alerts / CodeQL (grouped by severity)
  - Secret scanning alerts (active count)
  - Open pull requests from a Renovate bot user

Can publish a static Confluence page (every repository, no filters) and attach
a CSV of every individual security issue.

Usage:
  ./dependabot-report.sh [OPTIONS] [owner/repo ...]
  python3 dependabot-report.py [OPTIONS] [owner/repo ...]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, TextIO
from urllib import error, parse, request
from report_csv import (
    ISSUE_COLUMNS, LEVELS, SUMMARY_COLUMNS,
    build_report, load_csv_report, write_metadata,
)

from confluence_publisher import (
    DEFAULT_CONFLUENCE_URL,
    DEFAULT_SPACE_KEY,
    ConfluenceConfig,
    build_report_storage,
    publish_report,
)

ALLOWED_STATES = {"open", "dismissed", "fixed", "auto_dismissed"}
API_BASE = "https://api.github.com"
DEFAULT_RENOVATE_USER = "renovate-pagopa"

# Map code-scanning rule.severity to standard levels when security_severity_level is absent
_CODE_SCAN_FALLBACK: dict[str, str] = {
    "error": "HIGH",
    "warning": "MEDIUM",
    "note": "LOW",
    "recommendation": "LOW",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SeverityCounts:
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    unknown: int = 0

    @property
    def total(self) -> int:
        return self.critical + self.high + self.medium + self.low + self.unknown

    def to_dict(self) -> dict:
        return {
            "critical": self.critical,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "unknown": self.unknown,
            "total": self.total,
        }

    def __add__(self, other: "SeverityCounts") -> "SeverityCounts":
        return SeverityCounts(
            critical=self.critical + other.critical,
            high=self.high + other.high,
            medium=self.medium + other.medium,
            low=self.low + other.low,
            unknown=self.unknown + other.unknown,
        )


@dataclass
class RepoReport:
    repository: str
    dependabot: SeverityCounts
    code_scanning: SeverityCounts
    secret_scanning: int
    renovate_prs: int

    @property
    def total(self) -> int:
        return (
            self.dependabot.total
            + self.code_scanning.total
            + self.secret_scanning
            + self.renovate_prs
        )

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "dependabot": self.dependabot.to_dict(),
            "code_scanning": self.code_scanning.to_dict(),
            "secret_scanning": self.secret_scanning,
            "renovate_prs": self.renovate_prs,
            "total": self.total,
        }


@dataclass
class SecurityIssue:
    source: str
    repository: str
    number: str
    severity: str
    state: str
    title: str
    identifier: str
    location: str
    created_at: str
    url: str
    extra: str

    def to_csv_row(self) -> list[str]:
        return [
            self.source,
            self.repository,
            self.number,
            self.severity,
            self.state,
            self.title,
            self.identifier,
            self.location,
            self.created_at,
            self.url,
            self.extra,
        ]


ISSUES_CSV_HEADER = ISSUE_COLUMNS


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[INFO]  {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"[WARN]  {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_env_file(path: str | None = None) -> Path | None:
    """Load KEY=VALUE pairs into os.environ without overriding existing vars.

    Resolution order when path is omitted:
      1. .env in the current working directory
      2. .env next to this script
    """
    if path:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            print(f"[ERROR] Env file not found: {candidate}", file=sys.stderr)
            sys.exit(1)
        candidates = [candidate]
    else:
        script_env = Path(__file__).resolve().parent / ".env"
        cwd_env = Path.cwd() / ".env"
        candidates = []
        seen: set[Path] = set()
        for item in (cwd_env, script_env):
            resolved = item.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(item)

    for candidate in candidates:
        if not candidate.is_file():
            continue
        with candidate.open(encoding="utf-8") as handle:
            for raw in handle:
                parsed = _parse_env_line(raw)
                if not parsed:
                    continue
                key, value = parsed
                os.environ.setdefault(key, value)
        return candidate
    return None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate GitHub security report grouped by severity.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("repos", nargs="*", help="Repository in owner/repo format")
    parser.add_argument(
        "-f", "--file", dest="repos_file",
        help="Read repository list from FILE (one owner/repo per line)",
    )
    parser.add_argument(
        "-o", "--output", default="table", choices=["table", "csv", "json"],
        help="Output format: table (default), csv, json",
    )
    parser.add_argument(
        "-s", "--state", default="open",
        help="Alert state: open (default), dismissed, fixed, auto_dismissed",
    )
    parser.add_argument(
        "--renovate-user", default=DEFAULT_RENOVATE_USER, dest="renovate_user",
        help=f"GitHub login prefix for Renovate bot (default: {DEFAULT_RENOVATE_USER})",
    )
    parser.add_argument(
        "--out-file", dest="out_file",
        help="Write report to FILE instead of stdout",
    )
    parser.add_argument(
        "--env-file", dest="env_file",
        help="Load environment variables from FILE (default: ./.env or script-dir/.env)",
    )
    parser.add_argument(
        "--issues-csv", dest="issues_csv",
        help="Write a detailed CSV of every security issue to FILE",
    )
    parser.add_argument(
        "--input-csv",
        help="Reuse a summary or detailed CSV without querying GitHub",
    )
    parser.add_argument(
        "--confluence", action="store_true",
        help="Publish the report to Confluence and attach the detailed CSV",
    )
    parser.add_argument(
        "--confluence-url", dest="confluence_url", default=None,
        help=f"Confluence API URL (default: {DEFAULT_CONFLUENCE_URL} or CONFLUENCE_URL)",
    )
    parser.add_argument(
        "--confluence-cloud-id", dest="confluence_cloud_id", default=None,
        help="Confluence Cloud ID (or CONFLUENCE_CLOUD_ID)",
    )
    parser.add_argument(
        "--confluence-space", dest="confluence_space", default=None,
        help=f"Confluence space key (default: {DEFAULT_SPACE_KEY} or CONFLUENCE_SPACE)",
    )
    parser.add_argument(
        "--confluence-user", dest="confluence_user", default=None,
        help="Confluence user email (or CONFLUENCE_USER / CONFLUENCE_EMAIL)",
    )
    parser.add_argument(
        "--confluence-token", dest="confluence_token", default=None,
        help="Confluence API token (or CONFLUENCE_API_TOKEN / CONFLUENCE_API_KEY)",
    )
    parser.add_argument(
        "--confluence-parent-id", dest="confluence_parent_id", default=None,
        help="Optional parent page id (or CONFLUENCE_PARENT_ID)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Repository loading
# ---------------------------------------------------------------------------

def load_repositories(cli_repos: list[str], repos_file: str | None) -> list[str]:
    repos: list[str] = list(cli_repos)

    if repos_file:
        try:
            with open(repos_file, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    repos.append(line)
        except FileNotFoundError:
            print(f"[ERROR] File not found: {repos_file}", file=sys.stderr)
            sys.exit(1)

    seen: set[str] = set()
    deduplicated: list[str] = []
    for repo in repos:
        if repo not in seen:
            seen.add(repo)
            deduplicated.append(repo)
    return deduplicated


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _parse_link_header(link_header: str | None) -> dict[str, str]:
    if not link_header:
        return {}
    links: dict[str, str] = {}
    for part in link_header.split(","):
        section = part.strip()
        if ";" not in section:
            continue
        url_part, meta_part = section.split(";", 1)
        url = url_part.strip().strip("<>")
        rel = ""
        for meta in meta_part.split(";"):
            meta = meta.strip()
            if meta.startswith("rel="):
                rel = meta.split("=", 1)[1].strip('"')
                break
        if rel:
            links[rel] = url
    return links


def _github_get_json(url: str, token: str) -> tuple[object, dict[str, str]]:
    req = request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dependabot-report-script",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            charset = resp.headers.get_content_charset("utf-8")
            body = resp.read().decode(charset)
            payload = json.loads(body) if body else []
            headers = {k: v for k, v in resp.headers.items()}
            return payload, headers
    except error.HTTPError as exc:
        details = ""
        try:
            charset = exc.headers.get_content_charset("utf-8") if exc.headers else "utf-8"
            raw = exc.read().decode(charset)
            parsed = json.loads(raw)
            details = str(parsed.get("message") or "")
        except Exception:
            pass
        message = f"HTTP {exc.code}" + (f": {details}" if details else "")
        raise RuntimeError(message) from exc
    except error.URLError as exc:
        raise RuntimeError(f"Connection error: {exc.reason}") from exc


def _iter_paginated(base_url: str, token: str) -> Iterable[dict]:
    """Iterate over all pages of a GitHub list endpoint."""
    next_url: str | None = base_url
    while next_url:
        payload, headers = _github_get_json(next_url, token)
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected GitHub response shape (expected list)")
        for item in payload:
            if isinstance(item, dict):
                yield item
        next_url = _parse_link_header(headers.get("Link")).get("next")


# ---------------------------------------------------------------------------
# Alert fetchers
# ---------------------------------------------------------------------------

def iter_dependabot_alerts(repo: str, state: str, token: str) -> Iterable[dict]:
    query = parse.urlencode({"state": state, "per_page": "100"})
    yield from _iter_paginated(f"{API_BASE}/repos/{repo}/dependabot/alerts?{query}", token)


def iter_code_scanning_alerts(repo: str, state: str, token: str) -> Iterable[dict]:
    # code-scanning uses "open" / "dismissed" / "fixed" — map our state
    cs_state = "open" if state == "open" else state
    query = parse.urlencode({"state": cs_state, "per_page": "100"})
    yield from _iter_paginated(f"{API_BASE}/repos/{repo}/code-scanning/alerts?{query}", token)


def iter_secret_scanning_alerts(repo: str, token: str) -> Iterable[dict]:
    query = parse.urlencode({"state": "open", "per_page": "100"})
    yield from _iter_paginated(f"{API_BASE}/repos/{repo}/secret-scanning/alerts?{query}", token)


def iter_renovate_prs(repo: str, renovate_user: str, token: str) -> Iterable[dict]:
    query = parse.urlencode({"state": "open", "per_page": "100"})
    for pr in _iter_paginated(f"{API_BASE}/repos/{repo}/pulls?{query}", token):
        login: str = (pr.get("user") or {}).get("login") or ""
        if login.startswith(renovate_user):
            yield pr


# ---------------------------------------------------------------------------
# Severity counters
# ---------------------------------------------------------------------------

def _empty_counts() -> SeverityCounts:
    return SeverityCounts()


def count_dependabot_severity(alerts: Iterable[dict]) -> SeverityCounts:
    counts = _empty_counts()
    for alert in alerts:
        raw = (alert.get("security_advisory") or {}).get("severity") or ""
        label = raw.upper()
        if label == "CRITICAL":
            counts.critical += 1
        elif label == "HIGH":
            counts.high += 1
        elif label == "MEDIUM":
            counts.medium += 1
        elif label == "LOW":
            counts.low += 1
        else:
            counts.unknown += 1
    return counts


def count_code_scanning_severity(alerts: Iterable[dict]) -> SeverityCounts:
    counts = _empty_counts()
    for alert in alerts:
        rule = alert.get("rule") or {}
        # Prefer security_severity_level; fall back to rule.severity
        level: str = (rule.get("security_severity_level") or "").upper()
        if not level:
            level = _CODE_SCAN_FALLBACK.get((rule.get("severity") or "").lower(), "UNKNOWN")
        if level == "CRITICAL":
            counts.critical += 1
        elif level == "HIGH":
            counts.high += 1
        elif level == "MEDIUM":
            counts.medium += 1
        elif level == "LOW":
            counts.low += 1
        else:
            counts.unknown += 1
    return counts


def issues_from_dependabot(repo: str, alerts: Iterable[dict]) -> list[SecurityIssue]:
    issues: list[SecurityIssue] = []
    for alert in alerts:
        advisory = alert.get("security_advisory") or {}
        dependency = alert.get("dependency") or {}
        package = (dependency.get("package") or {}).get("name") or ""
        issues.append(
            SecurityIssue(
                source="dependabot",
                repository=repo,
                number=str(alert.get("number") or ""),
                severity=(advisory.get("severity") or "unknown").upper(),
                state=str(alert.get("state") or ""),
                title=str(advisory.get("summary") or ""),
                identifier=str(package),
                location=str(dependency.get("manifest_path") or ""),
                created_at=str(alert.get("created_at") or ""),
                url=str(alert.get("html_url") or ""),
                extra=str(advisory.get("cve_id") or advisory.get("ghsa_id") or ""),
            )
        )
    return issues


def issues_from_code_scanning(repo: str, alerts: Iterable[dict]) -> list[SecurityIssue]:
    issues: list[SecurityIssue] = []
    for alert in alerts:
        rule = alert.get("rule") or {}
        instance = alert.get("most_recent_instance") or {}
        location = instance.get("location") or {}
        level = (rule.get("security_severity_level") or "").upper()
        if not level:
            level = _CODE_SCAN_FALLBACK.get((rule.get("severity") or "").lower(), "UNKNOWN")
        tool = (alert.get("tool") or {}).get("name") or ""
        issues.append(
            SecurityIssue(
                source="code_scanning",
                repository=repo,
                number=str(alert.get("number") or ""),
                severity=level,
                state=str(alert.get("state") or ""),
                title=str(rule.get("description") or rule.get("name") or ""),
                identifier=str(rule.get("id") or ""),
                location=str(location.get("path") or ""),
                created_at=str(alert.get("created_at") or ""),
                url=str(alert.get("html_url") or ""),
                extra=str(tool),
            )
        )
    return issues


def issues_from_secret_scanning(repo: str, alerts: Iterable[dict]) -> list[SecurityIssue]:
    issues: list[SecurityIssue] = []
    for alert in alerts:
        secret_type = alert.get("secret_type_display_name") or alert.get("secret_type") or ""
        issues.append(
            SecurityIssue(
                source="secret_scanning",
                repository=repo,
                number=str(alert.get("number") or ""),
                severity="",
                state=str(alert.get("state") or ""),
                title=str(secret_type),
                identifier=str(alert.get("secret_type") or ""),
                location="",
                created_at=str(alert.get("created_at") or ""),
                url=str(alert.get("html_url") or ""),
                extra=str(alert.get("validity") or ""),
            )
        )
    return issues


def issues_from_renovate(repo: str, pull_requests: Iterable[dict]) -> list[SecurityIssue]:
    issues: list[SecurityIssue] = []
    for pr in pull_requests:
        login = (pr.get("user") or {}).get("login") or ""
        issues.append(
            SecurityIssue(
                source="renovate",
                repository=repo,
                number=str(pr.get("number") or ""),
                severity="",
                state=str(pr.get("state") or ""),
                title=str(pr.get("title") or ""),
                identifier=str(login),
                location="",
                created_at=str(pr.get("created_at") or ""),
                url=str(pr.get("html_url") or ""),
                extra="",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _severity_section(
    title: str,
    rows: list[RepoReport],
    get_counts: "Callable[[RepoReport], SeverityCounts]",
    out: TextIO,
) -> None:
    sep = "+-" + "-" * 45 + "-+-----------+---------+---------+---------+-----------+-----------+"
    header = f"| {'REPOSITORY':<45} | CRITICAL  | HIGH    | MEDIUM  | LOW     | UNKNOWN   | TOTAL     |"

    total_c = sum(get_counts(r).critical for r in rows)
    total_h = sum(get_counts(r).high for r in rows)
    total_m = sum(get_counts(r).medium for r in rows)
    total_l = sum(get_counts(r).low for r in rows)
    total_u = sum(get_counts(r).unknown for r in rows)
    total_t = total_c + total_h + total_m + total_l + total_u

    print(f"  {title}", file=out)
    print(sep, file=out)
    print(header, file=out)
    print(sep, file=out)
    for row in rows:
        c = get_counts(row)
        print(
            f"| {row.repository:<45} | {c.critical:<9} | {c.high:<7} | {c.medium:<7} | "
            f"{c.low:<7} | {c.unknown:<9} | {c.total:<9} |",
            file=out,
        )
    print(sep, file=out)
    print(
        f"| {'TOTAL':<45} | {total_c:<9} | {total_h:<7} | {total_m:<7} | "
        f"{total_l:<7} | {total_u:<9} | {total_t:<9} |",
        file=out,
    )
    print(sep, file=out)
    print(file=out)


def render_table(
    rows: list[RepoReport], state: str, out: TextIO, generated_at: str | None = None,
) -> None:
    generated = (
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if generated_at else datetime.now(timezone.utc)
    ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(file=out)
    print("  GitHub Security Report", file=out)
    print(f"  Alert state : {state}", file=out)
    print(f"  Generated   : {generated}", file=out)
    print(file=out)

    _severity_section("Dependabot Alerts", rows, lambda r: r.dependabot, out)
    _severity_section("Code Scanning Alerts (CodeQL)", rows, lambda r: r.code_scanning, out)

    # Secrets + Renovate PRs section
    sep2 = "+-" + "-" * 45 + "-+----------------+------------------+"
    print("  Secret Scanning & Renovate PRs", file=out)
    print(sep2, file=out)
    print(f"| {'REPOSITORY':<45} | ACTIVE SECRETS | RENOVATE PRs OPEN |", file=out)
    print(sep2, file=out)
    for row in rows:
        print(f"| {row.repository:<45} | {row.secret_scanning:<14} | {row.renovate_prs:<17} |", file=out)
    print(sep2, file=out)
    print(
        f"| {'TOTAL':<45} | {sum(r.secret_scanning for r in rows):<14} | "
        f"{sum(r.renovate_prs for r in rows):<17} |",
        file=out,
    )
    print(sep2, file=out)
    print(file=out)


def render_csv(rows: list[RepoReport], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(SUMMARY_COLUMNS)
    for r in rows:
        writer.writerow([
            r.repository,
            r.dependabot.critical, r.dependabot.high, r.dependabot.medium, r.dependabot.low, r.dependabot.unknown, r.dependabot.total,
            r.code_scanning.critical, r.code_scanning.high, r.code_scanning.medium, r.code_scanning.low, r.code_scanning.unknown, r.code_scanning.total,
            r.secret_scanning,
            r.renovate_prs,
            r.total,
        ])
    # Totals row
    writer.writerow([
        "TOTAL",
        sum(r.dependabot.critical for r in rows),
        sum(r.dependabot.high for r in rows),
        sum(r.dependabot.medium for r in rows),
        sum(r.dependabot.low for r in rows),
        sum(r.dependabot.unknown for r in rows),
        sum(r.dependabot.total for r in rows),
        sum(r.code_scanning.critical for r in rows),
        sum(r.code_scanning.high for r in rows),
        sum(r.code_scanning.medium for r in rows),
        sum(r.code_scanning.low for r in rows),
        sum(r.code_scanning.unknown for r in rows),
        sum(r.code_scanning.total for r in rows),
        sum(r.secret_scanning for r in rows),
        sum(r.renovate_prs for r in rows),
        sum(r.total for r in rows),
    ])


def render_json(rows: list[RepoReport], state: str, renovate_user: str, out: TextIO) -> None:
    payload = build_report(
        [r.to_dict() for r in rows],
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        state, renovate_user,
    )
    print(json.dumps(payload, indent=2), file=out)


def render_issues_csv(issues: list[SecurityIssue], out: TextIO) -> None:
    writer = csv.writer(out)
    writer.writerow(ISSUES_CSV_HEADER)
    for issue in issues:
        writer.writerow(issue.to_csv_row())


def report_date_label(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime("%Y-%m-%d")


def confluence_page_title(report_date: str) -> str:
    return f"{report_date} Report di sicurezza Github"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _safe_fetch(label: str, fetch_fn, repo: str) -> object:
    """Call fetch_fn(repo) and return a safe fallback on error."""
    try:
        return fetch_fn(repo)
    except RuntimeError as exc:
        warn(f"{repo} [{label}]: {exc}")
        return None


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.input_csv:
        args.input_csv = resolve_input(args.input_csv)
    env_path = load_env_file(args.env_file)
    if env_path:
        log(f"Loaded environment from {env_path}")

    if args.state not in ALLOWED_STATES:
        print(
            f"[ERROR] Invalid state '{args.state}'. Choose one of: {', '.join(sorted(ALLOWED_STATES))}.",
            file=sys.stderr,
        )
        return 1

    if args.input_csv:
        if args.repos or args.repos_file or args.issues_csv:
            print(
                "[ERROR] --input-csv cannot be combined with repositories, --file, or --issues-csv.",
                file=sys.stderr,
            )
            return 1
        try:
            snapshot = load_csv_report(args.input_csv)
            rows = [
                RepoReport(
                    repository=row["repository"],
                    dependabot=SeverityCounts(**{k: row["dependabot"][k] for k in LEVELS}),
                    code_scanning=SeverityCounts(**{k: row["code_scanning"][k] for k in LEVELS}),
                    secret_scanning=row["secret_scanning"],
                    renovate_prs=row["renovate_prs"],
                )
                for row in snapshot["repositories"]
            ]
            return write_outputs(args, rows, [], snapshot)
        except (OSError, ValueError, csv.Error) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

    token = os.getenv("GH_TOKEN")
    if not token:
        print("[ERROR] Missing GH_TOKEN. Set it in .env or the environment.", file=sys.stderr)
        print("        The token needs repo and security_events scopes.", file=sys.stderr)
        return 1

    repos = load_repositories(args.repos, args.repos_file)
    if not repos:
        print("[ERROR] No repositories specified. Use -f FILE or pass repos as arguments.", file=sys.stderr)
        return 1

    log(f"Fetching security data (state={args.state}) for {len(repos)} repositories…")

    rows: list[RepoReport] = []
    issues: list[SecurityIssue] = []
    for repo in repos:
        log(f"Processing {repo}…")

        dep_alerts = _safe_fetch(
            "dependabot", lambda r: list(iter_dependabot_alerts(r, args.state, token)), repo
        )
        dep_list = dep_alerts if isinstance(dep_alerts, list) else []
        dep_counts = count_dependabot_severity(dep_list)
        issues.extend(issues_from_dependabot(repo, dep_list))

        cs_alerts = _safe_fetch(
            "code-scanning", lambda r: list(iter_code_scanning_alerts(r, args.state, token)), repo
        )
        cs_list = cs_alerts if isinstance(cs_alerts, list) else []
        cs_counts = count_code_scanning_severity(cs_list)
        issues.extend(issues_from_code_scanning(repo, cs_list))

        sec_alerts = _safe_fetch(
            "secret-scanning", lambda r: list(iter_secret_scanning_alerts(r, token)), repo
        )
        sec_list = sec_alerts if isinstance(sec_alerts, list) else []
        secret_count = len(sec_list)
        issues.extend(issues_from_secret_scanning(repo, sec_list))

        renovate_result = _safe_fetch(
            "renovate-prs",
            lambda r: list(iter_renovate_prs(r, args.renovate_user, token)),
            repo,
        )
        renovate_list = renovate_result if isinstance(renovate_result, list) else []
        issues.extend(issues_from_renovate(repo, renovate_list))

        rows.append(RepoReport(
            repository=repo,
            dependabot=dep_counts,
            code_scanning=cs_counts,
            secret_scanning=secret_count,
            renovate_prs=len(renovate_list),
        ))

    snapshot = build_report(
        [r.to_dict() for r in rows],
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        args.state, args.renovate_user,
    )
    snapshot["report_date"] = report_date_label()
    snapshot["csv_is_detailed"] = True
    try:
        return write_outputs(args, rows, issues, snapshot)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def resolve_output(path: str) -> str:
    """Place relative output paths inside OUTPUT_DIR (absolute paths are kept)."""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    OUTPUT_DIR.mkdir(exist_ok=True)
    return str(OUTPUT_DIR / p)


def resolve_input(path: str) -> str:
    """Fall back to OUTPUT_DIR for a relative input that is not found as given."""
    p = Path(path)
    if not p.is_absolute() and not p.exists() and (OUTPUT_DIR / p).exists():
        return str(OUTPUT_DIR / p)
    return path


def write_outputs(
    args: argparse.Namespace,
    rows: list[RepoReport],
    issues: list[SecurityIssue],
    snapshot: dict,
) -> int:
    report_date = snapshot["report_date"]
    issues_csv_path = args.input_csv or args.issues_csv
    if args.confluence and not issues_csv_path:
        issues_csv_path = f"{report_date}-github-security-issues.csv"
    if issues_csv_path and not args.input_csv:
        issues_csv_path = resolve_output(issues_csv_path)
    if args.out_file:
        args.out_file = resolve_output(args.out_file)

    if args.out_file:
        output_path = Path(args.out_file).resolve()
        if issues_csv_path and output_path in {
            Path(issues_csv_path).resolve(),
            Path(f"{issues_csv_path}.metadata.json").resolve(),
        }:
            raise ValueError("--out-file must not overwrite the input/issues CSV or its metadata")
        if issues_csv_path and args.output == "csv" and Path(f"{args.out_file}.metadata.json").resolve() == Path(issues_csv_path).resolve():
            raise ValueError("Summary metadata must not overwrite the input/issues CSV")

    def _write(out: TextIO) -> None:
        if args.output == "table":
            render_table(rows, snapshot["alert_state"], out, snapshot["generated_at"])
        elif args.output == "csv":
            render_csv(rows, out)
        else:
            payload = {key: snapshot[key] for key in (
                "generated_at", "alert_state", "renovate_user", "repositories", "totals",
            )}
            print(json.dumps(payload, indent=2), file=out)

    if args.out_file:
        with open(args.out_file, "w", encoding="utf-8", newline="") as out:
            _write(out)
        if args.output == "csv":
            write_metadata(args.out_file, snapshot, snapshot["report_date"])
    else:
        _write(sys.stdout)

    if issues_csv_path and not args.input_csv:
        with open(issues_csv_path, "w", encoding="utf-8", newline="") as csv_out:
            render_issues_csv(issues, csv_out)
        write_metadata(issues_csv_path, snapshot, report_date)
        log(f"Wrote {len(issues)} issues to {issues_csv_path}")

    if args.confluence:
        try:
            cfg = ConfluenceConfig.from_env(
                url=args.confluence_url,
                space_key=args.confluence_space,
                token=args.confluence_token,
                user=args.confluence_user,
                parent_id=args.confluence_parent_id,
                cloud_id=args.confluence_cloud_id,
            )
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

        page_title = confluence_page_title(report_date)
        csv_name = os.path.basename(issues_csv_path)
        generated_at = snapshot["generated_at"]
        storage = build_report_storage(
            title=page_title,
            generated_at=generated_at,
            alert_state=snapshot["alert_state"],
            rows=rows,
            csv_filename=csv_name,
            get_dependabot=lambda r: r.dependabot,
            get_code_scanning=lambda r: r.code_scanning,
            get_secret_scanning=lambda r: r.secret_scanning,
            get_renovate=lambda r: r.renovate_prs,
            get_total=lambda r: r.total,
            issue_count=snapshot["totals"]["total"],
            csv_is_detailed=snapshot["csv_is_detailed"],
        )
        log(f"Publishing Confluence page '{page_title}' in space {cfg.space_key}…")
        try:
            page_url = publish_report(
                cfg,
                title=page_title,
                storage_html=storage,
                csv_path=issues_csv_path,
                csv_filename=csv_name,
            )
        except RuntimeError as exc:
            print(f"[ERROR] Confluence publish failed: {exc}", file=sys.stderr)
            return 1
        log(f"Published Confluence page: {page_url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
