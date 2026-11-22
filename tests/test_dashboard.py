"""Dashboard (D-012): rendering from audit records, edge states, escaping, self-containment."""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime
from pathlib import Path

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
        "Portfolio value",
        "Target allocation",
        "Timestral forecasts, 5-day cumulative return",
        "Latest decision",
        "All runs",
        "Executions",
        "No orders have been reconciled yet",
        "dry run",
        "accepted",
        "fake",  # the model id reported by the (fake) LLM
        "decide_v1",
    ):
        assert section in html, section
    assert "n/a" in html  # no slippage yet
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
    assert "Rejection reason: invalid_json" in html
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
    assert "+10.0 bps" in html and "vs reference price" in html
    assert "14/15 filled" in html
    assert "expired" in html and "+10.0" in html
    assert "No orders have been reconciled yet" not in html


def test_slippage_tile_without_any_filled_order(tmp_path: Path) -> None:
    store, broker = live_run(tmp_path)
    states = {o["id"]: OrderState("expired", 0.0, None, None, None) for o in broker.submitted}
    reconcile_runs(store, Lookup(states), LATER)
    html = page(store)
    assert "no fills reconciled yet" in html and "0/15 filled" in html


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
    assert html.count('role="img"') == 2
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
    assert "Target allocation" in html and not (site / "data").exists()
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
