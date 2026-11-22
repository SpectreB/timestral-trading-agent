"""Read-only dashboard (D-012): one self-contained HTML page built from the audit log.

No server, no external resource: the page is plain HTML and CSS with server-rendered SVG
charts, plus a ~20-line script for tooltips. Everything shown is derived from the immutable
run records and reconciliation files, so the page can be regenerated at any time and always
agrees with the audit log. All text that comes from the data (LLM analysis, rationales,
symbols) is HTML-escaped: the LLM output is untrusted.

Usage:
    python -m agent.dashboard --audit-dir audit --out site
"""

from __future__ import annotations

import argparse
import html
import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.audit import FileAuditStore
from agent.scoring import calibration, score_forecasts

DEFAULT_REPO = "https://github.com/SpectreB/timestral-trading-agent"
AUDIT_BRANCH = "audit-log"

CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--s1:#2a78d6;
--s2:#eb6834;--good:#006300;--warn:#a86f00;--crit:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;
--good:#0ca30c;--warn:#fab219;--crit:#d03b3b}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--good:#0ca30c;--warn:#fab219;
--crit:#d03b3b}
*{box-sizing:border-box}
body{margin:0;background:var(--page,#f9f9f7)}
.viz-root{background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif;min-height:100vh}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 4px}
.sub{color:var(--ink2);margin:0 0 20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
padding:16px;margin:0 0 16px}
.sec-sub{color:var(--ink2);font-size:13px;margin:0 0 12px}
.hero{font-size:56px;line-height:1.1;font-weight:600;margin:4px 0}
.delta{font-size:14px;color:var(--ink2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;
margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px}
.tile .l{color:var(--ink2);font-size:13px}.tile .v{font-size:24px;font-weight:600}
.tile .n{color:var(--muted);font-size:12px}
.badge{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:1px 10px;
font-size:12px;font-weight:600;white-space:nowrap}
.b-good{color:var(--good)}.b-warn{color:var(--warn)}.b-crit{color:var(--crit)}
.b-mute{color:var(--ink2)}
svg{display:block;width:100%;height:auto;overflow:visible}
.chart{overflow-x:auto}.chart svg{min-width:600px}
svg text{fill:var(--muted);font:12px system-ui,sans-serif}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.line{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.area{fill:var(--s1);fill-opacity:.10}
.dot{fill:var(--s1);stroke:var(--surface);stroke-width:2}
.ref{stroke:var(--muted);stroke-width:1;stroke-dasharray:none}
.hit{fill:transparent}.xhair{stroke:var(--muted);stroke-width:1;opacity:0}
.pt:hover .xhair,.pt:focus .xhair{opacity:1}
.pt:focus{outline:none}.pt:focus .dot{stroke:var(--ink)}
.range{fill:var(--s1);fill-opacity:.35}.med{stroke:var(--ink);stroke-width:2}
.alloc{display:grid;grid-template-columns:64px 1fr 56px;gap:8px;align-items:center;
padding:3px 0;border-radius:6px}
.alloc:hover,.alloc:focus{background:var(--grid);outline:none}
.alloc .name{font-weight:600;font-size:13px}.alloc .val{text-align:right;font-size:13px;
color:var(--ink2);font-variant-numeric:tabular-nums}
.track{position:relative;height:14px}
.bar{position:absolute;left:0;top:0;bottom:0;background:var(--s1);border-radius:0 4px 4px 0}
.bar.cash{background:var(--axis)}
.tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--ink2);font-size:13px;margin:8px 0 0}
.legend i{display:inline-block;vertical-align:middle;margin-right:6px}
.sw{width:12px;height:12px;border-radius:3px;background:var(--s1)}
.tk{width:2px;height:14px;background:var(--ink)}
.rg{width:14px;height:10px;border-radius:3px;background:var(--s1);opacity:.45}
details{margin:8px 0 0}summary{cursor:pointer;color:var(--ink2);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
pre.txt{white-space:pre-wrap;font:13px/1.5 system-ui,sans-serif;color:var(--ink);margin:8px 0 0}
a{color:var(--s1)}
.foot{color:var(--muted);font-size:12px;margin-top:24px}
.tip{position:fixed;z-index:9;pointer-events:none;background:var(--surface);color:var(--ink2);
border:1px solid var(--axis);border-radius:8px;padding:6px 10px;font-size:12px;
box-shadow:0 2px 8px rgba(0,0,0,.15);max-width:320px}
.tip strong{display:block;color:var(--ink);font-size:13px}
@media (max-width:560px){.hero{font-size:40px}.alloc{grid-template-columns:52px 1fr 48px}}
"""

SCRIPT = """
(()=>{const tip=document.createElement('div');tip.className='tip';tip.hidden=true;
document.body.appendChild(tip);
function show(el,x,y){const lines=el.dataset.tip.split('\\n');tip.replaceChildren();
const h=document.createElement('strong');h.textContent=lines[0];tip.appendChild(h);
for(const t of lines.slice(1)){const d=document.createElement('div');d.textContent=t;
tip.appendChild(d);}tip.hidden=false;const r=tip.getBoundingClientRect();
tip.style.left=Math.max(8,Math.min(x+14,innerWidth-r.width-8))+'px';
tip.style.top=Math.max(8,y-r.height-12)+'px';}
for(const el of document.querySelectorAll('[data-tip]')){
el.addEventListener('pointermove',e=>show(el,e.clientX,e.clientY));
el.addEventListener('pointerleave',()=>{tip.hidden=true;});
el.addEventListener('focus',()=>{const r=el.getBoundingClientRect();
show(el,r.left+r.width/2,r.top);});
el.addEventListener('blur',()=>{tip.hidden=true;});}})();
"""


# --------------------------------------------------------------------------- helpers


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _tip(*lines: str) -> str:
    """A tooltip attribute: first line is the value, the others give context."""
    return 'data-tip="' + _e("\n".join(lines)).replace("\n", "&#10;") + '"'


def _usd(x: float) -> str:
    return f"${x:,.0f}"


def _pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def _spct(x: float, digits: int = 1) -> str:
    return f"{100 * x:+.{digits}f}%"


def _nice_step(span: float, target: int = 4) -> float:
    raw = span / target
    magnitude = 10.0 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)


def _nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    """Clean tick values inside [lo, hi]."""
    step = _nice_step(hi - lo, target)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) / step) + 1)]


def _day(run: dict[str, Any]) -> str:
    return datetime.fromisoformat(run["started_at"]).strftime("%b %d").replace(" 0", " ")


def _badge(text: str, kind: str, icon: str) -> str:
    return f'<span class="badge b-{kind}"><span aria-hidden="true">{icon}</span> {_e(text)}</span>'


_OUTCOME_BADGE = {
    "accepted": ("accepted", "good", "✓"),
    "corrected": ("accepted with corrections", "warn", "!"),
    "overridden": ("circuit breaker override", "crit", "✕"),
    "fallback_hold": ("rejected, positions held", "crit", "✕"),
}

_EXECUTION_LABEL = {
    "live": ("live orders sent", "good", "✓"),
    "dry_run": ("dry run", "mute", "○"),
    "dry_run_kill_switch": ("dry run (kill switch off)", "mute", "○"),
    "hold": ("no orders (positions held)", "mute", "○"),
}


def _outcome(run: dict[str, Any]) -> str:
    return _badge(*_OUTCOME_BADGE[run["decision"]["outcome"]])


def _execution(run: dict[str, Any]) -> str:
    return _badge(*_EXECUTION_LABEL[run["execution_status"]])


def _last_attempt(run: dict[str, Any]) -> dict[str, Any]:
    attempt: dict[str, Any] = run["decision"]["attempts"][-1]
    return attempt


def _analysis(run: dict[str, Any]) -> str:
    try:
        text = json.loads(_last_attempt(run)["answer"])["analysis"]
    except (ValueError, KeyError, TypeError):
        return ""
    return text if isinstance(text, str) else ""


def _corrections(run: dict[str, Any]) -> list[dict[str, Any]]:
    corrections: list[dict[str, Any]] = _last_attempt(run)["guardrails"]["corrections"]
    return [c for c in corrections if c["counted"]]


# --------------------------------------------------------------------------- sections


def _header(runs: list[dict[str, Any]], generated_at: datetime, repo: str) -> str:
    sub = (
        "An autonomous paper-trading agent: Timestral forecasts risk, a Mistral LLM adjusts a "
        "rule-based baseline, deterministic guardrails decide what reaches the broker. "
        "Paper money only. Read-only view generated from the immutable "
        f'<a href="{_e(repo)}/tree/{AUDIT_BRANCH}">audit log</a>.'
    )
    return f'<h1>Timestral Trading Agent</h1><p class="sub">{sub}</p>'


def _hero(runs: list[dict[str, Any]]) -> str:
    first, last = runs[0], runs[-1]
    equity, start = last["account"]["equity"], first["account"]["equity"]
    change = equity / start - 1
    invested = 1 - last["portfolios"]["final_cash"]
    return (
        '<div class="card"><div class="l sec-sub">Portfolio value at the latest run</div>'
        f'<div class="hero">{_usd(equity)}</div>'
        f'<div class="delta">{_spct(change, 2)} since the first run ({_usd(start)}) · '
        f"{_pct(invested, 0)} invested in the latest target · {_e(last['run_id'])}</div></div>"
    )


def _tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="n">{_e(note)}</div>' if note else ""
    return (
        f'<div class="tile"><div class="l">{_e(label)}</div><div class="v">{_e(value)}</div>'
        f"{note_html}</div>"
    )


def _kpis(runs: list[dict[str, Any]], fills: list[dict[str, Any]]) -> str:
    outcomes = [r["decision"]["outcome"] for r in runs]
    n_corr = sum(len(_corrections(r)) for r in runs)
    live = sum(r["execution_status"] == "live" for r in runs)
    held = sum(o == "fallback_hold" for o in outcomes)
    weighted = [
        (f["summary"]["slippage_bps_weighted"], f["summary"]["filled_notional"])
        for f in fills
        if f["summary"]["slippage_bps_weighted"] is not None
    ]
    total = sum(n for _, n in weighted)
    slippage = f"{sum(s * n for s, n in weighted) / total:+.1f} bps" if total > 0 else "n/a"
    note = "no fills reconciled yet" if not weighted else "vs reference price, + is worse"
    tiles = [
        _tile("Runs recorded", str(len(runs)), f"{live} with live orders"),
        _tile("Guardrail corrections", str(n_corr), "rule violations fixed and logged"),
        _tile("Proposals rejected", str(held), "positions held instead"),
        _tile("Average slippage", slippage, note),
    ]
    return f'<div class="kpis">{"".join(tiles)}</div>'


def _equity_chart(runs: list[dict[str, Any]]) -> str:
    width, height, left, right, top, bottom = 720, 240, 64, 24, 16, 36
    equity: list[float] = [r["account"]["equity"] for r in runs]
    start = equity[0]
    lo, hi = min([*equity, start]), max([*equity, start])
    pad = max((hi - lo) * 0.25, start * 0.002)
    lo, hi = lo - pad, hi + pad
    n = len(runs)

    def x(i: int) -> float:
        return left + (width - left - right) * (0.5 if n == 1 else i / (n - 1))

    def y(v: float) -> float:
        return top + (height - top - bottom) * (1 - (v - lo) / (hi - lo))

    parts = []
    for tick in _nice_ticks(lo, hi):
        parts.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" '
            f'y2="{y(tick):.1f}"/><text x="{left - 8}" y="{y(tick) + 4:.1f}" '
            f'text-anchor="end">{_usd(tick)}</text>'
        )
    base_y = height - bottom
    parts.append(
        f'<line class="axis" x1="{left}" x2="{width - right}" y1="{base_y}" y2="{base_y}"/>'
    )
    parts.append(
        f'<line class="ref" x1="{left}" x2="{width - right}" y1="{y(start):.1f}" '
        f'y2="{y(start):.1f}" stroke-dasharray="2 4"/>'
        f'<text x="{width - right}" y="{y(start) - 6:.1f}" text-anchor="end">'
        f"first run {_usd(start)}</text>"
    )
    points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(equity))
    if n > 1:
        parts.append(
            f'<polygon class="area" points="{left},{base_y} {points} {width - right},{base_y}"/>'
        )
        parts.append(f'<polyline class="line" points="{points}"/>')
    band = (width - left - right) / max(n - 1, 1)
    seen: set[str] = set()
    for i, run in enumerate(runs):
        day = _day(run)
        if day not in seen:
            seen.add(day)
            parts.append(
                f'<text x="{x(i):.1f}" y="{height - 12}" text-anchor="middle">{_e(day)}</text>'
            )
        dd = run["account"]["drawdown_from_peak"]
        tip = _tip(
            _usd(equity[i]),
            f"{run['run_id']}",
            f"drawdown from peak {_pct(dd)}",
            _OUTCOME_BADGE[run["decision"]["outcome"]][0],
        )
        parts.append(
            f'<g class="pt" tabindex="0" {tip}><line class="xhair" x1="{x(i):.1f}" '
            f'x2="{x(i):.1f}" y1="{top}" y2="{base_y}"/>'
            f'<circle class="dot" cx="{x(i):.1f}" cy="{y(equity[i]):.1f}" r="4"/>'
            f'<rect class="hit" x="{x(i) - band / 2:.1f}" y="{top}" width="{band:.1f}" '
            f'height="{base_y - top}"/></g>'
        )
    rows = "".join(
        f"<tr><td>{_e(r['run_id'])}</td><td class='num'>{_usd(r['account']['equity'])}</td>"
        f"<td class='num'>{_pct(r['account']['drawdown_from_peak'])}</td></tr>"
        for r in runs
    )
    return (
        '<div class="card"><h2>Portfolio value</h2>'
        '<p class="sec-sub">Equity at the start of each run, from the account snapshot in the '
        "audit record.</p>"
        f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Portfolio value over {n} runs, from {_usd(equity[0])} to '
        f'{_usd(equity[-1])}">{"".join(parts)}</svg></div>'
        '<details><summary>Table view</summary><div class="scroll"><table><tr><th>Run</th>'
        '<th class="num">Equity</th><th class="num">Drawdown</th></tr>'
        f"{rows}</table></div></details></div>"
    )


def _allocation(run: dict[str, Any]) -> str:
    p = run["portfolios"]
    proposed = p["proposed"] or {}
    final, baseline = p["final"], p["baseline"]
    scale = max(0.25, *final.values(), *baseline.values())
    rows, table = [], []
    for symbol in sorted(final, key=lambda s: -final[s]):
        view = proposed.get(symbol)
        lines = [
            f"{symbol}: {_pct(final[symbol])} target",
            f"baseline {_pct(baseline[symbol])} · current {_pct(p['current'][symbol])}",
        ]
        if view:
            lines.append(
                f"LLM proposed {_pct(view['weight'])} · confidence {view['confidence']:.2f}"
            )
            lines.append(view["rationale"])
        bar, mark = 100 * final[symbol] / scale, 100 * baseline[symbol] / scale
        rows.append(
            f'<div class="alloc" tabindex="0" {_tip(*lines)}>'
            f'<span class="name">{_e(symbol)}</span><span class="track">'
            f'<span class="bar" style="width:{bar:.1f}%"></span>'
            f'<span class="tick" style="left:calc({mark:.1f}% - 1px)"></span></span>'
            f'<span class="val">{_pct(final[symbol])}</span></div>'
        )
        llm_weight = _pct(view["weight"]) if view else "n/a"
        confidence = f"{view['confidence']:.2f}" if view else "n/a"
        why = _e(view["rationale"]) if view else ""
        table.append(
            f"<tr><td>{_e(symbol)}</td><td class='num'>{_pct(p['current'][symbol])}</td>"
            f"<td class='num'>{_pct(baseline[symbol])}</td><td class='num'>{llm_weight}</td>"
            f"<td class='num'>{_pct(final[symbol])}</td><td class='num'>{confidence}</td>"
            f"<td>{why}</td></tr>"
        )
    cash = p["final_cash"]
    cash_bar = 100 * cash / scale
    rows.append(
        f'<div class="alloc" tabindex="0" {_tip(f"Cash: {_pct(cash)}")}>'
        f'<span class="name">Cash</span><span class="track">'
        f'<span class="bar cash" style="width:{cash_bar:.1f}%"></span></span>'
        f'<span class="val">{_pct(cash)}</span></div>'
    )
    legend = (
        '<div class="legend"><span><i class="sw"></i>Target weight (after guardrails)</span>'
        '<span><i class="tk"></i>Baseline (Timestral rule)</span></div>'
    )
    return (
        '<div class="card"><h2>Target allocation</h2>'
        f'<p class="sec-sub">{_e(run["run_id"])}: what the agent decided to hold, against the '
        "baseline derived from Timestral's forecast intervals. Hover a row for the rationale.</p>"
        f"{''.join(rows)}{legend}"
        '<details><summary>Table view</summary><div class="scroll"><table><tr><th>Asset</th>'
        '<th class="num">Current</th><th class="num">Baseline</th><th class="num">LLM</th>'
        '<th class="num">Final</th><th class="num">Confidence</th><th>Rationale</th></tr>'
        f"{''.join(table)}</table></div></details></div>"
    )


def _forecasts(run: dict[str, Any]) -> str:
    horizon = max(int(h) for h in next(iter(run["forecasts"].values())))
    key = str(horizon)
    fc = {s: h[key] for s, h in run["forecasts"].items()}
    baseline = run["portfolios"]["baseline"]
    order = sorted(fc, key=lambda s: fc[s]["width_80"])
    extent: float = max(max(abs(f["q10"]), abs(f["q90"])) for f in fc.values())
    unit = _nice_step(2 * extent)
    n_ticks = math.ceil(extent / unit)  # the scale must contain every interval, ticks included
    bound = n_ticks * unit
    step = [i * unit for i in range(-n_ticks, n_ticks + 1)]
    width, left, right, top, row = 720, 56, 64, 8, 24
    height = top + row * len(order) + 28

    def x(v: float) -> float:
        return left + (width - left - right) * (v + bound) / (2 * bound)

    parts = []
    for tick in step:
        parts.append(
            f'<line class="grid" x1="{x(tick):.1f}" x2="{x(tick):.1f}" y1="{top}" '
            f'y2="{height - 28}"/><text x="{x(tick):.1f}" y="{height - 10}" '
            f'text-anchor="middle">{"0%" if tick == 0 else _spct(tick, 0)}</text>'
        )
    parts.append(
        f'<line class="axis" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="{top}" y2="{height - 28}"/>'
    )
    rows = []
    for i, s in enumerate(order):
        f = fc[s]
        cy = top + row * i + row / 2
        tip = _tip(
            f"{s}: {_spct(f['q10'])} to {_spct(f['q90'])}",
            f"median {_spct(f['median'])} · 80% interval width {100 * f['width_80']:.1f} pts",
            f"baseline weight {_pct(baseline[s])}",
        )
        parts.append(
            f'<g class="pt" tabindex="0" {tip}><text x="{left - 8}" y="{cy + 4:.1f}" '
            f'text-anchor="end">{_e(s)}</text>'
            f'<rect class="range" x="{x(f["q10"]):.1f}" y="{cy - 6:.1f}" '
            f'width="{x(f["q90"]) - x(f["q10"]):.1f}" height="12" rx="4"/>'
            f'<line class="med" x1="{x(f["median"]):.1f}" x2="{x(f["median"]):.1f}" '
            f'y1="{cy - 7:.1f}" y2="{cy + 7:.1f}"/>'
            f'<text x="{width - right + 8}" y="{cy + 4:.1f}">{100 * f["width_80"]:.1f} pts</text>'
            f'<rect class="hit" x="0" y="{cy - row / 2:.1f}" width="{width}" height="{row}"/></g>'
        )
        rows.append(
            f"<tr><td>{_e(s)}</td><td class='num'>{_spct(f['q10'])}</td>"
            f"<td class='num'>{_spct(f['median'])}</td><td class='num'>{_spct(f['q90'])}</td>"
            f"<td class='num'>{100 * f['width_80']:.1f}</td>"
            f"<td class='num'>{_pct(baseline[s])}</td></tr>"
        )
    legend = (
        '<div class="legend"><span><i class="rg"></i>80% interval (q10 to q90)</span>'
        '<span><i class="tk"></i>Median</span></div>'
    )
    return (
        f'<div class="card"><h2>Timestral forecasts, {horizon}-day cumulative return</h2>'
        f'<p class="sec-sub">{_e(run["run_id"])}: sorted from the most to the least certain. '
        "The narrower the interval, the larger the baseline weight. The median is shown for "
        "reference only: it has no measured directional skill.</p>"
        f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="80% forecast intervals of {len(order)} assets over {horizon} days">'
        f"{''.join(parts)}</svg></div>{legend}"
        '<details><summary>Table view</summary><div class="scroll"><table><tr><th>Asset</th>'
        '<th class="num">q10</th><th class="num">Median</th><th class="num">q90</th>'
        '<th class="num">Width (pts)</th><th class="num">Baseline</th></tr>'
        f"{''.join(rows)}</table></div></details></div>"
    )


def _calibration(scores: list[dict[str, Any]]) -> str:
    rows, table = [], []
    for c in calibration(scores):
        h, share = c["horizon"], c["inside_80"]
        direction = "n/a" if c["direction_hit"] is None else _pct(c["direction_hit"], 0)
        tip = _tip(
            f"{h}-day forecasts: {_pct(share, 0)} inside the 80% interval",
            f"{c['n']} scored forecasts · target 80%",
            f"direction hit rate {direction} · mean absolute error of the median "
            f"{_pct(c['abs_error_median'], 2)}",
        )
        rows.append(
            f'<div class="alloc" tabindex="0" {tip}><span class="name">{h}d</span>'
            f'<span class="track"><span class="bar" style="width:{100 * share:.1f}%"></span>'
            f'<span class="tick" style="left:calc(80% - 1px)"></span></span>'
            f'<span class="val">{_pct(share, 0)}</span></div>'
        )
        table.append(
            f"<tr><td>{h}</td><td class='num'>{c['n']}</td><td class='num'>{_pct(share, 0)}</td>"
            f"<td class='num'>{direction}</td>"
            f"<td class='num'>{_pct(c['abs_error_median'], 2)}</td></tr>"
        )
    legend = (
        '<div class="legend"><span><i class="sw"></i>Share of outcomes inside the 80% interval'
        '</span><span><i class="tk"></i>Target (80%)</span></div>'
    )
    return (
        '<div class="card"><h2>Forecast calibration, live</h2>'
        '<p class="sec-sub">Timestral\'s intervals against the prices that followed. A calibrated '
        "model puts about 80% of outcomes inside its 80% interval. Early on, few forecasts are "
        "scored and the figures are noisy, and the forecasts of one day are strongly "
        "correlated: read the counts before the percentages.</p>"
        f"{''.join(rows)}{legend}"
        '<details><summary>Table view</summary><div class="scroll"><table><tr><th>Horizon (days)'
        '</th><th class="num">Scored</th><th class="num">Inside 80%</th>'
        '<th class="num">Direction hit</th><th class="num">Median abs. error</th></tr>'
        f"{''.join(table)}</table></div></details></div>"
    )


def _decision(run: dict[str, Any]) -> str:
    attempt = _last_attempt(run)
    corrections = _corrections(run)
    reason = attempt["guardrails"]["rejection_reason"]
    items = [
        f"<li>{_e(c['rule'])} {_e(c['symbol'] or '')}: {c['before']:.4f} → {c['after']:.4f}</li>"
        for c in corrections
    ]
    fixes = f"<p>Guardrail corrections:</p><ul>{''.join(items)}</ul>" if items else ""
    rejected = f"<p>Rejection reason: {_e(reason)}</p>" if reason else ""
    analysis = _analysis(run)
    body = f'<pre class="txt">{_e(analysis)}</pre>' if analysis else "<p>No parsable analysis.</p>"
    tokens_in = sum(a["prompt_tokens"] for a in run["decision"]["attempts"])
    tokens_out = sum(a["completion_tokens"] for a in run["decision"]["attempts"])
    return (
        '<div class="card"><h2>Latest decision</h2>'
        f'<p class="sec-sub">{_e(run["run_id"])} · {_outcome(run)} · {_execution(run)}</p>'
        f"<p>Model <b>{_e(attempt['model'])}</b> · prompt "
        f"<b>{_e(run['decision']['prompt_version'])}</b> · "
        f"{len(run['decision']['attempts'])} attempt(s) · {tokens_in:,} tokens in, "
        f"{tokens_out:,} out · code <b>{_e(run['git_sha'][:7])}</b></p>"
        f"{rejected}{fixes}<details open><summary>The LLM's analysis</summary>{body}</details>"
        "</div>"
    )


def _runs_table(runs: list[dict[str, Any]], fills: list[dict[str, Any]], repo: str) -> str:
    by_run = {f["run_id"]: f["summary"] for f in fills}
    rows = []
    for r in reversed(runs):
        s = by_run.get(r["run_id"])
        fill = f"{s['n_filled']}/{s['n_orders']} filled" if s else "not reconciled"
        link = f"{repo}/blob/{AUDIT_BRANCH}/runs/{r['run_id']}.json"
        rows.append(
            f'<tr><td><a href="{_e(link)}">{_e(r["run_id"])}</a></td><td>{_outcome(r)}</td>'
            f"<td>{_execution(r)}</td><td class='num'>{len(_corrections(r))}</td>"
            f"<td class='num'>{len(r['orders'])}</td><td>{_e(fill)}</td>"
            f"<td class='num'>{_usd(r['account']['equity'])}</td></tr>"
        )
    return (
        '<div class="card"><h2>All runs</h2><p class="sec-sub">Newest first. Each run id links '
        "to its immutable record.</p><div class='scroll'><table><tr><th>Run</th><th>Decision</th>"
        '<th>Execution</th><th class="num">Corrections</th><th class="num">Orders</th>'
        '<th>Fills</th><th class="num">Equity</th></tr>'
        f"{''.join(rows)}</table></div></div>"
    )


def _fills_table(fills: list[dict[str, Any]]) -> str:
    rows = []
    for f in reversed(fills):
        for o in f["orders"]:
            slip = "n/a" if o["slippage_bps"] is None else f"{o['slippage_bps']:+.1f}"
            price = "n/a" if o["avg_fill_price"] is None else f"{o['avg_fill_price']:,.2f}"
            rows.append(
                f"<tr><td>{_e(f['run_id'])}</td><td>{_e(o['symbol'])}</td><td>{_e(o['side'])}</td>"
                f"<td class='num'>{o['quantity']:,.4f}</td><td>{_e(o['status'])}</td>"
                f"<td class='num'>{o['filled_qty']:,.4f}</td><td class='num'>{price}</td>"
                f"<td class='num'>{slip}</td></tr>"
            )
    if not rows:
        return (
            '<div class="card"><h2>Executions</h2><p class="sec-sub">No orders have been '
            "reconciled yet. Fills and slippage appear here once orders have been sent and have "
            "reached a final state.</p></div>"
        )
    return (
        '<div class="card"><h2>Executions</h2><p class="sec-sub">What the broker did with the '
        "orders. Slippage in basis points against the reference price: positive is worse for us."
        "</p><div class='scroll'><table><tr><th>Run</th><th>Asset</th><th>Side</th>"
        '<th class="num">Ordered</th><th>Status</th><th class="num">Filled</th>'
        '<th class="num">Avg price</th><th class="num">Slippage (bps)</th></tr>'
        f"{''.join(rows)}</table></div></div>"
    )


def _downloads(repo: str, tables: list[str]) -> str:
    links = "".join(f'<li><a href="data/{_e(t)}">{_e(t)}</a></li>' for t in tables)
    return (
        '<div class="card"><h2>Export</h2><p class="sec-sub">The flat tables derived from the '
        f"audit records (CSV).</p><ul>{links}</ul>"
        f'<p>Full records, prompts and reasoning traces: <a href="{_e(repo)}/tree/{AUDIT_BRANCH}">'
        f"{AUDIT_BRANCH} branch</a>. Design decisions: "
        f'<a href="{_e(repo)}/blob/main/docs/decisions.md">docs/decisions.md</a>.</p></div>'
    )


# --------------------------------------------------------------------------- page


def render(
    runs: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    generated_at: datetime,
    repo: str = DEFAULT_REPO,
    tables: list[str] | None = None,
    scores: list[dict[str, Any]] | None = None,
) -> str:
    if runs:
        body = "".join(
            [
                _hero(runs),
                _kpis(runs, fills),
                _equity_chart(runs),
                _allocation(runs[-1]),
                _forecasts(runs[-1]),
                _calibration(scores) if scores else "",
                _decision(runs[-1]),
                _runs_table(runs, fills, repo),
                _fills_table(fills),
            ]
        )
    else:
        body = (
            '<div class="card"><h2>No runs yet</h2><p class="sec-sub">The agent has not recorded '
            "a run. This page fills in automatically after the first scheduled run.</p></div>"
        )
    stamp = generated_at.isoformat(timespec="seconds")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Timestral Trading Agent</title>"
        '<meta name="description" content="Read-only view of an autonomous paper-trading agent: '
        'Timestral forecasts, Mistral LLM decisions, guardrails and audit log.">'
        f'<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">'
        f"{_header(runs, generated_at, repo)}{body}"
        f"{_downloads(repo, tables or [])}"
        f'<p class="foot">Generated {_e(stamp)} from the audit log. Paper trading, not financial '
        f"advice.</p></div></div><script>{SCRIPT}</script></body></html>"
    )


def chronological(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first; for runs at the same instant the morning run comes before the afternoon."""
    return sorted(runs, key=lambda r: (r["started_at"], r["mode"] != "morning"))


def build(audit_dir: Path, out_dir: Path, generated_at: datetime, repo: str = DEFAULT_REPO) -> Path:
    store = FileAuditStore(audit_dir)
    runs = chronological(store.read_runs())
    fills = store.read_fills()
    out_dir.mkdir(parents=True, exist_ok=True)
    tables: list[str] = []
    source = audit_dir / "tables"
    if source.exists():
        (out_dir / "data").mkdir(exist_ok=True)
        for csv_file in sorted(source.glob("*.csv")):
            shutil.copy(csv_file, out_dir / "data" / csv_file.name)
            tables.append(csv_file.name)
    page = out_dir / "index.html"
    scores = score_forecasts(runs, store.read_observations())
    page.write_text(render(runs, fills, generated_at, repo, tables, scores), encoding="utf-8")
    return page


def main() -> None:  # pragma: no cover - CLI wiring, runs in the dashboard workflow
    import os
    from datetime import UTC

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--out", default="site", type=Path)
    args = parser.parse_args()
    slug = os.environ.get("GITHUB_REPOSITORY")
    repo = f"https://github.com/{slug}" if slug else DEFAULT_REPO
    print(build(args.audit_dir, args.out, datetime.now(UTC), repo))


if __name__ == "__main__":  # pragma: no cover
    main()
