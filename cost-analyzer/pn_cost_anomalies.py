#!/usr/bin/env python3
"""SEND (Piattaforma Notifiche) - cost anomaly detection on the pn_cost_analyzer raw CSV.

Detects, per account and per Microservice / AWS service / usage type:
  * spikes: transient days well above the expected (weekday-adjusted) cost;
  * sustained increases: gradual growth (Mann-Kendall + Theil-Sen), step increases (level shift)
    and new cost items;
  * month-start charges (billed on the 1st of the month), reported apart with month-over-month change.

Produces an executive HTML report and a detailed XLSX workbook.
Pure Python (no numpy/pandas); only openpyxl is required for the XLSX output.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

LEVELS: Dict[str, Tuple[str, ...]] = {
    "microservice": ("microservice",),
    "service": ("service",),
    "usage_type": ("usage_type",),
    "detail": ("microservice", "service", "usage_type"),
}
LEVEL_LABELS = {"microservice": "Microservice", "service": "AWS service", "usage_type": "Usage type",
                "detail": "Microservice / AWS service / Usage type"}
# fields used to describe the main drivers of a finding, per level
DRIVER_FIELDS = {"microservice": ("service", "usage_type"), "service": ("microservice", "usage_type"),
                 "usage_type": ("microservice", "service")}
PATTERN_LABELS = {"gradual": "Gradual growth", "step": "Step increase", "new": "New cost"}


# --------------------------------------------------------------------------- statistics helpers

def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def mann_kendall(ys: Sequence[float]) -> Tuple[float, float]:
    """One-sided Mann-Kendall test for an increasing trend. Returns (tau, p-value)."""
    n = len(ys)
    if n < 8:
        return 0.0, 1.0
    s = 0
    for i in range(n - 1):
        yi = ys[i]
        for j in range(i + 1, n):
            d = ys[j] - yi
            s += (d > 0) - (d < 0)
    ties = defaultdict(int)
    for y in ys:
        ties[round(y, 6)] += 1
    var = (n * (n - 1) * (2 * n + 5) - sum(t * (t - 1) * (2 * t + 5) for t in ties.values())) / 18
    if var <= 0:
        return 0.0, 1.0
    z = (s - 1) / math.sqrt(var) if s > 0 else ((s + 1) / math.sqrt(var) if s < 0 else 0.0)
    return s / (n * (n - 1) / 2), 0.5 * math.erfc(z / math.sqrt(2))


def theil_sen(xs: Sequence[int], ys: Sequence[float]) -> Tuple[float, float]:
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i])
              for i in range(len(xs) - 1) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    slope = median(slopes) if slopes else 0.0
    return slope, median([y - slope * x for x, y in zip(xs, ys)])


# --------------------------------------------------------------------------- data loading

def latest_raw_csv(directory: str) -> Optional[str]:
    files = sorted(glob.glob(os.path.join(directory, "*_raw.csv")), key=os.path.getmtime)
    return files[-1] if files else None


def load_series(path: str, exclude_services: List[str], last_days: Optional[int], drop_last_days: int):
    """Reads the raw CSV and builds daily series for each (profile, level, key)."""
    excluded = {s.lower() for s in exclude_services}
    rows, all_dates, accounts, estimated = [], set(), {}, set()
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            all_dates.add(r["date"])
            accounts[r["profile"]] = r["account_id"]
            if r["estimated"] == "True":
                estimated.add(r["date"])
            if r["service"].lower() in excluded:
                continue
            rows.append((r["profile"], r["date"], r["microservice"], r["service"], r["usage_type"],
                         float(r["cost"])))
    if not all_dates:
        raise SystemExit(f"No data in {path}")
    d0, d1 = date.fromisoformat(min(all_dates)), date.fromisoformat(max(all_dates))
    days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    if drop_last_days:
        days = days[:-drop_last_days]
    if last_days:
        days = days[-last_days:]
    idx = {d: i for i, d in enumerate(days)}
    n = len(days)
    series: Dict[str, Dict[Tuple[str, Tuple[str, ...]], List[float]]] = {
        lvl: defaultdict(lambda: [0.0] * n) for lvl in LEVELS}
    totals: Dict[str, List[float]] = defaultdict(lambda: [0.0] * n)
    for profile, day, ms, svc, ut, cost in rows:
        i = idx.get(day)
        if i is None:
            continue
        rec = {"microservice": ms, "service": svc, "usage_type": ut}
        totals[profile][i] += cost
        for lvl, fields in LEVELS.items():
            series[lvl][(profile, tuple(rec[f] for f in fields))][i] += cost
    return days, series, totals, accounts, sorted(d for d in estimated if d in idx)


# --------------------------------------------------------------------------- per-series analysis

def month_start_info(values: List[float], days: List[str], min_abs: float) -> Optional[Dict]:
    """Detects costs billed on the 1st of the month (e.g. subscriptions, Shield, Support fees)."""
    starts = [i for i, d in enumerate(days) if d.endswith("-01")]
    if not starts:
        return None
    others = [v for i, v in enumerate(values) if i not in starts]
    base = median(others)
    if all(values[i] >= max(3 * base, base + min_abs) for i in starts):
        charges = [(days[i], values[i]) for i in starts]
        info = {"indices": starts, "charges": charges, "base_daily": base}
        if len(charges) >= 2:
            prev, last = charges[-2][1], charges[-1][1]
            info["mom_delta"] = last - prev
            info["mom_pct"] = (last - prev) / prev * 100 if prev > 0 else math.inf
        return info
    return None


def weekday_factors(values: List[float], valid: List[int], weekdays: List[int]) -> List[float]:
    meds = [median([values[i] for i in valid if weekdays[i] == wd]) for wd in range(7)]
    norm = mean(meds)
    if norm <= 0:
        return [1.0] * 7
    return [max(m / norm, 0.2) for m in meds]


def detect_spikes(values, adj, valid, factors, weekdays, cfg) -> Tuple[List[Dict], List[Dict]]:
    """Returns (spikes, level_shift_starts) using a robust z-score on weekday-adjusted values."""
    n, w = len(values), cfg.spike_window
    valid_set = set(valid)
    base, resid = {}, {}
    for i in valid:
        window = [adj[j] for j in range(max(0, i - w), min(n, i + w + 1)) if j != i and j in valid_set]
        base[i] = median(window)
        resid[i] = adj[i] - base[i]
    rs = list(resid.values())
    mr = median(rs)
    scale = max(1.4826 * median([abs(r - mr) for r in rs]), 0.1 * median([adj[i] for i in valid]), 0.01)
    flagged = {}
    for i in valid:
        same_wd = [values[j] for j in valid if weekdays[j] == weekdays[i] and j != i]
        expected = max(base[i] * factors[weekdays[i]], median(same_wd))
        excess = values[i] - expected
        z = resid[i] / scale
        if z >= cfg.spike_z and excess >= cfg.spike_min_abs and values[i] >= cfg.spike_ratio * expected:
            flagged[i] = (expected, excess, z)
    events, cur = [], []
    for i in sorted(flagged):
        if cur and i != cur[-1] + 1:
            events.append(cur)
            cur = []
        cur.append(i)
    if cur:
        events.append(cur)

    spikes, shifts = [], []
    for ev in events:
        peak = max(ev, key=lambda i: values[i])
        before = [adj[j] for j in range(max(0, ev[0] - 14), ev[0]) if j in valid_set]
        after = [adj[j] for j in range(ev[-1] + 1, min(n, ev[-1] + 8)) if j in valid_set]
        item = {
            "start": ev[0], "end": ev[-1], "peak": peak, "peak_value": values[peak],
            "expected": flagged[peak][0], "excess": sum(flagged[i][1] for i in ev),
            "ratio": values[peak] / flagged[peak][0] if flagged[peak][0] > 0 else math.inf,
            "z": max(flagged[i][2] for i in ev), "ongoing": ev[-1] >= n - 2,
        }
        mb = median(before)
        if (len(before) >= 7 and len(after) >= 3 and median(after) >= cfg.spike_ratio * mb
                and median(after) - mb > 0):
            shifts.append(item)  # the cost stayed high: it is the beginning of a level shift, not a spike
        else:
            spikes.append(item)
    return spikes, shifts


def detect_increase(adj, valid, spike_days, n, cfg) -> Optional[Dict]:
    pts = [i for i in valid if i not in spike_days]
    if len(pts) < 14:
        return None
    ys = [adj[i] for i in pts]
    tau, p = mann_kendall(ys)
    r = cfg.recent_days
    recent = [adj[i] for i in pts if i >= n - r]
    early = [adj[i] for i in pts if i < r]
    if not recent or not early:
        return None
    early_avg, recent_avg = mean(early), mean(recent)
    delta = recent_avg - early_avg
    growth = delta / early_avg * 100 if early_avg > 0 else math.inf
    slope, intercept = theil_sen(pts, ys) if tau > 0 else (0.0, median(ys))
    res = {"tau": tau, "p_value": p, "early_avg": early_avg, "recent_avg": recent_avg, "growth_pct": growth,
           "monthly_increase": delta * 30, "slope_per_day": slope, "trend_30d": slope * 30}
    if delta * 30 < cfg.min_monthly_increase:
        return None

    pattern = None
    if tau > 0 and p <= cfg.trend_alpha and slope > 0 and growth >= cfg.min_growth_pct:
        pattern = "gradual"
    else:
        base_lvl, rec_lvl = median(early), median(recent)
        shift = rec_lvl - base_lvl
        persistent = sum(1 for y in recent if y >= base_lvl + 0.5 * shift) >= 0.7 * len(recent)
        if shift > 0 and persistent and growth >= cfg.step_min_pct and p <= cfg.step_alpha:
            pattern = "new" if base_lvl <= 0.01 * rec_lvl else "step"
    if pattern is None:
        return None
    # week-over-week consistency (full weeks counted backwards from the last day)
    weeks = []
    for k in range(n // 7):
        lo, hi = n - 7 * (k + 1), n - 7 * k
        wk = [adj[i] for i in pts if lo <= i < hi]
        if wk:
            weeks.append(mean(wk))
    weeks.reverse()
    res["weeks_up"] = f"{sum(1 for a, b in zip(weeks, weeks[1:]) if b > a)}/{max(len(weeks) - 1, 0)}"
    res["pattern"] = pattern
    return res


def analyze_series(values: List[float], days: List[str], weekdays: List[int], cfg) -> Optional[Dict]:
    if sum(values) < cfg.min_total:
        return None
    n = len(values)
    ms = month_start_info(values, days, cfg.spike_min_abs)
    masked = set(ms["indices"]) if ms else set()
    valid = [i for i in range(n) if i not in masked]
    factors = weekday_factors(values, valid, weekdays)
    adj = [values[i] / factors[weekdays[i]] for i in range(n)]
    spikes, shifts = detect_spikes(values, adj, valid, factors, weekdays, cfg)
    # a jump is a step change only if the cost is still high in the recent days, otherwise it was a burst
    recent = [adj[i] for i in valid if i >= n - cfg.recent_days]
    steps = []
    for s in shifts:
        b = median([adj[i] for i in valid if i < s["start"]])
        if recent and median(recent) >= max(cfg.spike_ratio * b, b + cfg.min_monthly_increase / 30):
            steps.append(s)
        else:
            spikes.append(s)
    spikes.sort(key=lambda s: s["start"])
    spike_days = {i for s in spikes for i in range(s["start"], s["end"] + 1)}
    inc = detect_increase(adj, valid, spike_days, n, cfg)
    if steps:
        s = steps[-1]
        b, a = mean([adj[i] for i in valid if i < s["start"]]), mean(recent)
        if inc is None and (a - b) * 30 >= cfg.min_monthly_increase:
            inc = {"tau": None, "p_value": None, "slope_per_day": None, "trend_30d": None, "weeks_up": ""}
        if inc is not None:
            inc.update(pattern="new" if b <= 0.01 * a else "step", since=days[s["start"]], early_avg=b,
                       recent_avg=a, monthly_increase=(a - b) * 30,
                       growth_pct=(a - b) / b * 100 if b > 0 else math.inf)
    mom_flag = bool(ms and ms.get("mom_delta", 0) >= cfg.min_monthly_increase
                    and ms.get("mom_pct", 0) >= cfg.min_growth_pct)
    if not spikes and inc is None and not ms:
        return None
    return {"spikes": spikes, "increase": inc, "month_start": ms, "month_start_increase": mom_flag,
            "total": sum(values)}


# --------------------------------------------------------------------------- drivers

def drivers(level: str, profile: str, key: Tuple[str, ...], detail: Dict, days_range: Tuple[int, int],
            base_range: Tuple[int, int], top: int = 3) -> str:
    """Detail series (microservice/service/usage type) that explain most of the change of a finding."""
    if level not in DRIVER_FIELDS:
        return ""
    pos = {"microservice": 0, "service": 1, "usage_type": 2}[LEVELS[level][0]]
    contrib = []
    for (prof, dkey), vals in detail.items():
        if prof != profile or dkey[pos] != key[0]:
            continue
        cur = mean(vals[days_range[0]:days_range[1]])
        base = median(vals[base_range[0]:base_range[1]]) if base_range[1] > base_range[0] else 0.0
        delta = (cur - base) * (days_range[1] - days_range[0])
        if delta > 0:
            contrib.append((delta, " / ".join(dkey[{"microservice": 0, "service": 1, "usage_type": 2}[f]]
                                              for f in DRIVER_FIELDS[level])))
    tot = sum(c for c, _ in contrib)
    contrib.sort(reverse=True)
    return "; ".join(f"{label} ({c / tot * 100:.0f}%)" for c, label in contrib[:top]) if tot > 0 else ""


# --------------------------------------------------------------------------- orchestration

def env_name(profile: str) -> str:
    name = profile[len("sso_pn-"):] if profile.startswith("sso_pn-") else profile
    return name[:-3] if name.endswith("-ro") else name


def severity(impact: float, cfg) -> str:
    if impact >= cfg.high_impact:
        return "HIGH"
    return "MEDIUM" if impact >= cfg.medium_impact else "LOW"


def run_analysis(days, series, cfg):
    weekdays = [date.fromisoformat(d).weekday() for d in days]
    n = len(days)
    detail = series["detail"]
    spikes, increases, monthly = [], [], []
    for level in cfg.levels:
        for (profile, key), values in series[level].items():
            res = analyze_series(values, days, weekdays, cfg)
            if not res:
                continue
            ref = {"profile": profile, "env": env_name(profile), "level": level,
                   "resource": " / ".join(key), "key": key, "values": values}
            for s in res["spikes"]:
                lo = max(0, s["start"] - 14)
                spikes.append({**ref, **s, "severity": severity(s["excess"], cfg),
                               "start_date": days[s["start"]], "end_date": days[s["end"]],
                               "peak_date": days[s["peak"]], "days": s["end"] - s["start"] + 1,
                               "drivers": drivers(level, profile, key, detail, (s["start"], s["end"] + 1),
                                                  (lo, s["start"]))})
            inc = res["increase"]
            if inc:
                increases.append({**ref, **inc, "severity": severity(inc["monthly_increase"], cfg),
                                  "drivers": drivers(level, profile, key, detail, (n - cfg.recent_days, n),
                                                     (0, cfg.recent_days))})
            ms = res["month_start"]
            if ms:
                monthly.append({**ref, **ms, "flag": res["month_start_increase"]})
    spikes.sort(key=lambda x: x["excess"], reverse=True)
    increases.sort(key=lambda x: x["monthly_increase"], reverse=True)
    monthly.sort(key=lambda x: (not x["flag"], -x["charges"][-1][1]))
    return spikes, increases, monthly


# --------------------------------------------------------------------------- output

def money(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isinf(v)):
        return "n/a"
    return f"${v:,.0f}"


def pct(v) -> str:
    if v is None:
        return ""
    return "new" if math.isinf(v) else f"{v:+.0f}%"


def account_overview(totals, days, accounts, cfg) -> List[Dict]:
    n, r = len(days), cfg.recent_days
    out = []
    for prof, vals in totals.items():
        first, last = mean(vals[:r]), mean(vals[n - r:])
        out.append({"profile": prof, "env": env_name(prof), "account_id": accounts.get(prof, ""),
                    "total": sum(vals), "first_avg": first, "last_avg": last, "values": vals,
                    "change_pct": (last - first) / first * 100 if first > 0 else math.inf})
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


def summary_data(days, overview, spikes, increases, monthly, cfg) -> Dict:
    """Figures and key messages (plain text) shared by the HTML and XLSX summaries."""
    lv = set(cfg.summary_levels)
    s_inc = [x for x in increases if x["level"] in lv]
    s_spk = [x for x in spikes if x["level"] in lv]
    s_mon = [x for x in monthly if x["level"] in lv and x["flag"]]
    r = cfg.recent_days
    first = sum(o["first_avg"] for o in overview)
    last = sum(o["last_avg"] for o in overview)
    d = {"inc": s_inc, "spk": s_spk, "mon": s_mon, "total": sum(o["total"] for o in overview),
         "first": first, "last": last, "change": (last - first) / first * 100 if first else math.inf,
         "inc_high": sum(1 for x in s_inc if x["severity"] == "HIGH"),
         "inc_monthly": sum(x["monthly_increase"] for x in s_inc),
         "spk_high": sum(1 for x in s_spk if x["severity"] == "HIGH"),
         "spk_excess": sum(x["excess"] for x in s_spk)}
    msgs = [
        f"Total analysed cost {money(d['total'])}; average daily run-rate went from {money(first)}/day "
        f"(first {r} days) to {money(last)}/day (last {r} days), {pct(d['change'])}.",
        f"{len(s_inc)} sustained cost increases ({d['inc_high']} high severity), about "
        f"{money(d['inc_monthly'])}/month of additional run-rate (not additive across levels: a microservice "
        f"and its AWS services overlap).",
        f"{len(s_spk)} anomalous spike events, total excess cost {money(d['spk_excess'])} over the expected cost.",
    ]
    for x in s_inc[:3]:
        msgs.append(f"{x['env']} - {LEVEL_LABELS[x['level']]} {x['resource']}: "
                    f"{PATTERN_LABELS[x['pattern']].lower()}, {money(x['early_avg'])}/day -> "
                    f"{money(x['recent_avg'])}/day (~{money(x['monthly_increase'])}/month more)"
                    + (f"; driven by {x['drivers'].split(';')[0]}" if x["drivers"] else "") + ".")
    if s_mon:
        msgs.append(f"{len(s_mon)} month-start charges increased month over month.")
    d["messages"] = msgs
    return d


def _sparkline(values: List[float], marks: Sequence[Tuple[int, int]] = (), split: Optional[int] = None,
               width: int = 180, height: int = 36) -> str:
    """Inline SVG line chart; `marks` are highlighted day ranges, `split` a vertical marker."""
    vmax = max(max(values), 0.0) or 1.0
    vmin = min(0.0, min(values))
    span = (vmax - vmin) or 1.0
    step = width / max(len(values) - 1, 1)

    def y(v):
        return height - 2 - (v - vmin) / span * (height - 4)
    pts = " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(values))
    parts = [f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for lo, hi in marks:
        parts.append(f'<rect class="mark" x="{max(lo * step - step / 2, 0):.1f}" y="0" '
                     f'width="{max((hi - lo + 1) * step, 2):.1f}" height="{height}"/>')
    if split is not None:
        parts.append(f'<line class="split" x1="{split * step:.1f}" y1="0" x2="{split * step:.1f}" y2="{height}"/>')
    parts.append(f'<polyline points="{pts}"/></svg>')
    return "".join(parts)


HTML_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px;color:#1f2937;background:#f8fafc}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:2px solid #e5e7eb;padding-bottom:4px}
.muted{color:#6b7280;font-size:12px}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;min-width:180px}
.card .v{font-size:22px;font-weight:600}.card .l{font-size:12px;color:#6b7280}
ul.msgs{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:12px 12px 12px 32px;margin:0}
ul.msgs li{margin:4px 0}.tw{overflow-x:auto}
table{border-collapse:collapse;background:#fff;font-size:12.5px;width:100%}
th,td{border:1px solid #e5e7eb;padding:5px 8px;text-align:left;vertical-align:middle}
th{background:#305496;color:#fff;position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap}
th.num,td.num{text-align:right;white-space:nowrap}tr:nth-child(even) td{background:#f9fafb}
.sev{display:inline-block;padding:1px 8px;border-radius:10px;font-weight:600;font-size:11px}
.HIGH{background:#f8cbad;color:#7f1d1d}.MEDIUM{background:#ffe699;color:#78350f}.LOW{background:#e2efda;color:#14532d}
.up{color:#b91c1c;font-weight:600}.down{color:#15803d}.drv{font-size:11.5px;color:#374151;max-width:420px}
svg.spark polyline{fill:none;stroke:#0b5fff;stroke-width:1.4}svg.spark .mark{fill:#fca5a5;opacity:.55}
svg.spark .split{stroke:#9ca3af;stroke-dasharray:2 2}.method li{margin:4px 0;font-size:13px}
.legend{font-size:12px;color:#6b7280;margin:4px 0 8px}.legend i{display:inline-block;width:12px;height:10px;
background:#fca5a5;margin:0 4px -1px 0}
"""

