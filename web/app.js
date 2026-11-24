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
  const SERIES = {
    agent: ["Agent", "var(--c)", ""],
    baseline: ["Timestral rule alone", "var(--ink2)", "4 3"],
    equal_weight: ["Equal weight", "var(--ink3)", "1 3"],
    spy: ["SPY (market)", "var(--etf)", "6 3"],
  };
  const TABS = [
    ["live", "Live", "What the two agents hold, what they decided and why, and Timestral's raw forecast behind it."],
    ["how", "How it works", "The design, the guardrails, and the evidence that the decisions are sound."],
    ["try", "Try it", "Ask an agent to decide now on live data. No order is ever sent."],
  ];

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
  const SHIELD = "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4";
  const FLASK = "M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3M7.5 15h9";
  const REWIND = "M3 12a9 9 0 1 0 2.6-6.4L3 8M3 3v5h5M12 7v5l3 2";
  const NEWS = "M4 5h12v14H6a2 2 0 0 1-2-2zM16 9h4v8a2 2 0 0 1-4 0M7 9h6M7 13h6";

  /* A long paragraph shows its first sentence; the reader opens the rest if they want it. */
  function Fold({ first, children, small }) {
    const [open, setOpen] = useState(false);
    return html`<p className=${small ? "note small" : "note"}>${first}${open ? html` ${children}` : " "}<button className="more" aria-expanded=${open}
      onClick=${() => setOpen(!open)}>${open ? "Less" : "More"}</button></p>`;
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
        <header><h2>Account value</h2><span className="aside">both paper accounts, read at every run and every hour of the US session</span></header>
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
          <div className="readout">${live.map((a, k) => { const p = nearest(pts[k], t); return html`<span key=${a.key} className=${"c-" + a.key}>
            <${AgentIcon} k=${a.key} size=${14} />${a.title} ${usd2(p.equity)} <span className="muted">${when(p.at)}</span></span>`; })}</div>
        </div>
      </section>`;
  }

  /* One asset: Timestral's 80% interval (q10 to q90) and median for each horizon, drawn as a
     fan from today's price (0%) to 5 days ahead. All fans share one scale, so widths compare. */
  function Fan({ a, bound }) {
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
    const [open, setOpen] = useState(false);
    const toggle = () => setOpen(!open);
    return html`
      <div className=${"fan" + (open ? " open" : "")} role="button" tabIndex="0" aria-expanded=${open}
           title="Show the numbers for each horizon" onClick=${toggle} onKeyDown=${key(toggle)}>
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
        ${open && html`<table className="mini"><thead><tr><th>Days</th><th className="num">q10</th><th className="num">Median</th><th className="num">q90</th><th className="num">Spread</th></tr></thead>
          <tbody>${hs.map((h) => html`<tr key=${h.h}><td>${h.h}</td><td className="num">${spct(h.q10)}</td><td className="num">${spct(h.median)}</td>
            <td className="num">${spct(h.q90)}</td><td className="num">${pct(h.spread)}</td></tr>`)}</tbody></table>`}
      </div>`;
  }

  function ReplayChart({ replay }) {
    const W = 760, H = 220, L = 52, R = 16, T = 12, B = 28;
    const keys = Object.keys(SERIES);
    const all = keys.flatMap((k) => replay.curves[k]);
    let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
    const pad = Math.max((hi - lo) * 0.12, 0.002);
    lo -= pad; hi += pad;
    const n = replay.curves.agent.length;
    const x = (i) => L + (W - L - R) * (i / (n - 1));
    const y = (v) => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
    const sessions = ["start", ...replay.sessions_list];
    return html`
      <div className="chart">
        <svg viewBox=${`0 0 ${W} ${H}`} role="img" aria-label="Cumulative return of the agent and its references over the replayed sessions">
          ${ticks(lo, hi).map((t) => html`<g key=${t}><line className="grid" x1=${L} x2=${W - R} y1=${y(t)} y2=${y(t)} />
            <text x=${L - 8} y=${y(t) + 4} textAnchor="end">${spct(t, 0)}</text></g>`)}
          <line className="axis" x1=${L} x2=${W - R} y1=${y(0)} y2=${y(0)} />
          ${[[1, "start"], [Math.floor(n / 2), "middle"], [n - 1, "end"]].map(([i, anchor]) => html`<text key=${i} x=${x(i)} y=${H - 8} textAnchor=${anchor}>${sessions[i]}</text>`)}
          ${keys.slice().reverse().map((k) => html`<polyline key=${k} fill="none" stroke=${SERIES[k][1]}
              strokeWidth=${k === "agent" ? 2 : 1.3} strokeDasharray=${SERIES[k][2]}
              points=${replay.curves[k].map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ")} />`)}
        </svg>
        <div className="legend">${keys.map((k) => html`<span key=${k}>
          <svg className="key" width="22" height="8"><line x1="0" x2="22" y1="4" y2="4" stroke=${SERIES[k][1]} strokeWidth="2" strokeDasharray=${SERIES[k][2]} /></svg>
          ${SERIES[k][0]} <span className="num">${spct(replay.curves[k][n - 1])}</span></span>`)}</div>
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
          <dt>Value read</dt><dd>${when(a.now.at)}${a.now.source === "sample" ? "" : " (at a run)"}</dd>
          <dt>Invested</dt><dd>${pct(1 - a.decision.cash, 0)}</dd>
          <dt>Last run</dt><dd>${when(a.decision.at)} · <${Outcome} o=${a.decision.outcome} /></dd>
          <dt>Next run</dt><dd>${when(a.next_run)}</dd>
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
    return html`
      <section className="sec">
        <header><h2>Last decision</h2><span className="aside">${d.run_id} · ${when(d.at)}</span></header>
        <p className="facts">
          <span><${Outcome} o=${d.outcome} /></span>
          <span>${EXECUTION[d.execution] || d.execution}</span>
          <span>model <b>${d.model}</b></span><span>prompt <b>${d.prompt}</b></span>
          ${d.portfolio_vol && d.portfolio_vol.final != null && html`<span>forecast risk, ${d.portfolio_vol.horizon} days: <b>${pct(d.portfolio_vol.final)}</b> (rule alone ${pct(d.portfolio_vol.rule)})</span>`}
          <a href=${d.record}>full record</a>
        </p>
        <div className="decision">
          <div>
            <h3 style=${{ marginTop: 0 }}>What the LLM wrote</h3>
            <p className="reading">${short}</p>
            ${text.length > 520 && html`<button className="more" onClick=${() => setFull(!full)}>${full ? "Show less" : "Read all"}</button>`}
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
    const [view, setView] = useState("fans");
    const bound = useMemo(() => {
      const m = Math.max(...f.assets.flatMap((x) => x.horizons.map((h) => Math.max(Math.abs(h.q10), Math.abs(h.q90)))));
      return Math.ceil(m * 20) / 20;
    }, [f]);
    const hs = f.assets[0].horizons.map((h) => h.h);
    const maxH = hs[hs.length - 1];
    const tq = f.tool_quantiles;
    const qcols = tq ? Object.keys(Object.values(tq.assets)[0]) : [];
    return html`
      <section className="sec">
        <header><h2><${Timestral} />Timestral's forecast behind it</h2>
          <span className="views" role="group" aria-label="View">
            ${[["fans", "Chart"], ["table", "Numbers"], ["raw", "Raw output"]].map(([k, t]) =>
              html`<button key=${k} aria-pressed=${view === k} onClick=${() => setView(k)}>${t}</button>`)}
          </span></header>
        <${Fold} first=${html`The wider the <b>spread</b> (<span className="formula">q90 − q10</span>), the riskier the asset and the smaller its weight.`}>
          ${f.model}, on ${f.context_days} days of closes up to ${f.last_close}, gives for each asset a distribution of the cumulative
          return 1 to ${maxH} days ahead. The rule weights each asset by <span className="formula">1 / spread</span>; the LLM then adjusts.
          The median is shown for reference: Timestral has no measured directional skill here.</${Fold}>
        ${view === "fans" && html`<${React.Fragment}>
          <div className="fans">${f.assets.map((x) => html`<${Fan} key=${x.symbol} a=${x} bound=${bound} />`)}</div>
          <div className="legend"><span><i className="sq etf"></i>ETF</span><span><i className="sq stock"></i>Stock</span>
            <span>Band: 80% interval, today to ${maxH} days · line: median · same scale (±${pct(bound, 0)}) for every asset · riskiest first · click an asset for its numbers</span></div>
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

  function Holdings({ a }) {
    const scale = Math.max(0.25, ...a.holdings.map((h) => Math.max(h.final, h.rule)));
    const cash = a.decision.cash;
    const equity = a.runs[0].equity;
    return html`
      <section className="sec">
        <header><h2>Target portfolio</h2><span className="aside">after the guardrails, at the last run</span></header>
        ${a.holdings.map((h) => html`<div key=${h.symbol} className="hold" title=${h.rationale}>
          <span className="sym">${h.symbol}</span>
          <span className="bar"><i style=${{ width: (100 * h.final / scale) + "%", background: h.type === "stock" ? "var(--stock)" : "var(--etf)" }}></i>
            <u style=${{ left: `calc(${100 * h.rule / scale}% - 1px)` }}></u></span>
          <span className="num">${usd(h.value)}</span><span className="num">${pct(h.final)}</span>
          <span className="num cf muted">${h.confidence != null ? h.confidence.toFixed(2) : ""}</span></div>`)}
        <div className="hold"><span className="sym">Cash</span><span className="bar"><i style=${{ width: (100 * cash / scale) + "%", background: "var(--cash)" }}></i></span>
          <span className="num">${usd(cash * equity)}</span><span className="num">${pct(cash)}</span><span className="cf"></span></div>
        <div className="legend"><span><i className="sq etf"></i>ETF</span><span><i className="sq stock"></i>Stock</span><span><i className="sq cashc"></i>Cash</span>
          <span><i className="tick"></i>Weight from the Timestral rule</span><span>last column: the LLM's stated confidence · hover a row for its reason</span></div>
      </section>`;
  }

  function Orders({ a }) {
    const [all, setAll] = useState(false);
    const rows = all ? a.orders : a.orders.slice(0, 10);
    const filled = a.orders.filter((o) => o.price != null);
    return html`
      <section className="sec">
        <header><h2>Orders</h2><span className="aside">${a.orders.length} orders · ${filled.length} filled</span></header>
        ${a.orders.length === 0 ? html`<p className="soft">No order sent yet.</p>` : html`
          <div className="scroll"><table>
            <thead><tr><th>Time</th><th>Asset</th><th>Side</th><th className="num">Shares</th><th className="num">Price</th><th className="num">Value</th><th className="num">Slippage</th><th>Status</th></tr></thead>
            <tbody>${rows.map((o, i) => html`<tr key=${i}><td className="mono">${o.at ? when(o.at) : o.run_id}</td><td><b>${o.symbol}</b></td>
              <td className=${o.side === "buy" ? "" : "warn"}>${o.side === "buy" ? "Buy" : "Sell"}</td>
              <td className="num">${o.quantity.toFixed(2)}</td><td className="num">${o.price != null ? usd2(o.price) : ""}</td>
              <td className="num">${o.value != null ? usd(o.value) : ""}</td>
              <td className="num">${o.slippage_bps != null ? o.slippage_bps.toFixed(1) + " bps" : ""}</td>
              <td className=${o.status === "filled" ? "muted" : "warn"}>${o.status === "sent" ? "sent, awaiting the broker" : o.status}</td></tr>`)}</tbody></table></div>
          ${a.orders.length > 10 && html`<button className="more" onClick=${() => setAll(!all)}>${all ? "Show the latest 10" : `Show all ${a.orders.length}`}</button>`}
          <p className="muted" style=${{ fontSize: 12 }}>Slippage against the reference price, in basis points: negative is better for us.</p>`}
      </section>`;
  }

  function Runs({ a }) {
    return html`
      <section className="sec">
        <header><h2>Every run</h2><a className="aside" href=${a.records}>all records</a></header>
        <div className="scroll"><table>
          <thead><tr><th>Run</th><th>Started</th><th>Decision</th><th>Execution</th><th className="num">Account</th><th className="num">Orders</th><th className="num">Corrections</th><th></th></tr></thead>
          <tbody>${a.runs.map((r) => html`<tr key=${r.run_id}><td className="mono">${r.run_id}</td><td>${when(r.at)}</td>
            <td><${Outcome} o=${r.outcome} /></td><td className="soft">${EXECUTION[r.execution] || r.execution}</td>
            <td className="num">${usd(r.equity)}</td><td className="num">${r.orders}</td><td className="num">${r.corrections}</td>
            <td><a href=${r.record}>record</a></td></tr>`)}</tbody></table></div>
      </section>`;
  }

  function Live({ data, agent, setAgent }) {
    const a = data.agents.find((x) => x.key === agent) || data.agents[0];
    return html`
      <${React.Fragment}>
        <p className="question">Two agents, one question: <b>should the LLM drive Timestral, or adjust what Timestral proposes?</b></p>
        <div className="agents">${data.agents.map((x) => html`<${AgentCard} key=${x.key} a=${x} selected=${x.key === a.key} onSelect=${() => setAgent(x.key)} />`)}</div>
        <p className="same">Same for both: <span>data</span><span>15 assets</span><span>guardrails</span><span>order code</span><span>own paper account</span></p>
        <${BothValues} agents=${data.agents} />
        <div className=${"switch c-" + a.key}>
          <span className="label">Showing</span>
          <span className="seg">${data.agents.map((x) => html`<button key=${x.key} className=${"c-" + x.key} aria-pressed=${x.key === a.key} onClick=${() => setAgent(x.key)}><${AgentIcon} k=${x.key} size=${14} />${x.title}</button>`)}</span>
        </div>
        ${!a.started ? html`<p className="soft">This agent has not run yet.</p>` : html`<div className=${"c-" + a.key}>
          <${Decision} a=${a} />
          <${Forecast} a=${a} />
          <${Holdings} a=${a} />
          <${Orders} a=${a} />
          <${Runs} a=${a} />
        </div>`}
      </${React.Fragment}>`;
  }

  // ------------------------------------------------------------------ how it works
  /* The two agents as two flows. Two different loops: only the tool agent decides to call
     Timestral again or stop; both agents get one retry when the guardrails reject the answer. */
  function Diagram() {
    const W = 136, H = 52;
    const cols = [8, 186, 364, 542, 720];
    const mid = (i) => cols[i] + W / 2;
    const box = (i, y, title, sub, opts = {}) => html`<g key=${title + i + y}>
      <rect className=${opts.ts ? "ts" : ""} x=${cols[i]} y=${y} width=${W} height=${opts.h || H} />
      ${opts.ts && html`<${Timestral} size=${12} x=${cols[i] + 6} y=${y + 6} />`}
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
            ${box(0, t, "LLM explores", "reads the market")}
            ${box(1, t - 12, "Timestral tools", ["get_forecasts", "evaluate_portfolio", "get_baseline"], { ts: true, h: 76, mono: true })}
            ${box(2, t, "LLM decides", "strict JSON")}
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
            ${box(2, p, "LLM adjusts", "+15 pts at most")}
            ${box(3, p, "Guardrails", "correct or reject")}
            ${box(4, p, "Orders", "Alpaca paper")}
            ${[0, 1, 2, 3].map((i) => arrow(cols[i] + W, cols[i + 1], p + 26))}
            ${loop(3, 2, p + 52, "rejected: one retry with the reason, then hold", "retry")}
          </g>
        </svg>
      </figure>`;
  }

  function Measures({ data }) {
    const agents = data.agents;
    return html`<div className="scroll"><table>
      <thead><tr><th></th>${agents.map((a) => html`<th key=${a.key} className=${"num c-" + a.key}><${AgentIcon} k=${a.key} size=${14} />${a.title}</th>`)}</tr></thead>
      <tbody>${data.measure_rows.map((r) => html`<${React.Fragment} key=${r.key}>
        ${r.group && html`<tr className="grp"><td colSpan=${agents.length + 1}>${r.group}</td></tr>`}
        <tr><td>${r.key === "calls" ? html`<${Timestral} size=${13} />` : ""}${r.label}</td>${agents.map((a) => html`<td key=${a.key} className="num">${a.measures[r.key] || "n/a"}</td>`)}</tr>
      </${React.Fragment}>`)}</tbody></table></div>`;
  }

  function Behaviour({ b }) {
    const cell = (c) => c == null ? html`<span className="muted">not run</span>`
      : html`<span className=${c[0] === c[1] ? "" : "warn"}>${c[0]}/${c[1]}</span>`;
    return html`
      <div className="scroll"><table>
        <thead><tr><th>Situation</th><th className="num c-tools"><${AgentIcon} k="tools" size=${14} />Tool agent</th><th className="num c-pipeline"><${AgentIcon} k="pipeline" size=${14} />Pipeline agent</th></tr></thead>
        <tbody>${b.scenarios.map((s) => html`<tr key=${s.title}><td title=${s.expectation}>${s.title}</td>
          <td className="num">${cell(s.tools)}</td><td className="num">${cell(s.pipeline)}</td></tr>`)}</tbody></table></div>
      <${Fold} small first="Passes out of repeats; hover a situation for what a sound decision does.">
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
      <p className="note"><b>No.</b> News did not lower Timestral's error: ${c.origins} days, ${c.stocks} stocks, ${c.etfs} ETFs as control.</p>
      <div className="scroll"><table>
        <thead><tr><th>Forecast built from</th><th className="num">Error vs prices only</th><th className="num">95% range</th><th className="num">ETFs (control)</th><th>Verdict</th></tr></thead>
        <tbody>${["news", "events", "news+events"].map(row)}</tbody></table></div>
      <${Fold} small first="Negative is worse.">
        The rule was set before the results: lower error on stocks, calibration kept, control unharmed. The covariates move the
        forecasts but the moves cancel out; intervals widen by 2 to 3.5% for stocks and ETFs alike, which explains the slightly
        better interval hit (stocks ${pct(c.summary.find((x) => x.variant === "prices" && x.kind === "stock").coverage_80)} with prices only).
        ${c.model}, ${c.first} to ${c.last}, ${c.horizon} days ahead. Limits: one six-month period, 55 earnings releases, one
        sentiment score per article. Not tested yet: known-in-advance calendars, market variables such as volatility.</${Fold}>`;
  }

  function How({ data }) {
    const withReplay = data.agents.filter((a) => a.replay);
    const live = data.agents.filter((a) => a.started && (a.calibration.length || a.risk_calibration.length));
    return html`
      <${React.Fragment}>
        <section className="sec"><header><h2>The idea</h2></header>
          <${Fold} first=${html`Timestral forecasts the <b>risk</b>, a Mistral LLM <b>decides</b>, deterministic guardrails <b>bound</b> the result.`}>
            Timestral, Mistral's time-series model, turned out well calibrated on risk but without a reliable sense of direction, so
            it is used as a risk engine: how wide its interval is decides how much an asset can weigh. The LLM reads those forecasts
            with the portfolio and the market context and proposes a portfolio; the guardrails correct or reject it before any order.
            Two agents test two ways of combining Timestral and the LLM, each on its own paper account.</${Fold}>
          <${Diagram} />
        </section>
        <section className="sec"><header><h2>Inputs</h2></header>
          <dl className="inputs">${data.inputs.map((r) => html`<${React.Fragment} key=${r.label}><dt>${r.label === "Forecast" ? html`<${Timestral} size=${13} />` : ""}${r.label}</dt>
            <dd>${r.items.map((i) => html`<span key=${i}>${i}</span>`)}</dd></${React.Fragment}>`)}</dl>
        </section>
        <section className="sec"><header><h2><${Glyph} d=${SHIELD} size=${16} />What the LLM cannot override</h2><span className="aside">deterministic code, outside the LLM</span></header>
          <ul className="rules">${data.rules.map((r) => html`<li key=${r.title}><b>${r.title}</b><span>${r.text}</span></li>`)}</ul>
        </section>
        <section className="sec"><header><h2>The two agents side by side</h2><span className="aside">live runs so far, behaviour tests, 63-session replay</span></header>
          <${Measures} data=${data} />
        </section>
        ${data.behaviour && html`<section className="sec"><header><h2><${Glyph} d=${FLASK} size=${16} />Behaviour tests</h2><span className="aside">situations built on purpose, each repeated</span></header>
          <${Behaviour} b=${data.behaviour} /></section>`}
        ${withReplay.length > 0 && html`<section className="sec"><header><h2><${Glyph} d=${REWIND} size=${16} />Tested on the past</h2><span className="aside">one decision a day, point-in-time data, renamed assets and shifted dates</span></header>
          <${Fold} first=${html`A check of behaviour on ${withReplay[0].replay.sessions} past sessions, not a measure of skill.`}>
            The LLM could have remembered what happened on real dates, so assets were renamed and dates shifted; fills at the next
            open, with costs. The period is a falling market.</${Fold}>
          ${withReplay.map((a) => html`<div key=${a.key} className=${"c-" + a.key}>
            <h3 style=${{ color: "var(--c)" }}><${AgentIcon} k=${a.key} />${a.title} · ${a.replay.first} to ${a.replay.last}</h3>
            <${ReplayChart} replay=${a.replay} />
            <${Fold} small first=${a.replay.comparisons.some((c) => c.conclusive)
                ? "Some differences between the layers are significant: see below."
                : `No layer made a difference that ${a.replay.sessions} sessions can distinguish from chance.`}>
              ${a.replay.comparisons.map((c) => `${c.label}: ${pts(c.difference, 2)}${c.conclusive ? " (significant)" : ""}.`).join(" ")}
              <a href=${a.replay.readme}>Full replay report</a>.</${Fold}></div>`)}
        </section>`}
        ${(live.length > 0 || withReplay.length > 0) && html`<section className="sec"><header><h2><${Timestral} />Is Timestral's risk right?</h2><span className="aside">target: 80% of outcomes inside the 80% interval</span></header>
          <div className="scroll"><table><thead><tr><th>Agent</th><th>Source</th><th className="num">Horizon</th><th className="num">Forecasts</th><th className="num">Inside 80%</th><th className="num">Realised / predicted</th></tr></thead>
            <tbody>${data.agents.flatMap((a) => [
              ...(a.replay ? a.replay.risk_calibration.map((r) => ({ a, src: "replay, portfolio", h: r.horizon, n: r.n, inside: r.inside_80, ratio: r.rms_z })) : []),
              ...((a.started ? a.calibration : []).map((r) => ({ a, src: "live, per asset", h: r.horizon, n: r.n, inside: r.inside_80, ratio: null }))),
            ]).map((r, i) => html`<tr key=${i}><td className=${"c-" + r.a.key} style=${{ color: "var(--c)" }}>${r.a.title}</td><td>${r.src}</td>
              <td className="num">${r.h}d</td><td className="num">${r.n}</td><td className="num">${pct(r.inside, 0)}</td><td className="num">${r.ratio != null ? r.ratio.toFixed(2) : ""}</td></tr>`)}</tbody></table></div>
          <p className="note small">Ratio above 1: risk underestimated. Live figures are early and correlated.</p>
        </section>`}
        ${data.covariates && html`<section className="sec"><header><h2><${Glyph} d=${NEWS} size=${16} />Do news help the forecast?</h2></header><${Covariates} c=${data.covariates} /></section>`}
        ${data.next && data.next.length > 0 && html`<section className="sec"><header><h2>What's next</h2><span className="aside">kept in step with docs/roadmap.md</span></header>
          <div className="next">${[["running", "Running"], ["next", "Next"], ["later", "Later, deliberately"]].map(([k, t]) => html`<div key=${k}>
            <h3>${t}</h3>
            <ul>${data.next.filter((n) => n.status === k).map((n) => html`<li key=${n.id + n.title}><b><span className="mono muted">${n.id}</span> ${n.title}</b><span>${n.text}</span></li>`)}</ul></div>`)}</div>
        </section>`}
        <section className="sec"><header><h2>Records and documents</h2></header>
          <dl className="kv">
            <dt>Audit log</dt><dd><a href=${data.links.audit_log}>every run, prompt, reasoning trace and order, immutable</a></dd>
            <dt>Decisions</dt><dd><a href=${data.links.decisions}>design decisions with the options considered</a></dd>
            <dt>Code</dt><dd><a href=${data.repo}>repository</a></dd>
            ${data.links.tables.length > 0 && html`<dt>Tables (CSV)</dt><dd>${data.links.tables.map((t, i) => html`<${React.Fragment} key=${t}>${i ? " · " : ""}<a href=${t}>${t.replace("data/", "")}</a></${React.Fragment}>`)}</dd>`}
            <dt>Plain page</dt><dd><a href=${data.links.classic}>the same records without JavaScript</a></dd>
          </dl>
        </section>
      </${React.Fragment}>`;
  }

  // ------------------------------------------------------------------ try it
  function Try({ data }) {
    const s = data.simulations;
    const [opts, setOpts] = useState(() => Object.fromEntries(s.choices.map((c) => [c.key, c.options[0].value])));
    const body = s.choices.map((c) => `${c.key}: ${opts[c.key]}`).join("\n") + "\n\n" + s.body;
    const url = `${s.issue_url}?title=${encodeURIComponent(s.title)}&body=${encodeURIComponent(body)}`;
    return html`
      <${React.Fragment}>
        <section className="sec"><header><h2>Make an agent decide now</h2></header>
          <p className="callout"><b>No order is ever sent.</b> The button opens a pre-filled GitHub issue; the agent's decision is posted back in about 6 minutes.</p>
          <div className="form">
            ${s.choices.map((c) => html`<label key=${c.key}>${c.label}
              <select value=${opts[c.key]} onChange=${(e) => setOpts({ ...opts, [c.key]: e.target.value })}>
                ${c.options.map((o) => html`<option key=${o.value} value=${o.value}>${o.label}</option>`)}</select></label>`)}
            <a className="btn" href=${url} target="_blank" rel="noopener">Request a simulation</a>
          </div>
          <p className="muted" style=${{ fontSize: 12 }}>Cautious keeps 30% in cash and small positions; bold allows larger positions, never leverage.
            At most one simulation a minute and 20 a day. Only these three choices are read from the issue.</p>
        </section>
        <section className="sec"><header><h2>Recent simulations</h2></header>
          ${s.recent.length === 0 ? html`<p className="soft">None yet.</p>` : s.recent.map((r) => html`<details key=${r.run_id} className="run">
            <summary><span className="mono">${r.run_id}</span> · ${r.requested_by} · <${Outcome} o=${r.outcome} /> · ${r.orders} order(s) planned, none sent</summary>
            <p>${r.analysis || "No parsable analysis."}</p><p><a href=${r.record}>full record</a></p></details>`)}
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

  const readTab = () => { const t = (location.hash || "").slice(1); return TABS.some((x) => x[0] === t) ? t : "live"; };

  function App() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [tab, setTab] = useState(readTab);
    const [agent, setAgent] = useState(null);
    useEffect(() => {
      fetch("data.json", { cache: "no-store" })
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then((d) => { setData(d); const first = d.agents.find((a) => a.started) || d.agents[0]; setAgent(first && first.key); })
        .catch((e) => setError(String(e)));
      const onHash = () => setTab(readTab());
      window.addEventListener("hashchange", onHash);
      return () => window.removeEventListener("hashchange", onHash);
    }, []);
    const go = (t) => { history.replaceState(null, "", "#" + t); setTab(t); };
    const onArrows = (e) => {
      const i = TABS.findIndex((x) => x[0] === tab);
      const j = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null;
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
        <div className="top"><div className="wrap">
          <div className="brand">
            <div className="title"><h1><${Timestral} size=${22} />Timestral Trading Agent</h1><span className="badge">Paper trading</span></div>
            <div className="actions">
              <a className="act" href=${data.links.audit_log}><${Glyph} d=${DOC} />Audit log</a>
              <a className="act" href=${data.repo}><${Glyph} d=${CODE} />Code</a>
            </div>
          </div>
          <p className="lede">An autonomous agent trading two paper accounts on US ETFs and stocks. Timestral forecasts the risk, a Mistral LLM
            decides, deterministic guardrails bound every decision before it reaches the broker.</p>
          <${Fresh} at=${data.generated_at} />
        </div></div>
        <main className="wrap">
          <nav className="guide" role="tablist" aria-label="Sections" onKeyDown=${onArrows}>
            ${TABS.map(([k, t, d], i) => html`<button key=${k} id=${"tab-" + k} role="tab" aria-selected=${tab === k} aria-controls="panel"
              tabIndex=${tab === k ? 0 : -1} onClick=${() => go(k)}><b><span className="k">${i + 1}</span>${t}</b><span>${d}</span></button>`)}
          </nav>
          <div id="panel" role="tabpanel" aria-labelledby=${"tab-" + tab}>
          ${tab === "live" && html`<${Live} data=${data} agent=${agent} setAgent=${setAgent} />`}
          ${tab === "how" && html`<${How} data=${data} />`}
          ${tab === "try" && html`<${Try} data=${data} />`}
          </div>
          <footer className="foot"><span>Generated ${when(data.generated_at)} from the audit log.</span><span>Paper trading, not financial advice.</span>
            <a href=${data.links.decisions}>design decisions</a><a href=${data.links.classic}>plain page</a></footer>
        </main>
      </${React.Fragment}>`;
  }

  ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
})();
