#!/usr/bin/env python3
"""
Audit AWS Lambda runtimes across multiple AWS profiles/accounts.

For each account reachable through the given profiles, lists the Lambda functions
whose runtime is older than a configurable minimum version (Node.js and, optionally,
Python), detecting the owning CloudFormation stack and the "Microservice" tag.
Results are written to an XLSX report.

Examples:
  python3 lambda_runtime_audit.py
  python3 lambda_runtime_audit.py --nodejs-min 22 --check-python --python-min 3.12
  python3 lambda_runtime_audit.py --profiles sso_pn-core-dev --regions eu-south-1 eu-central-1
"""

import argparse
import datetime
import re
import sys
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

DEFAULT_PROFILES = [
    "sso_pn-cicd",
    "sso_pn-core-dev",
    "sso_pn-confinfo-dev",
    "sso_pn-core-test",
    "sso_pn-confinfo-test",
    "sso_pn-core-uat",
    "sso_pn-confinfo-uat",
    "sso_pn-core-hotfix",
    "sso_pn-confinfo-hotfix",
    "sso_pn-core-prod-ro",
    "sso_pn-confinfo-prod-ro",
]
DEFAULT_NODEJS_MIN = "22"
DEFAULT_PYTHON_MIN = "3.12"
CFN_STACK_TAG = "aws:cloudformation:stack-name"
CFN_LOGICAL_ID_TAG = "aws:cloudformation:logical-id"
MICROSERVICE_TAG = "Microservice"

RUNTIME_RE = {
    "nodejs": re.compile(r"^nodejs(?P<ver>[\d.]*?)(?:\.x)?(?:-edge)?$"),
    "python": re.compile(r"^python(?P<ver>[\d.]*)$"),
}


def parse_version(text: str) -> Tuple[int, ...]:
    text = text.strip().lower().rstrip("x").rstrip(".")
    if not text:
        return (0,)
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid version: {!r}".format(text))


def runtime_version(runtime: str, family: str) -> Optional[Tuple[int, ...]]:
    """Return the parsed version if runtime belongs to family, else None."""
    m = RUNTIME_RE[family].match(runtime or "")
    if not m:
        return None
    return parse_version(m.group("ver"))


def is_older(version: Tuple[int, ...], minimum: Tuple[int, ...]) -> bool:
    length = max(len(version), len(minimum))
    pad = lambda v: v + (0,) * (length - len(v))  # noqa: E731
    return pad(version) < pad(minimum)


def list_functions(lambda_client) -> List[dict]:
    functions = []
    for page in lambda_client.get_paginator("list_functions").paginate():
        functions.extend(page.get("Functions", []))
    return functions


def get_tags(lambda_client, arn: str) -> Dict[str, str]:
    try:
        return lambda_client.list_tags(Resource=arn).get("Tags", {}) or {}
    except ClientError as e:
        print("    WARN: cannot read tags for {}: {}".format(arn, e), file=sys.stderr)
        return {}


def audit_profile(profile: str, regions: Optional[List[str]], checks: Dict[str, Tuple[int, ...]],
                  seen: set, results: List[dict], summary: List[dict]) -> None:
    print("[{}]".format(profile))
    try:
        session = boto3.Session(profile_name=profile)
        account_id = session.client("sts").get_caller_identity()["Account"]
    except (BotoCoreError, ClientError) as e:
        print("  ERROR: {}".format(e), file=sys.stderr)
        summary.append({"profile": profile, "account": "", "region": "", "total": "",
                        "outdated": "", "status": "ERROR: {}".format(e)})
        return

    for region in regions or [session.region_name]:
        key = (account_id, region)
        if key in seen:
            print("  {} / {}: already audited via another profile, skipping".format(account_id, region))
            continue
        seen.add(key)
        try:
            client = session.client("lambda", region_name=region)
            functions = list_functions(client)
        except (BotoCoreError, ClientError) as e:
            print("  ERROR {}: {}".format(region, e), file=sys.stderr)
            summary.append({"profile": profile, "account": account_id, "region": region,
                            "total": "", "outdated": "", "status": "ERROR: {}".format(e)})
            continue

        outdated = 0
        for fn in functions:
            runtime = fn.get("Runtime")
            if not runtime:  # container image functions
                continue
            for family, minimum in checks.items():
                version = runtime_version(runtime, family)
                if version is None or not is_older(version, minimum):
                    continue
                outdated += 1
                tags = get_tags(client, fn["FunctionArn"])
                results.append({
                    "Profile": profile,
                    "Account ID": account_id,
                    "Region": region,
                    "Function Name": fn["FunctionName"],
                    "Runtime": runtime,
                    "Family": family,
                    "Minimum Required": ".".join(str(p) for p in minimum),
                    "CloudFormation Stack": tags.get(CFN_STACK_TAG, ""),
                    "CFN Logical ID": tags.get(CFN_LOGICAL_ID_TAG, ""),
                    "Microservice": tags.get(MICROSERVICE_TAG, ""),
                    "Last Modified": fn.get("LastModified", ""),
                    "Function ARN": fn["FunctionArn"],
                })
        print("  {} / {}: {} functions, {} outdated".format(account_id, region, len(functions), outdated))
        summary.append({"profile": profile, "account": account_id, "region": region,
                        "total": len(functions), "outdated": outdated, "status": "OK"})


