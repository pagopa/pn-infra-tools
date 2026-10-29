"""Download dei costi da AWS Cost Explorer con cache locale su file JSON.

Cost Explorer consente al massimo 2 GroupBy per richiesta: per ottenere la terna
Microservice / Servizio AWS / Usage Type si esegue una query SERVICE x USAGE_TYPE
(che fornisce il totale completo) e poi, per ciascun servizio rilevante, una query
filtrata per servizio con GroupBy TAG(Microservice) x USAGE_TYPE.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

UNTAGGED = "(untagged)"
NOT_DETAILED = "(non dettagliato)"


class CostCacheMiss(Exception):
    pass


def cache_path(cache_dir: str, profile: str, start: str, end: str, metric: str, tag_key: str,
               excluded_record_types: List[str], min_service_cost: float) -> str:
    sig = json.dumps([metric, tag_key, sorted(excluded_record_types), min_service_cost])
    digest = hashlib.sha1(sig.encode()).hexdigest()[:10]
    return os.path.join(cache_dir, profile, f"{start}_{end}_{metric}_{digest}.json")


def _build_filter(excluded_record_types: List[str], service: Optional[str] = None) -> Optional[dict]:
    parts = []
    if excluded_record_types:
        parts.append({"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": list(excluded_record_types)}}})
    if service is not None:
        parts.append({"Dimensions": {"Key": "SERVICE", "Values": [service]}})
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"And": parts}


def _query(ce, start: str, end: str, metric: str, group_by: List[dict], flt: Optional[dict]):
    """GetCostAndUsage giornaliero con paginazione; ritorna [(date, keys, amount, unit, estimated)]."""
    kwargs = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": [metric],
        "GroupBy": group_by,
    }
    if flt:
        kwargs["Filter"] = flt
    out = []
    calls = 0
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        calls += 1
        for rbt in resp.get("ResultsByTime", []):
            day = rbt["TimePeriod"]["Start"]
            estimated = bool(rbt.get("Estimated", False))
            for g in rbt.get("Groups", []):
                m = g["Metrics"][metric]
                amount = float(m["Amount"])
                if amount == 0:
                    continue
                out.append((day, g["Keys"], amount, m.get("Unit", "USD"), estimated))
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token
    return out, calls


def _tag_value(raw: str, tag_key: str) -> str:
    prefix = f"{tag_key}$"
    value = raw[len(prefix):] if raw.startswith(prefix) else raw
    return value or UNTAGGED


def fetch_account_costs(profile: str, start: str, end: str, metric: str, tag_key: str,
                        excluded_record_types: List[str], min_service_cost: float,
                        log: Callable[[str], None] = print) -> Dict:
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=profile)
    cfg = Config(retries={"max_attempts": 10, "mode": "adaptive"})
    account_id = session.client("sts", config=cfg).get_caller_identity()["Account"]
    ce = session.client("ce", region_name="us-east-1", config=cfg)

    base_rows, calls = _query(
        ce, start, end, metric,
        [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        _build_filter(excluded_record_types),
    )
    service_totals: Dict[str, float] = {}
    for _, keys, amount, _, _ in base_rows:
        service_totals[keys[0]] = service_totals.get(keys[0], 0.0) + amount

    detailed = sorted(s for s, t in service_totals.items() if abs(t) >= min_service_cost)
    log(f"[{profile}] account {account_id}: {len(service_totals)} servizi, "
        f"{len(detailed)} da dettagliare per tag '{tag_key}'")
    rows = []
    for svc in detailed:
        svc_rows, c = _query(
            ce, start, end, metric,
            [{"Type": "TAG", "Key": tag_key}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            _build_filter(excluded_record_types, svc),
        )
        calls += c
        for day, keys, amount, unit, est in svc_rows:
            rows.append({"date": day, "microservice": _tag_value(keys[0], tag_key), "service": svc,
                         "usage_type": keys[1], "cost": amount, "unit": unit, "estimated": est})
    detailed_set = set(detailed)
    for day, keys, amount, unit, est in base_rows:
        if keys[0] not in detailed_set:
            rows.append({"date": day, "microservice": NOT_DETAILED, "service": keys[0],
                         "usage_type": keys[1], "cost": amount, "unit": unit, "estimated": est})

    log(f"[{profile}] {len(rows)} righe scaricate con {calls} chiamate Cost Explorer")
    return {
        "profile": profile,
        "account_id": account_id,
        "start": start,
        "end": end,
        "metric": metric,
        "tag_key": tag_key,
        "excluded_record_types": list(excluded_record_types),
        "min_service_cost": min_service_cost,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ce_api_calls": calls,
        "rows": rows,
    }


def load_or_fetch(profile: str, start: str, end: str, metric: str, tag_key: str,
                  excluded_record_types: List[str], min_service_cost: float, cache_dir: str,
                  refresh: bool = False, offline: bool = False,
                  log: Callable[[str], None] = print) -> Dict:
    path = cache_path(cache_dir, profile, start, end, metric, tag_key, excluded_record_types, min_service_cost)
    if os.path.exists(path) and not refresh:
        log(f"[{profile}] dati letti dalla cache {path}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if offline:
        raise CostCacheMiss(f"nessuna cache per {profile} nel periodo {start} - {end} ({path})")
    data = fetch_account_costs(profile, start, end, metric, tag_key, excluded_record_types,
                               min_service_cost, log=log)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    log(f"[{profile}] dati salvati in cache {path}")
    return data
