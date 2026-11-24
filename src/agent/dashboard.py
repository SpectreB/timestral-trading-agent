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
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from agent.audit import FileAuditStore
from agent.config import AgentConfig, AssetType, tool_agent_config
from agent.metrics import MIN_SESSIONS_FOR_CI, SERIES, performance_report
from agent.scoring import calibration, score_forecasts

DEFAULT_REPO = "https://github.com/SpectreB/timestral-trading-agent"
AUDIT_BRANCH = "audit-log"
TOOLS_FOLDER = "agents/tools"  # the tool agent's records on the audit-log branch (D-018)
SIM_TITLE = "Simulation request"  # the Simulate workflow reacts to issues titled so (D-017)
SIM_BODY = (
    "Submitting this issue asks the agent to run one simulation now: live market data, "
    "Timestral forecasts, the LLM and the guardrails, on its current portfolio. No order is sent. "
    "The answer is posted here in about 6 minutes. At most one simulation a minute and "
    "20 a day."
)

CSS = """
.viz-root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--s1:#2a78d6;
--s2:#eb6834;--good:#006300;--warn:#a86f00;--crit:#d03b3b;--c1:#1a8f81;--c2:#6a4cb8;
--tool:#2a78d6;--pipe:#e07b2a}
/* Light by default, whatever the system theme: the dark theme is opt-in (data-theme). */
:root[data-theme="dark"] .viz-root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--good:#0ca30c;--warn:#fab219;
--crit:#d03b3b;--c1:#2ec4b0;--c2:#9b82e0}
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
.delta.up,.delta.down{font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;
margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px}
.tile .l{color:var(--ink2);font-size:13px}.tile .v{font-size:24px;font-weight:600}
.tile .n{color:var(--muted);font-size:12px}
.badge{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:1px 10px;
font-size:12px;font-weight:600;white-space:nowrap;
background:color-mix(in srgb,currentColor 13%,transparent)}
.up{color:var(--good)}.down{color:var(--crit)}
.glance{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;
margin:0 0 16px}
.gcard{background:var(--surface);border:1px solid var(--border);border-top:5px solid var(--axis);
border-radius:12px;padding:16px}
.gcard.tools{border-top-color:var(--tool)}.gcard.pipeline{border-top-color:var(--pipe)}
.gcard.tools h2{color:var(--tool)}.gcard.pipeline h2{color:var(--pipe)}
.gcard h2{font-size:17px}.gcard .tag{color:var(--ink2);font-size:13px;margin:0 0 10px}
.journey{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.journey .from{font-size:20px;color:var(--ink2)}.journey .go{font-size:24px;color:var(--muted)}
.journey .to{font-size:42px;font-weight:700;line-height:1.1}
.gdelta{font-size:16px;font-weight:600;margin:2px 0 10px}
.gdelta span{font-weight:400;color:var(--ink2);font-size:13px;margin-left:6px}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.simform{display:flex;flex-wrap:wrap;gap:12px 16px;align-items:end;margin:8px 0 12px}
.simform label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--ink2)}
.simform select{font:inherit;font-size:14px;padding:6px 8px;border-radius:8px;
border:1px solid var(--axis);background:var(--surface);color:var(--ink)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:13px;margin:8px 0}
.kv span{color:var(--muted)}.kv b{font-weight:600}
.inrow{display:grid;grid-template-columns:92px 1fr;gap:8px;align-items:start;padding:6px 0;
border-top:1px solid var(--grid)}.inrow>b{font-size:13px}.inrow .pills{margin:0}
.card[class*="sec-"]{border-left:4px solid var(--sec)}
.card[class*="sec-"]>h2::before{content:"";display:inline-block;width:9px;height:9px;
border-radius:50%;background:var(--sec);margin-right:8px}
.side{font-weight:600}.side.buy{color:var(--ink)}.side.sell{color:var(--warn)}
[data-panel="tools"]{--agent:var(--tool)}[data-panel="pipeline"]{--agent:var(--pipe)}
.sec-agent{--sec:var(--agent,var(--axis))}.sec-cmp{--sec:var(--ink2)}.sec-safe{--sec:var(--c2)}
.sec-how{--sec:var(--c1)}.sec-sim{--sec:var(--warn)}.sec-cov{--sec:var(--c1)}
ul.plain{margin:8px 0 0;padding-left:18px;font-size:13px;line-height:1.5}ul.plain li{margin:4px 0}
.b-good{color:var(--good)}.b-warn{color:var(--warn)}.b-crit{color:var(--crit)}
.b-mute{color:var(--ink2)}
svg{display:block;width:100%;height:auto;overflow:visible}
.chart{overflow-x:auto}.chart svg{min-width:600px}
h3{font-size:14px;margin:16px 0 4px}
.status{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 8px}
.chip{border:1px solid var(--border);background:var(--surface);border-radius:999px;
padding:1px 10px;font-size:12px;color:var(--ink2)}
.alerts{list-style:none;margin:0 0 16px;padding:0;display:flex;flex-wrap:wrap;gap:8px}
.read{margin:4px 0}
.sline{fill:none;stroke:var(--muted);stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}
.s-agent.sline{stroke:var(--agent,var(--s1));stroke-width:2.5}.sdot{fill:var(--muted)}
.sdot.s-agent{fill:var(--agent,var(--s1));stroke:var(--surface);stroke-width:2}
.key{display:inline-flex;gap:6px;align-items:center;background:none;border:1px solid var(--border);
border-radius:999px;padding:2px 10px;color:var(--ink2);font:inherit;font-size:13px;cursor:pointer;
white-space:nowrap}
.key[aria-pressed="false"]{opacity:.45;text-decoration:line-through}
.meter{position:relative;height:10px;background:var(--grid);border-radius:5px;margin:4px 0 12px}
.meter .fill{position:absolute;left:0;top:0;bottom:0;background:var(--s1);border-radius:5px}
.meter i{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}
.tabs{display:flex;gap:8px;margin:0 0 12px;flex-wrap:wrap}
.tab{background:none;border:1px solid var(--border);border-radius:999px;padding:6px 16px;
color:var(--ink2);font:inherit;font-weight:600;cursor:pointer}
.tab::before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;
background:var(--tool)}#tab-pipeline::before{background:var(--pipe)}
.tab[aria-selected="true"]{background:var(--tool);border-color:var(--tool);color:#fff}
#tab-pipeline[aria-selected="true"]{background:var(--pipe);border-color:var(--pipe)}
.tab[aria-selected="true"]::before{background:#fff}
.btn{display:inline-block;background:var(--s1);color:#fff;border-radius:8px;padding:8px 16px;
font-weight:600;text-decoration:none}.btn:hover{opacity:.9}
.alloc .val{white-space:nowrap}
.alloc .val small{color:var(--muted);margin-left:0}
details.ev{border-top:1px solid var(--grid);margin:0;padding:8px 0}
details.ev>summary{font-size:14px}details.ev .card{border:0;padding:0;margin:8px 0 0}
details.ev .card>h2{display:none}
details.run{border-top:1px solid var(--grid);padding:6px 0}
details.run>summary{font-size:13px}
svg text{fill:var(--muted);font:12px system-ui,sans-serif}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.line{fill:none;stroke:var(--agent,var(--s1));stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.area{fill:var(--agent,var(--s1));fill-opacity:.10}
.dot{fill:var(--agent,var(--s1));stroke:var(--surface);stroke-width:2}
.ref{stroke:var(--muted);stroke-width:1;stroke-dasharray:none}
.hit{fill:transparent}.xhair{stroke:var(--muted);stroke-width:1;opacity:0}
.pt:hover .xhair,.pt:focus .xhair{opacity:1}
.pt:focus{outline:none}.pt:focus .dot{stroke:var(--ink)}
.range{fill:var(--s1);fill-opacity:.35}.med{stroke:var(--ink);stroke-width:2}
.alloc{display:grid;grid-template-columns:64px 1fr 250px;gap:8px;align-items:center;
padding:3px 0;border-radius:6px}
.alloc:hover,.alloc:focus{background:var(--grid);outline:none}
.alloc .name{font-weight:600;font-size:13px}.alloc .val{text-align:right;font-size:13px;
color:var(--ink2);font-variant-numeric:tabular-nums}
.track{position:relative;height:14px}
.bar{position:absolute;left:0;top:0;bottom:0;background:var(--c1);border-radius:0 4px 4px 0}
.bar.stock{background:var(--c2)}
td .sw{display:inline-block;margin-right:6px;vertical-align:middle}
.dg{max-width:100%;height:auto;margin:8px 0}
.dg rect{fill:var(--surface);stroke:var(--axis);stroke-width:1.2}
.dg rect{stroke-width:2}.dg rect.pipe{stroke:var(--pipe)}.dg rect.tool{stroke:var(--tool)}
.dg rect.pipe.ts{fill:color-mix(in srgb,var(--pipe) 16%,var(--surface))}
.dg rect.tool.ts{fill:color-mix(in srgb,var(--tool) 16%,var(--surface))}
.dg text.row.pipe{fill:var(--pipe)}.dg text.row.tool{fill:var(--tool)}
.dg text{fill:var(--ink);font-size:12px}.dg text.sm{fill:var(--ink2);font-size:11px}
.dg text.row{fill:var(--ink);font-size:13px;font-weight:600}
.dg line{stroke:var(--ink2);stroke-width:1.5}.dg marker path{fill:var(--ink2);stroke:none}
.rules{list-style:none;padding:0;margin:0 0 8px}
.rules li{padding:6px 0;border-top:1px solid var(--grid);font-size:14px}
.rules li:first-child{border-top:0}.rules small{display:block;color:var(--ink2);font-size:13px}
.bar.cash{background:var(--axis)}
.alloc .val b{color:var(--ink);font-weight:600}
.alloc .val{display:flex;justify-content:flex-end;align-items:baseline;gap:8px}
.cmp td.num,.cmp th.num{width:190px}
.num{white-space:nowrap}
th.c-tools{color:var(--tool);border-bottom:3px solid var(--tool)}
th.c-pipeline{color:var(--pipe);border-bottom:3px solid var(--pipe)}
.cmp tr.grp td{font-size:12px;font-weight:600;color:var(--ink2);text-transform:uppercase;
letter-spacing:.04em;padding-top:12px}
.mv{display:inline-block;width:46px;text-align:right;font-size:12px;color:var(--muted)}
.mv.up{color:var(--ink)}.mv.down{color:var(--warn)}
.tick{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--ink2);font-size:13px;margin:8px 0 0}
.legend i{display:inline-block;vertical-align:middle;margin-right:6px}
.sw{width:12px;height:12px;border-radius:3px;background:var(--c1)}
.sw.stock{background:var(--c2)}.sw.cash{background:var(--axis)}
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
@media (max-width:560px){.hero{font-size:40px}.alloc{grid-template-columns:52px 1fr 150px}
.alloc .val{gap:4px;font-size:12px}}
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
for(const b of document.querySelectorAll('[data-toggle]')){
b.addEventListener('click',()=>{const on=b.getAttribute('aria-pressed')!=='true';
b.setAttribute('aria-pressed',on?'true':'false');
const box=b.closest('.card')||document;
for(const g of box.querySelectorAll('[data-series="'+b.dataset.toggle+'"]')){
g.style.display=on?'':'none';}});}
for(const t of document.querySelectorAll('[data-tab]')){
t.addEventListener('click',()=>{for(const o of document.querySelectorAll('[data-tab]')){
o.setAttribute('aria-selected',o===t?'true':'false');}
for(const p of document.querySelectorAll('[data-panel]')){
p.hidden=p.dataset.panel!==t.dataset.tab;}});}
for(const el of document.querySelectorAll('[data-tip]')){
el.addEventListener('pointermove',e=>show(el,e.clientX,e.clientY));
el.addEventListener('pointerleave',()=>{tip.hidden=true;});
el.addEventListener('focus',()=>{const r=el.getBoundingClientRect();
show(el,r.left+r.width/2,r.top);});
el.addEventListener('blur',()=>{tip.hidden=true;});}})();
(()=>{const go=document.getElementById('sim-go');if(!go)return;
const sel=[...document.querySelectorAll('[data-opt]')];
const upd=()=>{const lines=sel.map(s=>s.dataset.opt+': '+s.value).join('\\n');
go.href=go.dataset.base+'?title='+encodeURIComponent(go.dataset.title)+'&body='
+encodeURIComponent(lines+'\\n\\n'+go.dataset.text);};
for(const s of sel)s.addEventListener('change',upd);upd();})();
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


def _allocation(run: dict[str, Any], first: dict[str, Any]) -> str:
    p = run["portfolios"]
    proposed = p["proposed"] or {}
    final, baseline = p["final"], p["baseline"]
    scale = max(0.25, *final.values(), *baseline.values())
    shares = (run.get("risk") or {}).get("risk_contributions_final", {})
    equity = run["account"]["equity"]
    stocks = {a["symbol"] for a in run["config"]["universe"] if a["type"] == "stock"}

    def move(symbol: str) -> str:
        """Change against the portfolio held before this run, in points of the portfolio."""
        pts = 100 * (final[symbol] - p["current"][symbol])
        if abs(pts) < 0.05:
            return '<span class="mv"></span>'
        arrow, kind = ("▲", "up") if pts > 0 else ("▼", "down")
        return f'<span class="mv {kind}">{arrow} {abs(pts):.1f}</span>'

    def risk_note(symbol: str) -> str:
        return f"<small>risk {_pct(shares[symbol], 0)}</small>" if symbol in shares else ""

    rows, table = [], []
    for symbol in sorted(final, key=lambda s: -final[s]):
        view = proposed.get(symbol)
        lines = [
            f"{symbol}: {_usd(final[symbol] * equity)} target ({_pct(final[symbol])})",
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
            f'<span class="bar{" stock" if symbol in stocks else ""}" style="width:{bar:.1f}%">'
            f'</span><span class="tick" style="left:calc({mark:.1f}% - 1px)"></span></span>'
            f'<span class="val">{move(symbol)}<b>{_usd(final[symbol] * equity)}</b> '
            f"{_pct(final[symbol])}{risk_note(symbol)}</span></div>"
        )
        llm_weight = _pct(view["weight"]) if view else "n/a"
        confidence = f"{view['confidence']:.2f}" if view else "n/a"
        why = _e(view["rationale"]) if view else ""
        kind = " stock" if symbol in stocks else ""
        table.append(
            f'<tr><td><i class="sw{kind}"></i>{_e(symbol)}</td>'
            f"<td class='num'>{_pct(p['current'][symbol])}</td>"
            f"<td class='num'>{_pct(baseline[symbol])}</td><td class='num'>{llm_weight}</td>"
            f"<td class='num'>{move(symbol)}{_pct(final[symbol])}</td>"
            f"<td class='num'>{_usd(final[symbol] * equity)}</td><td class='num'>{confidence}</td>"
            f"<td>{why}</td></tr>"
        )
    vols = ((run.get("risk") or {}).get("portfolio_vol") or {}).get(
        str(max(int(h) for h in run["forecasts"][next(iter(run["forecasts"]))])), {}
    )
    vol_line = (
        f'<p class="sec-sub">Forecast volatility of the final portfolio over the horizon: '
        f"{_pct(vols['final'])} (Timestral rule alone: {_pct(vols['baseline'])}).</p>"
        if "final" in vols and "baseline" in vols
        else ""
    )
    cash = p["final_cash"]
    began = datetime.fromisoformat(first["started_at"]).strftime("%d %b").lstrip("0")
    at_start = sum(first["portfolios"]["current"].values())
    journey = (
        f'<div class="pills"><span class="chip">Start, {_e(began)}: {_pct(at_start, 0)} invested'
        f'</span><span class="go" aria-hidden="true">→</span>'
        f'<span class="chip">Now: {_pct(1 - cash, 0)} invested, {_pct(cash, 0)} cash</span></div>'
    )
    cash_bar = 100 * cash / scale
    rows.append(
        f'<div class="alloc" tabindex="0" {_tip(f"Cash: {_usd(cash * equity)} ({_pct(cash)})")}>'
        f'<span class="name">Cash</span><span class="track">'
        f'<span class="bar cash" style="width:{cash_bar:.1f}%"></span></span>'
        f'<span class="val"><span class="mv"></span><b>{_usd(cash * equity)}</b> '
        f"{_pct(cash)}</span></div>"
    )
    legend = (
        '<div class="legend"><span><i class="sw"></i>ETF</span>'
        '<span><i class="sw stock"></i>Stock</span><span><i class="sw cash"></i>Cash</span>'
        '<span><i class="tk"></i>Baseline (Timestral rule)</span>'
        '<span><b class="side buy">▲</b> <b class="side sell">▼</b> change since the last run, '
        "in points</span></div>"
    )
    return (
        '<div class="card"><h2>What it holds</h2>'
        f'<p class="sec-sub">{_e(run["run_id"])}: target value and weight after the guardrails. '
        f"The small figure is the asset's share of the forecast risk. Hover a row for the "
        f"rationale.</p>{journey}{vol_line}"
        f"{''.join(rows)}{legend}"
        '<details><summary>Table view</summary><div class="scroll"><table><tr><th>Asset</th>'
        '<th class="num">Current</th><th class="num">Baseline</th><th class="num">LLM</th>'
        '<th class="num">Final</th><th class="num">Target value</th>'
        '<th class="num">Confidence</th><th>Rationale</th></tr>'
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


def _stamp(moment: str | None, fallback: str) -> str:
    if not moment:
        return fallback
    return datetime.fromisoformat(moment).strftime("%d %b %H:%M").lstrip("0") + " UTC"


def _trades(fills: list[dict[str, Any]], runs: list[dict[str, Any]]) -> str:
    """The paper-trading activity itself (the brief asks for it): the latest orders with what
    the broker did, most recent first. The slippage detail stays in the evidence below."""
    orders = [(f["run_id"], o) for f in reversed(fills) for o in f["orders"]]
    reconciled = {f["run_id"] for f in fills}
    for run in reversed(runs):  # orders sent but not yet reconciled: shown as sent, not hidden
        if run["run_id"] in reconciled or run["execution_status"] != "live":
            continue
        orders += [
            (
                run["run_id"],
                {
                    **o,
                    "status": "not reconciled yet",
                    "filled_qty": 0.0,
                    "avg_fill_price": None,
                    "submitted_at": run["started_at"],
                },
            )
            for o in run["orders"]
        ]
    if not orders:
        return (
            '<div class="card"><h2>Trades</h2><p class="sec-sub">No order has been sent to the '
            "broker yet.</p></div>"
        )
    done = [o for _, o in orders if o["status"] == "filled" and o["avg_fill_price"] is not None]

    def value(o: dict[str, Any]) -> float:
        return float(o["filled_qty"] * o["avg_fill_price"])

    bought = sum(value(o) for o in done if o["side"] == "buy")
    sold = sum(value(o) for o in done if o["side"] == "sell")

    def row(run_id: str, o: dict[str, Any]) -> str:
        buy = o["side"] == "buy"
        side = (
            f'<span class="side {"buy" if buy else "sell"}">{"▲ Buy" if buy else "▼ Sell"}</span>'
        )
        filled = o["status"] == "filled" and o["avg_fill_price"] is not None
        waiting = o["status"] == "not reconciled yet"  # said once above, not on every row
        price = f"{o['avg_fill_price']:,.2f}" if filled else ""
        amount = _usd(value(o)) if filled else ""
        status = "" if filled or waiting else _badge(o["status"], "warn", "!")
        when = _stamp(o.get("filled_at") or o.get("submitted_at"), run_id)
        fill_cells = f"<td class='num'>{price}</td><td class='num'>{amount}</td>" if done else ""
        return (
            f"<tr><td>{_e(when)}</td><td><b>{_e(o['symbol'])}</b></td><td>{side}</td>"
            f"<td class='num'>{o['filled_qty'] or o['quantity']:,.2f}</td>"
            f"{fill_cells}<td>{status}</td></tr>"
        )

    head = (
        "<div class='scroll'><table><tr><th>When</th><th>Asset</th><th>Side</th>"
        '<th class="num">Shares</th>'
        + ('<th class="num">Price</th><th class="num">Value</th>' if done else "")
        + "<th></th></tr>"
    )
    first = "".join(row(r, o) for r, o in orders[:12])
    more = ""
    if len(orders) > 12:
        rest = "".join(row(r, o) for r, o in orders[12:])
        more = (
            f"<details><summary>The {len(orders) - 12} earlier orders</summary>"
            f"{head}{rest}</table></div></details>"
        )
    waiting = sum(o["status"] == "not reconciled yet" for _, o in orders)
    pills = [f'<span class="chip">{len(orders)} orders</span>']
    if done:
        pills += [
            f'<span class="chip">{len(done)} filled</span>',
            f'<span class="chip">{_usd(bought)} bought</span>',
            f'<span class="chip">{_usd(sold)} sold</span>',
        ]
    if waiting:
        pills.append(_badge("awaiting the broker's final state", "mute", "○"))
    chips = f'<div class="pills">{"".join(pills)}</div>'
    return f'<div class="card"><h2>Trades</h2>{chips}{head}{first}</table></div>{more}</div>'


_SERIES_LABEL = {
    "agent": "Agent (after guardrails)",
    "llm_proposal": "LLM proposal",
    "baseline": "Timestral rule",
    "equal_weight": "Equal weight",
    "spy": "SPY (market)",
}
_SERIES_DASH = {
    "agent": "",
    "llm_proposal": "6 3",
    "baseline": "2 3",
    "equal_weight": "9 3 2 3",
    "spy": "1 4",
}
_LEVEL_LABEL = {
    "alert": "alert",
    "reduce_only": "reduce-only mode",
    "cap_exposure_60": "exposure capped at 60%",
    "baseline_half_exposure_and_pause": "baseline at half exposure, LLM paused",
}


def _when(moment: datetime) -> str:
    return moment.strftime("%a %d %b, %H:%M").replace(" 0", " ") + " ET"


SLOT_HOURS = (10, 15)  # the two scheduled runs of the day, US Eastern time


def _next_slot(now: datetime) -> datetime:
    """The next scheduled slot after `now` (weekdays only; holidays are not modelled). Counted
    from the present, not from the last run: a run that did not happen must not make the
    page announce a slot that is already in the past."""
    et = now.astimezone(ZoneInfo("America/New_York"))
    day = et.replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        if day.weekday() < 5:
            for hour in SLOT_HOURS:
                if (slot := day.replace(hour=hour)) > et:
                    return slot
        day += timedelta(days=1)


def _next_run(started: datetime, mode: str) -> datetime:
    """The next scheduled run (holidays are not modelled, so this is approximate)."""
    if mode == "morning":
        return started.replace(hour=15, minute=0, second=0, microsecond=0)
    day = started + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=10, minute=0, second=0, microsecond=0)


def _alerts(run: dict[str, Any], fills: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """What deserves attention at the last run: shown at the top, never folded away."""
    out: list[tuple[str, str]] = []
    attempt = _last_attempt(run)
    outcome = run["decision"]["outcome"]
    if outcome == "fallback_hold":
        reason = attempt["guardrails"]["rejection_reason"]
        out.append(("crit", f"LLM proposal rejected ({reason}): positions held"))
    if outcome == "overridden":
        out.append(("crit", "Circuit breaker override: the LLM was bypassed"))
    if n := len(_corrections(run)):
        out.append(("warn", f"{n} guardrail correction(s) at the last run"))
    actions = [a for a in attempt["guardrails"]["drawdown_actions"] if a != "alert"]
    if actions:
        out.append(
            ("crit", "Circuit breaker active: " + ", ".join(_LEVEL_LABEL[a] for a in actions))
        )
    elif attempt["guardrails"]["drawdown_actions"]:
        out.append(("warn", "Drawdown alert level reached"))
    for f in fills:
        s = f["summary"]
        if f["run_id"] == run["run_id"] and (
            s["n_not_sent"] or s["n_not_found"] or s["n_unfilled"]
        ):
            out.append(("warn", f"{s['n_unfilled']} unfilled, {s['n_not_sent']} unsent order(s)"))
    return out


def _status(runs: list[dict[str, Any]], fills: list[dict[str, Any]]) -> str:
    last = runs[-1]
    started = datetime.fromisoformat(last["started_at"])
    attempt = _last_attempt(last)
    prompt = last["decision"]["prompt_version"]
    chips = [
        f'<p class="sec-sub">{_e(attempt["model"])} · prompt {_e(prompt)}'
        f" · code {_e(last['git_sha'][:7])} · {_e(_when(started))}</p>"
    ]
    alerts = _alerts(last, fills)
    if alerts:
        lines = "".join(
            f"<li>{_badge(text, kind, '!' if kind == 'warn' else '✕')}</li>"
            for kind, text in alerts
        )
    else:
        return f'<div class="status">{"".join(chips)}</div>'  # nothing wrong: nothing to add
    return f'<div class="status">{"".join(chips)}</div><ul class="alerts">{lines}</ul>'


def _today(run: dict[str, Any], fills: list[dict[str, Any]]) -> str:
    p = run["portfolios"]
    cur, fin = p["current"], p["final"]
    proposed = p["proposed"] or {}
    moves = sorted(
        ((s, fin[s] - cur[s]) for s in fin if abs(fin[s] - cur[s]) >= 0.005),
        key=lambda m: -abs(m[1]),
    )
    rows = []
    for symbol, delta in moves[:6]:
        view = proposed.get(symbol)
        why = view["rationale"] if view else ""
        short = why if len(why) <= 90 else why[:87] + "..."
        origin = "cash" if cur[symbol] == 0 else _pct(cur[symbol])
        why_cell = f"<td {_tip(why)}>{_e(short)}</td>" if why else "<td></td>"
        rows.append(
            f"<tr><td><b>{_e(symbol)}</b></td><td>{origin} → {_pct(fin[symbol])}</td>"
            f"<td class='num'>{100 * delta:+.1f} pts</td>{why_cell}</tr>"
        )
    if rows:
        more = (
            f'<p class="sec-sub">+ {len(moves) - 6} smaller move(s)</p>' if len(moves) > 6 else ""
        )
        table = (
            "<div class='scroll'><table><tr><th>Asset</th><th>Weight</th>"
            "<th class='num'>Change</th>"
            f"<th>Reason given by the LLM</th></tr>{''.join(rows)}</table></div>{more}"
        )
    else:
        table = "<p>No change: positions held.</p>"
    statuses = Counter(o["status"] for o in run["orders"])
    sent = ", ".join(f"{n} {_e(s)}" for s, n in statuses.items()) or "none"
    summary = next((f["summary"] for f in fills if f["run_id"] == run["run_id"]), None)
    done = ""
    if summary:
        done = f" · {summary['n_filled']}/{summary['n_orders']} filled"
        if summary["slippage_bps_weighted"] is not None:
            done += f", average slippage {summary['slippage_bps_weighted']:+.1f} bps"
    analysis = _analysis(run)
    if not analysis:
        note = "<p>No parsable analysis.</p>"
    else:
        note = (
            f'<p class="read">{_e(analysis[:197] + "..." if len(analysis) > 200 else analysis)}</p>'
        )
        if len(analysis) > 200:
            note += (
                f'<details><summary>Full analysis</summary><pre class="txt">{_e(analysis)}</pre>'
                "</details>"
            )
    return (
        f'<div class="card"><h2>At the last run</h2><p class="sec-sub">{_e(run["run_id"])} · '
        f"{_outcome(run)}</p>{_tool_use(run)}{table}"
        f'<p class="sec-sub">Orders: {sent}{_e(done)}</p>'
        f"<h3>The LLM's reading</h3>{note}</div>"
    )


def _cumulative(daily: list[dict[str, Any]], series: str) -> list[float]:
    out, wealth = [0.0], 1.0
    for row in daily:
        wealth *= 1 + row[series]
        out.append(wealth - 1)
    return out


def _series_chart(report: dict[str, Any]) -> str:
    daily = report["daily"]
    width, height, left, right, top, bottom = 720, 260, 56, 24, 16, 36
    curves = {s: _cumulative(daily, s) for s in SERIES}
    values = [v for c in curves.values() for v in c]
    lo, hi = min([*values, 0.0]), max([*values, 0.0])
    pad = max((hi - lo) * 0.15, 0.002)
    lo, hi = lo - pad, hi + pad
    n = len(daily) + 1

    def x(i: int) -> float:
        return left + (width - left - right) * i / (n - 1)

    def y(v: float) -> float:
        return top + (height - top - bottom) * (1 - (v - lo) / (hi - lo))

    parts = []
    for tick in _nice_ticks(lo, hi, 6):
        label = "0%" if abs(tick) < 1e-12 else _spct(tick, 1)
        parts.append(
            f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" '
            f'y2="{y(tick):.1f}"/><text x="{left - 8}" y="{y(tick) + 4:.1f}" '
            f'text-anchor="end">{label}</text>'
        )
    parts.append(
        f'<line class="axis" x1="{left}" x2="{width - right}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>'
    )
    parts.append(f'<text x="{x(0):.1f}" y="{height - 12}" text-anchor="start">start</text>')
    step = max(1, (n - 1) // 6)
    for i, row in enumerate(daily, start=1):
        # Every `step` sessions, plus the last one (unless a label is already too close to it).
        if i == n - 1 or (i % step == 0 and n - 1 - i >= step / 2):
            parts.append(
                f'<text x="{x(i):.1f}" y="{height - 12}" text-anchor="middle">'
                f"{_e(row['session'][5:])}</text>"
            )
    for series in reversed(SERIES):  # the agent is drawn last, on top
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(curves[series]))
        dash = f' stroke-dasharray="{_SERIES_DASH[series]}"' if _SERIES_DASH[series] else ""
        parts.append(
            f'<g data-series="{series}">'
            f'<polyline class="sline s-{series}" points="{points}"{dash}/>'
            f'<circle class="sdot s-{series}" cx="{x(n - 1):.1f}" cy="{y(curves[series][-1]):.1f}" '
            f'r="4"/></g>'
        )
    band = (width - left - right) / max(n - 1, 1)
    for i, row in enumerate(daily, start=1):
        tip = _tip(
            f"{row['session']}: agent {_spct(curves['agent'][i], 2)} since the start",
            *(f"{_SERIES_LABEL[s]} {_spct(curves[s][i], 2)}" for s in SERIES if s != "agent"),
        )
        parts.append(
            f'<g class="pt" tabindex="0" {tip}><line class="xhair" x1="{x(i):.1f}" '
            f'x2="{x(i):.1f}" y1="{top}" y2="{height - bottom}"/>'
            f'<rect class="hit" x="{x(i) - band / 2:.1f}" y="{top}" width="{band:.1f}" '
            f'height="{height - top - bottom}"/></g>'
        )
    legend = "".join(
        f'<button type="button" class="key" data-toggle="{s}" aria-pressed="true">'
        f'<svg width="26" height="10" aria-hidden="true"><line x1="0" x2="26" y1="5" y2="5" '
        f'class="sline s-{s}"'
        + (f' stroke-dasharray="{_SERIES_DASH[s]}"' if _SERIES_DASH[s] else "")
        + f"/></svg> {_e(_SERIES_LABEL[s])}</button>"
        for s in SERIES
    )
    rows = "".join(
        f"<tr><td>{_e(row['session'])}</td>"
        + "".join(f"<td class='num'>{_spct(curves[s][i], 2)}</td>" for s in SERIES)
        + "</tr>"
        for i, row in enumerate(daily, start=1)
    )
    head = "".join(f"<th class='num'>{_e(_SERIES_LABEL[s])}</th>" for s in SERIES)
    return (
        f'<div class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Cumulative return of the agent and four references over {len(daily)} '
        f'sessions">{"".join(parts)}</svg></div>'
        f'<div class="legend">{legend}</div>'
        '<details><summary>Table view</summary><div class="scroll"><table><tr><th>Session</th>'
        f"{head}</tr>{rows}</table></div></details>"
    )


def _comparisons(report: dict[str, Any]) -> str:
    rows = []
    for c in report["comparisons"]:
        n = c["sessions"]
        if c["conclusive"]:
            verdict = _badge("conclusive: the 95% interval excludes zero", "good", "✓")
        elif c["ci95_mean_daily"] is None:
            verdict = _badge(f"too early ({n} of {MIN_SESSIONS_FOR_CI} sessions)", "mute", "○")
        else:
            verdict = _badge("not distinguishable from zero", "mute", "○")
        rows.append(
            f"<tr><td>{_e(c['label'])}</td>"
            f"<td class='num'>{100 * c['cumulative_difference']:+.2f} pts"
            f"</td><td class='num'>{n}</td><td>{verdict}</td></tr>"
        )
    return (
        "<div class='scroll'><table><tr><th>Question</th><th class='num'>Difference since the "
        "start</th><th class='num'>Sessions</th><th>Can we tell?</th></tr>"
        f"{''.join(rows)}</table></div>"
    )


def _exposure(run: dict[str, Any]) -> str:
    invested = 1 - run["portfolios"]["final_cash"]
    drawdown = run["account"]["drawdown_from_peak"]
    levels = sorted(run["config"]["drawdown_levels"], key=lambda lv: -lv["threshold"])
    scale = abs(levels[-1]["threshold"])
    upcoming = next((lv for lv in levels if drawdown > lv["threshold"]), None)
    nxt = (
        f"next safeguard at {_pct(upcoming['threshold'], 0)} ({_LEVEL_LABEL[upcoming['action']]})"
        if upcoming
        else "every safeguard level has been reached"
    )
    ticks = "".join(
        f'<i style="left:{100 * abs(lv["threshold"]) / scale:.1f}%" '
        f"{_tip(_pct(lv['threshold'], 0) + ': ' + _LEVEL_LABEL[lv['action']])}></i>"
        for lv in levels
    )
    fill = min(abs(drawdown) / scale, 1.0) * 100
    names = " · ".join(f"{_pct(lv['threshold'], 0)} {_LEVEL_LABEL[lv['action']]}" for lv in levels)
    return (
        f'<p class="sec-sub">{_pct(invested, 0)} invested in the latest target · drawdown '
        f"{_pct(drawdown)} from peak · {_e(nxt)}</p>"
        f'<div class="meter" role="img" aria-label="Drawdown against the circuit-breaker levels">'
        f'<span class="fill" style="width:{fill:.1f}%"></span>{ticks}</div>'
        f'<p class="sec-sub">Safeguards: {names}.</p>'
    )


def _performance(
    runs: list[dict[str, Any]], report: dict[str, Any] | None, replay: str = ""
) -> str:
    equity, start = runs[-1]["account"]["equity"], runs[0]["account"]["equity"]
    hero = (
        f'<div class="hero">{_usd(equity)}</div>'
        f'<div class="delta {"" if equity == start else "up" if equity > start else "down"}">'
        f"{_spct(equity / start - 1, 2)} "
        f"since the first run ({_usd(start)}) · account value at the latest run</div>"
    )
    if report is None or report["sessions"] < 1:
        body = (
            '<p class="sec-sub">No session has closed since the first run: the chart appears '
            "after the first closing prices.</p>"
        )
    else:
        first, last = report["first_session"], report["last_session"]
        body = (
            f'<p class="sec-sub">The agent against four references, {report["sessions"]} '
            f"session(s), {_e(first)} to {_e(last)}. Valued without costs: the real account is "
            "the figure above.</p>"
            f"{_series_chart(report)}<details><summary>What does each layer add?</summary>"
            f"{_comparisons(report)}"
            '<p class="sec-sub">A difference is only called conclusive when its 95% interval '
            f"(from {MIN_SESSIONS_FOR_CI} sessions on) excludes zero: a few days of returns cannot "
            "separate skill from noise.</p></details>"
        )
    definitions = (
        "<details><summary>Definitions</summary><ul>"
        "<li><b>Agent</b>: the final target after the guardrails.</li>"
        "<li><b>LLM proposal</b>: the weights the LLM asked for, before the guardrails.</li>"
        "<li><b>Timestral rule</b>: weights inversely proportional to the width of Timestral's "
        "forecast interval, no LLM.</li>"
        "<li><b>Equal weight</b>: the same share in each of the 15 assets, same cash.</li>"
        "<li><b>SPY</b>: the S&amp;P 500 ETF, the market.</li></ul></details>"
    )
    exposure = _exposure(runs[-1])
    return (
        f'<div class="card"><h2>Performance</h2>{hero}{body}{replay}{exposure}{definitions}</div>'
    )


SIM_CHOICES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "agent": (
        "Agent",
        (
            ("tools", "Tool agent (final)"),
            ("pipeline", "Pipeline agent (reference)"),
            ("news", "Tool agent + news covariates"),
        ),
    ),
    "stance": (
        "Risk stance",
        (
            ("balanced", "Balanced: the live limits"),
            ("cautious", "Cautious: 30% cash, small positions"),
            ("bold", "Bold: larger positions, no leverage"),
        ),
    ),
    "start": (
        "Start from",
        (
            ("current", "Its current portfolio"),
            ("cash", "All cash"),
            ("equal", "Equal weight, 6% each"),
        ),
    ),
}


def _simulations(sims: list[dict[str, Any]], repo: str) -> str:
    """The evaluators' entry point: choose, request a simulation, see the ones already run."""
    first = {key: opts[0][0] for key, (_, opts) in SIM_CHOICES.items()}
    lines = "\n".join(f"{key}: {value}" for key, value in first.items())
    base = f"{repo}/issues/new"
    url = f"{base}?" + urlencode({"title": SIM_TITLE, "body": f"{lines}\n\n{SIM_BODY}"})
    items = []
    for r in list(reversed(sims))[:10]:
        who = r.get("requested_by") or "unknown"
        analysis = _analysis(r)
        excerpt = analysis if len(analysis) <= 300 else analysis[:297] + "..."
        link = f"{repo}/blob/{AUDIT_BRANCH}/simulations/runs/{r['run_id']}.json"
        items.append(
            f'<details class="run"><summary>{_e(r["run_id"])} · requested by {_e(who)} · '
            f"{_outcome(r)} · {len(r['orders'])} order(s) planned, none sent</summary>"
            f'<p class="read">{_e(excerpt) or "No parsable analysis."}</p>'
            f'<p><a href="{_e(link)}">Full record</a></p></details>'
        )
    listing = "".join(items) or '<p class="sec-sub">No simulation has been requested yet.</p>'
    controls = "".join(
        f'<label>{_e(label)}<select data-opt="{key}">'
        + "".join(f'<option value="{v}">{_e(text)}</option>' for v, text in opts)
        + "</select></label>"
        for key, (label, opts) in SIM_CHOICES.items()
    )
    return (
        '<div class="card"><h2>Run it yourself</h2><p class="sec-sub">Pick an agent, a stance and '
        "a starting point, and watch it decide now on live data. "
        "<b>Simulations never send an order.</b></p>"
        f'<div class="simform">{controls}<a class="btn" id="sim-go" href="{_e(url)}" '
        f'data-base="{_e(base)}" data-title="{_e(SIM_TITLE)}" data-text="{_e(SIM_BODY)}">'
        "Run a simulation</a></div>"
        '<details><summary>How it works and its limits</summary><p class="sec-sub">The button '
        "opens a pre-filled GitHub issue (a GitHub account is needed); the answer is posted in "
        "the issue in about 6 minutes and appears below. Cautious keeps 30% in cash and small "
        "positions; bold allows larger positions but never leverage. At most one simulation a "
        "minute and 20 a day; a request that comes too soon is answered in its issue."
        f"</p></details>{listing}</div>"
    )


