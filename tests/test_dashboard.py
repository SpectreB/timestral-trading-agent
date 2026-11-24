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
from tests.helpers import (
    BASELINE,
    CFG,
    FakeBroker,
    FakeMarketData,
    FakeTimestral,
    as_json,
    make_closes,
    proposal,
)
from tests.test_decide import BROKEN, NOW, FakeLLM
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
        "Account value over time",
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


def test_the_page_is_light_by_default_dark_is_opt_in_and_it_fits_a_phone(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    # A dark system theme must not darken the page: readers found it hard to read.
    assert "prefers-color-scheme" not in html and 'data-theme="dark"' in html
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
    order = [html.index(h) for h in ("At the last run", "Performance", "Trades", "What it holds",
                                     "Can we trust it?", "Export")]  # fmt: skip
    assert order == sorted(order)
    assert html.count('class="ev"') == 7  # evidence stays folded away until asked for


def test_the_status_banner_shows_when_it_ran_and_what_runs_next(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    # Last and next run are stacked in one small block, not two separate boxes.
    assert "<span>Last run</span><b>Mon 23 Nov, 10:00 ET</b>" in html
    # Counted from the page's own time (Mon 17:00 ET), not from the last run: Tue 10:00 ET.
    assert "<span>Next run</span><b>Tue 24 Nov, 10:00 ET</b>" in html
    assert "prompt decide_v1" in html
    # A page with nothing wrong says nothing about alerts: silence is the good news.
    assert "No alert at the last run" not in html and 'class="alerts"' not in html


def test_the_next_slot_is_counted_from_now_not_from_the_last_run() -> None:
    from agent.dashboard import _next_slot, _when

    def slot(moment: str) -> str:
        return _when(_next_slot(datetime.fromisoformat(moment)))

    assert slot("2026-11-23T08:00:00-05:00") == "Mon 23 Nov, 10:00 ET"
    assert slot("2026-11-23T10:00:00-05:00") == "Mon 23 Nov, 15:00 ET"  # a slot is not its own next
    assert slot("2026-11-23T22:00:00+00:00") == "Tue 24 Nov, 10:00 ET"  # 17:00 ET: tomorrow
    assert slot("2026-11-20T16:00:00-05:00") == "Mon 23 Nov, 10:00 ET"  # Friday evening: Monday


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


def test_the_allocation_shows_dollar_amounts_types_and_changes(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    # Target value = weight x account value ($100,000 in the test account), next to the weight.
    assert "<b>$6,000</b> 6.0%" in html
    assert "SPY: $6,000 target (6.0%)" in html
    assert '<th class="num">Target value</th>' in html and "<td class='num'>$6,000</td>" in html
    # Stocks and ETFs have their own colour, with a legend; every position was bought here.
    assert 'class="bar stock"' in html and 'class="bar"' in html
    assert '<i class="sw stock"></i>Stock' in html
    rows = html.split("What it holds")[1].split('<div class="legend">')[0]
    assert "▲ 6.0" in rows and "▼" not in rows


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
    assert "No session has closed since the first run" in html
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
    # A Python "\n" would put a raw line break inside a quoted JavaScript string.
    assert "join('\\n')" in SCRIPT
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
    assert "unfilled" not in html and 'class="alerts"' not in html
    assert "15/15 filled, average slippage +10.0 bps" in html


def test_the_simulation_card_lets_the_reader_choose_agent_stance_and_start(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    card = html.split("<h2>Run it yourself</h2>")[1].split("</div></div>")[0]
    for key in ("agent", "stance", "start"):
        assert f'<select data-opt="{key}">' in card
    labels = ("Tool agent (final)", "Pipeline agent (reference)", "Tool agent + news covariates")
    labels += ("Cautious: 30% cash, small positions", "All cash", "Equal weight, 90% invested")
    for label in labels:
        assert label in card
    # Without JavaScript the button still works, with the first choice of each list.
    assert "body=agent%3A+tools%0Astance%3A+balanced%0Astart%3A+current" in card
    # With it, the same link is rebuilt from the selects (title and text stay fixed).
    assert 'id="sim-go"' in card and 'data-base="https://github.com/' in card
    assert "sel.map(s=>s.dataset.opt+': '+s.value)" in html


def test_the_trades_are_shown_up_front_as_the_paper_trading_activity(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    html = page(store)
    card = html.split('<div class="card sec-agent"><h2>Trades</h2>')[1].split("</table></div>")[0]
    assert "15 orders" in card and "15 filled" in card and "bought" in card and "sold" in card
    assert card.count('class="side buy"') == 12 and "▲ Buy" in card  # the latest twelve
    assert "The 3 earlier orders" in html
    assert "<th>When</th>" in card and "UTC" in card
    # Before any order has reached the broker, the card says so instead of showing an empty table.
    _, _, _, dry = cycle(tmp_path / "dry")
    assert "No order has been sent to the broker yet." in page(dry)  # a dry run sent nothing


def test_orders_sent_but_not_reconciled_yet_are_shown_as_sent(tmp_path: Path) -> None:
    store, _ = live_run(tmp_path)  # live orders, no reconciliation file yet
    html = page(store)
    card = html.split('<div class="card sec-agent"><h2>Trades</h2>')[1].split("</table></div>")[0]
    # One note for the whole table instead of a badge on every row.
    assert "15 orders" in card and "awaiting the broker&#x27;s final state" in card
    assert "0 filled" not in card and "not reconciled yet" not in card
    assert "No order has been sent" not in html


def test_more_than_twelve_trades_fold_the_earlier_ones(tmp_path: Path) -> None:
    from agent.dashboard import _trades

    order = {"symbol": "SPY", "side": "sell", "quantity": 2.0, "status": "cancelled",
             "filled_qty": 0.0, "avg_fill_price": None, "filled_at": None,
             "submitted_at": "2026-11-23T15:42:09+00:00"}  # fmt: skip
    fills = [{"run_id": "r1", "orders": [order] * 13}]
    html = _trades(fills, [])
    assert "The 1 earlier orders" in html and html.count("▼ Sell") == 13
    assert "cancelled" in html and "filled" not in html  # nothing filled: no filled/bought chips
    assert "earlier orders" not in _trades(
        [{"run_id": "r1", "orders": [order]}], []
    )  # few: no fold


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


def test_evaluators_can_request_a_simulation_from_the_page(tmp_path: Path) -> None:
    _, _, _, store = cycle(tmp_path)
    html = page(store)
    assert "Run it yourself" in html and 'class="btn"' in html
    assert "/issues/new?title=Simulation+request&amp;body=" in html
    assert "Simulations never send an order." in html
    assert "No simulation has been requested yet." in html


def test_requested_simulations_are_listed_with_their_requester(tmp_path: Path) -> None:
    from agent.simulate import simulate

    cycle(tmp_path)
    simulate(
        now=NOW, cfg=CFG, market=FakeMarketData(make_closes(520)), broker=FakeBroker(),
        model=FakeTimestral([0.02] * 15), llm=FakeLLM([json.dumps(proposal())] * 2),
        root=tmp_path, requester="<b>evil</b>", env={},
    )  # fmt: skip
    html = build(tmp_path, tmp_path / "site", STAMP).read_text(encoding="utf-8")
    assert "2026-11-23_sim-100000" in html and "requested by &lt;b&gt;evil&lt;/b&gt;" in html
    assert "order(s) planned, none sent" in html
    assert "simulations/runs/2026-11-23_sim-100000.json" in html


def test_the_empty_page_still_offers_a_simulation(tmp_path: Path) -> None:
    html = page(FileAuditStore(tmp_path))
    assert "No runs yet" in html and "Run a simulation" in html


# --------------------------------------------------------------------------- two agents (D-018)


def _tool_cycle(audit: Path, broken: bool = False) -> FileAuditStore:
    """A tool agent run, recorded where the Trade workflow puts it."""
    from agent.config import tool_agent_config
    from agent.run import run_cycle
    from tests.test_decide_tools import EVALUATE, FakeToolLLM, step

    calls = [
        ("get_forecasts", '{"symbols": ["SPY"], "horizon": 5}'),
        ("evaluate_portfolio", EVALUATE),
        ("evaluate_portfolio", EVALUATE),
        ("get_baseline", "{}"),
        ("get_forecasts", '{"symbols": ["BTC"], "horizon": 5}'),
    ]
    store = FileAuditStore(audit / "agents" / "tools")
    run_cycle(
        mode="morning",
        now=NOW,
        cfg=tool_agent_config(),
        market=FakeMarketData(make_closes(520)),
        broker=FakeBroker(),
        model=FakeTimestral([0.02] * 15),
        llm=FakeToolLLM([step(*calls), step()], [BROKEN if broken else as_json(proposal())] * 2),  # type: ignore[arg-type]
        store=store,
        live=False,
        env={},
    )
    return store


def _replay_report(tmp_path: Path) -> dict[str, Any]:
    _, _, report = _report(tmp_path / "replay")
    return {**report, "account_return": -0.0123}


def test_both_agents_are_compared_and_the_tool_agent_comes_first(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    cycle(audit)
    _tool_cycle(audit)
    replay_dir, tool_replay_dir = tmp_path / "docs_replay", tmp_path / "docs_replay_tools"
    base = _replay_report(tmp_path)  # one report, shown for both agents with their own result
    for folder, ret in ((replay_dir, -0.0123), (tool_replay_dir, 0.0456)):
        folder.mkdir()
        (folder / "report.json").write_text(json.dumps({**base, "account_return": ret}))
    html = build(
        audit, site, STAMP, replay_dir=replay_dir, tool_replay_dir=tool_replay_dir
    ).read_text(encoding="utf-8")
    assert "Two ways of using Timestral" in html
    assert html.index('id="tab-tools"') < html.index('id="tab-pipeline"')
    assert 'id="panel-tools" data-panel="tools" aria-labelledby="tab-tools">' in html
    assert (
        'id="panel-pipeline" data-panel="pipeline" aria-labelledby="tab-pipeline" hidden>' in html
    )
    # How the tool agent used Timestral, at the last run and in its history.
    assert "the LLM called Timestral 5 time(s)" in html
    assert "had Timestral evaluate a candidate portfolio (2 times)" in html
    assert "1 refused call(s)" in html
    assert "refused: not in the universe: BTC" in html
    assert "probability of a loss" in html and "the rule-based weights" in html
    assert "1 asset(s) at 5 day(s)" in html
    assert "/blob/audit-log/agents/tools/runs/2026-11-23_morning.json" in html
    # The head-to-head card compares the two on concrete measures, each column in its colour.
    assert "How do they compare?" in html and 'class="num c-tools"' in html
    assert 'class="num c-pipeline"' in html
    assert "Timestral calls per decision" in html and "1 fixed run" in html
    # The replay is a dropdown under Performance, one per agent.
    assert html.count("<b>Tested on the past</b>") == 2
    assert "Simulated account: -1.23%" in html and "Simulated account: +4.56%" in html
    assert "/blob/main/docs/replay/README.md" in html
    assert "/blob/main/docs/replay_tools/README.md" in html
    # The two agents as two flows, instead of a list of sentences.
    assert 'class="dg"' in html and "asks, up to 12 calls" in html and "baseline: 1 / width" in html
    assert "How they differ" not in html


def _evals_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "evals"
    (folder / "tools_v2").mkdir(parents=True)

    def results(passes: list[bool], key: str = "calm") -> dict[str, Any]:
        scenario = {"key": key, "title": "Nothing happens", "expectation": "Stays close.",
                    "passed": all(passes), "runs": [{"repeat": i, "passed": x}
                                                    for i, x in enumerate(passes)]}  # fmt: skip
        return {"scenarios": [scenario], "stability": {"calls": 5, "max_pairwise_turnover": 0.06}}

    (folder / "results.json").write_text(json.dumps(results([False, False])))
    (folder / "tools_v2" / "results.json").write_text(json.dumps(results([True, False, True])))
    (folder / "tools_v2" / "focus_10_runs_v2.json").write_text(
        json.dumps({"calm": [True] * 7 + [False] * 3, "details": {}})
    )
    return folder


def test_the_page_says_why_the_agents_should_not_do_anything_silly(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    cycle(audit)
    html = build(audit, site, STAMP, evals_dir=_evals_dir(tmp_path)).read_text(encoding="utf-8")
    assert "Why it should not do anything silly" in html
    # The rules, with the limits read from the configuration.
    for fragment in ("Unusable answers are rejected", "at most 25% per ETF, 10% per stock",
                     "cash at least 10%", "3% alert, 5% no new risk", "turnover capped at 30%",
                     "An independent final check"):  # fmt: skip
        assert fragment in html
    # The behaviour tests: the tool agent's longer series replaces its short one, failures show.
    assert "How the LLM behaves when tested" in html
    assert "0/2" in html and "7/10" in html and "2/3" not in html
    assert "differ by at most 6 points" in html


def test_the_behaviour_table_copes_with_a_missing_agent_or_scenario() -> None:
    from agent.dashboard import _behaviour

    one = {"scenarios": [{"key": "crash", "title": "Crash", "expectation": "x", "passed": True,
                          "runs": [{"repeat": 0, "passed": True}]}],
           "stability": {"calls": 5, "max_pairwise_turnover": 0.02}}  # fmt: skip
    other = {**one, "scenarios": [{**one["scenarios"][0], "key": "calm"}]}
    assert _behaviour({"pipeline": None, "tools": None}) == ""
    only = _behaviour({"pipeline": one, "tools": None})
    assert "1/1" in only and "not measured" in only and "Tool agent: asked" not in only
    assert "not measured" in _behaviour({"pipeline": one, "tools": other})


def test_the_page_lists_its_inputs_as_keywords_and_colours_its_sections(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    cycle(audit)
    _tool_cycle(audit)
    html = build(audit, site, STAMP).read_text(encoding="utf-8")
    for keyword in ("Alpaca daily bars", "10 ETFs, 5 stocks", "Timestral-28M (pipeline agent)",
                    "Timestral-209M (tool agent)", "mistral-large-4-0",
                    "sources: Alpaca News, FRED",
                    "analyst: mistral-small-2603", "news: article volume and sentiment",
                    "events: earnings, company events, macro dates",
                    "built; not yet measured or used live"):  # fmt: skip
        assert keyword in html
    for key in ("cmp", "safe", "how", "sim", "agent"):  # the agents' own cards share their colour
        assert f'class="card sec-{key}"' in html
    # Green is kept for gains: the opening cards and the rules use neutral badges.
    head = html.split("Two ways of using Timestral")[0]
    assert "b-good" not in head.split('class="glance"')[1]


def _covariate_results() -> dict[str, Any]:
    def summary(variant: str, cov: float) -> list[dict[str, Any]]:
        return [
            {"variant": variant, "kind": k, "n": 10, "coverage_80": cov, "pinball": 0.01,
             "rms_ratio": 1.0, "hit_rate": 0.5}
            for k in ("stock", "etf")
        ]  # fmt: skip

    variants = ("news", "events", "news+events")
    return {
        "horizon": 5, "origins": 118, "first_origin": "2026-06-01", "last_origin": "2026-11-16",
        "stocks": 30, "stocks_with_news": 29, "etfs": 10,
        "summary": summary("prices", 0.78) + [r for v in variants for r in summary(v, 0.79)],
        "paired": [
            {"variant": v, "kind": k, "origins": 118, "mean_diff": -1e-5, "relative": -0.001,
             "ci95": [-2e-5, 1e-6]}
            for v in variants for k in ("stock", "etf")
        ],
        "verdicts": [{"variant": v, "decision": "no"} for v in variants],
    }  # fmt: skip


def test_the_covariate_trial_is_shown_as_tried_and_not_adopted(tmp_path: Path) -> None:
    audit, site, cov = tmp_path / "audit", tmp_path / "site", tmp_path / "cov"
    cycle(audit)
    _tool_cycle(audit)
    cov.mkdir()
    (cov / "results.json").write_text(json.dumps(_covariate_results()), encoding="utf-8")
    html = build(audit, site, STAMP, covariates_dir=cov).read_text(encoding="utf-8")
    assert 'class="card sec-cov"' in html
    for text in ("Tried, not adopted", "29 stocks and 10 ETFs on 118 past days", "Prices only",
                 "News and events", "no gain", "measured, not adopted", "What it cannot tell",
                 "Not tested: known-in-advance calendars"):  # fmt: skip
        assert text.replace("What it cannot tell", "what it cannot tell") in html
    assert "not yet measured or used live" not in html
    without = build(audit, tmp_path / "site2", STAMP).read_text(encoding="utf-8")
    assert "Do news help the forecast?" not in without
    assert "not yet measured or used live" in without


def test_the_two_agents_are_compared_on_measures_beyond_money(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    store, broker = live_run(audit)
    reconcile_runs(store, Lookup(states_for(broker)), LATER)
    _tool_cycle(audit)
    base = _replay_report(tmp_path)
    base["risk_calibration"] = [{"horizon": 5, "n": 59, "inside_80": 0.68, "rms_z": 1.26}]
    for name in ("past_pipeline", "past_tools"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text(json.dumps(base))
    html = build(
        audit,
        site,
        STAMP,
        replay_dir=tmp_path / "past_pipeline",
        tool_replay_dir=tmp_path / "past_tools",
        evals_dir=_evals_dir(tmp_path),
    ).read_text(encoding="utf-8")
    table = html.split("<h2>How do they compare?</h2>")[1].split("</table>")[0]
    cells = {
        m.group(1): re.findall(r"<td class='num'>([^<]*)</td>", m.group(2))
        for m in re.finditer(r"<tr><td>([^<]+)</td>(.*?)</tr>", table)
    }
    # Columns follow the colour code: the tool agent first, then the pipeline agent.
    assert table.index("c-tools") < table.index("c-pipeline")
    assert all(len(v) == 2 for v in cells.values())
    assert cells["Timestral calls per decision"] == ["5.0", "1 fixed run"]
    assert cells["Orders filled"] == ["n/a", "15/15"]  # the tool agent has no fills yet
    assert cells["Average slippage"] == ["n/a", "+10.0 bps"]
    assert cells["Risk interval hit (5 days, target 80%)"] == ["68%", "68%"]
    assert cells["Behaviour tests passed (more repeats for the tool agent)"] == [
        "7/10 (70%)",
        "0/2 (0%)",
    ]  # the tool agent: its 10-repeat series
    for key in ("Return over 63 replayed sessions", "Volatility, annualised",
                "Largest fall from a peak"):  # fmt: skip
        assert all(c.endswith("%") for c in cells[key])
    assert all(
        c != "n/a" for c in cells["Invested share"] + cells["Points traded per run (median)"]
    )


def test_measures_that_cannot_be_computed_are_left_out_not_invented(tmp_path: Path) -> None:
    from agent.dashboard import AgentView, _measures

    record = chronological(_tool_cycle(tmp_path).read_runs())[0]
    record["risk"] = None  # a run recorded without its risk figures
    fills = [
        {"run_id": "r", "summary": {"n_orders": 2, "n_filled": 1, "slippage_bps_weighted": None}}
    ]
    view = AgentView(
        "pipeline", "Pipeline agent", "", [record], fills, [], None, replay={"sessions": 0}
    )
    out = _measures(view, {"tools": None, "pipeline": None})
    assert out["risk"] == "n/a" and out["filled"] == "1/2" and "slippage" not in out
    assert "past_return" not in out and "tests" not in out and out["accepted"] == "1/1"
    assert _measures(AgentView("tools", "Tool agent", "", [], [], [], None), None) == {}


def test_each_agent_opens_with_where_it_started_and_where_it_is(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    cycle(audit)
    _tool_cycle(audit)
    html = build(audit, site, STAMP).read_text(encoding="utf-8")
    head = html.split("Two ways of using Timestral")[0]
    # Two cards before any other content: start, an arrow, now, the change, the status.
    assert head.count('class="gcard') == 2 and head.count('class="journey"') == 2
    assert '<span class="from">$100,000</span>' in head and "since " in head
    assert "Tool agent" in head and "Pipeline agent" in head
    assert "accepted" in head and "No alert" not in head  # silence is the good news
    # The allocation says what the portfolio was at the start, and now.
    assert "Start, " in html and ": 0% invested" in html and "Now: " in html
    # Each card has its agent's colour class; the colour code is the same everywhere.
    assert 'class="gcard tools"' in head and 'class="gcard pipeline"' in head


def test_a_loss_is_red_and_a_gain_is_green_with_the_sign(tmp_path: Path) -> None:
    from agent.dashboard import AgentView, _glance

    def view(first: float, last: float) -> AgentView:
        store = _tool_cycle(tmp_path / f"{first}-{last}")
        record = chronological(store.read_runs())[0]
        runs = [json.loads(json.dumps(record)) for _ in range(2)]  # a start and a latest run
        runs[0]["account"]["equity"], runs[1]["account"]["equity"] = first, last
        return AgentView("tools", "Tool agent", "", runs, [], [], None)

    down = _glance([view(100_000.0, 96_307.0)])
    assert 'class="to down"' in down and "-$3,693 (-3.69%)" in down
    up = _glance([view(100_000.0, 101_000.0)])
    assert 'class="to up"' in up and "$1,000 (+1.00%)" in up
    assert "Not started yet" in _glance(
        [AgentView("pipeline", "Pipeline agent", "", [], [], [], None)]
    )


def test_without_evaluation_results_the_page_keeps_the_rules_only(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    cycle(audit)
    html = build(audit, site, STAMP).read_text(encoding="utf-8")
    assert "Rules it cannot cross" in html and "How the LLM behaves when tested" not in html


def test_a_tool_agent_without_runs_yet_is_announced_and_not_selected(tmp_path: Path) -> None:
    audit = tmp_path / "audit"
    cycle(audit)
    (audit / "agents" / "tools" / "runs").mkdir(parents=True)
    html = build(audit, tmp_path / "site", STAMP).read_text(encoding="utf-8")
    assert "Not started yet" in html
    assert 'id="panel-tools" data-panel="tools" aria-labelledby="tab-tools" hidden>' in html
    assert "This agent has not recorded a run" in html
    assert "Tested on the past" not in html  # no replay report given


def test_a_page_with_only_the_tool_agent_still_lists_simulations(tmp_path: Path) -> None:
    from agent.dashboard import TOOLS_SUMMARY, AgentView

    store = _tool_cycle(tmp_path, broken=True)
    runs = chronological(store.read_runs())
    tool = AgentView("tools", "Tool agent", TOOLS_SUMMARY, runs, [], [], None, "agents/tools/")
    html = render([], [], STAMP, tool_agent=tool, replay={"sessions": 0})
    assert "Two ways of using Timestral" in html and "Run it yourself" in html
    assert "0/1" in html  # the one decision was a fallback hold: 0 accepted out of 1
    empty = render([], [], STAMP, tool_agent=AgentView("tools", "Tool agent", "", [], [], [], None))
    assert "No runs yet" in empty


def test_the_account_value_follows_the_samples_taken_between_runs(tmp_path: Path) -> None:
    audit, site = tmp_path / "audit", tmp_path / "site"
    store = FileAuditStore(audit)
    cycle(audit)
    first = chronological(store.read_runs())[0]
    start = first["account"]["equity"]
    day = first["started_at"][:10]
    store.write_valuation("early", {"at": f"{day}T00:05:00+00:00", "equity": 1.0})  # before the run
    store.write_valuation("a", {"at": f"{day}T20:10:00+00:00", "equity": start + 1234.5})
    store.write_valuation("b", {"at": f"{day}T21:10:00+00:00", "equity": start + 2000.0})
    html = build(audit, site, STAMP).read_text(encoding="utf-8")
    assert f"${start + 2000:,.0f}" in html  # the hero and the card show the latest sample
    assert "latest sample, " in html
    assert "account sample" in html and "Account value over time" in html
    assert "$1 " not in html and "$1.00" not in html  # the sample before the first run is left out
    assert "sampled every hour of the US session" in html
    # Without samples the figures come from the latest run, as before.
    cycle(tmp_path / "audit2")
    plain = build(tmp_path / "audit2", tmp_path / "site2", STAMP)
    assert "account sample" not in plain.read_text(encoding="utf-8")
