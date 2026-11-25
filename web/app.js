/* Timestral Trading Agent: the dashboard (D-012).
 *
 * React 18 with htm (JSX-like templates, no build step). All figures come from data.json, which
 * Python writes from the audit records (src/agent/web.py): this file only lays them out.
 * Text from the LLM is rendered as text, never as HTML. */
(() => {
  "use strict";
  const { useState, useEffect, useMemo } = React;
  const html = htm.bind(React.createElement);

  // ------------------------------------------------------------------ formatting
  const TZ = "America/New_York";
  const usd = (x) =>
    (x < 0 ? "-$" : "$") + Math.abs(x).toLocaleString("en-US", { maximumFractionDigits: 0 });
  const usd2 = (x) =>
    "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = (x, d = 1) => (100 * x).toFixed(d) + "%";
  const spct = (x, d = 1) => (x > 0 ? "+" : "") + (100 * x).toFixed(d) + "%";
  const pts = (x, d = 1) => (x > 0 ? "+" : "") + (100 * x).toFixed(d) + " pts";
  const when = (iso) =>
    new Date(iso).toLocaleString("en-US", {
      timeZone: TZ, weekday: "short", day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }) + " ET";
  const day = (iso) =>
    new Date(iso).toLocaleDateString("en-US", { timeZone: TZ, day: "numeric", month: "short" });
  const key = (fn) => (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fn(); } };
  const sign = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");

  const OUTCOME = {
    accepted: ["Accepted", "ok"],
    corrected: ["Accepted with corrections", "mid"],
    overridden: ["Circuit breaker override", "bad"],
    fallback_hold: ["Rejected, positions held", "bad"],
  };
  const EXECUTION = {
    live: "orders sent",
    dry_run: "dry run",
    dry_run_kill_switch: "dry run (kill switch off)",
    hold: "no orders",
  };
  const TABS = [
    ["live", "Live", "What the agents hold and decide"],
    ["how", "How it works", "Who does what, and within which limits?"],
    ["evidence", "Evidence", "How do we know the decisions are sound?"],
    ["try", "Try it", "Make an agent decide now"],
  ];
  const ROUTES = [...TABS.map((t) => t[0]), "lexicon"];

  const Outcome = ({ o }) => {
    const [label, kind] = OUTCOME[o] || [o, ""];
    return html`<span className=${"state " + kind}>${label}</span>`;
  };


  // ------------------------------------------------------------------ marks
  /* Timestral: the pixel hourglass of its launch visual, sand falling into an M. One letter per
     cell: D frame, G glass, W light, P peach sand, O orange sand, "." empty. */
  const HOURGLASS = [
    "DDDDDDDDD",
    ".GWWWWWG.",
    ".GWPPPOG.",
    "..GPOOG..",
    "...GOG...",
    "....O....",
    "...GOG...",
    "..GOPOG..",
    ".GOOGOOG.",
    ".GOGOGOG.",
    "DDDDDDDDD",
  ];
  const HG = { D: "#1f7be0", G: "#b9d7fb", W: "#e9f3ff", O: "#fa520f", P: "#ffb08f" };
  function Timestral({ size = 16, x, y }) {
    const cells = [];
    HOURGLASS.forEach((row, r) => [...row].forEach((c, k) => {
      if (c !== ".") cells.push(html`<rect key=${r + "-" + k} x=${k} y=${r} width="1.02" height="1.02" fill=${HG[c]} />`);
    }));
    return html`<svg className="mark" x=${x} y=${y} width=${(size * 9) / 11} height=${size} viewBox="0 0 9 11"
      shapeRendering="crispEdges" role="img" aria-label="Timestral">${cells}</svg>`;
  }

  /* The Mistral LLM: the pixel cat of Mistral's visuals ("chonk"). Letters: O orange, D darker
     orange, K black (eyes and nose), R red collar, Y yellow tag. */
  const CHONK = [
    "...D.......D...",
    "...DO.....OD...",
    "...OOOOOOOOO...",
    "...OOKOOOKOO...",
    "...OOOOKOOOO...",
    "...OOOOOOOOO...",
    "...RRRRRRRRR...",
    "...OOOOYOOOO...",
    "..OOODOOODOOO..",
    "..OOOOOOOOOOO.D",
    "..ODDOOOOODDO.D",
    "..OOOOOOOOOOODD",
    "..OOOOOOOOOOOD.",
    ".DDDDDDDDDDDDD.",
  ];
  const CH = { O: "#f0892b", D: "#e2602a", K: "#1c1c1c", R: "#d2341c", Y: "#f4c542" };
  function Chonk({ size = 16, x, y }) {
    const cells = [];
    CHONK.forEach((row, r) => [...row].forEach((c, k) => {
      if (c !== ".") cells.push(html`<rect key=${r + "-" + k} x=${k} y=${r} width="1.02" height="1.02" fill=${CH[c]} />`);
    }));
    return html`<svg className="mark" x=${x} y=${y} width=${(size * 15) / 14} height=${size} viewBox="0 0 15 14"
      shapeRendering="crispEdges" role="img" aria-label="Mistral LLM">${cells}</svg>`;
  }

  /* One pictogram per agent, in its colour: a wrench for the agent that uses tools, three
     chained blocks for the fixed pipeline. */
  function AgentIcon({ k, size = 16, x, y }) {
    return html`<svg className=${"mark c-" + k} x=${x} y=${y} width=${size} height=${size} viewBox="0 0 24 24" fill="none"
      stroke="var(--c)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      ${k === "tools"
        ? html`<path d="M15 3a5 5 0 0 0-4.6 6.9l-6.9 6.9a1.9 1.9 0 0 0 2.7 2.7l6.9-6.9A5 5 0 0 0 19.9 8L17 10.9l-2.6-.6-.6-2.6L16.7 4.8A5 5 0 0 0 15 3z" />`
        : html`<${React.Fragment}><rect x="1.5" y="8.5" width="5" height="7" rx="1" /><rect x="9.5" y="8.5" width="5" height="7" rx="1" />
            <rect x="17.5" y="8.5" width="5" height="7" rx="1" /><path d="M6.5 12h3M14.5 12h3" /></${React.Fragment}>`}
    </svg>`;
  }
  const Glyph = ({ d, size = 14 }) => html`<svg className="mark" width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d=${d} /></svg>`;
  const DOC = "M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7";
  const CODE = "M8 7l-5 5 5 5M16 7l5 5-5 5";
  const CHART = "M3 20h18M5 16l4-5 4 3 6-8";
  const GRID = "M3 5h18v14H3zM3 10h18M3 15h18M9 5v14";
  const BRACES = "M8 4c-2 0-2 1.5-2 3v2c0 1.5-1 2-2 2 1 0 2 .5 2 2v3c0 1.5 0 3 2 3M16 4c2 0 2 1.5 2 3v2c0 1.5 1 2 2 2-1 0-2 .5-2 2v3c0 1.5 0 3-2 3";
  /* GitHub's mark (Octicons "mark-github", MIT), to say where the button leads. */
  const GitHubMark = () => html`<svg className="mark" width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z" /></svg>`;
  const ARROW = "M4 12h15M13 6l6 6-6 6";
  const BOOK = "M4 5h6a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H4zM20 5h-6a3 3 0 0 0-3 3v12a2 2 0 0 1 2-2h7z";
  const PLAY = "M7 4l13 8-13 8z";
  const SHIELD = "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4";
  const FLASK = "M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3M7.5 15h9";
  const REWIND = "M3 12a9 9 0 1 0 2.6-6.4L3 8M3 3v5h5M12 7v5l3 2";
  const NEWS = "M4 5h12v14H6a2 2 0 0 1-2-2zM16 9h4v8a2 2 0 0 1-4 0M7 9h6M7 13h6";

  /* A long paragraph shows its first sentence; the reader opens the rest if they want it. */
  function Fold({ first, children, small }) {
    const [open, setOpen] = useState(false);
    const cls = small ? "note small" : "note";
    const toggle = html`<button className="more" aria-expanded=${open} onClick=${() => setOpen(!open)}>${open ? "Less" : "More"}</button>`;
    if (!open) return html`<p className=${cls}>${first} ${toggle}</p>`;
    return html`<div className="fold">
      <p className=${cls}>${first}</p>
      <p className=${cls + " rest"}>${children}</p>
      <p className=${cls + " toggle"}>${toggle}</p></div>`;
  }

  /* A tooltip anchored under any element of a section with the class "tipped": returns the
     handlers to spread on the trigger and the box to render once in the section. */
  function useTip() {
    const [tip, setTip] = useState(null); // { id, top, left, body }
    const open = (id, body) => (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const s = e.currentTarget.closest("section").getBoundingClientRect();
      setTip({ id, body, top: r.bottom - s.top + 4, left: Math.max(0, Math.min(r.left - s.left, s.width - 380)) });
    };
    const close = () => setTip(null);
    const on = (id, body) => ({
      tabIndex: 0, onMouseEnter: open(id, body), onMouseLeave: close, onFocus: open(id, body), onBlur: close,
      onClick: (e) => (tip && tip.id === id ? close() : open(id, body)(e)),
    });
    const box = tip && html`<div role="tooltip" className="tipbox" style=${{ top: tip.top, left: tip.left }}>${tip.body}</div>`;
    return { on, box, active: tip && tip.id };
  }

  // ------------------------------------------------------------------ charts
  function ticks(lo, hi, n = 4) {
    const raw = (hi - lo) / n;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
    return out;
  }

  /* Both accounts on one time axis, each in its agent's colour. Hover (or touch) reads the
     value of each account at that moment. */
  function BothValues({ agents }) {
    const live = agents.filter((a) => a.started && a.series.length);
    const [at, setAt] = useState(null);
    if (!live.length) return null;
    const W = 1080, H = 260, L = 74, R = 12, T = 14, B = 28;
    const pts = live.map((a) => a.series.map((p) => ({ ...p, t: Date.parse(p.at) })));
    const ts = pts.flat().map((p) => p.t);
    const t0 = Math.min(...ts), t1 = Math.max(...ts);
    const vals = pts.flat().map((p) => p.equity).concat(live.map((a) => a.start.equity));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.25, hi * 0.002);
    lo -= pad; hi += pad;
    const x = (t) => (t1 === t0 ? (L + W - R) / 2 : L + (W - L - R) * (t - t0) / (t1 - t0));
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const days = [];
    pts.flat().slice().sort((p, q) => p.t - q.t).forEach((p) => { const d = day(p.at); if (!days.some((e) => e[0] === d)) days.push([d, p.t]); });
    const nearest = (list, t) => list.reduce((best, p) => (Math.abs(p.t - t) < Math.abs(best.t - t) ? p : best), list[0]);
    const t = at === null ? t1 : at;
    const onMove = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      const px = ((e.clientX - r.left) * W) / r.width;
      setAt(t0 + ((Math.min(Math.max(px, L), W - R) - L) / (W - L - R)) * (t1 - t0));
    };
    const ref = live[0].start.equity;
    return html`
      <section className="sec">
        <header><h2>Account value</h2></header>
        <div className="chart">
          <svg viewBox=${`0 0 ${W} ${H}`} role="img" onMouseMove=${onMove} onMouseLeave=${() => setAt(null)}
               aria-label=${live.map((a) => `${a.title}: ${usd(a.now.equity)}`).join(", ")}>
            ${ticks(lo, hi).map((v) => html`<g key=${v}><line className="grid" x1=${L} x2=${W - R} y1=${y(v)} y2=${y(v)} />
              <text x=${L - 8} y=${y(v) + 4} textAnchor="end">${usd(v)}</text></g>`)}
            <line className="ref" x1=${L} x2=${W - R} y1=${y(ref)} y2=${y(ref)} />
            <text x=${W - R} y=${y(ref) - 6} textAnchor="end">start ${usd(ref)}</text>
            ${days.map(([d, dt], i) => html`<text key=${d} x=${x(dt)} y=${H - 8} textAnchor=${i === 0 ? "start" : x(dt) > W - R - 40 ? "end" : "middle"}>${d}</text>`)}
            ${at !== null && html`<line className="hover" x1=${x(t)} x2=${x(t)} y1=${T} y2=${H - B} />`}
            ${live.map((a, k) => [a, k]).reverse().map(([a, k]) => html`<g key=${a.key} className=${"c-" + a.key}>
              ${pts[k].length > 1 && html`<polyline className="line" points=${pts[k].map((p) => `${x(p.t).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ")} />`}
              ${pts[k].map((p, i) => html`<circle key=${i} className="pt" cx=${x(p.t)} cy=${y(p.equity)} r=${p === nearest(pts[k], t) ? 4.5 : 2.5} />`)}</g>`)}
          </svg>
          ${at !== null && html`<div className="readout">${live.map((a, k) => { const p = nearest(pts[k], t); return html`<span key=${a.key} className=${"c-" + a.key}>
            <${AgentIcon} k=${a.key} size=${14} />${a.title} ${usd2(p.equity)} <span className="muted">${when(p.at)}</span></span>`; })}</div>`}
        </div>
      </section>`;
  }

  /* One asset: Timestral's 80% interval (q10 to q90) and median for each horizon, drawn as a
     fan from today's price (0%) to 5 days ahead. All fans share one scale, so widths compare. */
  function Fan({ a, bound, on, tipOn }) {
    const W = 160, H = 96, L = 4, R = 4, T = 6, B = 6;
    const hs = a.horizons;
    const maxH = hs[hs.length - 1].h;
    const x = (h) => L + (W - L - R) * (h / maxH);
    const y = (v) => T + (H - T - B) * (0.5 - v / (2 * bound));
    const upper = [[0, 0], ...hs.map((h) => [h.h, h.q90])];
    const lower = [[0, 0], ...hs.map((h) => [h.h, h.q10])].reverse();
    const band = upper.concat(lower).map(([h, v]) => `${x(h).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const med = [[0, 0], ...hs.map((h) => [h.h, h.median])].map(([h, v]) => `${x(h).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const last = hs[hs.length - 1];
    const colour = a.type === "stock" ? "var(--stock)" : "var(--etf)";
    const body = html`<div className="tiphead"><${Timestral} size=${11} /><b>${a.symbol}</b> <span className="muted">${a.name}</span></div>
      <table className="mini"><thead><tr><th>Days</th><th className="num">q10</th><th className="num">Median</th><th className="num">q90</th><th className="num">Spread</th></tr></thead>
        <tbody>${hs.map((h) => html`<tr key=${h.h}><td>${h.h}</td><td className="num">${spct(h.q10)}</td><td className="num">${spct(h.median)}</td>
          <td className="num">${spct(h.q90)}</td><td className="num">${pct(h.spread)}</td></tr>`)}</tbody></table>
      <p>Rule weight ${pct(a.rule_weight)}, final ${pct(a.final_weight)}.</p>`;
    return html`
      <div className=${"fan" + (tipOn === a.symbol ? " on" : "")} ...${on(a.symbol, body)}>
        <div className="h"><b>${a.symbol}</b><span className="muted">${a.name}</span></div>
        <svg viewBox=${`0 0 ${W} ${H}`} role="img"
             aria-label=${`${a.symbol}: 80% interval at ${maxH} days from ${spct(last.q10)} to ${spct(last.q90)}`}>
          <line x1=${L} x2=${W - R} y1=${y(0)} y2=${y(0)} stroke="var(--rule2)" />
          <polygon points=${band} fill=${colour} opacity="0.16" />
          <polyline points=${med} fill="none" stroke=${colour} strokeWidth="1.5" />
          <text x=${W - R} y=${y(last.q90) - 3} textAnchor="end" style=${{ font: "10px var(--mono)", fill: "var(--ink3)" }}>${spct(last.q90, 0)}</text>
          <text x=${W - R} y=${y(last.q10) + 11} textAnchor="end" style=${{ font: "10px var(--mono)", fill: "var(--ink3)" }}>${spct(last.q10, 0)}</text>
        </svg>
        <div className="w"><span>spread ${pct(last.spread)}</span><span>rule ${pct(a.rule_weight)} → ${pct(a.final_weight)}</span></div>

      </div>`;
  }

  // ------------------------------------------------------------------ live
  function AgentCard({ a, selected, onSelect }) {
    if (!a.started) {
      return html`<div role="button" tabIndex="0" className=${"acard c-" + a.key} aria-pressed=${selected} onClick=${onSelect} onKeyDown=${key(onSelect)}>
        <div className="who"><b><${AgentIcon} k=${a.key} />${a.title}</b></div><${Traits} a=${a} />
        <p className="muted">Not started yet.</p></div>`;
    }
    const diff = a.now.equity - a.start.equity;
    return html`
      <div role="button" tabIndex="0" className=${"acard c-" + a.key} aria-pressed=${selected} onClick=${onSelect} onKeyDown=${key(onSelect)}>
        <div className="who"><b><${AgentIcon} k=${a.key} />${a.title}</b><span>${a.n_runs} run${a.n_runs > 1 ? "s" : ""} since ${day(a.start.at)}</span></div>
        <${Traits} a=${a} />
        <div className="value">${usd(a.now.equity)}</div>
        <div className=${"delta " + sign(diff)}>${(diff >= 0 ? "+" : "") + usd(diff)} (${spct(diff / a.start.equity, 2)}) <span className="muted">from ${usd(a.start.equity)}</span></div>
        <dl>
          <dt>Invested</dt><dd>${pct(1 - a.decision.cash, 0)}</dd>
          <dt>Last run</dt><dd>${when(a.decision.at)} · <${Outcome} o=${a.decision.outcome} /></dd>
        </dl>
        ${a.alerts.length > 0 && html`<div className="alert">${a.alerts.map((x, i) => html`<div key=${i} className=${x.level === "crit" ? "down" : "warn"}>▲ ${x.text}</div>`)}</div>`}
      </div>`;
  }

  const Traits = ({ a }) => a.traits && a.traits.length > 0 && html`<dl className="traits">
    ${a.traits.map(([k, v]) => html`<${React.Fragment} key=${k}><dt>${k}</dt><dd>${v}</dd></${React.Fragment}>`)}</dl>`;

  function Decision({ a }) {
    const d = a.decision;
    const [full, setFull] = useState(false);
    const text = d.analysis || "No parsable analysis.";
    const short = text.length > 520 && !full ? text.slice(0, 500).replace(/\s+\S*$/, "") + "…" : text;
    const moves = a.holdings
      .map((h) => ({ ...h, change: h.final - h.current }))
      .filter((h) => Math.abs(h.change) >= 0.005)
      .sort((p, q) => Math.abs(q.change) - Math.abs(p.change));
    const [shown, setShown] = useState(false);
    if (!shown) return html`
      <section className="sec">
        <header><h2>Last decision</h2><span className="aside">${d.run_id} · ${when(d.at)}</span></header>
        <button className="reveal" aria-expanded="false" onClick=${() => setShown(true)}>
          <span><b>Show the decision</b></span><span className="rv-end"><${Outcome} o=${d.outcome} /></span></button>
      </section>`;
    return html`
      <section className="sec open">
        <header><h2>Last decision</h2><span className="aside">${d.run_id} · ${when(d.at)}</span></header>
        <p className="facts">
          <span><${Outcome} o=${d.outcome} /></span>
          <span>${EXECUTION[d.execution] || d.execution}</span>
          <span>model <b>${d.model}</b></span><span>prompt <b>${d.prompt}</b></span>
          ${d.portfolio_vol && d.portfolio_vol.final != null && html`<span>forecast risk, ${d.portfolio_vol.horizon} days: <b>${pct(d.portfolio_vol.final)}</b> (rule alone ${pct(d.portfolio_vol.rule)})</span>`}
          <a href=${d.record}>full record</a>
        </p>
        <button className="reveal" aria-expanded="true" onClick=${() => setShown(false)}><span><b>Hide the decision</b></span></button>
        <div className="decision">
          <div>
            <h3 style=${{ marginTop: 0 }}><${Chonk} size=${14} />What the LLM wrote</h3>
            <p className="reading">${short}</p>
            ${text.length > 520 && html`<button className="more" aria-expanded=${full} onClick=${() => setFull(!full)}>${full ? "Show less" : "Read all"}</button>`}
            ${d.corrections.length > 0 && html`<${React.Fragment}>
              <h3>Corrected by the guardrails</h3>
              <table><tbody>${d.corrections.map((c, i) => html`<tr key=${i}><td>${c.rule}</td><td>${c.symbol || ""}</td>
                <td className="num">${pct(c.before)} → ${pct(c.after)}</td></tr>`)}</tbody></table></${React.Fragment}>`}
          </div>
          <div>
            ${d.tool_steps.length > 0 && html`<${React.Fragment}>
              <h3 style=${{ marginTop: 0 }}><${Timestral} size=${14} />Before deciding, it asked Timestral</h3>
              <ol className="steps">${d.tool_steps.map((s, i) => html`<li key=${i}><span>${s.label}${s.horizon ? `, ${s.horizon} day${s.horizon > 1 ? "s" : ""}` : ""}</span><small>${s.result}</small></li>`)}</ol>
            </${React.Fragment}>`}
            <h3 style=${d.tool_steps.length ? {} : { marginTop: 0 }}>What changed</h3>
            ${moves.length === 0
              ? html`<p className="soft">Nothing: positions held.</p>`
              : html`<table><thead><tr><th>Asset</th><th className="num">Weight</th><th className="num">Change</th></tr></thead>
                <tbody>${moves.slice(0, 8).map((h) => html`<tr key=${h.symbol}><td><b>${h.symbol}</b></td>
                  <td className="num">${h.current === 0 ? "cash" : pct(h.current)} → ${pct(h.final)}</td>
                  <td className=${"num " + sign(h.change)}>${pts(h.change)}</td></tr>`)}</tbody></table>
                ${moves.length > 8 && html`<p className="muted">+ ${moves.length - 8} smaller changes</p>`}`}
          </div>
        </div>
      </section>`;
  }

  function Forecast({ a }) {
    const f = a.forecast;
    const tip = useTip();
    const [view, setView] = useState("fans");
    const [shown, setShown] = useState(false);
    if (!shown) return html`
      <section className="sec">
        <header><h2><${Timestral} />Timestral's forecast behind it</h2></header>
        <button className="reveal" aria-expanded="false" onClick=${() => setShown(true)}>
          <span><b>Show the forecast</b></span><span className="rv-end muted">${f.assets.length} assets</span></button>
      </section>`;
    const bound = useMemo(() => {
      const m = Math.max(...f.assets.flatMap((x) => x.horizons.map((h) => Math.max(Math.abs(h.q10), Math.abs(h.q90)))));
      return Math.ceil(m * 20) / 20;
    }, [f]);
    const hs = f.assets[0].horizons.map((h) => h.h);
    const maxH = hs[hs.length - 1];
    const tq = f.tool_quantiles;
    const qcols = tq ? Object.keys(Object.values(tq.assets)[0]) : [];
    return html`
      <section className="sec tipped open">
        <header><h2><${Timestral} />Timestral's forecast behind it</h2>
          <button className="more" aria-expanded="true" onClick=${() => setShown(false)}>Hide</button>
          <span className="views" role="group" aria-label="View">
            ${[["fans", "Chart", CHART], ["table", "Numbers", GRID], ["raw", "Raw output", BRACES]].map(([k, t, d]) =>
              html`<button key=${k} aria-pressed=${view === k} onClick=${() => setView(k)}><${Glyph} d=${d} size=${13} />${t}</button>`)}
          </span></header>
        <${Fold} first=${html`The wider the <b>spread</b> (<span className="formula">q90 − q10</span>), the riskier the asset and the smaller its weight.`}>
          ${f.model}, on ${f.context_days} days of closes up to ${f.last_close}, gives for each asset a distribution of the cumulative
          return 1 to ${maxH} days ahead. The rule weights each asset by <span className="formula">1 / spread</span>; the LLM then adjusts.
          The median is shown for reference: Timestral has no measured directional skill here.</${Fold}>
        ${view === "fans" && html`<${React.Fragment}>
          <div className="fans">${f.assets.map((x) => html`<${Fan} key=${x.symbol} a=${x} bound=${bound} on=${tip.on} tipOn=${tip.active} />`)}</div>
          ${tip.box}
          <div className="legend"><span><i className="sq etf"></i>ETF</span><span><i className="sq stock"></i>Stock</span>
            <span>Band: 80% interval, today to ${maxH} days · line: median · same scale (±${pct(bound, 0)}) for every asset · riskiest first · hover or tap an asset for its numbers</span></div>
        </${React.Fragment}>`}
        ${view === "table" && html`<div className="scroll"><table>
          <thead><tr><th>Asset</th>${hs.map((h) => html`<th key=${h} className="num">${h}d q10</th>`)}<th className="num">${maxH}d median</th><th className="num">${maxH}d q90</th><th className="num">Spread ${maxH}d</th><th className="num">Rule</th><th className="num">Final</th></tr></thead>
          <tbody>${f.assets.map((x) => {
            const last = x.horizons[x.horizons.length - 1];
            return html`<tr key=${x.symbol}><td><i className=${"sq " + x.type}></i><b>${x.symbol}</b></td>
              ${x.horizons.map((h) => html`<td key=${h.h} className="num">${spct(h.q10)}</td>`)}
              <td className="num">${spct(last.median)}</td><td className="num">${spct(last.q90)}</td>
              <td className="num"><b>${pct(last.spread)}</b></td><td className="num">${pct(x.rule_weight)}</td><td className="num">${pct(x.final_weight)}</td></tr>`;
          })}</tbody></table></div>`}
        ${view === "raw" && html`<${React.Fragment}>
          <p className="note">Exactly as written in the audit record: per asset and horizon (days), cumulative returns as fractions.</p>
          <pre className="raw">${JSON.stringify(f.raw, null, 1)}</pre>
          ${tq && html`<${React.Fragment}>
            <h3>What the tool agent saw: ${qcols.length} values per asset at ${tq.horizon} days</h3>
            <div className="scroll"><table><thead><tr><th>Asset</th>${qcols.map((q) => html`<th key=${q} className="num">${q}</th>`)}</tr></thead>
              <tbody>${Object.entries(tq.assets).map(([s, v]) => html`<tr key=${s}><td><b>${s}</b></td>
                ${qcols.map((q) => html`<td key=${q} className="num">${q === "width_80" || q === "volatility" ? pct(v[q]) : spct(v[q])}</td>`)}</tr>`)}</tbody></table></div>
          </${React.Fragment}>`}
        </${React.Fragment}>`}
      </section>`;
  }

  /* The target portfolio: the bar, the value and the weight at first glance; the rest of the
     story of each asset (the rule's weight, what the LLM proposed, its confidence and its reason)
     opens on hover, on keyboard focus, or on a tap. */
  function Holdings({ a }) {
    const [tip, setTip] = useState(null); // { i, top }
    const scale = Math.max(0.25, ...a.holdings.map((h) => Math.max(h.final, h.rule)));
    const cash = a.decision.cash;
    const equity = a.runs[0].equity;
    const show = (i) => (e) => setTip({ i, top: e.currentTarget.offsetTop + e.currentTarget.offsetHeight });
    const hide = () => setTip(null);
    const h = tip && a.holdings[tip.i];
    return html`
      <section className="sec holdings">
        <header><h2>Target portfolio</h2></header>
        ${a.holdings.map((x, i) => html`<div key=${x.symbol} className=${"hold" + (tip && tip.i === i ? " on" : "")} tabIndex="0"
            aria-describedby=${tip && tip.i === i ? "hold-tip" : undefined}
            onMouseEnter=${show(i)} onMouseLeave=${hide} onFocus=${show(i)} onBlur=${hide}
            onClick=${(e) => (tip && tip.i === i ? hide() : show(i)(e))}>
          <span className="sym">${x.symbol}</span>
          <span className="bar"><i style=${{ width: (100 * x.final / scale) + "%", background: x.type === "stock" ? "var(--stock)" : "var(--etf)" }}></i>
            <u style=${{ left: `calc(${100 * x.rule / scale}% - 1px)` }}></u></span>
          <span className="num">${usd(x.value)}</span><span className="num">${pct(x.final)}</span></div>`)}
        <div className="hold"><span className="sym">Cash</span><span className="bar"><i style=${{ width: (100 * cash / scale) + "%", background: "var(--cash)" }}></i></span>
          <span className="num">${usd(cash * equity)}</span><span className="num">${pct(cash)}</span></div>
        ${h && html`<div id="hold-tip" role="tooltip" className="tipbox" style=${{ top: tip.top + 4 }}>
          <div className="tiphead"><b>${h.symbol}</b> <span className="muted">${h.name} · ${h.type === "stock" ? "stock" : "ETF"}</span></div>
          <dl>
            <dt><${Timestral} size=${11} />Timestral rule</dt><dd className="num">${pct(h.rule)}</dd>
            <dt><${Chonk} size=${11} />LLM proposed</dt><dd className="num">${h.proposed != null ? pct(h.proposed) : "n/a"}${h.confidence != null ? ` · confidence ${h.confidence.toFixed(2)}` : ""}</dd>
            <dt>Final, after guardrails</dt><dd className="num">${pct(h.final)} · ${usd(h.value)}</dd>
            ${h.risk_share != null && html`<dt>Share of the risk</dt><dd className="num">${pct(h.risk_share, 0)}</dd>`}
          </dl>
          ${h.rationale && html`<p>${h.rationale}</p>`}
        </div>`}
        <div className="legend"><span><i className="sq etf"></i>ETF</span><span><i className="sq stock"></i>Stock</span><span><i className="sq cashc"></i>Cash</span>
          <span><i className="tick"></i>Timestral rule</span><span className="muted">hover an asset for details</span></div>
      </section>`;
  }

  /* Where a tooltip opens: just under the row, measured from its section, so it is never
     clipped by a scrolling table. */
  const under = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    return r.bottom - e.currentTarget.closest("section").getBoundingClientRect().top;
  };

  /* The orders: when, what, which way, how much, what happened. Shares, prices and slippage open
     on hover, keyboard focus or a tap. */
  function Orders({ a }) {
    const [all, setAll] = useState(false);
    const [shown, setShown] = useState(false);
    if (!shown) return html`
      <section className="sec">
        <header><h2>Orders</h2></header>
        ${a.orders.length > 0 ? html`<button className="reveal" aria-expanded="false" onClick=${() => setShown(true)}>
          <span><b>Show the orders</b></span><span className="rv-end muted">${a.orders.length}</span></button>` : html`<p className="soft">None yet.</p>`}
      </section>`;
    const [tip, setTip] = useState(null); // { i, top }
    const rows = all ? a.orders : a.orders.slice(0, 10);
    const filled = a.orders.filter((o) => o.price != null);
    const show = (i) => (e) => setTip({ i, top: under(e) });
    const hide = () => setTip(null);
    const o = tip && rows[tip.i];
    const amount = (x) => (x.value != null ? usd(x.value) : x.reference_price != null ? "≈ " + usd(x.quantity * x.reference_price) : "");
    return html`
      <section className="sec tipped open">
        <header><h2>Orders</h2><button className="more" aria-expanded="true" onClick=${() => setShown(false)}>Hide</button></header>
        ${a.orders.length === 0 ? html`<p className="soft">No order sent yet.</p>` : html`
          <div className="scroll"><table>
            <thead><tr><th>Time</th><th>Asset</th><th>Side</th><th className="num">Value</th><th>Status</th></tr></thead>
            <tbody>${rows.map((x, i) => html`<tr key=${i} tabIndex="0" className=${"rowtip" + (tip && tip.i === i ? " on" : "")}
                onMouseEnter=${show(i)} onMouseLeave=${hide} onFocus=${show(i)} onBlur=${hide}
                onClick=${(e) => (tip && tip.i === i ? hide() : show(i)(e))}>
              <td className="mono">${x.at ? when(x.at) : x.run_id}</td><td><b>${x.symbol}</b></td>
              <td className=${x.side === "buy" ? "" : "warn"}>${x.side === "buy" ? "Buy" : "Sell"}</td>
              <td className="num">${amount(x)}</td>
              <td className=${x.status === "filled" ? "muted" : "warn"}>${x.status === "sent" ? "sent, awaiting the broker" : x.status}</td></tr>`)}</tbody></table></div>
          ${o && html`<div role="tooltip" className="tipbox" style=${{ top: tip.top + 4, left: "auto", right: 0 }}>
            <div className="tiphead"><b>${o.symbol}</b> <span className="muted">${o.side === "buy" ? "buy" : "sell"} · ${o.run_id}</span></div>
            <dl>
              <dt>Shares</dt><dd className="num">${o.quantity.toFixed(4)}</dd>
              ${o.reference_price != null && html`<dt>Reference price</dt><dd className="num">${usd2(o.reference_price)}</dd>`}
              ${o.limit_price != null && html`<dt>Limit price</dt><dd className="num">${usd2(o.limit_price)}</dd>`}
              <dt>Fill price</dt><dd className="num">${o.price != null ? usd2(o.price) : "not filled yet"}</dd>
              ${o.slippage_bps != null && html`<dt>Slippage</dt><dd className=${"num " + (o.slippage_bps <= 0 ? "up" : "down")}>${o.slippage_bps.toFixed(1)} bps</dd>`}
              ${o.submitted_at && html`<dt>Sent</dt><dd className="num">${when(o.submitted_at)}</dd>`}
            </dl>
          </div>`}
          ${a.orders.length > 10 && html`<button className="more" aria-expanded=${all} onClick=${() => setAll(!all)}>${all ? "Show the latest 10" : `Show all ${a.orders.length}`}</button>`}
          <div className="legend"><span><b>Slippage</b>: fill vs reference price, in bps; negative is better</span>
            <span className="muted">hover an order for details</span></div>`}
      </section>`;
  }

  function Runs({ a }) {
    const [shown, setShown] = useState(false);
    if (!shown) return html`
      <section className="sec">
        <header><h2>Every run</h2></header>
        <button className="reveal" aria-expanded="false" onClick=${() => setShown(true)}>
          <span><b>Show every run</b></span><span className="rv-end muted">${a.runs.length}</span></button>
      </section>`;
    return html`
      <section className="sec open">
        <header><h2>Every run</h2><button className="more" aria-expanded="true" onClick=${() => setShown(false)}>Hide</button><a className="aside" href=${a.records}>all records</a></header>
        <div className="scroll"><table>
          <thead><tr><th>Run</th><th>Started</th><th>Decision</th><th>Execution</th><th className="num">Account</th><th className="num">Orders</th><th className="num">Corrections</th><th></th></tr></thead>
          <tbody>${a.runs.map((r) => html`<tr key=${r.run_id}><td className="mono">${r.run_id}</td><td>${when(r.at)}</td>
            <td><${Outcome} o=${r.outcome} /></td><td className="soft">${EXECUTION[r.execution] || r.execution}</td>
            <td className="num">${usd(r.equity)}</td><td className="num">${r.orders}</td><td className="num">${r.corrections}</td>
            <td><a href=${r.record}>record</a></td></tr>`)}</tbody></table></div>
      </section>`;
  }

  function TryCta({ go }) {
    return html`<a className="cta" href="#try" onClick=${(e) => { e.preventDefault(); go("try"); }}>
      <span className="cta-mark"><${Glyph} d=${PLAY} size=${14} /></span>
      <span><b>Try it: see what an agent would do today</b></span>
      <span className="cta-go">Choose →</span></a>`;
  }

  function Live({ data, agent, setAgent, go }) {
    const a = data.agents.find((x) => x.key === agent) || data.agents[0];
    return html`
      <${React.Fragment}>
        <p className="question"><b>Should the LLM drive Timestral, or only adjust what Timestral proposes?</b> Each agent tests one answer.</p>
        <div className="agents">${data.agents.map((x) => html`<${AgentCard} key=${x.key} a=${x} selected=${x.key === a.key} onSelect=${() => setAgent(x.key)} />`)}</div>
        ${data.agents.some((x) => x.started) && html`<p className="same">Next run, both agents: <b>${when(data.agents.find((x) => x.started).next_run)}</b></p>`}
        <${TryCta} go=${go} />
        <${BothValues} agents=${data.agents} />
        <div className=${"switch c-" + a.key}>
          <span className="label">Showing</span>
          <span className="seg">${data.agents.map((x) => html`<button key=${x.key} className=${"c-" + x.key} aria-pressed=${x.key === a.key} onClick=${() => setAgent(x.key)}><${AgentIcon} k=${x.key} size=${14} />${x.title}</button>`)}</span>
        </div>
        ${!a.started ? html`<p className="soft">This agent has not run yet.</p>` : html`<div className=${"c-" + a.key}>
          <div className="pair"><${Decision} a=${a} /><${Forecast} a=${a} /></div>
          <${Holdings} a=${a} />
          <div className="pair"><${Orders} a=${a} /><${Runs} a=${a} /></div>
        </div>`}
      </${React.Fragment}>`;
  }

  // ------------------------------------------------------------------ how it works
  const LEXICON = [
    ["Paper trading", "Real prices, fictitious money: orders go to an Alpaca paper account."],
    ["Quantile (q10, q90)", "A value the outcome should stay below 10% (or 90%) of the time."],
    ["80% interval", "From q10 to q90: where Timestral expects 8 outcomes out of 10."],
    ["Spread", "The width of the 80% interval, q90 − q10: how uncertain the forecast is."],
    ["Timestral rule", "The baseline portfolio: each weight inversely proportional to the spread."],
    ["Calibration", "Whether 80% intervals really hold 80% of outcomes."],
    ["Pinball loss", "The error score of a quantile forecast: lower is better."],
    ["Covariates", "Extra series given to Timestral with the prices, here news and events."],
    ["Guardrails", "Deterministic checks that correct or reject the LLM's proposal."],
    ["Circuit breaker", "Limits that tighten as the account falls from its peak."],
    ["Drawdown", "How far the account is below its highest value."],
    ["No-trade band", "Changes smaller than this are not traded, to avoid paying for noise."],
    ["Turnover", "The share of the portfolio bought or sold in one run."],
    ["Basis point (bps)", "One hundredth of a percent."],
    ["Slippage", "Fill price against the reference price, in basis points."],
    ["Replay", "The agent run again on past sessions with only the data of each day."],
  ];

  const STEP_HELP = {
    "LLM explores": ["Reads the portfolio, prices and market context without Timestral's forecasts, and asks for them through tools.", "src/agent/decide_tools.py"],
    "Timestral tools": ["get_forecasts: quantiles for any assets and horizon.\nevaluate_portfolio: Timestral's forecast for a candidate portfolio, and whether the guardrails would accept it.\nget_baseline: the rule's weights.", "src/agent/tools.py"],
    "LLM decides": ["Returns the portfolio as strict JSON: its analysis first, then a weight, a confidence and a reason per asset.", "src/agent/decide.py"],
    "Guardrails": ["Rejects unusable answers, corrects and logs limit breaches, applies the circuit breaker; an independent final check follows.", "src/agent/guardrails.py · src/agent/safety.py"],
    "Orders": ["Marketable limit orders on the agent's own Alpaca paper account, idempotent ids, a kill switch.", "src/agent/execute.py"],
    "Timestral": ["Forecasts all 15 assets, 1 to 5 days ahead, in one batch on CPU.", "src/agent/forecast.py"],
    "Rule": ["Weights inversely proportional to the spread, within the limits.", "src/agent/baseline.py"],
    "LLM adjusts": ["Starts from the rule: free to reduce risk, at most +15 points to add, strict JSON.", "src/agent/decide.py"],
  };

  /* The two agents as two flows. Two different loops: only the tool agent decides to call
     Timestral again or stop; both agents get one retry when the guardrails reject the answer. */
  function Diagram({ repo }) {
    const tip = useTip();
    const openCode = (title) => () => window.open(`${repo}/blob/main/${STEP_HELP[title][1].split(" · ")[0]}`, "_blank", "noopener");
    const W = 136, H = 52;
    const cols = [8, 186, 364, 542, 720];
    const mid = (i) => cols[i] + W / 2;
    const box = (i, y, title, sub, opts = {}) => html`<g key=${title + i + y} className=${"step" + (tip.active === title + y ? " on" : "")}
      ...${tip.on(title + y, html`<div className="tiphead"><b>${title}</b></div><p className="plain">${STEP_HELP[title][0]}</p>
        <p className="mono muted">${STEP_HELP[title][1]}</p><p className="plain hint">Click to open the code on GitHub.</p>`)}
      onClick=${openCode(title)} onKeyDown=${key(openCode(title))}>
      <rect className=${opts.ts ? "bx ts" : "bx"} x=${cols[i]} y=${y} width=${W} height=${opts.h || H} />
      ${opts.ts && html`<${Timestral} size=${12} x=${cols[i] + 6} y=${y + 6} />`}
      ${opts.llm && html`<${Chonk} size=${12} x=${cols[i] + 6} y=${y + 6} />`}
      <text x=${mid(i)} y=${y + 21} textAnchor="middle" style=${{ fontWeight: 600 }}>${title}</text>
      ${(Array.isArray(sub) ? sub : [sub]).map((t, k) => html`<text key=${k} className=${opts.mono ? "sm mono" : "sm"} x=${mid(i)} y=${y + 38 + 13 * k} textAnchor="middle">${t}</text>`)}</g>`;
    const arrow = (x1, x2, y) => html`<g key=${"a" + x1 + y}><line x1=${x1} y1=${y} x2=${x2 - 7} y2=${y} />
      <path className="head" d=${`M${x2 - 7} ${y - 4} L${x2} ${y} L${x2 - 7} ${y + 4} z`} /></g>`;
    // A loop drawn under two boxes, from box `from` back to box `to`, with its label.
    const loop = (from, to, y, label, cls) => {
      const a = mid(from), b = mid(to), dip = y + 26;
      return html`<g key=${"l" + from + to + y} className=${"loop " + cls}>
        <path d=${`M${a} ${y} C${a} ${dip}, ${b} ${dip}, ${b} ${y + 6}`} fill="none" />
        <path className="head" d=${`M${b - 4} ${y + 8} L${b} ${y} L${b + 4} ${y + 8} z`} />
        <text className="sm" x=${(a + b) / 2} y=${dip + 12} textAnchor="middle">${label}</text></g>`;
    };
    const row = (y, k, label) => html`
      <${AgentIcon} k=${k} size=${15} x=${8} y=${y - 25} />
      <text className="row" x="28" y=${y - 12}>${label}</text>`;
    const t = 62, p = 232; // the two rows; the tool box rises 12 above its row
    return html`
      <figure style=${{ margin: 0 }}>
        <svg className="dg" viewBox="0 0 872 330" role="img"
             aria-label="Tool agent: the LLM calls Timestral's tools, gets the answers, and decides whether to call again or stop, up to 6 rounds and 12 calls; then it decides, the guardrails check, and the orders go out. Pipeline agent: Timestral forecasts all 15 assets once, a rule sets a baseline, the LLM adjusts it, then the same guardrails and orders. In both, a rejected answer gets one retry with the reason, then the positions are held.">
          <g className="c-tools">
            ${row(t - 16, "tools", "Tool agent: the LLM asks Timestral, then decides")}
            ${box(0, t, "LLM explores", "reads the market", { llm: true })}
            ${box(1, t - 12, "Timestral tools", ["get_forecasts", "evaluate_portfolio", "get_baseline"], { ts: true, h: 76, mono: true })}
            ${box(2, t, "LLM decides", "strict JSON", { llm: true })}
            ${box(3, t, "Guardrails", "correct or reject")}
            ${box(4, t, "Orders", "Alpaca paper")}
            ${arrow(cols[0] + W, cols[1], t + 26)}
            ${arrow(cols[1] + W, cols[2], t + 26)}
            ${arrow(cols[2] + W, cols[3], t + 26)}
            ${arrow(cols[3] + W, cols[4], t + 26)}
            ${loop(1, 0, t + 64, "answers · it calls again or stops (6 rounds, 12 calls at most)", "own")}
            ${loop(3, 2, t + 52, "rejected: one retry with the reason, then hold", "retry")}
          </g>
          <g className="c-pipeline">
            ${row(p, "pipeline", "Pipeline agent: Timestral first, the LLM adjusts")}
            ${box(0, p, "Timestral", "once, all 15 assets", { ts: true })}
            ${box(1, p, "Rule", "weight ∝ 1 / spread")}
            ${box(2, p, "LLM adjusts", "+15 pts at most", { llm: true })}
            ${box(3, p, "Guardrails", "correct or reject")}
            ${box(4, p, "Orders", "Alpaca paper")}
            ${[0, 1, 2, 3].map((i) => arrow(cols[i] + W, cols[i + 1], p + 26))}
            ${loop(3, 2, p + 52, "rejected: one retry with the reason, then hold", "retry")}
          </g>
        </svg>
        ${tip.box}
        <div className="legend"><span className="muted">hover a step for details, click for its code</span></div>
      </figure>`;
  }

  /* The side-by-side measures. Each name opens what it measures and how to read it, on hover,
     keyboard focus or a tap. */
  function Measures({ data }) {
    const agents = data.agents;
    const [tip, setTip] = useState(null); // { key, top }
    const show = (k) => (e) => setTip({ key: k, top: under(e) });
    const hide = () => setTip(null);
    const row = tip && data.measure_rows.find((r) => r.key === tip.key);
    return html`<${React.Fragment}><div className="scroll"><table>
      <thead><tr><th></th>${agents.map((a) => html`<th key=${a.key} className=${"num c-" + a.key}><${AgentIcon} k=${a.key} size=${14} />${a.title}</th>`)}</tr></thead>
      <tbody>${data.measure_rows.map((r) => html`<${React.Fragment} key=${r.key}>
        ${r.group && html`<tr className="grp"><td colSpan=${agents.length + 1}>${r.group}</td></tr>`}
        <tr className=${"rowtip" + (tip && tip.key === r.key ? " on" : "")} tabIndex=${r.help ? 0 : undefined}
            onMouseEnter=${r.help ? show(r.key) : undefined} onMouseLeave=${hide} onFocus=${r.help ? show(r.key) : undefined} onBlur=${hide}
            onClick=${r.help ? (e) => (tip && tip.key === r.key ? hide() : show(r.key)(e)) : undefined}>
          <td><span className=${r.help ? "term" : ""}>${r.key === "calls" ? html`<${Timestral} size=${13} />` : ""}${r.label}</span></td>
          ${agents.map((a) => html`<td key=${a.key} className="num">${a.measures[r.key] || "n/a"}</td>`)}</tr>
      </${React.Fragment}>`)}</tbody></table></div>
      ${row && html`<div role="tooltip" className="tipbox" style=${{ top: tip.top + 4, left: 0 }}>
        <div className="tiphead"><b>${row.label}</b></div><p className="plain">${row.help}</p></div>`}
      <div className="legend"><span className="muted">hover a measure for its definition</span></div>
    </${React.Fragment}>`;
  }

  function Behaviour({ b }) {
    const tip = useTip();
    const cell = (c) => c == null ? html`<span className="muted">not run</span>`
      : html`<span className=${c[0] === c[1] ? "" : "warn"}>${c[0]}/${c[1]}</span>`;
    return html`
      <div className="scroll"><table>
        <thead><tr><th>Situation</th><th className="num c-tools"><${AgentIcon} k="tools" size=${14} />Tool agent</th><th className="num c-pipeline"><${AgentIcon} k="pipeline" size=${14} />Pipeline agent</th></tr></thead>
        <tbody>${b.scenarios.map((s) => html`<tr key=${s.title}><td><span className=${"term" + (tip.active === s.title ? " on" : "")} ...${tip.on(s.title, html`<div className="tiphead"><b>${s.title}</b></div><p className="plain">${s.expectation}</p>`)}>${s.title}</span></td>
          <td className="num">${cell(s.tools)}</td><td className="num">${cell(s.pipeline)}</td></tr>`)}</tbody></table></div>
      ${tip.box}
      <${Fold} small first="Passes out of repeats. Hover or tap a situation to see what a sound decision does.">
        The tool agent was repeated more on the scenarios its prompt was tuned on, so the bases differ.
        ${Object.entries(b.stability).map(([k, s]) => ` ${k === "tools" ? "Tool" : "Pipeline"} agent: the same question ${s.calls} times, answers differ by at most ${pct(s.max_difference, 0)} of the portfolio.`).join("")}</${Fold}>`;
  }

  function Covariates({ c }) {
    const label = { prices: "Prices only", news: "News sentiment", events: "Company events", "news+events": "News and events" };
    const row = (v) => {
      const st = c.summary.find((s) => s.variant === v && s.kind === "stock");
      const ps = c.paired.find((p) => p.variant === v && p.kind === "stock");
      const pe = c.paired.find((p) => p.variant === v && p.kind === "etf");
      return html`<tr key=${v}><td>${label[v]}</td><td className="num">${spct(ps.relative, 2)}</td>
        <td className="num muted">${spct(ps.ci95[0] / st.pinball, 2)} to ${spct(ps.ci95[1] / st.pinball, 2)}</td>
        <td className="num">${spct(pe.relative, 2)}</td>
        <td>${v === "prices" ? "" : c.verdicts[v] === "no" ? "no gain" : c.verdicts[v] + " gain"}</td></tr>`;
    };
    return html`
      <p className="note"><b>No.</b> Adding news did not lower Timestral's forecast error, tested on ${c.stocks} stocks over ${c.origins} days, with ${c.etfs} ETFs as a control group.</p>
      <div className="scroll"><table>
        <thead><tr><th>Forecast built from</th><th className="num">Error vs prices only</th><th className="num">95% range</th><th className="num">ETFs (control)</th><th>Verdict</th></tr></thead>
        <tbody>${["news", "events", "news+events"].map(row)}</tbody></table></div>
      <${Fold} small first="Negative is worse.">
        The rule was set before the results: lower error on stocks, calibration kept, control unharmed. The covariates move the
        forecasts but the moves cancel out; intervals widen by 2 to 3.5% for stocks and ETFs alike, which explains the slightly
        better interval hit (stocks ${pct(c.summary.find((x) => x.variant === "prices" && x.kind === "stock").coverage_80)} with prices only). ${c.model},
        ${c.first} to ${c.last}, ${c.horizon} days ahead. Limits: one six-month period, 55 earnings releases, one
        sentiment score per article. Not tested yet: known-in-advance calendars, market variables such as volatility.</${Fold}>`;
  }

  function How({ data, go }) {
    const inTip = useTip();
    return html`
      <${React.Fragment}>
        <section className="sec tipped"><header><h2>The idea</h2></header>
          <${Fold} first=${html`Timestral estimates how risky each asset is. A Mistral LLM chooses the portfolio, and fixed rules check it before any order goes out.`}>
            Timestral, Mistral's time-series model, turned out well calibrated on risk but without a reliable sense of direction, so
            it is used as a risk engine: how wide its interval is decides how much an asset can weigh. The LLM reads those forecasts
            with the portfolio and the market context and proposes a portfolio; the guardrails correct or reject it before any order.
            Two agents test two ways of combining Timestral and the LLM, each on its own paper account.</${Fold}>
          <${Diagram} repo=${data.repo} />
        </section>
        <section className="sec tipped"><header><h2>Inputs</h2></header>
          <dl className="inputs">${data.inputs.map((r) => html`<${React.Fragment} key=${r.label}><dt>${r.label === "Forecast" ? html`<${Timestral} size=${13} />` : r.label === "Decision" ? html`<${Chonk} size=${13} />` : ""}${r.label}</dt>
            <dd>${r.items.map((i) => html`<span key=${i.text} className=${"kw" + (inTip.active === i.text ? " on" : "")}
              ...${inTip.on(i.text, html`<div className="tiphead"><b>${i.text}</b></div><p className="plain">${i.help}</p>`)}>${i.text}</span>`)}</dd></${React.Fragment}>`)}</dl>
          ${inTip.box}
          <div className="legend"><span className="muted">hover a keyword for details</span>
            ${data.covariates && html`<span>The news covariate trial: <a href="#evidence" onClick=${(e) => { e.preventDefault(); go("evidence"); }}>Evidence</a></span>`}</div>
        </section>
        <section className="sec"><header><h2><${Glyph} d=${SHIELD} size=${16} />What the LLM cannot override</h2></header>
          <ul className="rules">${data.rules.map((r) => html`<li key=${r.title}><b>${r.title}</b><span>${r.text}</span></li>`)}</ul>
        </section>
        ${data.next && data.next.length > 0 && html`<section className="sec"><header><h2><${Glyph} d=${ARROW} size=${16} />What's next</h2></header>
          <div className="next">${data.next.map((n) => html`<div key=${n.id} className="nextcard">
            <span className="kind">${n.kind}</span>
            <b>${n.text}</b></div>`)}</div>
        </section>`}
        <section className="sec"><header><h2>Records and documents</h2></header>
          <dl className="kv">
            <dt>Audit log</dt><dd><a href=${data.links.audit_log}>every run, prompt, reasoning trace and order, immutable</a></dd>
            <dt>Decisions</dt><dd><a href=${data.links.decisions}>design decisions with the options considered</a></dd>
            <dt>Code</dt><dd><a href=${data.repo}>repository</a></dd>
            ${data.links.tables.length > 0 && html`<dt>Tables (CSV)</dt><dd>${data.links.tables.map((t, i) => html`<${React.Fragment} key=${t}>${i ? " · " : ""}<a href=${t}>${t.replace("data/", "")}</a></${React.Fragment}>`)}</dd>`}
          </dl>
        </section>
      </${React.Fragment}>`;
  }

  /* Evidence: how we know the decisions are sound. Four checks, from the most controlled to the
     most real, each read with its limits. Not a race between the agents. */
  /* The replay of both agents on one chart, with the market for reference. */
  function ReplayBoth({ agents }) {
    const W = 760, H = 230, L = 52, R = 16, T = 12, B = 28;
    const base = agents[0].replay;
    const lines = [
      ...agents.map((a) => ({ key: a.key, a, vals: a.replay.curves.agent, dash: "", w: 2 })),
      { key: "spy", a: null, vals: base.curves.spy, dash: "5 3", w: 1.3 },
    ];
    const all = lines.flatMap((l) => l.vals);
    let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
    const pad = Math.max((hi - lo) * 0.12, 0.002);
    lo -= pad; hi += pad;
    const n = base.curves.agent.length;
    const x = (i) => L + (W - L - R) * (i / (n - 1));
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const sessions = ["start", ...base.sessions_list];
    return html`
      <div className="chart">
        <svg viewBox=${`0 0 ${W} ${H}`} role="img" aria-label=${lines.map((l) => `${l.a ? l.a.title : "SPY"} ${spct(l.vals[n - 1])}`).join(", ")}>
          ${ticks(lo, hi).map((t) => html`<g key=${t}><line className="grid" x1=${L} x2=${W - R} y1=${y(t)} y2=${y(t)} />
            <text x=${L - 8} y=${y(t) + 4} textAnchor="end">${spct(t, 0)}</text></g>`)}
          <line className="axis" x1=${L} x2=${W - R} y1=${y(0)} y2=${y(0)} />
          ${[[1, "start"], [Math.floor(n / 2), "middle"], [n - 1, "end"]].map(([i, anchor]) => html`<text key=${i} x=${x(i)} y=${H - 8} textAnchor=${anchor}>${sessions[i]}</text>`)}
          ${lines.slice().reverse().map((l) => html`<polyline key=${l.key} className=${l.a ? "c-" + l.a.key : ""} fill="none"
              stroke=${l.a ? "var(--c)" : "var(--etf)"} strokeWidth=${l.w} strokeDasharray=${l.dash}
              points=${l.vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")} />`)}
        </svg>
        <div className="legend">${lines.map((l) => html`<span key=${l.key} className=${l.a ? "c-" + l.a.key : ""}>
          ${l.a ? html`<${AgentIcon} k=${l.a.key} size=${13} />` : html`<svg className="key" width="22" height="8"><line x1="0" x2="22" y1="4" y2="4" stroke="var(--etf)" strokeWidth="2" strokeDasharray="5 3" /></svg>`}
          ${l.a ? l.a.title : "SPY, the market"} <b className="num">${spct(l.vals[n - 1])}</b></span>`)}</div>
      </div>`;
  }

  /* Evidence: how we know the decisions are sound. Four checks, from the most controlled to the
     most real, each read with its limits. Not a race between the agents. */
  function Evidence({ data }) {
    const withReplay = data.agents.filter((a) => a.replay);
    const [allRisk, setAllRisk] = useState(false);
    const jump = (id) => () => { const el = document.getElementById(id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); };
    const five = (a) => (a.replay ? a.replay.risk_calibration.find((r) => r.horizon === 5) : null);
    const spy = withReplay.length ? withReplay[0].replay.stats.spy : null;
    const cards = [
      data.behaviour && { id: "ev-tests", glyph: FLASK, title: "Situations built on purpose", unit: "passed, out of repeats", values: data.agents.map((a) => [a, a.measures.tests || "n/a"]) },
      withReplay.length && { id: "ev-replay", glyph: REWIND, title: `Replay, ${withReplay[0].replay.sessions} sessions`, unit: `return; the market (SPY) ${spct(spy.return)}`,
        values: withReplay.map((a) => [a, spct(a.replay.stats.agent.return)]) },
      withReplay.length && { id: "ev-risk", mark: true, title: "Risk interval hit, 5 days", unit: "target 80%",
        values: withReplay.map((a) => [a, five(a) ? pct(five(a).inside_80, 0) : "n/a"]) },
      data.covariates && { id: "ev-news", glyph: NEWS, title: "News as covariates", unit: "no lower error, not adopted", values: [[null, "No gain"]] },
    ].filter(Boolean);
    const riskRows = data.agents.flatMap((a) => [
      ...(a.replay ? a.replay.risk_calibration.map((r) => ({ a, src: "replay, portfolio", h: r.horizon, n: r.n, inside: r.inside_80, ratio: r.rms_z })) : []),
      ...((a.started ? a.calibration : []).map((r) => ({ a, src: "live, per asset", h: r.horizon, n: r.n, inside: r.inside_80, ratio: null }))),
    ]);
    const headline = riskRows.filter((r) => r.src.startsWith("replay") && r.h === 5);
    const conclusive = withReplay.some((a) => a.replay.comparisons.some((c) => c.conclusive));
    return html`
      <${React.Fragment}>
        <section className="sec"><header><h2>How do we know the decisions are sound?</h2></header>
          <p className="frame">Four checks, from a controlled test to live trading. <b>None of them can yet tell the two agents apart from chance</b>, and we don't claim otherwise.</p>
          <div className="bench">${cards.map((c) => html`<button key=${c.id} className="benchcard" onClick=${jump(c.id)}>
            <span className="bt">${c.mark ? html`<${Timestral} size=${13} />` : html`<${Glyph} d=${c.glyph} />`}${c.title}</span>
            <span className="bv">${c.values.map(([a, v]) => html`<span key=${a ? a.key : "all"} className=${a ? "c-" + a.key : ""}>
              ${a && html`<${AgentIcon} k=${a.key} size=${13} />`}<b>${v}</b></span>`)}</span>
            <span className="bu">${c.unit}</span></button>`)}</div>
        </section>
        <section className="sec tipped" id="ev-side"><header><h2>The two agents side by side</h2><span className="aside">live runs so far</span></header>
          <${Measures} data=${data} />
        </section>
        ${data.behaviour && html`<section className="sec tipped" id="ev-tests"><header><h2><${Glyph} d=${FLASK} size=${16} />Situations built on purpose</h2><span className="aside">each repeated</span></header>
          <p className="note">Made-up situations with a known right answer, each run several times.</p>
          <${Behaviour} b=${data.behaviour} /></section>`}
        ${withReplay.length > 0 && html`<section className="sec" id="ev-replay"><header><h2><${Glyph} d=${REWIND} size=${16} />Tested on the past</h2><span className="aside">${withReplay[0].replay.first} to ${withReplay[0].replay.last}, one decision a day</span></header>
          <${Fold} first=${`We replayed both agents on ${withReplay[0].replay.sessions} past sessions, while the market fell ${spct(-Math.abs(spy.return))}. That is too few sessions to judge skill.`}>
            The LLM could have remembered what happened on real dates, so assets were renamed and dates shifted; only the data of each
            day was available, fills at the next open, with costs.</${Fold}>
          <${ReplayBoth} agents=${withReplay} />
          <${Fold} small first=${conclusive ? "Some differences between the layers are significant." : "None of the gaps between the layers is larger than chance could explain."}>
            ${withReplay.map((a) => `${a.title}: ` + a.replay.comparisons.map((c) => `${c.label} ${pts(c.difference, 2)}${c.conclusive ? " (significant)" : ""}`).join("; ") + ". ").join("")}
            Reports: ${withReplay.map((a, k) => html`<${React.Fragment} key=${a.key}>${k ? ", " : ""}<a href=${a.replay.readme}>${a.title.toLowerCase()}</a></${React.Fragment}>`)}.</${Fold}>
        </section>`}
        ${riskRows.length > 0 && html`<section className="sec" id="ev-risk"><header><h2><${Timestral} />Is Timestral's risk right?</h2><span className="aside">target: 80%</span></header>
          <p className="note">An 80% interval should contain 8 outcomes out of 10.</p>
          <div className="scroll"><table><thead><tr><th>Agent</th><th>Source</th><th className="num">Horizon</th><th className="num">Forecasts</th><th className="num">Inside 80%</th><th className="num">Realised / predicted</th></tr></thead>
            <tbody>${(allRisk ? riskRows : headline).map((r, i) => html`<tr key=${i}><td className=${"c-" + r.a.key} style=${{ color: "var(--c)" }}>${r.a.title}</td><td>${r.src}</td>
              <td className="num">${r.h}d</td><td className="num">${r.n}</td><td className="num">${pct(r.inside, 0)}</td><td className="num">${r.ratio != null ? r.ratio.toFixed(2) : ""}</td></tr>`)}</tbody></table></div>
          ${riskRows.length > headline.length && html`<p className="note small"><button className="more" aria-expanded=${allRisk} onClick=${() => setAllRisk(!allRisk)}>${allRisk ? "Only the 5-day replay" : "Every horizon, and the live figures"}</button></p>`}
          <p className="note small">Ratio above 1: risk underestimated. Live figures are early and correlated.</p>
        </section>`}
        ${data.covariates && html`<section className="sec" id="ev-news"><header><h2><${Glyph} d=${NEWS} size=${16} />News as covariates: do they help Timestral?</h2><span className="aside">bonus, measured</span></header><${Covariates} c=${data.covariates} /></section>`}
      </${React.Fragment}>`;
  }

  function Lexicon() {
    return html`<section className="sec"><header><h2>Lexicon</h2></header>
      <dl className="lexicon">${LEXICON.map(([t, d]) => html`<${React.Fragment} key=${t}><dt>${t}</dt><dd>${d}</dd></${React.Fragment}>`)}</dl>
    </section>`;
  }

  // ------------------------------------------------------------------ try it
  /* Try it: the evaluator picks an agent, a stance and a starting point; the request opens as a
     pre-filled GitHub issue, which starts the run. Only these three choices are read. */
  const OPTION_NOTE = {
    balanced: "The live limits.",
    cautious: "30% cash at least, small positions.",
    bold: "Larger positions, never leverage.",
    current: "The portfolio it holds today.",
    cash: "As if it started today, all in cash.",
    equal: "The same share in every asset, 90% invested.",
    standard: "The 10 ETFs and 5 stocks traded live.",
    wide: "25 more large US stocks: more choice, the same guardrails.",
  };
  /* Is a simulation running? The page is rebuilt only once a run has finished, so it asks
     GitHub's public API itself (no token, read only): every 45 s while a run is in progress, at
     most 20 minutes, which keeps a visitor well under the 60 requests an hour allowed. */
  function SimStatus({ repo, builtAt }) {
    const [runs, setRuns] = useState([]);
    useEffect(() => {
      const api = repo.replace("https://github.com/", "https://api.github.com/repos/") + "/actions/workflows/simulate.yml/runs?per_page=5";
      let stopped = false, timer = null, polls = 0;
      const load = () => fetch(api, { headers: { Accept: "application/vnd.github+json" } })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (stopped || !d) return;
          const fresh = d.workflow_runs.filter((r) => r.status !== "completed" || Date.parse(r.updated_at) > Date.parse(builtAt));
          setRuns(fresh);
          if (fresh.some((r) => r.status !== "completed") && ++polls < 27) timer = setTimeout(load, 45000);
        })
        .catch(() => {});
      load();
      return () => { stopped = true; clearTimeout(timer); };
    }, [repo, builtAt]);
    if (!runs.length) return null;
    const ago = (iso) => { const m = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000)); return m < 1 ? "just now" : `${m} min ago`; };
    return html`<div className="simstatus">${runs.map((r) => {
      const state = r.status !== "completed" ? "running" : r.conclusion === "success" ? "done" : "failed";
      return html`<div key=${r.id} className=${"simline " + state}>
        <span className="dot"></span>
        <b>${state === "running" ? "Simulation running" : state === "done" ? "Simulation finished" : "Simulation failed"}</b>
        <span className="muted">${state === "running" ? `started ${ago(r.created_at)}` : state === "done" ? "the result appears here within a minute" : "nothing was traded"}</span>
        ${state === "done"
          ? html`<button className="more" onClick=${() => location.reload()}>Reload</button>`
          : html`<a href=${r.html_url} target="_blank" rel="noopener">${state === "running" ? "Follow on GitHub ↗" : "See why ↗"}</a>`}
      </div>`;
    })}</div>`;
  }

  /* One simulation: its options as chips, its verdict, and the portfolio it would have built.
     The latest is open; older ones are one line until clicked. */
  function SimResult({ r, s, open: first }) {
    const [open, setOpen] = useState(first);
    const label = (key) => {
      const c = s.choices.find((x) => x.key === key);
      const o = c && c.options.find((x) => x.value === r.options[key]);
      return o ? o.label.split(":")[0].replace(/ \(.*\)/, "") : r.options[key];
    };
    const top = r.portfolio.slice(0, 10);
    const rest = r.portfolio.slice(10).reduce((t, p) => t + p.weight, 0);
    const scale = Math.max(0.2, ...top.map((p) => p.weight), r.cash, rest);
    const bar = (name, w, colour, key) => html`<div key=${key || name} className="simbar">
      <span className="sym">${name}</span><span className="bar"><i style=${{ width: Math.min(100, (100 * w) / scale) + "%", background: colour }}></i></span>
      <span className="num">${pct(w)}</span></div>`;
    return html`<div className=${"simres" + (open ? " open" : "")}>
      <button className="simhead" aria-expanded=${open} onClick=${() => setOpen(!open)}>
        <span className="chips">${["agent", "stance", "start", "universe"].map((k) => html`<span key=${k} className="chip">${label(k)}</span>`)}</span>
        <span className="muted">${when(r.at)}</span>
        <${Outcome} o=${r.outcome} />
      </button>
      ${open && html`<div className="simbody">
        <div className="simbars">
          ${top.map((p) => bar(p.symbol, p.weight, p.type === "stock" ? "var(--stock)" : "var(--etf)"))}
          ${rest > 0 && bar(`+${r.portfolio.length - 10} more`, rest, "var(--rule2)", "rest")}
          ${bar("Cash", r.cash, "var(--cash)", "cash")}
        </div>
        <${Fold} small first=${`${r.portfolio.length} of ${r.assets} assets held, ${pct(1 - r.cash, 0)} invested · no order sent`}>
          ${r.analysis || "No analysis recorded."} <a href=${r.record}>Full record</a></${Fold}>
      </div>`}
    </div>`;
  }

  /* One pictogram per choice, each in a colour of Mistral's palette (red to amber). */
  const CHOICE_ICON = {
    agent: ["M8 8h8a3 3 0 0 1 3 3v5a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-5a3 3 0 0 1 3-3zM12 4v4M9.5 13h.01M14.5 13h.01", "#e10500"],
    stance: ["M4 17a8 8 0 0 1 16 0M12 17l4-5M7 17h.01M17 17h.01", "#fa500f"],
    start: ["M6 21V4M6 4h11l-2.5 4L17 12H6", "#ff8205"],
    universe: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M3 12h18M12 3c3.5 3.5 3.5 14.5 0 18M12 3c-3.5 3.5-3.5 14.5 0 18", "#e69500"],
  };

  function Try({ data }) {
    const s = data.simulations;
    const [opts, setOpts] = useState(() => Object.fromEntries(s.choices.map((c) => [c.key, c.options[0].value])));
    const body = s.choices.map((c) => `${c.key}: ${opts[c.key]}`).join("\n") + "\n\n" + s.body;
    const url = `${s.issue_url}?title=${encodeURIComponent(s.title)}&body=${encodeURIComponent(body)}`;
    const name = (c) => c.options.find((o) => o.value === opts[c.key]).label.split(/[:,]/)[0].replace(/ \(.*\)/, "").toLowerCase();
    return html`
      <${React.Fragment}>
        <section className="sec"><header><h2>Latest results</h2><span className="aside">about 6 minutes after a request</span></header>
          <${SimStatus} repo=${data.repo} builtAt=${data.generated_at} />
          ${s.recent.length === 0 ? html`<p className="soft">None yet.</p>` : s.recent.map((r, k) => html`<${SimResult} key=${r.run_id} r=${r} s=${s} open=${k === 0} />`)}
        </section>
        <section className="sec tryhero">
          <h2 className="big">Make an agent decide, now</h2>
          <p className="frame">Runs once on today's data, with the live guardrails. <b>No orders are sent.</b> The answer comes back in about 6 minutes.</p>
          <div className="choices">${s.choices.map((c, k) => html`<fieldset key=${c.key} className="choice">
            <legend><span className="choiceicon" style=${{ color: CHOICE_ICON[c.key][1] }}><${Glyph} d=${CHOICE_ICON[c.key][0]} size=${16} /></span>${c.label}</legend>
            ${c.options.map((o) => html`<label key=${o.value} className=${"opt" + (opts[c.key] === o.value ? " on" : "")}>
              <input type="radio" name=${c.key} value=${o.value} checked=${opts[c.key] === o.value} onChange=${() => setOpts({ ...opts, [c.key]: o.value })} />
              <span><b>${o.label.split(":")[0]}</b>${OPTION_NOTE[o.value] && html`<small>${OPTION_NOTE[o.value]}</small>`}</span></label>`)}
          </fieldset>`)}</div>
          <div className="launch">
            <p>You ask the <b>${name(s.choices[0])}</b>, <b>${name(s.choices[1])}</b>, starting from <b>${name(s.choices[2])}</b>, on <b>${name(s.choices[3])}</b>.</p>
            <a className="btn big" href=${url} target="_blank" rel="noopener"><${GitHubMark} />Request this run on GitHub ↗</a>
          </div>
        </section>

      </${React.Fragment}>`;
  }

  // ------------------------------------------------------------------ app
  /* How fresh the data is: green when the page was rebuilt in the last two hours (it is rebuilt
     after every run and every hourly account sample), grey otherwise, with the age spelled out. */
  function Fresh({ at }) {
    const minutes = Math.max(0, Math.round((Date.now() - Date.parse(at)) / 60000));
    const ago = minutes < 60 ? `${minutes} min ago` : minutes < 2880 ? `${Math.round(minutes / 60)} h ago` : `${Math.round(minutes / 1440)} days ago`;
    return html`<p className="fresh" title="Rebuilt from the audit log after every run and every hourly account sample">
      <span className=${"dot " + (minutes <= 120 ? "live" : "stale")}></span>Updated ${when(at)} <span className="muted">· ${ago}</span></p>`;
  }

  const readTab = () => { const t = (location.hash || "").slice(1); return ROUTES.includes(t) ? t : "live"; };

  function App() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [tab, setTab] = useState(readTab);
    const [agent, setAgent] = useState(null);
    const [compact, setCompact] = useState(false);
    const [sections, setSections] = useState([]);
    const [current, setCurrent] = useState(0);
    useEffect(() => {
      const t = setTimeout(() => {
        const found = [...document.querySelectorAll("#panel section.sec")].map((el, n) => {
          const h = el.querySelector("h2");
          if (!el.id) el.id = "sec-" + n;
          return h ? { id: el.id, label: h.textContent.trim() } : null;
        }).filter(Boolean);
        setSections(found);
      }, 50);
      return () => clearTimeout(t);
    }, [tab, data, agent]);
    useEffect(() => {
      const onScroll = () => {
        const els = sections.map((x) => document.getElementById(x.id)).filter(Boolean);
        let k = 0;
        els.forEach((el, n) => { if (el.getBoundingClientRect().top < 140) k = n; });
        setCurrent(k);
      };
      onScroll();
      window.addEventListener("scroll", onScroll, { passive: true });
      return () => window.removeEventListener("scroll", onScroll);
    }, [sections]);
    useEffect(() => {
      const onScroll = () => {
        const top = document.querySelector(".top");
        setCompact(top ? window.scrollY > top.offsetHeight : false);
      };
      window.addEventListener("scroll", onScroll, { passive: true });
      return () => window.removeEventListener("scroll", onScroll);
    }, []);
    useEffect(() => {
      fetch("data.json", { cache: "no-store" })
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then((d) => { setData(d); const first = d.agents.find((a) => a.started) || d.agents[0]; setAgent(first && first.key); })
        .catch((e) => setError(String(e)));
      const onHash = () => setTab(readTab());
      window.addEventListener("hashchange", onHash);
      return () => window.removeEventListener("hashchange", onHash);
    }, []);
    const go = (t) => { history.replaceState(null, "", "#" + t); setTab(t); window.scrollTo(0, 0); };
    const onArrows = (e) => {
      const i = TABS.findIndex((x) => x[0] === tab);
      const j = ["ArrowRight", "ArrowDown"].includes(e.key) ? i + 1 : ["ArrowLeft", "ArrowUp"].includes(e.key) ? i - 1 : null;
      if (j === null) return;
      const next = TABS[(j + TABS.length) % TABS.length][0];
      go(next);
      const el = document.getElementById("tab-" + next);
      if (el) el.focus();
    };
    if (error) return html`<p className="boot">The records could not be loaded (${error}). Try the <a href="classic.html">plain page</a>.</p>`;
    if (!data) return html`<p className="boot">Loading the agent's records…</p>`;
    return html`
      <${React.Fragment}>
        <div className=${"minibar" + (compact ? " on" : "")} aria-hidden=${!compact}><div className="wrap">
          <span className="mb-title"><${Timestral} size=${18} />Timestral Trading Agent<span className="badge">Paper trading</span></span>
          <span className="actions">
            <a className="act" tabIndex=${compact ? 0 : -1} href=${data.links.audit_log}><${Glyph} d=${DOC} />Audit log</a>
            <a className="act" tabIndex=${compact ? 0 : -1} href="#lexicon" onClick=${(e) => { e.preventDefault(); go("lexicon"); }}><${Glyph} d=${BOOK} />Lexicon</a>
            <a className="act" tabIndex=${compact ? 0 : -1} href=${data.repo}><${Glyph} d=${CODE} />Code</a>
          </span></div></div>
        <div className="top"><div className="wrap">
          <div className="brand">
            <div className="title"><h1><${Timestral} size=${22} />Timestral Trading Agent</h1><span className="badge">Paper trading</span></div>
            <div className="actions">
              <a className="act" href=${data.links.audit_log}><${Glyph} d=${DOC} />Audit log</a>
              <a className="act" href="#lexicon" onClick=${(e) => { e.preventDefault(); go("lexicon"); }}><${Glyph} d=${BOOK} />Lexicon</a>
              <a className="act" href=${data.repo}><${Glyph} d=${CODE} />Code</a>
            </div>
          </div>
          <p className="lede">Two autonomous agents trade US ETFs and stocks on paper accounts. Timestral estimates the risk, a Mistral LLM chooses
            the portfolio, and fixed rules check every decision before it reaches the broker.</p>
          <${Fresh} at=${data.generated_at} />
        </div></div>
        <main className="wrap layout">
          <aside className="side">
            <nav role="tablist" aria-label="Views" aria-orientation="vertical" onKeyDown=${onArrows}>
              ${TABS.map(([k, t, d], i) => html`<${React.Fragment} key=${k}>
                <button id=${"tab-" + k} role="tab" aria-selected=${tab === k} aria-controls="panel" title=${d}
                  tabIndex=${tab === k ? 0 : -1} onClick=${() => go(k)}><span className="k">${i + 1}</span>${t}</button>
                ${tab === k && sections.length > 1 && html`<ul className="toc">${sections.map((x, n) => html`<li key=${x.id}>
                  <a href=${"#" + x.id} className=${n === current ? "on" : ""} onClick=${(e) => { e.preventDefault(); const el = document.getElementById(x.id); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }}>${x.label}</a></li>`)}</ul>`}
              </${React.Fragment}>`)}
            </nav>
          </aside>
          <div className="content">
          <div id="panel" key=${tab} className="view" role="tabpanel" aria-labelledby=${"tab-" + tab}>
          ${tab === "live" && html`<${Live} data=${data} agent=${agent} setAgent=${setAgent} go=${go} />`}
          ${tab === "how" && html`<${How} data=${data} go=${go} />`}
          ${tab === "evidence" && html`<${Evidence} data=${data} />`}
          ${tab === "try" && html`<${Try} data=${data} go=${go} />`}
          ${tab === "lexicon" && html`<${Lexicon} />`}
          </div>
          </div>
        </main>
      </${React.Fragment}>`;
  }

  ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
})();