def _evidence(title: str, note: str, body: str) -> str:
    return (
        f'<details class="ev"><summary><b>{_e(title)}</b> '
        f'<span class="sec-sub">{_e(note)}</span></summary>{body}</details>'
    )


def _table(head: list[str], rows: list[list[str]]) -> str:
    cells = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<div class='scroll'><table><tr>{cells}</tr>{body}</table></div>"


def _risk_calibration(report: dict[str, Any] | None) -> str:
    rows = (report or {}).get("risk_calibration", [])
    if not rows:
        return "<p>No forecast is resolved yet: the first horizon needs one closed session.</p>"
    return _table(
        [
            "Horizon (days)",
            "Resolved forecasts",
            "Inside the 80% band (target 80%)",
            "RMS ratio (1 is exact)",
        ],
        [
            [str(r["horizon"]), str(r["n"]), _pct(r["inside_80"], 0), f"{r['rms_z']:.2f}"]
            for r in rows
        ],
    ) + (
        '<p class="sec-sub">Forecast portfolio volatility (Timestral volatilities with historical '
        "correlations) against the move that followed. A ratio above 1 means risk was "
        "underestimated.</p>"
    )


def _confidence(report: dict[str, Any] | None) -> str:
    c = (report or {}).get("confidence")
    if not c:
        return (
            "<p>No deviation from the baseline is resolved yet: the 5-day outcome of a tilt needs "
            "5 closed sessions.</p>"
        )
    bins = _table(
        ["Stated confidence", "Tilts", "Average stated", "Paid off"],
        [
            [b["range"], str(b["n"]), f"{b['mean_confidence']:.2f}", _pct(b["hit_rate"], 0)]
            for b in c["bins"]
        ],
    )
    return (
        f"<p>{c['n']} tilt(s) resolved · average confidence {c['mean_confidence']:.2f} · "
        f"paid off {_pct(c['hit_rate'], 0)} · Brier score {c['brier']:.3f}</p>{bins}"
        '<p class="sec-sub">A tilt "paid off" when the extra weight earned a positive return over '
        "5 days. A calibrated LLM pays off about as often as it says. Brier: 0 is perfect, always "
        "answering 0.5 scores 0.25.</p>"
    )


