#!/usr/bin/env python3
"""SEND (Piattaforma Notifiche) - analisi dei costi AWS per account, con focus sul tag Microservice."""
from __future__ import annotations

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analysis import analyze, date_range  # noqa: E402
from aws_costs import CostCacheMiss, load_or_fetch  # noqa: E402
from report import write_html, write_raw_csv, write_trend_csv  # noqa: E402

DEFAULT_PROFILES = [
    "sso_pn-cicd",
    "sso_pn-core-dev", "sso_pn-confinfo-dev",
    "sso_pn-core-test", "sso_pn-confinfo-test",
    "sso_pn-core-uat", "sso_pn-confinfo-uat",
    "sso_pn-core-hotfix", "sso_pn-confinfo-hotfix",
    "sso_pn-core-prod-ro", "sso_pn-confinfo-prod-ro",
]

_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, file=sys.stderr, flush=True)


def csv_list(value: str):
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_args(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profiles", type=csv_list, default=DEFAULT_PROFILES,
                   help="profili AWS separati da virgola (default: tutti gli account SEND)")
    p.add_argument("--days", type=int, default=60, help="giorni di analisi (default 60)")
    p.add_argument("--end", default=date.today().isoformat(),
                   help="data di fine ESCLUSA, YYYY-MM-DD (default oggi). Fissarla per riusare la cache")
    p.add_argument("--metric", default="UnblendedCost",
                   choices=["UnblendedCost", "AmortizedCost", "NetUnblendedCost", "NetAmortizedCost", "BlendedCost"])
    p.add_argument("--tag-key", default="Microservice", help="cost allocation tag (default Microservice)")
    p.add_argument("--exclude-record-types", type=csv_list, default=["Credit", "Refund"],
                   help="RECORD_TYPE da escludere, separati da virgola (default Credit,Refund; '' per nessuno)")
    p.add_argument("--min-service-cost", type=float, default=1.0,
                   help="costo minimo nel periodo perché un servizio sia dettagliato per tag (default 1 USD)")
    p.add_argument("--min-delta-abs", type=float, default=5.0,
                   help="incremento minimo (USD, su metà periodo) per segnalare un costo crescente (default 5)")
    p.add_argument("--min-delta-pct", type=float, default=10.0,
                   help="incremento percentuale minimo per segnalare un costo crescente (default 10)")
    p.add_argument("--top", type=int, default=50, help="righe massime per tabella nel report HTML")
    p.add_argument("--cache-dir", default=os.path.join(here, "cache"))
    p.add_argument("--output-dir", default=os.path.join(here, "output"))
    p.add_argument("--refresh", action="store_true", help="ignora la cache e riscarica da AWS")
    p.add_argument("--offline", action="store_true", help="usa solo la cache, nessuna chiamata AWS")
    p.add_argument("--workers", type=int, default=4, help="account elaborati in parallelo (default 4)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.refresh and args.offline:
        log("--refresh e --offline sono mutuamente esclusivi")
        return 2
    end = date.fromisoformat(args.end)
    start = end - timedelta(days=args.days)
    start_s, end_s = start.isoformat(), end.isoformat()
    days = date_range(start_s, end_s)
    log(f"Periodo analizzato: {start_s} -> {(end - timedelta(days=1)).isoformat()} ({args.days} giorni)")

    def work(profile):
        data = load_or_fetch(profile, start_s, end_s, args.metric, args.tag_key, args.exclude_record_types,
                             args.min_service_cost, args.cache_dir, refresh=args.refresh,
                             offline=args.offline, log=log)
        return {"data": data, "analysis": analyze(data["rows"], days, args.min_delta_abs, args.min_delta_pct)}

    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(work, prof): prof for prof in args.profiles}
        for fut in as_completed(futures):
            prof = futures[fut]
            try:
                results[prof] = fut.result()
            except CostCacheMiss as exc:
                errors[prof] = str(exc)
                log(f"[{prof}] {exc}")
            except Exception as exc:  # noqa: BLE001 - un account in errore non deve bloccare gli altri
                errors[prof] = f"{type(exc).__name__}: {exc}"
                log(f"[{prof}] ERRORE {errors[prof]}")

    accounts = [results[p] for p in args.profiles if p in results]
    if not accounts:
        log("Nessun dato disponibile, report non generato.")
        return 1

    os.makedirs(args.output_dir, exist_ok=True)
    base = os.path.join(args.output_dir, f"pn-costs_{start_s}_{end_s}")
    write_raw_csv(base + "_raw.csv", accounts)
    write_trend_csv(base + "_trend.csv", accounts)
    write_html(base + ".html", accounts, errors, {
        "start": start_s, "end_inclusive": days[-1], "days": args.days, "metric": args.metric,
        "tag_key": args.tag_key, "excluded_record_types": args.exclude_record_types,
        "min_delta_abs": args.min_delta_abs, "min_delta_pct": args.min_delta_pct, "top": args.top,
    })
    log(f"Report HTML : {base}.html")
    log(f"CSV grezzi  : {base}_raw.csv")
    log(f"CSV trend   : {base}_trend.csv")
    return 0 if not errors else 3


if __name__ == "__main__":
    sys.exit(main())
