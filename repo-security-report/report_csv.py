"""Read reusable security CSV snapshots without accessing GitHub."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path

LEVELS = ("critical", "high", "medium", "low", "unknown")
SOURCES = ("dependabot", "code_scanning")
SUMMARY_COLUMNS = [
    "repository",
    *(f"{source}_{level}" for source in SOURCES for level in (*LEVELS, "total")),
    "secret_scanning", "renovate_prs", "grand_total",
]
ISSUE_COLUMNS = [
    "source", "repository", "number", "severity", "state", "title",
    "identifier", "location", "created_at", "url", "extra",
]


def empty_repository(repository: str) -> dict:
    return {
        "repository": repository,
        **{source: {level: 0 for level in (*LEVELS, "total")} for source in SOURCES},
        "secret_scanning": 0, "renovate_prs": 0, "total": 0,
    }


def build_report(rows: list[dict], generated_at: str, state: str, renovate_user: str) -> dict:
    totals = empty_repository("TOTAL")
    del totals["repository"]
    for row in rows:
        for source in SOURCES:
            for level in (*LEVELS, "total"):
                totals[source][level] += row[source][level]
        for key in ("secret_scanning", "renovate_prs", "total"):
            totals[key] += row[key]
    return {
        "generated_at": generated_at, "alert_state": state,
        "renovate_user": renovate_user, "repositories": rows, "totals": totals,
    }


def write_metadata(csv_path: str, report: dict, report_date: str) -> None:
    path = Path(csv_path)
    metadata = {
        "generated_at": report["generated_at"],
        "alert_state": report["alert_state"],
        "renovate_user": report["renovate_user"],
        "report_date": report_date,
        "repositories": [row["repository"] for row in report["repositories"]],
        "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    Path(f"{path}.metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8",
    )


def _repository(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError(f"Invalid repository: {value!r}")
    return value


def load_csv_report(csv_path: str | Path) -> dict:
    path = Path(csv_path)
    metadata_path = Path(f"{path}.metadata.json")
    metadata = None
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError(f"Invalid metadata in {metadata_path}")
        if metadata.get("csv_sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError(f"CSV does not match metadata: {metadata_path}")
        for key in ("generated_at", "alert_state", "renovate_user", "report_date"):
            if not isinstance(metadata.get(key), str):
                raise ValueError(f"Invalid metadata field: {key}")
        datetime.fromisoformat(metadata["generated_at"].replace("Z", "+00:00"))
        datetime.strptime(metadata["report_date"], "%Y-%m-%d")
        if not isinstance(metadata.get("repositories"), list):
            raise ValueError("Invalid metadata repositories")
    else:
        warnings.warn(
            f"{metadata_path} not found: snapshot time/state are unavailable; "
            "detailed CSVs cannot identify repositories with zero findings.",
            stacklevel=2,
        )

    rows: dict[str, dict] = {}
    if metadata:
        for repo in metadata["repositories"]:
            if not isinstance(repo, str):
                raise ValueError("Invalid metadata repository")
            repo = _repository(repo)
            if repo in rows:
                raise ValueError(f"Duplicate metadata repository: {repo}")
            rows[repo] = empty_repository(repo)

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        detailed = reader.fieldnames == ISSUE_COLUMNS
        if not detailed and reader.fieldnames != SUMMARY_COLUMNS:
            raise ValueError("Unsupported CSV header: expected a summary or detailed issues CSV")
        summary_repos: set[str] = set()
        total_row = None
        for line, record in enumerate(reader, start=2):
            if None in record or any(value is None for value in record.values()):
                raise ValueError(f"Malformed CSV row at line {line}")
            repo = record["repository"].strip()
            if detailed:
                repo = _repository(repo)
                if metadata and repo not in rows:
                    raise ValueError(f"Repository missing from metadata: {repo}")
                row = rows.setdefault(repo, empty_repository(repo))
                source = record["source"]
                if source in SOURCES:
                    level = record["severity"].lower() or "unknown"
                    if level not in LEVELS:
                        raise ValueError(f"Invalid severity at line {line}: {level}")
                    row[source][level] += 1
                    row[source]["total"] += 1
                elif source in ("secret_scanning", "renovate"):
                    row["secret_scanning" if source == "secret_scanning" else "renovate_prs"] += 1
                else:
                    raise ValueError(f"Invalid source at line {line}: {source}")
                row["total"] += 1
            else:
                row = empty_repository(repo)
                values = {}
                for column in SUMMARY_COLUMNS[1:]:
                    value = record[column]
                    if not re.fullmatch(r"[0-9]+", value):
                        raise ValueError(f"Invalid count at line {line}: {column}={value!r}")
                    values[column] = int(value)
                for source in SOURCES:
                    row[source] = {level: values[f"{source}_{level}"] for level in (*LEVELS, "total")}
                    if row[source]["total"] != sum(row[source][level] for level in LEVELS):
                        raise ValueError(f"Inconsistent {source} total at line {line}")
                row.update(
                    secret_scanning=values["secret_scanning"],
                    renovate_prs=values["renovate_prs"], total=values["grand_total"],
                )
                if row["total"] != sum(row[source]["total"] for source in SOURCES) + row["secret_scanning"] + row["renovate_prs"]:
                    raise ValueError(f"Inconsistent grand total at line {line}")
                if repo == "TOTAL":
                    if total_row is not None:
                        raise ValueError("Duplicate TOTAL row")
                    total_row = row
                    continue
                repo = _repository(repo)
                if repo in summary_repos:
                    raise ValueError(f"Duplicate repository: {repo}")
                if metadata and repo not in rows:
                    raise ValueError(f"Repository missing from metadata: {repo}")
                summary_repos.add(repo)
                rows[repo] = row
        if not detailed and metadata and summary_repos != set(rows):
            raise ValueError("Summary repositories do not match metadata")

    generated_at = (
        metadata["generated_at"] if metadata else
        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    report = build_report(
        list(rows.values()), generated_at,
        metadata["alert_state"] if metadata else "unknown",
        metadata["renovate_user"] if metadata else "",
    )
    if total_row:
        del total_row["repository"]
        if total_row != report["totals"]:
            raise ValueError("CSV TOTAL row does not match repository counts")
    filename_date = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
    report["report_date"] = (
        metadata["report_date"] if metadata else
        filename_date.group(1) if filename_date else generated_at[:10]
    )
    datetime.strptime(report["report_date"], "%Y-%m-%d")
    report["csv_is_detailed"] = detailed
    return report