def _guardrails(runs: list[dict[str, Any]]) -> str:
    rules = Counter(c["rule"] for r in runs for c in _corrections(r))
    outcomes = Counter(r["decision"]["outcome"] for r in runs)
    held, overridden = outcomes["fallback_hold"], outcomes["overridden"]
    if not rules and not held and not overridden:
        return f"<p>No correction, rejection or override in {len(runs)} run(s).</p>"
    table = (
        _table(["Rule", "Corrections"], [[_e(r), str(n)] for r, n in rules.most_common()])
        if rules
        else ""
    )
    return (
        f"<p>{len(runs)} run(s) · {held} rejected proposal(s) · {overridden} circuit-breaker "
        f"override(s)</p>{table}"
    )


def _run_history(runs: list[dict[str, Any]], repo: str, prefix: str = "") -> str:
    items = []
    for r in reversed(runs):
        link = f"{repo}/blob/{AUDIT_BRANCH}/{prefix}runs/{r['run_id']}.json"
        fixes = "".join(
            f"<li>{_e(c['rule'])} {_e(c['symbol'] or '')}: "
            f"{c['before']:.4f} → {c['after']:.4f}</li>"
            for c in _corrections(r)
        )
        orders = (
            _table(
                ["Asset", "Side", "Quantity", "Limit", "Status"],
                [
                    [
                        _e(o["symbol"]),
                        _e(o["side"]),
                        f"{o['quantity']:,.4f}",
                        "n/a" if o["limit_price"] is None else f"{o['limit_price']:,.2f}",
                        _e(o["status"]),
                    ]
                    for o in r["orders"]
                ],
            )
            if r["orders"]
            else "<p>No order.</p>"
        )
        analysis = _analysis(r)
        items.append(
            f'<details class="run"><summary>{_e(r["run_id"])} · {_outcome(r)} · {_execution(r)} · '
            f"{_usd(r['account']['equity'])}</summary>"
            + (f"<h3>Corrections</h3><ul>{fixes}</ul>" if fixes else "")
            + _tool_calls_detail(r)
            + f"<h3>Orders</h3>{orders}<h3>The LLM's analysis</h3>"
            + (
                f'<pre class="txt">{_e(analysis)}</pre>'
                if analysis
                else "<p>No parsable analysis.</p>"
            )
            + f'<p><a href="{_e(link)}">Full immutable record</a></p></details>'
        )
    return "".join(items)