HTML_JS = """
document.querySelectorAll('table.sortable th').forEach(function(th){th.addEventListener('click',function(){
var t=th.closest('table'),b=t.tBodies[0],i=Array.prototype.indexOf.call(th.parentNode.children,th),
asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
var rows=Array.prototype.slice.call(b.rows);rows.sort(function(a,c){
var x=a.cells[i].dataset.v||a.cells[i].textContent,y=c.cells[i].dataset.v||c.cells[i].textContent,
nx=parseFloat(x),ny=parseFloat(y);var r=(!isNaN(nx)&&!isNaN(ny))?nx-ny:x.localeCompare(y);return asc?r:-r;});
rows.forEach(function(r){b.appendChild(r);});});});
"""


def write_html(path, days, overview, spikes, increases, monthly, estimated, cfg, source) -> None:
    from html import escape as e

    sd = summary_data(days, overview, spikes, increases, monthly, cfg)
    r, n = cfg.recent_days, len(days)

    def num(v, text=None):
        sort = "" if v is None or (isinstance(v, float) and math.isinf(v)) else f' data-v="{v:.4f}"'
        return f'<td class="num"{sort}>{e(text if text is not None else money(v))}</td>'

    def change(v):
        cls = "up" if v is not None and v > 0 else "down"
        sort = "" if v is None or math.isinf(v) else f' data-v="{v:.4f}"'
        return f'<td class="num {cls}"{sort}>{e(pct(v))}</td>'

    def sev(x):
        order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}[x["severity"]]
        return f'<td data-v="{order}"><span class="sev {x["severity"]}">{x["severity"]}</span></td>'

    def drv(x):
        return f'<td class="drv">{"<br>".join(e(p.strip()) for p in x["drivers"].split(";") if p.strip())}</td>'

    H = ['<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
         f"<title>AWS cost anomalies - SEND ({days[0]} - {days[-1]})</title><style>{HTML_CSS}</style></head><body>",
         f"<h1>AWS cost anomalies - SEND accounts</h1>",
         f'<div class="muted">Period {days[0]} &rarr; {days[-1]} ({n} days) &middot; generated '
         f"{datetime.now():%Y-%m-%d %H:%M} from <code>{e(os.path.basename(source))}</code> &middot; excluded "
         f"services: {e(', '.join(cfg.exclude_services) or 'none')} &middot; levels in this report: "
         f"{e(', '.join(LEVEL_LABELS[x] for x in cfg.summary_levels))}</div>"]
    if estimated:
        H.append(f'<div class="muted">Costs from {estimated[0]} onwards are still <b>estimated</b> by AWS and '
                 f"may change.</div>")

    H.append('<div class="cards">')
    for value, label in ((money(sd["total"]), f"Total cost in {n} days"),
                         (f'{money(sd["first"])} &rarr; {money(sd["last"])}',
                          f"Avg daily cost, first vs last {r} days ({pct(sd['change'])})"),
                         (f'{len(sd["inc"])} <span class="muted">({sd["inc_high"]} high)</span>',
                          f'Sustained increases, ~{money(sd["inc_monthly"])}/month'),
                         (f'{len(sd["spk"])} <span class="muted">({sd["spk_high"]} high)</span>',
                          f'Spike events, {money(sd["spk_excess"])} excess')):
        H.append(f'<div class="card"><div class="v">{value}</div><div class="l">{e(label)}</div></div>')
    H.append("</div><h2>Key messages</h2><ul class=\"msgs\">")
    H.extend(f"<li>{e(m)}</li>" for m in sd["messages"])
    H.append("</ul>")

    H.append(f'<h2>Accounts overview</h2><div class="tw"><table class="sortable"><thead><tr><th>Account</th>'
             f'<th>Account ID</th><th class="num">Total</th><th class="num">Avg/day first {r}d</th>'
             f'<th class="num">Avg/day last {r}d</th><th class="num">Change</th><th>Daily cost</th></tr></thead><tbody>')
    for o in overview:
        H.append(f"<tr><td>{e(o['env'])}</td><td>{e(o['account_id'])}</td>{num(o['total'])}{num(o['first_avg'])}"
                 f"{num(o['last_avg'])}{change(o['change_pct'])}<td>{_sparkline(o['values'])}</td></tr>")
    H.append("</tbody></table></div>")

    H.append(f"<h2>Sustained increases (top {cfg.top} of {len(sd['inc'])})</h2>")
    H.append('<div class="legend">Dashed line: start of the step increase, when detected.</div>')
    if sd["inc"]:
        H.append('<div class="tw"><table class="sortable"><thead><tr><th>Severity</th><th>Account</th><th>Level</th>'
                 '<th>Resource</th><th>Pattern</th><th class="num">Avg/day before</th><th class="num">Avg/day now</th>'
                 '<th class="num">Change</th><th class="num">Extra/month</th><th>Weeks up</th><th>Daily cost</th>'
                 "<th>Main drivers</th></tr></thead><tbody>")
        for x in sd["inc"][:cfg.top]:
            pattern = PATTERN_LABELS[x["pattern"]] + (f" since {x['since']}" if x.get("since") else "")
            split = days.index(x["since"]) if x.get("since") else None
            H.append(f"<tr>{sev(x)}<td>{e(x['env'])}</td><td>{e(LEVEL_LABELS[x['level']])}</td>"
                     f"<td><b>{e(x['resource'])}</b></td><td>{e(pattern)}</td>{num(x['early_avg'])}"
                     f"{num(x['recent_avg'])}{change(x['growth_pct'])}{num(x['monthly_increase'])}"
                     f"<td>{e(x['weeks_up'])}</td><td>{_sparkline(x['values'], split=split)}</td>{drv(x)}</tr>")
        H.append("</tbody></table></div>")
    else:
        H.append('<p class="muted">No sustained increases above thresholds.</p>')

    H.append(f"<h2>Anomalous spikes (top {cfg.top} of {len(sd['spk'])})</h2>")
    H.append('<div class="legend"><i></i>spike days</div>')
    if sd["spk"]:
        H.append('<div class="tw"><table class="sortable"><thead><tr><th>Severity</th><th>Account</th><th>Level</th>'
                 '<th>Resource</th><th>When</th><th class="num">Peak cost</th><th class="num">Expected</th>'
                 '<th class="num">Ratio</th><th class="num">Excess</th><th>Daily cost</th><th>Main drivers</th>'
                 "</tr></thead><tbody>")
        for x in sd["spk"][:cfg.top]:
            when = x["start_date"] if x["days"] == 1 else f"{x['start_date']} → {x['end_date']}"
            if x["ongoing"]:
                when += " (ongoing)"
            ratio = None if math.isinf(x["ratio"]) else x["ratio"]
            H.append(f"<tr>{sev(x)}<td>{e(x['env'])}</td><td>{e(LEVEL_LABELS[x['level']])}</td>"
                     f"<td><b>{e(x['resource'])}</b></td><td data-v=\"{x['start_date']}\">{e(when)}</td>"
                     f"{num(x['peak_value'])}{num(x['expected'])}"
                     f"{num(ratio, 'n/a' if ratio is None else f'x{ratio:.1f}')}{num(x['excess'])}"
                     f"<td>{_sparkline(x['values'], marks=[(x['start'], x['end'])])}</td>{drv(x)}</tr>")
        H.append("</tbody></table></div>")
    else:
        H.append('<p class="muted">No spikes above thresholds.</p>')

    if sd["mon"]:
        H.append('<h2>Month-start charges increased month over month</h2><div class="tw"><table class="sortable">'
                 "<thead><tr><th>Account</th><th>Level</th><th>Resource</th><th>Charges</th>"
                 '<th class="num">Change</th><th class="num">Change $</th></tr></thead><tbody>')
        for x in sd["mon"][:cfg.top]:
            ch = "<br>".join(f"{d}: {money(v)}" for d, v in x["charges"])
            H.append(f"<tr><td>{e(x['env'])}</td><td>{e(LEVEL_LABELS[x['level']])}</td><td><b>{e(x['resource'])}"
                     f"</b></td><td>{ch}</td>{change(x['mom_pct'])}{num(x['mom_delta'])}</tr>")
        H.append("</tbody></table></div>")

    H.append('<h2>Method</h2><ul class="method">')
    H.extend(f"<li>{e(m)}</li>" for m in method_notes(cfg))
    H.append(f"</ul><script>{HTML_JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(H))


def method_notes(cfg) -> List[str]:
    r = cfg.recent_days
    return [
        f"Each daily series is weekday-adjusted (weekends cost less) and costs billed on the 1st of the month are "
        f"set apart. Series with less than {money(cfg.min_total)} in the period are ignored.",
        f"Spike: robust z-score >= {cfg.spike_z} against a {2 * cfg.spike_window + 1}-day rolling median, at least "
        f"x{cfg.spike_ratio} the expected cost and {money(cfg.spike_min_abs)} above it. A jump that is still high "
        f"in the last {r} days is reported as a step increase.",
        f"Gradual growth: Mann-Kendall trend test (p <= {cfg.trend_alpha}) with positive Theil-Sen slope and "
        f"average daily cost of the last {r} days >= +{cfg.min_growth_pct:.0f}% vs the first {r} days.",
        f"Step increase / new cost: a persistent jump (shown with its start date; before/now averages are computed "
        f"around the jump), or a level of the last {r} days >= +{cfg.step_min_pct:.0f}% vs the first {r} days, "
        f"held for most of the last {r} days.",
        f"Increases are reported only when worth >= {money(cfg.min_monthly_increase)}/month (extra/month = "
        f"difference of average daily cost x 30). Severity by monthly impact (increases) or excess cost (spikes): "
        f"HIGH >= {money(cfg.high_impact)}, MEDIUM >= {money(cfg.medium_impact)}.",
        "(untagged) = resources without the Microservice tag; (non dettagliato) = services below the "
        "cost-analyzer detail threshold. All levels and the daily values are in the XLSX report.",
    ]


def write_xlsx(path, days, overview, spikes, increases, monthly, cfg) -> None:
    from openpyxl import Workbook
    from openpyxl.chart import LineChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    sev_fill = {"HIGH": "F8CBAD", "MEDIUM": "FFE699", "LOW": "E2EFDA"}

    def sheet(ws, headers, rows, sev_col=None):
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="305496")
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for row in rows:
            ws.append([None if isinstance(v, float) and math.isinf(v) else v for v in row])
            if sev_col is not None and row[sev_col] in sev_fill:
                ws.cell(ws.max_row, sev_col + 1).fill = PatternFill("solid", fgColor=sev_fill[row[sev_col]])
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        for col in ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)):
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(width + 2, 10), 60)

    def r2(v):
        return None if v is None or (isinstance(v, float) and math.isinf(v)) else round(v, 2)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"AWS cost anomalies - SEND accounts ({days[0]} -> {days[-1]}, {len(days)} days)"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Key messages"])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    for m in summary_data(days, overview, spikes, increases, monthly, cfg)["messages"]:
        ws.append([f"- {m}"])
    ws.append([])
    ws.append(["Method"])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    for m in method_notes(cfg):
        ws.append([f"- {m}"])
    ws.column_dimensions["A"].width = 180

    sheet(wb.create_sheet("Accounts"), ["Account", "Account ID", "Total", "Avg/day first", "Avg/day last",
                                        "Change %"],
          [[o["env"], o["account_id"], r2(o["total"]), r2(o["first_avg"]), r2(o["last_avg"]),
            r2(o["change_pct"])] for o in overview])

    sheet(wb.create_sheet("Sustained increases"),
          ["Severity", "Account", "Level", "Microservice", "AWS service", "Usage type", "Pattern", "Since",
           "Avg/day before", "Avg/day now", "Change %", "Extra cost/month", "Trend slope $/day per 30d",
           "Mann-Kendall tau", "p-value", "Weeks up", "Period total", "Main drivers"],
          [[x["severity"], x["env"], LEVEL_LABELS[x["level"]], *_split_key(x), PATTERN_LABELS[x["pattern"]],
            x.get("since", ""), r2(x["early_avg"]), r2(x["recent_avg"]), r2(x["growth_pct"]),
            r2(x["monthly_increase"]), r2(x.get("trend_30d")),
            None if x.get("tau") is None else round(x["tau"], 3),
            None if x.get("p_value") is None else float(f"{x['p_value']:.2e}"), x["weeks_up"],
            r2(sum(x["values"])), x["drivers"]] for x in increases], sev_col=0)

    sheet(wb.create_sheet("Spikes"),
          ["Severity", "Account", "Level", "Microservice", "AWS service", "Usage type", "Start", "End", "Days",
           "Peak date", "Peak cost", "Expected cost", "Ratio", "Excess cost", "Robust z", "Ongoing",
           "Main drivers"],
          [[x["severity"], x["env"], LEVEL_LABELS[x["level"]], *_split_key(x), x["start_date"], x["end_date"],
            x["days"], x["peak_date"], r2(x["peak_value"]), r2(x["expected"]), r2(x["ratio"]), r2(x["excess"]),
            round(min(x["z"], 1e6), 1), x["ongoing"], x["drivers"]] for x in spikes], sev_col=0)

    max_ch = max((len(x["charges"]) for x in monthly), default=0)
    sheet(wb.create_sheet("Month-start charges"),
          ["Increased", "Account", "Level", "Microservice", "AWS service", "Usage type",
           *[f"Charge {i + 1}" for i in range(max_ch)], "MoM change", "MoM change %", "Typical daily cost"],
          [[x["flag"], x["env"], LEVEL_LABELS[x["level"]], *_split_key(x),
            *[f"{d}: {v:,.2f}" for d, v in x["charges"]], *[""] * (max_ch - len(x["charges"])),
            r2(x.get("mom_delta")), r2(x.get("mom_pct")), r2(x["base_daily"])] for x in monthly])

    # daily values of every finding, plus charts for the top items of the summary levels
    ws = wb.create_sheet("Daily series")
    seen, rows = set(), []
    for kind, items in (("Increase", increases), ("Spike", spikes)):
        for x in items:
            k = (x["profile"], x["level"], x["key"])
            if k not in seen:
                seen.add(k)
                rows.append([kind, x["env"], LEVEL_LABELS[x["level"]], x["resource"],
                             *[round(v, 2) for v in x["values"]]])
    sheet(ws, ["First finding", "Account", "Level", "Resource", *days], rows)

    lv = set(cfg.summary_levels)
    charts = wb.create_sheet("Charts")
    for n_chart, (title, items) in enumerate((("Top sustained increases", increases), ("Top spikes", spikes))):
        top = []
        for x in items:
            if x["level"] in lv and (x["profile"], x["level"], x["key"]) not in {(t["profile"], t["level"], t["key"])
                                                                                for t in top}:
                top.append(x)
            if len(top) == 6:
                break
        if not top:
            continue
        base_row = charts.max_row + 2 if charts.max_row > 1 else 1
        charts.cell(base_row, 1, "Series")
        for j, d in enumerate(days):
            charts.cell(base_row, j + 2, d)
        for i, x in enumerate(top, 1):
            charts.cell(base_row + i, 1, f"{x['env']} - {x['resource']}")
            for j, v in enumerate(x["values"]):
                charts.cell(base_row + i, j + 2, round(v, 2))
        ch = LineChart()
        ch.title, ch.y_axis.title, ch.height, ch.width = title, "USD/day", 10, 30
        ch.add_data(Reference(charts, min_col=1, max_col=len(days) + 1, min_row=base_row + 1,
                              max_row=base_row + len(top)), from_rows=True, titles_from_data=True)
        ch.set_categories(Reference(charts, min_col=2, max_col=len(days) + 1, min_row=base_row))
        charts.add_chart(ch, f"A{20 + n_chart * 22}")

    sheet(wb.create_sheet("Parameters"), ["Parameter", "Value"],
          [[k, ", ".join(v) if isinstance(v, list) else v] for k, v in sorted(vars(cfg).items())])
    wb.save(path)


