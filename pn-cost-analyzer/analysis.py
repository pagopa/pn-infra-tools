"""Analisi dei trend di costo per singolo account."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Sequence, Tuple

LEVELS: Dict[str, Tuple[str, ...]] = {
    "microservice": ("microservice",),
    "service": ("service",),
    "usage_type": ("usage_type",),
    "detail": ("microservice", "service", "usage_type"),
}

LEVEL_LABELS = {
    "microservice": "Microservice",
    "service": "Servizio AWS",
    "usage_type": "Usage type",
    "detail": "Microservice / Servizio AWS / Usage type",
}


def date_range(start: str, end: str) -> List[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days)]


def _slope(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def analyze(rows: List[dict], days: List[str], min_delta_abs: float, min_delta_pct: float) -> Dict:
    """Per ogni livello di aggregazione calcola serie giornaliere e confronto tra le due metà del periodo."""
    idx = {d: i for i, d in enumerate(days)}
    half = len(days) // 2
    prev_days, recent_days = len(days) - half, half
    totals_daily = [0.0] * len(days)
    estimated_days = set()
    for r in rows:
        if r["date"] in idx:
            totals_daily[idx[r["date"]]] += r["cost"]
            if r.get("estimated"):
                estimated_days.add(r["date"])

    result = {
        "days": days,
        "prev_period": (days[0], days[prev_days - 1]) if days else None,
        "recent_period": (days[prev_days], days[-1]) if recent_days else None,
        "estimated_days": sorted(estimated_days),
        "total_daily": totals_daily,
        "total": sum(totals_daily),
        "total_prev": sum(totals_daily[:prev_days]),
        "total_recent": sum(totals_daily[prev_days:]),
        "levels": {},
    }

    for level, fields in LEVELS.items():
        series: Dict[Tuple[str, ...], List[float]] = defaultdict(lambda: [0.0] * len(days))
        for r in rows:
            i = idx.get(r["date"])
            if i is not None:
                series[tuple(r[f] for f in fields)][i] += r["cost"]
        items = []
        for key, values in series.items():
            prev = sum(values[:prev_days])
            recent = sum(values[prev_days:])
            # normalizza alla stessa durata nel caso di numero di giorni dispari
            prev_norm = prev * recent_days / prev_days if prev_days else 0.0
            delta = recent - prev_norm
            pct = (delta / prev_norm * 100) if prev_norm > 0 else (float("inf") if recent > 0 else 0.0)
            slope = _slope(values)
            growing = delta >= min_delta_abs and pct >= min_delta_pct and slope > 0
            items.append({
                "key": key,
                "values": values,
                "total": prev + recent,
                "prev": prev_norm,
                "recent": recent,
                "delta": delta,
                "delta_pct": pct,
                "slope_per_day": slope,
                "is_new": prev == 0 and recent > 0,
                "growing": growing,
            })
        items.sort(key=lambda x: x["total"], reverse=True)
        result["levels"][level] = {
            "fields": fields,
            "items": items,
            "growing": sorted((x for x in items if x["growing"]), key=lambda x: x["delta"], reverse=True),
        }
    return result