def _downloads(repo: str, tables: list[str]) -> str:
    links = "".join(f'<li><a href="data/{_e(t)}">{_e(t)}</a></li>' for t in tables)
    return (
        '<div class="card"><h2>Export</h2><p class="sec-sub">The flat tables derived from the '
        f"audit records (CSV).</p><ul>{links}</ul>"
        f'<p>Full records, prompts and reasoning traces: <a href="{_e(repo)}/tree/{AUDIT_BRANCH}">'
        f"{AUDIT_BRANCH} branch</a>. Design decisions: "
        f'<a href="{_e(repo)}/blob/main/docs/decisions.md">docs/decisions.md</a>.</p></div>'
    )


_TOOL_LABEL = {
    "get_forecasts": "read Timestral's forecasts",
    "evaluate_portfolio": "had Timestral evaluate a candidate portfolio",
    "get_baseline": "looked at the Timestral rule",
}


def _tool_calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    return list(run["decision"].get("tool_calls") or [])


def _tool_use(run: dict[str, Any]) -> str:
    """How the tool agent used Timestral before deciding (D-018); nothing for the pipeline."""
    calls = _tool_calls(run)
    if not calls:
        return ""
    counts = Counter(c["name"] for c in calls)
    parts = [
        f"{_e(_TOOL_LABEL.get(n, n))} ({k} times)" if k > 1 else _e(_TOOL_LABEL.get(n, n))
        for n, k in counts.items()
    ]
    errors = sum("error" in c["result"] for c in calls)
    note = f", {errors} refused call(s)" if errors else ""
    return (
        f'<p class="sec-sub">Before deciding, the LLM called Timestral {len(calls)} time(s): '
        f"{'; '.join(parts)}{note}.</p>"
    )