def _split_key(x) -> List[str]:
    k = dict(zip(LEVELS[x["level"]], x["key"]))
    return [k.get("microservice", ""), k.get("service", ""), k.get("usage_type", "")]


# --------------------------------------------------------------------------- CLI

def csv_list(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_args(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", help="raw CSV produced by pn_cost_analyzer.py (default: latest *_raw.csv in output/)")
    p.add_argument("--output-dir", help="output directory (default: same directory as the input)")
    p.add_argument("--days", type=int, help="analyse only the last N days of the input (default: all)")
    p.add_argument("--drop-last-days", type=int, default=0,
                   help="ignore the last N days, e.g. when the last day is incomplete (default 0)")
    p.add_argument("--exclude-services", type=csv_list, default=["Tax"],
                   help="AWS services to ignore, comma separated (default Tax; '' for none)")
    p.add_argument("--levels", type=csv_list, default=list(LEVELS), help="levels to analyse (default all)")
    p.add_argument("--summary-levels", type=csv_list, default=["microservice", "service"],
                   help="levels shown in the executive summary (default microservice,service)")
    p.add_argument("--min-total", type=float, default=50.0,
                   help="ignore series costing less than this in the period (USD, default 50)")
    p.add_argument("--spike-z", type=float, default=5.0, help="robust z-score threshold for spikes (default 5)")
    p.add_argument("--spike-ratio", type=float, default=1.5, help="min peak/expected ratio for spikes (default 1.5)")
    p.add_argument("--spike-min-abs", type=float, default=25.0, help="min daily excess for spikes (USD, default 25)")
    p.add_argument("--spike-window", type=int, default=7, help="half-width of the rolling median window (default 7)")
    p.add_argument("--trend-alpha", type=float, default=0.01, help="Mann-Kendall significance (default 0.01)")
    p.add_argument("--step-alpha", type=float, default=0.05,
                   help="Mann-Kendall significance required for step increases (default 0.05)")
    p.add_argument("--min-growth-pct", type=float, default=20.0,
                   help="min growth over the period for gradual increases (%%, default 20)")
    p.add_argument("--step-min-pct", type=float, default=30.0, help="min growth for step increases (%%, default 30)")
    p.add_argument("--min-monthly-increase", type=float, default=100.0,
                   help="min additional monthly run-rate to report an increase (USD, default 100)")
    p.add_argument("--recent-days", type=int, default=14, help="days considered 'recent' (default 14)")
    p.add_argument("--high-impact", type=float, default=1000.0, help="USD threshold for HIGH severity (default 1000)")
    p.add_argument("--medium-impact", type=float, default=250.0,
                   help="USD threshold for MEDIUM severity (default 250)")
    p.add_argument("--top", type=int, default=15, help="rows per table in the executive summary (default 15)")
    args = p.parse_args(argv)
    for lvl in args.levels + args.summary_levels:
        if lvl not in LEVELS:
            p.error(f"unknown level {lvl!r}, valid: {', '.join(LEVELS)}")
    args.levels = list(dict.fromkeys(args.levels + args.summary_levels))
    if not args.input:
        args.input = latest_raw_csv(os.path.join(here, "output"))
        if not args.input:
            p.error("no *_raw.csv found in output/, use --input")
    args.output_dir = args.output_dir or os.path.dirname(os.path.abspath(args.input))
    return args


def main(argv=None) -> int:
    cfg = parse_args(argv)
    print(f"Reading {cfg.input}", file=sys.stderr)
    days, series, totals, accounts, estimated = load_series(cfg.input, cfg.exclude_services, cfg.days,
                                                            cfg.drop_last_days)
    print(f"Period {days[0]} -> {days[-1]} ({len(days)} days), {len(totals)} accounts", file=sys.stderr)
    spikes, increases, monthly = run_analysis(days, series, cfg)
    overview = account_overview(totals, days, accounts, cfg)

    os.makedirs(cfg.output_dir, exist_ok=True)
    base = os.path.join(cfg.output_dir, f"pn-cost-anomalies_{days[0]}_{days[-1]}")
    write_html(base + ".html", days, overview, spikes, increases, monthly, estimated, cfg, cfg.input)
    write_xlsx(base + ".xlsx", days, overview, spikes, increases, monthly, cfg)
    print(f"{len(increases)} sustained increases, {len(spikes)} spikes, "
          f"{sum(1 for m in monthly if m['flag'])} month-start increases", file=sys.stderr)
    print(f"Executive summary: {base}.html\nDetailed report  : {base}.xlsx", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