def autosize(ws) -> None:
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(width + 2, 80)


def write_sheet(ws, headers: List[str], rows: List[List]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="305496")
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    autosize(ws)


def write_report(path: str, results: List[dict], summary: List[dict],
                 checks: Dict[str, Tuple[int, ...]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Outdated Lambdas"
    headers = ["Profile", "Account ID", "Region", "Function Name", "Runtime", "Family",
               "Minimum Required", "CloudFormation Stack", "CFN Logical ID", "Microservice",
               "Last Modified", "Function ARN"]
    rows = sorted(results, key=lambda r: (r["Profile"], r["Region"], r["Family"], r["Function Name"]))
    write_sheet(ws, headers, [[r[h] for h in headers] for r in rows])

    ws_sum = wb.create_sheet("Summary")
    write_sheet(ws_sum, ["Profile", "Account ID", "Region", "Total Lambdas", "Outdated Lambdas", "Status"],
                [[s["profile"], s["account"], s["region"], s["total"], s["outdated"], s["status"]]
                 for s in summary])

    ws_runtime = wb.create_sheet("By Runtime")
    counts: Dict[Tuple[str, str], int] = {}
    for r in results:
        counts[(r["Profile"], r["Runtime"])] = counts.get((r["Profile"], r["Runtime"]), 0) + 1
    write_sheet(ws_runtime, ["Profile", "Runtime", "Count"],
                [[p, rt, c] for (p, rt), c in sorted(counts.items())])

    ws_info = wb.create_sheet("Audit Info")
    write_sheet(ws_info, ["Parameter", "Value"], [
        ["Generated at", datetime.datetime.now().isoformat(timespec="seconds")],
        *[["Minimum {} version".format(f), ".".join(str(p) for p in v)] for f, v in checks.items()],
    ])
    wb.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit outdated Lambda runtimes across AWS accounts.")
    parser.add_argument("--profiles", nargs="+", default=DEFAULT_PROFILES,
                        help="AWS CLI profiles to audit (default: PN profiles)")
    parser.add_argument("--regions", nargs="+",
                        help="Regions to scan (default: region configured in each profile)")
    parser.add_argument("--nodejs-min", type=parse_version, default=parse_version(DEFAULT_NODEJS_MIN),
                        help="Minimum Node.js version, e.g. 22 (default: {})".format(DEFAULT_NODEJS_MIN))
    parser.add_argument("--skip-nodejs", action="store_true", help="Do not check Node.js runtimes")
    parser.add_argument("--check-python", action="store_true", help="Also check Python runtimes")
    parser.add_argument("--python-min", type=parse_version, default=parse_version(DEFAULT_PYTHON_MIN),
                        help="Minimum Python version, e.g. 3.12 (default: {})".format(DEFAULT_PYTHON_MIN))
    parser.add_argument("--output", default="output/lambda_runtime_audit_{}.xlsx".format(
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S")), help="Output XLSX file path")
    args = parser.parse_args()

    checks: Dict[str, Tuple[int, ...]] = {}
    if not args.skip_nodejs:
        checks["nodejs"] = args.nodejs_min
    if args.check_python:
        checks["python"] = args.python_min
    if not checks:
        parser.error("Nothing to check: use --check-python or remove --skip-nodejs")

    results: List[dict] = []
    summary: List[dict] = []
    seen: set = set()
    for profile in args.profiles:
        audit_profile(profile, args.regions, checks, seen, results, summary)

    write_report(args.output, results, summary, checks)
    print("\n{} outdated Lambda functions found. Report: {}".format(len(results), args.output))
    return 1 if any(s["status"] != "OK" for s in summary) else 0


if __name__ == "__main__":
    sys.exit(main())