def _tool_calls_detail(run: dict[str, Any]) -> str:
    calls = _tool_calls(run)
    if not calls:
        return ""
    rows = []
    for c in calls:
        result = c["result"]
        if "error" in result:
            seen = f"refused: {result['error']}"
        elif c["name"] == "evaluate_portfolio":
            pf = result["timestral_portfolio_forecast"]
            seen = (
                f"invested {_pct(result['invested'], 0)}, probability of a loss "
                f"{_pct(pf['probability_of_loss'], 0)}, bad case (5%) {_spct(pf['q05'], 1)}, "
                f"guardrails: {result['guardrails']['would_be']}"
            )
        elif c["name"] == "get_forecasts":
            seen = f"{len(result['assets'])} asset(s) at {result['horizon']} day(s)"
        else:
            seen = "the rule-based weights"
        rows.append([_e(c["name"]), _e(seen)])
    return "<h3>Tool calls</h3>" + _table(["Tool", "What came back"], rows)


@dataclass(frozen=True)
class AgentView:
    """One agent on the page (D-018): its records and where they live in the audit log."""

    key: str
    title: str
    summary: str
    runs: list[dict[str, Any]]
    fills: list[dict[str, Any]]
    scores: list[dict[str, Any]]
    report: dict[str, Any] | None
    prefix: str = ""  # folder of its records on the audit-log branch
    replay: dict[str, Any] | None = None  # its replay report (D-015), shown under Performance
    replay_folder: str = ""  # where the replay records live in the repository


def _agent_body(agent: AgentView, repo: str) -> str:
    runs, fills, report = agent.runs, agent.fills, agent.report
    if not runs:
        return (
            '<div class="card"><h2>No runs yet</h2><p class="sec-sub">This agent has not recorded '
            "a run. Its section fills in automatically after its first scheduled run.</p></div>"
        )
    evidence = [
        _evidence("Timestral's forecasts", "the risk behind the weights", _forecasts(runs[-1])),
        *(
            [
                _evidence(
                    "Forecast calibration", "do the intervals hold?", _calibration(agent.scores)
                )
            ]
            if agent.scores
            else []
        ),
        _evidence(
            "Forecast risk against what happened", "portfolio level", _risk_calibration(report)
        ),
        _evidence("The LLM's confidence", "is it calibrated?", _confidence(report)),
        _evidence("Guardrail activity", "corrections and rejections", _guardrails(runs)),
        _evidence("Executions and slippage", "what the broker did", _fills_table(fills)),
        _evidence("Account value per run", "the real account", _equity_chart(runs)),
        _evidence(
            "History of runs", "click a run for its detail", _run_history(runs, repo, agent.prefix)
        ),
    ]
    return "".join(
        [
            _status(runs, fills),
            _today(runs[-1], fills),
            _performance(runs, report, _replay(agent.replay, repo, agent.replay_folder)),
            _trades(fills, runs),
            _allocation(runs[-1], runs[0]),
            '<div class="card"><h2>Can we trust it?</h2><p class="sec-sub">The evidence behind '
            "the figures above. Click a line to open it.</p>" + "".join(evidence) + "</div>",
        ]
    )


_TAGLINE = {
    "tools": "The final agent: the LLM questions Timestral through tools, then builds.",
    "pipeline": "The reference: Timestral and a rule set a baseline, the LLM adjusts it.",
}


