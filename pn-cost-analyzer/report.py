"""Generazione report HTML e CSV."""
from __future__ import annotations

import csv
import html
import math
from datetime import datetime
from typing import Dict, List, Sequence

from analysis import LEVEL_LABELS, LEVELS

RAW_FIELDS = ["profile", "account_id", "date", "microservice", "service", "usage_type", "cost", "unit", "estimated"]


def write_raw_csv(path: str, accounts: List[Dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(RAW_FIELDS)
        for acc in accounts:
            data = acc["data"]
            for r in sorted(data["rows"], key=lambda x: (x["date"], x["microservice"], x["service"], x["usage_type"])):
                w.writerow([data["profile"], data["account_id"], r["date"], r["microservice"], r["service"],
                            r["usage_type"], f"{r['cost']:.6f}", r["unit"], r["estimated"]])


def write_trend_csv(path: str, accounts: List[Dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["profile", "account_id", "level", "microservice", "service", "usage_type", "total",
                    "prev_period", "recent_period", "delta", "delta_pct", "slope_per_day", "is_new", "growing"])
        for acc in accounts:
            data, an = acc["data"], acc["analysis"]
            for level, lv in an["levels"].items():
                for it in lv["items"]:
                    k = dict(zip(lv["fields"], it["key"]))
                    pct = "" if math.isinf(it["delta_pct"]) else f"{it['delta_pct']:.2f}"
                    w.writerow([data["profile"], data["account_id"], level, k.get("microservice", ""),
                                k.get("service", ""), k.get("usage_type", ""), f"{it['total']:.4f}",
                                f"{it['prev']:.4f}", f"{it['recent']:.4f}", f"{it['delta']:.4f}", pct,
                                f"{it['slope_per_day']:.6f}", it["is_new"], it["growing"]])


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    if math.isinf(v):
        return "nuovo"
    return f"{v:+.1f}%"


def _sparkline(values: Sequence[float], split: int, width: int = 160, height: int = 32) -> str:
    if not values:
        return ""
    vmax = max(values) or 1.0
    vmin = min(0.0, min(values))
    span = (vmax - vmin) or 1.0
    step = width / max(len(values) - 1, 1)
    pts = " ".join(f"{i * step:.1f},{height - (v - vmin) / span * (height - 2) - 1:.1f}" for i, v in enumerate(values))
    x = split * step
    return (f'<svg class="spark" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{height}" class="split"/>'
            f'<polyline points="{pts}"/></svg>')


def _chart(days: List[str], values: List[float], split: int, estimated: set, width: int = 900,
           height: int = 220) -> str:
    if not values:
        return ""
    pad_l, pad_b, pad_t = 70, 30, 10
    w, h = width - pad_l - 10, height - pad_b - pad_t
    vmax = max(values) or 1.0
    bw = w / len(values)
    parts = [f'<svg class="chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for t in range(5):
        v = vmax * t / 4
        y = pad_t + h - h * t / 4
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - 10}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{_money(v)}</text>')
    for i, (d, v) in enumerate(zip(days, values)):
        bh = h * v / vmax if vmax else 0
        cls = "bar est" if d in estimated else ("bar recent" if i >= split else "bar")
        parts.append(f'<rect class="{cls}" x="{pad_l + i * bw + 1:.1f}" y="{pad_t + h - bh:.1f}" '
                     f'width="{max(bw - 2, 1):.1f}" height="{bh:.1f}"><title>{d}: {_money(v)}</title></rect>')
        if i % 7 == 0:
            parts.append(f'<text x="{pad_l + i * bw:.1f}" y="{height - 10}">{d[5:]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _items_table(items: List[Dict], fields: Sequence[str], split: int, limit: int, cls: str) -> str:
    if not items:
        return '<p class="muted">Nessun elemento.</p>'
    head = "".join(f"<th>{html.escape(LEVEL_LABELS.get(f, f))}</th>" for f in fields)
    out = [f'<div class="tw"><table class="sortable {cls}"><thead><tr>{head}<th class="num">Totale</th>'
           '<th class="num">Periodo prec.</th><th class="num">Periodo recente</th><th class="num">Delta</th>'
           '<th class="num">Delta %</th><th class="num">Trend $/giorno</th><th>Andamento</th></tr></thead><tbody>']
    for it in items[:limit]:
        keys = "".join(f"<td>{html.escape(k)}</td>" for k in it["key"])
        pct_sort = 1e12 if math.isinf(it["delta_pct"]) else it["delta_pct"]
        dcls = "up" if it["delta"] > 0 else ("down" if it["delta"] < 0 else "")
        out.append(
            f"<tr>{keys}<td class='num' data-v='{it['total']}'>{_money(it['total'])}</td>"
            f"<td class='num' data-v='{it['prev']}'>{_money(it['prev'])}</td>"
            f"<td class='num' data-v='{it['recent']}'>{_money(it['recent'])}</td>"
            f"<td class='num {dcls}' data-v='{it['delta']}'>{_money(it['delta'])}</td>"
            f"<td class='num {dcls}' data-v='{pct_sort}'>{_pct(it['delta_pct'])}</td>"
            f"<td class='num' data-v='{it['slope_per_day']}'>{it['slope_per_day']:+.3f}</td>"
            f"<td>{_sparkline(it['values'], split)}</td></tr>")
    out.append("</tbody></table></div>")
    if len(items) > limit:
        out.append(f'<p class="muted">Mostrati {limit} di {len(items)} elementi (vedi CSV per l\'elenco completo).</p>')
    return "".join(out)


CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px;color:#1d2433;background:#f6f7f9}
h1{margin-bottom:4px}h2{margin-top:40px;border-bottom:2px solid #0b5fff;padding-bottom:4px}
section.account{background:#fff;padding:16px 24px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:32px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{padding:4px 8px;border-bottom:1px solid #e3e6eb;text-align:left;vertical-align:middle}
th{background:#eef1f5;cursor:pointer;position:sticky;top:0}
td.num,th.num{text-align:right;white-space:nowrap}
.up{color:#c62828;font-weight:600}.down{color:#2e7d32}
.muted{color:#6b7280;font-size:12px}
.kpi{display:inline-block;margin:8px 24px 8px 0}.kpi b{display:block;font-size:20px}
svg.spark polyline{fill:none;stroke:#0b5fff;stroke-width:1.5}svg line.split{stroke:#bbb;stroke-dasharray:2 2}
svg.chart{width:100%;max-width:900px;height:auto}.tw{overflow-x:auto}svg.chart text{font-size:10px;fill:#6b7280}svg.chart .grid{stroke:#eee}
svg.chart .bar{fill:#9db8f5}svg.chart .bar.recent{fill:#0b5fff}svg.chart .bar.est{fill:#f5b041}
details{margin:8px 0}summary{cursor:pointer;font-weight:600;padding:4px 0}
nav a{margin-right:12px}
"""

JS = """
document.querySelectorAll('table.sortable th').forEach(function(th){
  th.addEventListener('click',function(){
    var table=th.closest('table'),tbody=table.tBodies[0],idx=Array.prototype.indexOf.call(th.parentNode.children,th);
    var asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
    var rows=Array.prototype.slice.call(tbody.rows);
    rows.sort(function(a,b){
      var x=a.cells[idx],y=b.cells[idx];
      var vx=x.dataset.v!==undefined?parseFloat(x.dataset.v):x.textContent;
      var vy=y.dataset.v!==undefined?parseFloat(y.dataset.v):y.textContent;
      if(vx<vy)return asc?-1:1;if(vx>vy)return asc?1:-1;return 0;});
    rows.forEach(function(r){tbody.appendChild(r);});
  });
});
"""


def write_html(path: str, accounts: List[Dict], errors: Dict[str, str], params: Dict) -> None:
    out = ["<!DOCTYPE html><html lang='it'><head><meta charset='utf-8'>",
           "<title>SEND - Analisi costi AWS</title>", f"<style>{CSS}</style></head><body>",
           "<h1>SEND - Analisi costi AWS</h1>",
           f"<p class='muted'>Periodo {html.escape(params['start'])} &rarr; {html.escape(params['end_inclusive'])} "
           f"({params['days']} giorni) &middot; metrica {html.escape(params['metric'])} &middot; tag "
           f"<code>{html.escape(params['tag_key'])}</code> &middot; record type esclusi: "
           f"{html.escape(', '.join(params['excluded_record_types']) or 'nessuno')} &middot; generato il "
           f"{datetime.now().strftime('%Y-%m-%d %H:%M')}</p>",
           "<p class='muted'>Il confronto avviene tra la prima e la seconda metà del periodo (valori normalizzati "
           "sulla stessa durata). Un elemento è considerato <b>in crescita</b> se delta &ge; "
           f"{_money(params['min_delta_abs'])}, delta % &ge; {params['min_delta_pct']:.0f}% e la pendenza della "
           "regressione lineare sui costi giornalieri è positiva. Le barre arancioni indicano giorni con costi "
           "stimati (non ancora consolidati).</p>"]

    out.append("<h2>Riepilogo account</h2><table class='sortable'><thead><tr><th>Profilo</th><th>Account</th>"
               "<th class='num'>Totale periodo</th><th class='num'>Periodo prec.</th><th class='num'>Periodo recente</th>"
               "<th class='num'>Delta</th><th class='num'>Delta %</th><th class='num'>Microservice in crescita</th>"
               "<th class='num'>Servizi in crescita</th><th class='num'>Usage type in crescita</th></tr></thead><tbody>")
    for acc in accounts:
        d, an = acc["data"], acc["analysis"]
        half = len(an["days"]) // 2
        prev_n = an["total_prev"] * half / (len(an["days"]) - half) if an["days"] else 0
        delta = an["total_recent"] - prev_n
        pct = delta / prev_n * 100 if prev_n else float("inf")
        dcls = "up" if delta > 0 else "down"
        out.append(f"<tr><td><a href='#{html.escape(d['profile'])}'>{html.escape(d['profile'])}</a></td>"
                   f"<td>{html.escape(d['account_id'])}</td>"
                   f"<td class='num' data-v='{an['total']}'>{_money(an['total'])}</td>"
                   f"<td class='num' data-v='{prev_n}'>{_money(prev_n)}</td>"
                   f"<td class='num' data-v='{an['total_recent']}'>{_money(an['total_recent'])}</td>"
                   f"<td class='num {dcls}' data-v='{delta}'>{_money(delta)}</td>"
                   f"<td class='num {dcls}' data-v='{0 if math.isinf(pct) else pct}'>{_pct(pct)}</td>"
                   + "".join(f"<td class='num' data-v='{len(an['levels'][lv]['growing'])}'>"
                             f"{len(an['levels'][lv]['growing'])}</td>"
                             for lv in ("microservice", "service", "usage_type")) + "</tr>")
    out.append("</tbody></table>")
    for profile, err in errors.items():
        out.append(f"<p class='up'>Profilo {html.escape(profile)}: errore - {html.escape(err)}</p>")

    limit = params["top"]
    for acc in accounts:
        d, an = acc["data"], acc["analysis"]
        split = len(an["days"]) - len(an["days"]) // 2
        prev_p, rec_p = an["prev_period"], an["recent_period"]
        out.append(f"<section class='account' id='{html.escape(d['profile'])}'>"
                   f"<h2>{html.escape(d['profile'])} &mdash; {html.escape(d['account_id'])}</h2>"
                   f"<p class='muted'>Dati scaricati il {html.escape(d['fetched_at'])} "
                   f"({d['ce_api_calls']} chiamate Cost Explorer). Periodo precedente {prev_p[0]} &rarr; {prev_p[1]}, "
                   f"periodo recente {rec_p[0]} &rarr; {rec_p[1]}.</p>"
                   f"<div class='kpi'>Totale<b>{_money(an['total'])}</b></div>"
                   f"<div class='kpi'>Periodo precedente<b>{_money(an['total_prev'])}</b></div>"
                   f"<div class='kpi'>Periodo recente<b>{_money(an['total_recent'])}</b></div>")
        out.append(_chart(an["days"], an["total_daily"], split, set(an["estimated_days"])))
        for level in ("microservice", "service", "usage_type", "detail"):
            lv = an["levels"][level]
            label = LEVEL_LABELS[level]
            out.append(f"<h3>Costi crescenti per {html.escape(label)} ({len(lv['growing'])})</h3>")
            out.append(_items_table(lv["growing"], LEVELS[level], split, limit, "growing"))
            out.append(f"<details><summary>Top {limit} per costo totale - {html.escape(label)}</summary>"
                       f"{_items_table(lv['items'], LEVELS[level], split, limit, 'all')}</details>")
        out.append("</section>")

    out.append(f"<script>{JS}</script></body></html>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
