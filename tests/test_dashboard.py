"""Dashboard (D-012): rendering from audit records, edge states, escaping, self-containment."""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent.audit import FileAuditStore
from agent.dashboard import _nice_ticks, build, chronological, render
from agent.reconcile import OrderState, reconcile_runs
from tests.helpers import BASELINE, CFG, FakeBroker, as_json, proposal
from tests.test_decide import BROKEN, FakeLLM
from tests.test_reconcile import LATER, Lookup, live_run, states_for
from tests.test_run import cycle

STAMP = datetime(2026, 11, 23, 22, 0, tzinfo=UTC)
EVIL = '<script>alert(1)</script> "><img src=x onerror=alert(1)>'


def page(store: FileAuditStore) -> str:
    runs = chronological(store.read_runs())
    return render(runs, store.read_fills(), STAMP, tables=["decisions.csv"])


def test_empty_log_renders_a_clear_empty_state(tmp_path: Path) -> None:
    html = page(FileAuditStore(tmp_path))
    assert "No runs yet" in html
    assert "Portfolio value" not in html
    assert "Export" in html and "Paper trading, not financial advice" in html


def test_a_single_dry_run_shows_every_section(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    for section in (
        "$100,000",
        "Performance",
        "What it holds",
        "At the last run",
        "Can we trust it?",
        "Timestral forecasts, 5-day cumulative return",
        "History of runs",
        "Executions and slippage",
        "Account value per run",
        "No orders have been reconciled yet",
        "dry run",
        "accepted",
        "fake",  # the model id reported by the (fake) LLM
        "decide_v1",
    ):
        assert section in html, section
    assert 'href="data/decisions.csv"' in html
    assert "blob/audit-log/runs/2026-11-23_morning.json" in html


def test_two_runs_draw_the_equity_line_and_a_drawdown(tmp_path: Path) -> None:
    cycle(tmp_path, mode="morning")
    drifted = FakeBroker(equity=94_000.0, positions=_held(94_000.0))
    _, _, _, store = cycle(tmp_path, mode="afternoon", broker=drifted)
    html = page(store)
    assert 'class="line"' in html and 'class="area"' in html
    assert html.count('class="pt"') >= 2
    assert "drawdown from peak -6.0%" in html
    assert "$94,000" in html and "-6.00% since the first run" in html
    assert html.count("Nov 23") == 1  # one date label for two runs on the same day


def _held(equity: float) -> dict[str, object]:
    from tests.helpers import positions_for

    return positions_for(BASELINE, equity)  # type: ignore[no-any-return]


def test_corrections_are_listed_with_the_rules_that_fired(tmp_path: Path) -> None:
    llm = FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))])
    _, _, _, store = cycle(tmp_path, llm=llm)
    html = page(store)
    assert "accepted with corrections" in html
    assert "above_asset_cap NVDA" in html


def test_a_rejected_run_is_shown_as_such(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path, llm=FakeLLM([BROKEN, BROKEN]))
    html = page(store)
    assert "rejected, positions held" in html
    assert "LLM proposal rejected (invalid_json): positions held" in html
    assert "No parsable analysis." in html
    assert "no orders (positions held)" in html


def test_an_analysis_that_is_not_text_is_ignored(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path, llm=FakeLLM(['{"analysis": 5}', '{"analysis": 5}']))
    assert "No parsable analysis." in page(store)


def test_the_circuit_breaker_override_is_visible(tmp_path: Path) -> None:
    cycle(tmp_path, mode="morning")
    crashed = FakeBroker(equity=80_000.0, positions=_held(80_000.0))
    _, _, _, store = cycle(tmp_path, mode="afternoon", broker=crashed)
    assert "circuit breaker override" in page(store)


def test_kill_switch_and_live_runs_are_distinguished(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path / "a", live=True, env={})
    assert "dry run (kill switch off)" in page(store)
    store, _ = live_run(tmp_path / "b")
    assert "live orders sent" in page(store)