def _glance(agents: list[AgentView], moment: datetime | None = None) -> str:
    """What the reader needs first, for each agent: where it started, where it is, is anything
    wrong. Everything else on the page is evidence one click away."""
    cards = []
    for a in agents:
        tag = f'<p class="tag">{_e(_TAGLINE.get(a.key, a.summary))}</p>'
        if not a.runs:
            cards.append(
                f'<div class="gcard {a.key}"><h2>{_e(a.title)}</h2>{tag}'
                '<p class="sec-sub">Not started yet: its section fills in after its first run.</p>'
                "</div>"
            )
            continue
        first, last = a.runs[0], a.runs[-1]
        start, now = first["account"]["equity"], last["account"]["equity"]
        kind = "" if now == start else "up" if now > start else "down"
        alerts = _alerts(last, a.fills)
        level = "crit" if any(k == "crit" for k, _ in alerts) else "warn" if alerts else ""
        status = (
            _badge(f"{len(alerts)} alert(s)", level, "!" if level == "warn" else "✕")
            if alerts
            else _badge("No alert", "good", "✓")
        )
        began = datetime.fromisoformat(first["started_at"]).strftime("%d %b").lstrip("0")
        started = datetime.fromisoformat(last["started_at"])
        upcoming = _when(_next_slot(moment) if moment else _next_run(started, last["mode"]))
        # Green is for gains: a normal state is neutral, only a deviation takes a colour.
        calm = f"{_outcome(last)}{status if alerts else ''}".replace("b-good", "b-mute")
        cards.append(
            f'<div class="gcard {a.key}"><h2>{_e(a.title)}</h2>{tag}'
            f'<div class="journey"><span class="from">{_usd(start)}</span>'
            f'<span class="go" aria-label="to">→</span>'
            f'<span class="to {kind}">{_usd(now)}</span></div>'
            f'<div class="gdelta {kind}">{_usd(now - start).replace("$-", "-$")} '
            f"({_spct(now / start - 1, 2)})<span>since {_e(began)} · {len(a.runs)} run(s)</span>"
            f'</div><div class="pills">{calm}</div>'
            f'<div class="kv"><span>Last run</span><b>{_e(_when(started))}</b>'
            f"<span>Next run</span><b>{_e(upcoming)}</b></div></div>"
        )
    return f'<div class="glance">{"".join(cards)}</div>'


def _inputs(cov: dict[str, Any] | None = None) -> str:
    """What goes in, as keywords: the reviewer looks for the data, the models and the covariates."""
    pipe, tool = AgentConfig(), tool_agent_config()
    etfs = sum(a.type is AssetType.ETF for a in pipe.universe)
    stocks = len(pipe.universe) - etfs

    def size(cfg: AgentConfig) -> str:
        return cfg.forecast.model_id.split("/")[-1]

    rows = [
        (
            "Data",
            [
                "Alpaca daily bars",
                f"{len(pipe.universe)} assets: {etfs} ETFs, {stocks} stocks",
                f"{pipe.forecast.context_length}-day context",
            ],
        ),
        (
            "Forecasts",
            [
                f"{size(pipe)} (pipeline agent)",
                f"{size(tool)} (tool agent)",
                f"quantiles, 1 to {pipe.forecast.horizon} days",
            ],
        ),
        ("Decision", [pipe.llm.model, "native reasoning", "strict JSON"]),
        (
            "Covariates",
            [
                "news: article volume and sentiment (stocks)",
                "events: earnings, company events, macro dates",
                "sources: Alpaca News, FRED",
                f"analyst: {pipe.news.analyst_model}",
            ],
        ),
        ("Orders", ["Alpaca paper accounts", "marketable limit orders", "idempotent order ids"]),
    ]
    out = []
    for label, items in rows:
        pills = "".join(f'<span class="chip">{_e(i)}</span>' for i in items)
        if label == "Covariates":
            pills += (
                _badge("measured, not adopted", "mute", "-")
                if cov
                else _badge("built; not yet measured or used live", "warn", "!")
            )
        out.append(f'<div class="inrow"><b>{_e(label)}</b><div class="pills">{pills}</div></div>')
    return "".join(out)


_VARIANT_LABEL = {
    "news": "News sentiment",
    "events": "Company events",
    "news+events": "News and events",
}
_VERDICT_BADGE = {
    "clear": ("clear gain", "good", "✓"),
    "weak": ("weak gain", "warn", "!"),
    "no": ("no gain", "mute", "-"),
}


def _covariates(cov: dict[str, Any] | None) -> str:
    """The covariate trial of D-019: what was tried, the result, and what it cannot tell."""
    if not cov:
        return ""
    summary, pairs = cov["summary"], {(p["variant"], p["kind"]): p for p in cov["paired"]}
    verdicts = {v["variant"]: v for v in cov["verdicts"]}

    def row(variant: str, label: str) -> str:
        stock = next(s for s in summary if s["variant"] == variant and s["kind"] == "stock")
        etf = next(s for s in summary if s["variant"] == variant and s["kind"] == "etf")
        if variant == "prices":
            gain, ctrl, verdict = "reference", "reference", _badge("reference", "mute", "-")
        else:
            lo, hi = pairs[(variant, "stock")]["ci95"]
            rel = pairs[(variant, "stock")]["relative"]
            gain = f"{rel:+.2%} ({lo / stock['pinball']:+.2%} to {hi / stock['pinball']:+.2%})"
            ctrl = f"{pairs[(variant, 'etf')]['relative']:+.2%}"
            text, kind, icon = _VERDICT_BADGE[verdicts[variant]["decision"]]
            verdict = _badge(text, kind, icon)
        return (
            f"<tr><td>{_e(label)}</td><td class='num'>{stock['coverage_80']:.1%}</td>"
            f"<td class='num'>{etf['coverage_80']:.1%}</td><td class='num'>{_e(gain)}</td>"
            f"<td class='num'>{_e(ctrl)}</td><td>{verdict}</td></tr>"
        )

    rows = row("prices", "Prices only") + "".join(row(v, _VARIANT_LABEL[v]) for v in _VARIANT_LABEL)
    n_stocks = cov.get("stocks_with_news", cov["stocks"])
    return (
        '<div class="card"><h2>Do news help the forecast?</h2>'
        '<p class="sec-sub"><b>Tried, not adopted.</b> The same Timestral model forecast '
        f"{n_stocks} stocks and {cov['etfs']} ETFs on {cov['origins']} past days "
        f"({cov['first_origin']} to {cov['last_origin']}, {cov['horizon']}-day horizon), with and "
        "without news covariates. The ETFs are the control: they have no company news.</p>"
        '<div class="scroll"><table class="cmp"><tr><th>Forecast built from</th>'
        '<th class="num">Interval hit, stocks</th><th class="num">Interval hit, ETFs</th>'
        '<th class="num">Error vs prices only, stocks (95% range)</th>'
        '<th class="num">Same, ETFs (control)</th><th>Verdict</th></tr>'
        f"{rows}</table></div>"
        '<p class="sec-sub">Interval hit: share of outcomes inside the 80% interval, ideal 80%. '
        "Error: pinball loss, a negative figure means worse. The pre-declared rule (D-019) needs "
        "a lower error on stocks, calibration kept, and the control unharmed.</p>"
        "<details><summary>How to read it, and what it cannot tell</summary>"
        "<ul class='plain'>"
        "<li>The covariates do change the forecasts (the median flips sign in 11 to 23% of cases), "
        "but the changes cancel out: the average error does not move.</li>"
        "<li>Intervals widen by 2 to 3.5% for stocks <i>and</i> ETFs alike. That is a general "
        "caution once covariates are present, not information from the news, so the slightly "
        "better interval hit is not a gain.</li>"
        f"<li>Limits: one period of about six months, {cov['origins']} overlapping daily origins, "
        "55 earnings releases only, and one sentiment score per article from a small model, "
        "which is the weakest kind of information at five days.</li>"
        "<li>Not tested: known-in-advance calendars (earnings dates, central banks), market "
        "variables such as volatility, and the effect around events only.</li></ul></details>"
        "</div>"
    )


def _compare(cov: dict[str, Any] | None = None) -> str:
    """How the two approaches work, drawn, with the inputs as keywords (D-018)."""
    return (
        '<div class="card"><h2>Two ways of using Timestral</h2><p class="sec-sub">Same 15 assets, '
        "same data, same guardrails, one paper account each. Only the use of Timestral differs."
        "</p>" + _diagram() + _inputs(cov) + "</div>"
    )


def _pass_share(counts: dict[str, tuple[int, int]]) -> str:
    passed, total = sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())
    return f"{passed}/{total} ({100 * passed / total:.0f}%)" if total else "n/a"


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2)


def _measures(a: AgentView, evals: dict[str, Any] | None) -> dict[str, str]:
    """Concrete measures of one agent, as text: how it decides, what risk it takes, how it
    executes, how it behaved in tests, how it did on the past. 'n/a' when not measurable yet."""
    out: dict[str, str] = {}
    runs = a.runs
    if runs:
        n = len(runs)
        outcomes = Counter(r["decision"]["outcome"] for r in runs)
        port = [r["portfolios"] for r in runs]

        def moved(x: dict[str, Any], other: str) -> float:
            return float(100 * sum(abs(x["final"][s] - x[other][s]) for s in x["final"]))

        ratios = []
        for r in runs:
            vols = (r.get("risk") or {}).get("portfolio_vol") or {}
            if vols:
                v = vols[max(vols, key=int)]
                if v.get("final") and v.get("baseline"):
                    ratios.append(v["final"] / v["baseline"])
        traded, away = (
            _median([moved(x, "current") for x in port]),
            _median([moved(x, "baseline") for x in port]),
        )
        calls = [len(_tool_calls(r)) for r in runs]
        out["calls"] = f"{sum(calls) / n:.1f}" if any(calls) else "1 fixed run"
        out["accepted"] = f"{outcomes['accepted']}/{n}"
        out["corrections"] = f"{sum(len(_corrections(r)) for r in runs) / n:.1f}"
        out["traded"], out["away"] = f"{traded:.0f}", f"{away:.0f}"
        out["risk"] = f"{sum(ratios) / len(ratios):.2f}x" if ratios else "n/a"
        out["invested"] = _pct(sum(1 - x["final_cash"] for x in port) / n, 0)
        sums = [f["summary"] for f in a.fills]
        if sums:
            out["filled"] = f"{sum(x['n_filled'] for x in sums)}/{sum(x['n_orders'] for x in sums)}"
            slips = [
                x["slippage_bps_weighted"] for x in sums if x["slippage_bps_weighted"] is not None
            ]
            if slips:
                out["slippage"] = f"{sum(slips) / len(slips):+.1f} bps"
    rep = a.replay
    if rep and rep.get("sessions"):
        mine = rep["series"]["agent"]
        out["past_return"] = _spct(mine["cumulative_return"], 1)
        out["past_vol"] = _pct(mine["annualised_volatility"], 1)
        out["past_dd"] = _pct(mine["max_drawdown"], 1)
        five = next((r for r in rep["risk_calibration"] if r["horizon"] == 5), None)
        if five:
            out["past_cov"] = _pct(five["inside_80"], 0)
    if evals:
        res = evals.get("tools" if a.key == "tools" else "pipeline")
        if res:
            focus = evals.get("tools_focus") if a.key == "tools" else None
            out["tests"] = _pass_share(_scenario_counts(res, focus))
    return out


_MEASURE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("Decisions", "calls", "Timestral calls per decision"),
    ("", "accepted", "Accepted as proposed"),
    ("", "corrections", "Guardrail corrections per run"),
    ("", "traded", "Points traded per run (median)"),
    ("", "away", "Points away from the Timestral rule (median)"),
    ("Risk", "risk", "Forecast risk against the rule"),
    ("", "invested", "Invested share"),
    ("Execution", "filled", "Orders filled"),
    ("", "slippage", "Average slippage"),
    ("Tests", "tests", "Behaviour tests passed (more repeats for the tool agent)"),
    ("The past", "past_return", "Return over 63 replayed sessions"),
    ("", "past_vol", "Volatility, annualised"),
    ("", "past_dd", "Largest fall from a peak"),
    ("", "past_cov", "Risk interval hit (5 days, target 80%)"),
)


def _head_to_head(agents: list[AgentView], evals: dict[str, Any] | None) -> str:
    """The two agents on concrete measures, not only on money: one row per measure."""
    values = {a.key: _measures(a, evals) for a in agents}
    head = "".join(f'<th class="num c-{a.key}">{_e(a.title)}</th>' for a in agents)
    rows = []
    for group, key, label in _MEASURE_ROWS:
        if group:
            rows.append(f'<tr class="grp"><td colspan="{len(agents) + 1}">{_e(group)}</td></tr>')
        cells = "".join(f"<td class='num'>{_e(values[a.key].get(key, 'n/a'))}</td>" for a in agents)
        rows.append(f"<tr><td>{_e(label)}</td>{cells}</tr>")
    return (
        '<div class="card"><h2>How do they compare?</h2><p class="sec-sub">On how they decide, '
        "the risk they take, how they execute and how they behave in tests, not only on money. "
        "Live figures cover the few runs so far; the past is a 63-session replay.</p>"
        f'<div class="scroll"><table class="cmp"><tr><th></th>{head}</tr>{"".join(rows)}</table>'
        "</div></div>"
    )


def _tabs(agents: list[AgentView], repo: str) -> str:
    first = next((i for i, a in enumerate(agents) if a.runs), 0)
    buttons = "".join(
        f'<button type="button" role="tab" class="tab" id="tab-{a.key}" data-tab="{a.key}" '
        f'aria-controls="panel-{a.key}" aria-selected="{"true" if i == first else "false"}">'
        f"{_e(a.title)}</button>"
        for i, a in enumerate(agents)
    )
    panels = "".join(
        f'<section role="tabpanel" id="panel-{a.key}" data-panel="{a.key}" '
        f'aria-labelledby="tab-{a.key}"{"" if i == first else " hidden"}>{_agent_body(a, repo)}'
        "</section>"
        for i, a in enumerate(agents)
    )
    return f'<div class="tabs" role="tablist" aria-label="Agent">{buttons}</div>{panels}'


def _replay(report: dict[str, Any] | None, repo: str, folder: str) -> str:
    """Test case 1 (D-015): the same agent replayed on past sessions, anonymised. A dropdown
    under Performance: it is evidence for the reader who wants it, not the headline."""
    if not report or report.get("sessions", 0) < 1:
        return ""
    link = f"{repo}/blob/main/{folder}/README.md"
    body = (
        f'<p class="sec-sub">The same agent replayed on {report["sessions"]} past sessions, one '
        "decision a day, with strictly point-in-time data. Assets were renamed and dates shifted "
        "so the LLM could not recognise the period; fills at the next open plus costs. "
        f"Simulated account: {_spct(report['account_return'], 2)}.</p>"
        f"{_series_chart(report)}{_comparisons(report)}"
        f'<p class="sec-sub">A falling market, and only {report["sessions"]} sessions: read the '
        f'intervals, not the point estimates. <a href="{_e(link)}">Full replay report</a>.</p>'
    )
    return _evidence(
        "Tested on the past",
        f"{report['sessions']} sessions, {report['first_session']} to {report['last_session']}",
        body,
    )


# --------------------------------------------------------------------------- safeguards


def _flow(x: int, y: int, cls: str, title: str, sub: str) -> str:
    return (
        f'<rect class="{cls}" x="{x}" y="{y}" width="124" height="54" rx="8"/>'
        f'<text x="{x + 62}" y="{y + 23}" text-anchor="middle"><tspan font-weight="600">'
        f'{_e(title)}</tspan></text><text class="sm" x="{x + 62}" y="{y + 40}" '
        f'text-anchor="middle">{_e(sub)}</text>'
    )


def _flow_arrow(x: int, y: int, both: bool = False) -> str:
    start = ' marker-start="url(#dg-head)"' if both else ""
    return f'<line x1="{x}" y1="{y}" x2="{x + 36}" y2="{y}"{start} marker-end="url(#dg-head)"/>'


def _diagram() -> str:
    """The two agents as two flows, each in its colour. Shaded: Timestral. Plain: the LLM and
    the rest. What differs is how Timestral and the LLM are chained."""
    cols = [8, 168, 328, 488, 648]
    top, bottom = 30, 140
    parts = [
        '<text class="row tool" x="8" y="18">Tool agent: the LLM asks Timestral, then '
        "builds</text>",
        _flow(cols[0], top, "tool", "LLM", "asks, up to 12 calls"),
        _flow(cols[1], top, "tool ts", "Timestral tools", "forecast, evaluate"),
        _flow(cols[2], top, "tool", "LLM", "builds the portfolio"),
        _flow(cols[3], top, "tool", "Guardrails", "same limits"),
        _flow(cols[4], top, "tool", "Orders", "Alpaca paper"),
        '<text class="row pipe" x="8" y="128">Pipeline agent: Timestral first, the LLM '
        "adjusts</text>",
        _flow(cols[0], bottom, "pipe ts", "Timestral", "forecasts 15 assets"),
        _flow(cols[1], bottom, "pipe", "Rule", "baseline: 1 / width"),
        _flow(cols[2], bottom, "pipe", "LLM", "adjusts, max +15 pts"),
        _flow(cols[3], bottom, "pipe", "Guardrails", "correct or reject"),
        _flow(cols[4], bottom, "pipe", "Orders", "Alpaca paper"),
    ]
    for y in (top + 27, bottom + 27):
        for i in range(4):
            parts.append(_flow_arrow(cols[i] + 124, y, both=(y == top + 27 and i == 0)))
    return (
        '<figure><svg class="dg" viewBox="0 0 780 205" role="img" aria-label="The tool agent lets '
        "the LLM question Timestral through tools and build the portfolio itself. The pipeline "
        "agent runs Timestral on every asset, applies a rule and lets the LLM adjust it. Both "
        'then go through the same guardrails and orders.">'
        '<defs><marker id="dg-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z"/></marker></defs>'
        f"{''.join(parts)}</svg>"
        '<figcaption class="sec-sub">Shaded: Timestral. Same guardrails and orders for both; only '
        "the way Timestral and the LLM are chained differs.</figcaption></figure>"
    )


def _rules() -> str:
    """The guardrails in one line each, with the limits read from the configuration."""
    cfg = AgentConfig()
    r = cfg.risk
    levels = ", ".join(
        f"{_pct(abs(lv.threshold), 0)} {lbl}"
        for lv, lbl in zip(
            cfg.drawdown_levels,
            ("alert", "no new risk", "60% invested at most", "half the baseline, LLM paused"),
            strict=True,
        )
    )
    items = [
        (
            "Unusable answers are rejected",
            "invalid JSON, an unknown asset, weights that do not add up. The agent then keeps its "
            "positions.",
        ),
        (
            "Violations are corrected and logged",
            f"long only, no leverage, cash at least {_pct(r.min_cash, 0)}, at most "
            f"{_pct(r.max_weight_etf, 0)} per ETF, {_pct(r.max_weight_stock, 0)} per stock, "
            f"{_pct(r.max_weight_group, 0)} per sector. Pipeline agent: also at most "
            f"{100 * r.max_increase_vs_baseline:.0f} points above its baseline.",
        ),
        ("Losses trigger a circuit breaker", f"drawdown of {levels}."),
        (
            "Orders are bounded",
            f"no trade under {100 * cfg.execution.no_trade_band:.0f} points, turnover capped at "
            f"{_pct(cfg.execution.max_turnover_per_run, 0)} per run, limit orders, one order id "
            "per decision, paper account only, a kill switch.",
        ),
        (
            "An independent final check",
            "a deliberately simple second implementation re-verifies the core limits before any "
            "order leaves.",
        ),
    ]
    pills = "".join(f'<span class="badge b-mute">✓ {_e(title)}</span>' for title, _ in items)
    detail = (
        "<ul class='rules'>"
        + "".join(f"<li><b>{_e(title)}</b><small>{_e(text)}</small></li>" for title, text in items)
        + "</ul>"
    )
    return (
        f'<div class="pills">{pills}</div>'
        f"<details><summary>The limits in detail</summary>{detail}</details>"
    )


def _scenario_counts(
    results: dict[str, Any], focus: dict[str, Any] | None = None
) -> dict[str, tuple[int, int]]:
    """Passes out of repeats per scenario; a dedicated longer series replaces the short one."""
    counts = {
        s["key"]: (sum(r["passed"] for r in s["runs"]), len(s["runs"]))
        for s in results["scenarios"]
    }
    for key, series in (focus or {}).items():
        if key in counts and isinstance(series, list) and series:
            counts[key] = (sum(bool(x) for x in series), len(series))
    return counts