def test_reconciled_fills_feed_the_table_and_the_slippage_tile(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    states = states_for(broker)
    states[broker.submitted[0]["id"]] = OrderState("expired", 0.0, None, None, None)
    reconcile_runs(store, Lookup(states), LATER)
    html = page(store)
    assert "14/15 filled, average slippage +10.0 bps" in html
    assert "1 unfilled, 0 unsent order(s)" in html  # surfaced as an alert, not buried
    assert "expired" in html and "+10.0" in html
    assert "No orders have been reconciled yet" not in html


def test_no_slippage_is_claimed_without_any_filled_order(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    states = {o["id"]: OrderState("expired", 0.0, None, None, None) for o in broker.submitted}
    reconcile_runs(store, Lookup(states), LATER)
    html = page(store)
    assert "0/15 filled" in html and "average slippage" not in html
    assert "15 unfilled" in html


def test_llm_text_is_escaped_everywhere(tmp_path: Path) -> None:
    answer = proposal(rationale=EVIL)
    answer["analysis"] = EVIL
    _, _, _, store = cycle(tmp_path, llm=FakeLLM([json.dumps(answer)]))
    html = page(store)
    assert "alert(1)" in html  # the text is displayed...
    assert "<script>alert(1)" not in html and "<img src=x" not in html  # ...never as markup
    assert "&lt;script&gt;alert(1)" in html
    assert html.count("<script>") == 1  # only our own tooltip script
    tips = re.findall(r'data-tip="([^"]*)"', html)
    assert tips and all("<" not in t and ">" not in t for t in tips)


def test_the_page_is_self_contained(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "src=" not in html and "<link" not in html and "@import" not in html
    assert not re.search(r"url\(\s*['\"]?https?:", html)
    external = set(re.findall(r'href="(https?://[^"]+)"', html))
    assert all(url.startswith("https://github.com/SpectreB/") for url in external)


def test_light_and_dark_themes_and_phone_layout_are_defined(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "prefers-color-scheme:dark" in html and 'data-theme="dark"' in html
    assert "viewport" in html and "max-width:560px" in html


def test_charts_have_text_alternatives(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert html.count('role="img"') == 3  # forecasts, account value, circuit-breaker meter
    assert html.count("<summary>Table view</summary>") == 3
    assert 'aria-label="80% forecast intervals of 15 assets over 5 days"' in html


@pytest.mark.parametrize(
    ("lo", "hi"), [(0.0, 1.0), (-0.07, 0.07), (99_000.0, 100_500.0), (1e-4, 3e-4)]
)
def test_axis_ticks_are_clean_and_inside_the_range(lo: float, hi: float) -> None:
    ticks = _nice_ticks(lo, hi)
    assert 2 <= len(ticks) <= 8
    assert all(lo - 1e-9 <= t <= hi + 1e-9 for t in ticks)
    steps = {round(b - a, 9) for a, b in itertools.pairwise(ticks)}
    assert len(steps) == 1


def test_build_writes_the_page_and_copies_the_tables(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    _, _, _, store = cycle(audit)
    store.build_tables()
    index = build(audit, site, STAMP, repo="https://github.com/example/repo")
    assert index == site / "index.html"
    assert (site / "data" / "decisions.csv").exists() and (site / "data" / "fills.csv").exists()
    html = index.read_text(encoding="utf-8")
    assert 'href="data/orders.csv"' in html
    assert "https://github.com/example/repo/tree/audit-log" in html
    assert "Generated 2026-11-23T22:00:00+00:00" in html


def test_build_without_tables_still_works(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    cycle(audit)
    html = build(audit, site, STAMP).read_text(encoding="utf-8")
    assert "What it holds" in html and not (site / "data").exists()
    assert as_json(proposal()) and CFG  # keep shared imports exercised


def test_every_forecast_bar_stays_inside_the_chart(tmp_path: Path) -> None:
    # A very uncertain asset (+/-19%) next to calm ones: the scale must contain all of them.
    _, _, _, store = cycle(tmp_path)
    runs = chronological(store.read_runs())
    nvda = runs[-1]["forecasts"]["NVDA"]["5"]
    nvda.update(q10=-0.19, q90=0.19, width_80=0.38)
    html = render(runs, [], STAMP)
    bars = re.findall(r'class="range" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', html)
    assert len(bars) == 15
    left, right_edge = 56, 720 - 64  # the plot area of the chart
    for x, width in bars:
        assert left - 0.1 <= float(x) and float(x) + float(width) <= right_edge + 0.1
    assert "-20%" in html and ">0%<" in html and "+0%" not in html


def test_charts_scroll_instead_of_shrinking_their_text_on_phones(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert html.count('<div class="chart"><svg') == 2
    assert ".chart svg{min-width:600px}" in html


def _scored(tmp_path: Path, closes_after: float) -> tuple[FileAuditStore, list[dict[str, object]]]:
    from agent.scoring import score_forecasts
    from tests.test_scoring import SYMS

    _, _, _, store = cycle(tmp_path)
    dates = ["2026-11-20", "2026-11-23", "2026-11-24"]
    closes = {d: dict.fromkeys(SYMS, 100.0 if d == dates[0] else closes_after) for d in dates}
    store.write_observation("2026-11-24", {"as_of": "2026-11-24", "closes": closes})
    runs = chronological(store.read_runs())
    return store, score_forecasts(runs, store.read_observations())  # type: ignore[return-value]


def test_live_calibration_appears_once_forecasts_are_scored(tmp_path: Path) -> None:
    store, scores = _scored(tmp_path, closes_after=101.0)
    html = render(chronological(store.read_runs()), [], STAMP, scores=scores)
    assert "Forecast calibration, live" in html
    assert "1d" in html and "2d" in html and "100%" in html  # a +1% move is inside the interval
    assert "15 scored forecasts" in html and "target 80%" in html
    assert "n/a" in html  # a flat forecast median has no direction
    assert "left:calc(80% - 1px)" in html  # the 80% target tick


def test_calibration_is_hidden_until_something_is_scored(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    assert "Forecast calibration" not in page(store)


def test_build_includes_the_calibration_from_recorded_observations(tmp_path: Path) -> None:
    _scored(tmp_path / "audit", closes_after=101.0)
    html = build(tmp_path / "audit", tmp_path / "site", STAMP).read_text(encoding="utf-8")
    assert "Forecast calibration, live" in html


def test_a_direction_hit_rate_is_shown_when_forecasts_have_a_direction(tmp_path: Path) -> None:
    from agent.scoring import score_forecasts
    from tests.test_scoring import OBS, run_record

    scores = score_forecasts([run_record()], [OBS])  # median +1%, realised +1% then +0.5%
    _, _, _, store = cycle(tmp_path)
    html = render(chronological(store.read_runs()), [], STAMP, scores=scores)
    assert "direction hit rate 100%" in html


# --------------------------------------------------------------------------- redesign (T-04)


def _report(tmp_path: Path, closes_after: tuple[float, ...] = (101.0, 102.0, 101.5)) -> Any:
    """A real run plus observed closes: the performance report as the page gets it."""
    from agent.metrics import performance_report
    from tests.test_scoring import SYMS

    _, _, _, store = cycle(tmp_path)
    dates = ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"]
    prices = (100.0, *closes_after)
    closes = {d: dict.fromkeys(SYMS, p) for d, p in zip(dates, prices, strict=True)}
    closes["2026-11-24"]["SPY"] = 99.0  # the market falls on day two: references differ
    store.write_observation("2026-11-25", {"as_of": "2026-11-25", "closes": closes})
    runs = chronological(store.read_runs())
    return store, runs, performance_report(runs, store.read_observations())


def test_the_page_is_organised_around_the_readers_questions(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    order = [html.index(h) for h in ("dry run", "At the last run", "Performance",
                                     "What it holds", "Can we trust it?", "Export")]  # fmt: skip
    assert order == sorted(order)
    assert html.count('class="ev"') == 7  # evidence stays folded away until asked for


def test_the_status_banner_shows_when_it_ran_and_what_runs_next(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "Last run <b>Mon 23 Nov, 10:00 ET</b> (morning)" in html
    assert "Next run about <b>Mon 23 Nov, 15:00 ET</b>" in html
    assert "prompt decide_v1" in html and "No alert at the last run" in html


def test_the_next_run_skips_the_weekend() -> None:
    from agent.dashboard import _next_run, _when

    friday = datetime.fromisoformat("2026-11-20T15:02:00-05:00")
    assert _when(_next_run(friday, "afternoon")) == "Mon 23 Nov, 10:00 ET"
    assert _when(_next_run(friday, "morning")) == "Fri 20 Nov, 15:00 ET"
    assert _when(_next_run(datetime.fromisoformat("2026-11-23T15:02:00-05:00"), "afternoon")) == (
        "Tue 24 Nov, 10:00 ET"
    )


def test_alerts_surface_corrections_and_circuit_breaker_levels(tmp_path: Path) -> None:
    llm = FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))])
    _, _, _, store = cycle(tmp_path / "a", llm=llm)
    assert "1 guardrail correction(s) at the last run" in page(store)
    cycle(tmp_path / "b", mode="morning")
    drifted = FakeBroker(equity=94_000.0, positions=_held(94_000.0))
    _, _, _, store = cycle(tmp_path / "b", mode="afternoon", broker=drifted)
    assert "Circuit breaker active: reduce-only mode" in page(store)
    cycle(tmp_path / "c", mode="morning")
    dipped = FakeBroker(equity=96_500.0, positions=_held(96_500.0))
    _, _, _, store = cycle(tmp_path / "c", mode="afternoon", broker=dipped)
    assert "Drawdown alert level reached" in page(store)
    cycle(tmp_path / "d", mode="morning")
    crashed = FakeBroker(equity=80_000.0, positions=_held(80_000.0))
    _, _, _, store = cycle(tmp_path / "d", mode="afternoon", broker=crashed)
    assert "Circuit breaker override: the LLM was bypassed" in page(store)


def test_the_last_run_lists_its_biggest_moves_with_the_reason(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "cash → 6.0%" in html and "+6.0 pts" in html
    assert "Justified by the forecast interval." in html
    assert "+ 9 smaller move(s)" in html  # 15 moves from cash, the six largest are listed
    assert "Orders: 15 dry_run" in html


def test_a_long_analysis_is_excerpted_with_the_full_text_one_click_away(tmp_path: Path) -> None:
    answer = proposal()
    answer["analysis"] = "Risk-off regime. " * 40
    _, _, _, store = cycle(tmp_path, llm=FakeLLM([json.dumps(answer)]))
    html = page(store)
    assert "Full analysis" in html and "Risk-off regime." in html
    assert "..." in html


def test_the_unchanged_portfolio_is_stated_plainly(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    runs = chronological(store.read_runs())
    runs[-1]["portfolios"]["current"] = dict(runs[-1]["portfolios"]["final"])
    assert "No change: positions held." in render(runs, [], STAMP)


def test_performance_waits_for_the_first_closed_session(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "No trading session has closed since the first run" in html
    assert "What does each layer add?" not in html
    assert "0.0% from peak" in html or "0.0% invested" in html or "drawdown" in html


def test_performance_compares_the_agent_with_four_references(tmp_path: Path) -> None:
    _, runs, report = _report(tmp_path)
    html = render(runs, [], STAMP, report=report)
    assert "3 session(s), 2026-11-23 to 2026-11-25" in html
    assert html.count('data-toggle="') == 5 and 'aria-pressed="true"' in html
    for label in (
        "Agent (after guardrails)",
        "LLM proposal",
        "Timestral rule",
        "Equal weight",
        "SPY",
    ):
        assert label in html
    assert 'class="sline s-agent"' in html and "What does each layer add?" in html
    assert "too early (3 of 10 sessions)" in html
    assert "A difference is only called conclusive" in html
    assert "<summary>Definitions</summary>" in html


def test_a_comparison_is_called_conclusive_only_with_its_interval(tmp_path: Path) -> None:
    _, runs, report = _report(tmp_path)
    comps = report["comparisons"]
    comps[0].update(conclusive=True, ci95_mean_daily=(0.001, 0.003))
    comps[1].update(conclusive=False, ci95_mean_daily=(-0.001, 0.002))
    html = render(runs, [], STAMP, report=report)
    assert "conclusive: the 95% interval excludes zero" in html
    assert "not distinguishable from zero" in html


def test_the_circuit_breaker_meter_shows_the_next_safeguard(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    runs = chronological(store.read_runs())
    html = render(runs, [], STAMP)
    assert "next safeguard at -3% (alert)" in html
    assert "Safeguards: -3% alert · -5% reduce-only mode" in html
    assert html.count("<i style=") == 4
    runs[-1]["account"]["drawdown_from_peak"] = -0.06
    assert "next safeguard at -8% (exposure capped at 60%)" in render(runs, [], STAMP)
    runs[-1]["account"]["drawdown_from_peak"] = -0.15
    assert "every safeguard level has been reached" in render(runs, [], STAMP)


def test_the_allocation_shows_each_assets_share_of_the_forecast_risk(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    runs = chronological(store.read_runs())
    html = render(runs, [], STAMP)
    assert "<small>risk " in html
    assert "Forecast volatility of the final portfolio over the horizon" in html
    del runs[-1]["risk"]  # a record written before the risk module existed
    old = render(runs, [], STAMP)
    assert "<small>risk " not in old and "Forecast volatility of the final" not in old


def test_the_evidence_explains_what_is_not_resolved_yet(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "No forecast is resolved yet" in html
    assert "No deviation from the baseline is resolved yet" in html
    assert "No correction, rejection or override in 1 run(s)." in html


def test_the_evidence_shows_risk_and_confidence_calibration(tmp_path: Path) -> None:
    _, runs, report = _report(tmp_path)
    report["risk_calibration"] = [{"horizon": 1, "n": 4, "inside_80": 0.75, "rms_z": 0.9}]
    report["confidence"] = {
        "n": 10,
        "mean_confidence": 0.62,
        "hit_rate": 0.5,
        "brier": 0.26,
        "bins": [{"range": "0.55-0.65", "n": 10, "mean_confidence": 0.62, "hit_rate": 0.5}],
    }
    html = render(runs, [], STAMP, report=report)
    assert "75%" in html and "0.90" in html and "underestimated" in html
    assert "10 tilt(s) resolved" in html and "Brier score 0.260" in html and "0.55-0.65" in html


def test_guardrail_activity_counts_rules_rejections_and_overrides(tmp_path: Path) -> None:
    llm = FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))])
    _, _, _, store = cycle(tmp_path, llm=llm)
    html = page(store)
    assert "above_asset_cap" in html and "0 rejected proposal(s)" in html


def test_every_run_can_be_opened_in_place(tmp_path: Path) -> None:
    cycle(tmp_path, mode="morning", llm=FakeLLM([json.dumps(proposal(NVDA=0.15, SPY=0.01))]))
    _, _, _, store = cycle(tmp_path, mode="afternoon", broker=FakeBroker(equity=100_000.0))
    html = page(store)
    assert html.count('<details class="run">') == 2
    assert "Full immutable record" in html and "<h3>Corrections</h3>" in html
    assert "<h3>Orders</h3>" in html
    runs = chronological(store.read_runs())
    runs[0]["orders"] = []
    assert "No order." in render(runs, [], STAMP)


def test_the_legend_toggle_script_is_present_and_valid() -> None:
    import shutil
    import subprocess
    import tempfile

    from agent.dashboard import SCRIPT

    assert "data-toggle" in SCRIPT and "aria-pressed" in SCRIPT
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(SCRIPT)
    assert subprocess.run([node, "--check", f.name], capture_output=True).returncode == 0


def test_a_fully_filled_run_raises_no_execution_alert(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    html = page(store)
    assert "No alert at the last run" in html and "unfilled" not in html
    assert "15/15 filled, average slippage +10.0 bps" in html


def test_a_long_series_labels_the_time_axis_sparingly(tmp_path: Path) -> None:
    _, runs, report = _report(tmp_path)
    rows = [dict(report["daily"][0], session=f"2026-12-{day:02d}") for day in range(1, 15)]
    report.update(
        daily=rows, sessions=len(rows), first_session="2026-12-01", last_session="2026-12-14"
    )
    html = render(runs, [], STAMP, report=report)
    labels = re.findall(r'text-anchor="middle">(12-\d\d)</text>', html)
    assert 3 <= len(labels) < len(rows)  # a handful of dates, the last one always shown
    assert labels[-1] == "12-14"