def _score(count: tuple[int, int] | None) -> str:
    if count is None:
        return "not measured"
    passed, n = count
    if passed == n:
        return _badge(f"{passed}/{n}", "mute", "✓")
    return _badge(f"{passed}/{n}", "warn", "!")


def _behaviour(evals: dict[str, Any]) -> str:
    pipeline, tools = evals.get("pipeline"), evals.get("tools")
    base = pipeline or tools
    if not base:
        return ""
    p = _scenario_counts(pipeline) if pipeline else {}
    t = _scenario_counts(tools, evals.get("tools_focus")) if tools else {}
    rows = [
        [
            f"<b>{_e(s['title'])}</b>",
            _score(t.get(s["key"])),
            _score(p.get(s["key"])),
        ]
        for s in base["scenarios"]
    ]
    notes = []
    for label, res in (("Pipeline agent", pipeline), ("Tool agent", tools)):
        if res:
            st = res["stability"]
            notes.append(
                f"{label}: asked the same question {st['calls']} times, answers differ by at most "
                f"{100 * st['max_pairwise_turnover']:.0f} points of the portfolio."
            )
    what = "".join(
        f"<li><b>{_e(x['title'])}</b><small>{_e(x['expectation'])}</small></li>"
        for x in base["scenarios"]
    )
    return (
        "<h3>How the LLM behaves when tested</h3>"
        '<p class="sec-sub">Seven situations built on purpose, each repeated. Passes out of '
        "repeats: the tool agent's prompt was tuned on longer series, so the bases differ.</p>"
        + _table(["Situation", "Tool agent", "Pipeline agent"], rows)
        .replace("<th>Tool agent</th>", '<th class="c-tools">Tool agent</th>')
        .replace("<th>Pipeline agent</th>", '<th class="c-pipeline">Pipeline agent</th>')
        + "<details><summary>What each situation tests, and how stable the answers are</summary>"
        f"<ul class='rules'>{what}</ul>"
        f'<p class="sec-sub">{_e(" ".join(notes))} Where an agent does not always pass, it is '
        "shown as it is: the guardrails bound the consequences either way.</p></details>"
    )


def _safeguards(evals: dict[str, Any] | None) -> str:
    return (
        '<div class="card"><h2>Why it should not do anything silly</h2>'
        f"<h3>Rules it cannot cross</h3>{_rules()}{_behaviour(evals) if evals else ''}</div>"
    )


# --------------------------------------------------------------------------- page


PIPELINE_SUMMARY = (
    "Timestral always runs; a rule turns its forecast intervals into a baseline portfolio and "
    "the LLM adjusts it, at most +15 points per asset. The reference, unchanged since the start."
)
TOOLS_SUMMARY = (
    "The LLM queries Timestral through tools: forecasts at the horizon it chooses, a forecast of "
    "any candidate portfolio, a check against the guardrails, the rule as a reference. It builds "
    "the portfolio itself, within the same absolute limits. The final agent."
)


_SECTIONS = {
    "How do they compare?": "cmp",
    "Why it should not do anything silly": "safe",
    "Two ways of using Timestral": "how",
    "Run it yourself": "sim",
    "Do news help the forecast?": "cov",
    # Inside an agent's tab, every card takes that agent's colour.
    "Performance": "agent",
    "Portfolio value": "agent",
    "What it holds": "agent",
    "Trades": "agent",
    "At the last run": "agent",
    "Can we trust it?": "agent",
}


def _sections(page: str) -> str:
    """A colour per kind of section, so the page can be scanned before it is read."""
    for title, key in _SECTIONS.items():
        page = page.replace(
            f'<div class="card"><h2>{title}</h2>', f'<div class="card sec-{key}"><h2>{title}</h2>'
        )
    return page


def render(
    runs: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    generated_at: datetime,
    repo: str = DEFAULT_REPO,
    tables: list[str] | None = None,
    scores: list[dict[str, Any]] | None = None,
    report: dict[str, Any] | None = None,
    simulations: list[dict[str, Any]] | None = None,
    tool_agent: AgentView | None = None,
    replay: dict[str, Any] | None = None,
    evals: dict[str, Any] | None = None,
    covariates: dict[str, Any] | None = None,
) -> str:
    """`runs`, `fills`, `scores` and `report` are the pipeline agent's; `tool_agent` adds the
    second agent of D-018 (shown first: it is the final agent); `replay` is the D-015 report."""
    pipeline = AgentView(
        "pipeline",
        "Pipeline agent",
        PIPELINE_SUMMARY,
        runs,
        fills,
        scores or [],
        report,
        replay=replay,
        replay_folder="docs/replay",
    )
    agents = [tool_agent, pipeline] if tool_agent is not None else [pipeline]
    intro = (
        '<h1>Timestral Trading Agent</h1><p class="sub">An autonomous paper-trading agent: '
        "Timestral forecasts the risk, a Mistral LLM decides, guardrails decide what reaches "
        "the broker. Paper money only. Read-only view of the immutable "
        f'<a href="{_e(repo)}/tree/{AUDIT_BRANCH}">audit log</a>.</p>'
    )
    if any(a.runs for a in agents):
        body = (
            _glance(agents, generated_at)
            + (_head_to_head(agents, evals) if len(agents) > 1 else "")
            + _safeguards(evals)
            + (_compare(covariates) if len(agents) > 1 else "")
            + _covariates(covariates)
            + _simulations(simulations or [], repo)
            + (_tabs(agents, repo) if len(agents) > 1 else _agent_body(pipeline, repo))
        )
    else:
        body = (
            '<div class="card"><h2>No runs yet</h2><p class="sec-sub">The agent has not recorded '
            "a run. This page fills in automatically after the first scheduled run.</p></div>"
        ) + _simulations(simulations or [], repo)
    stamp = generated_at.isoformat(timespec="seconds")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Timestral Trading Agent</title>"
        '<meta name="description" content="Read-only view of an autonomous paper-trading agent: '
        'Timestral forecasts, Mistral LLM decisions, guardrails and audit log.">'
        f'<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">'
        f"{intro}{_sections(body)}{_downloads(repo, tables or [])}"
        f'<p class="foot">Generated {_e(stamp)} from the audit log. Paper trading, not financial '
        f"advice.</p></div></div><script>{SCRIPT}</script></body></html>"
    )


def _view(store: FileAuditStore, key: str, title: str, summary: str, prefix: str) -> AgentView:
    runs = chronological(store.read_runs())
    observations = store.read_observations()
    return AgentView(
        key,
        title,
        summary,
        runs,
        store.read_fills(),
        score_forecasts(runs, observations),
        performance_report(runs, observations),
        prefix,
    )


def chronological(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first; for runs at the same instant the morning run comes before the afternoon."""
    return sorted(runs, key=lambda r: (r["started_at"], r["mode"] != "morning"))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _load_evals(folder: Path) -> dict[str, Any] | None:
    """The behaviour tests of D-016: the pipeline agent, and the tool agent's final prompt."""
    found = {
        "pipeline": _load_json(folder / "results.json"),
        "tools": _load_json(folder / "tools_v2" / "results.json"),
        "tools_focus": _load_json(folder / "tools_v2" / "focus_10_runs_v2.json"),
    }
    return found if found["pipeline"] or found["tools"] else None


def build(
    audit_dir: Path,
    out_dir: Path,
    generated_at: datetime,
    repo: str = DEFAULT_REPO,
    replay_dir: Path | None = None,
    tool_replay_dir: Path | None = None,
    evals_dir: Path | None = None,
    covariates_dir: Path | None = None,
) -> Path:
    pipeline = _view(FileAuditStore(audit_dir), "pipeline", "Pipeline agent", PIPELINE_SUMMARY, "")
    tools_dir = audit_dir / TOOLS_FOLDER
    tool_agent = (
        _view(FileAuditStore(tools_dir), "tools", "Tool agent", TOOLS_SUMMARY, f"{TOOLS_FOLDER}/")
        if (tools_dir / "runs").exists()
        else None
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    tables: list[str] = []
    source = audit_dir / "tables"
    if source.exists():
        (out_dir / "data").mkdir(exist_ok=True)
        for csv_file in sorted(source.glob("*.csv")):
            shutil.copy(csv_file, out_dir / "data" / csv_file.name)
            tables.append(csv_file.name)
    replay = _load_json(replay_dir / "report.json") if replay_dir is not None else None
    if tool_agent is not None and tool_replay_dir is not None:
        tool_agent = replace(
            tool_agent,
            replay=_load_json(tool_replay_dir / "report.json"),
            replay_folder="docs/replay_tools",
        )
    evals = _load_evals(evals_dir) if evals_dir is not None else None
    covariates = _load_json(covariates_dir / "results.json") if covariates_dir else None
    page = out_dir / "index.html"
    sims = chronological(FileAuditStore(audit_dir / "simulations").read_runs())
    page.write_text(
        render(
            pipeline.runs,
            pipeline.fills,
            generated_at,
            repo,
            tables,
            pipeline.scores,
            pipeline.report,
            sims,
            tool_agent=tool_agent,
            replay=replay,
            evals=evals,
            covariates=covariates,
        ),
        encoding="utf-8",
    )
    return page


def main() -> None:  # pragma: no cover - CLI wiring, runs in the dashboard workflow
    import os
    from datetime import UTC

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-dir", default="audit", type=Path)
    parser.add_argument("--out", default="site", type=Path)
    parser.add_argument("--replay-dir", default=Path("docs/replay"), type=Path)
    parser.add_argument("--tool-replay-dir", default=Path("docs/replay_tools"), type=Path)
    parser.add_argument("--evals-dir", default=Path("docs/evals"), type=Path)
    parser.add_argument("--covariates-dir", default=Path("docs/covariates"), type=Path)
    args = parser.parse_args()
    slug = os.environ.get("GITHUB_REPOSITORY")
    repo = f"https://github.com/{slug}" if slug else DEFAULT_REPO
    print(
        build(
            args.audit_dir,
            args.out,
            datetime.now(UTC),
            repo,
            args.replay_dir,
            args.tool_replay_dir,
            args.evals_dir,
            args.covariates_dir,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
