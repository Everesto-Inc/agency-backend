from datetime import UTC, date, datetime

import pytest

from agency_platform.errors import BadRequestError
from edge_gateway.dashboard import MonthRange, breakdown, change, fill_daily, month_options, pct_change, run_stats

TODAY = date(2026, 11, 22)


def test_month_range_current_and_past():
    cur = MonthRange.parse(None, TODAY)
    assert (cur.key, cur.label, cur.is_current) == ("2026-11", "November 2026", True)
    assert cur.last_day == TODAY and cur.period().days == 22  # current month: up to today
    past = MonthRange.parse("2026-02", TODAY)
    assert (past.start, past.next_start, past.last_day) == (date(2026, 2, 1), date(2026, 3, 1), date(2026, 2, 28))
    assert past.period().days == 28 and not past.is_current
    dec = MonthRange.parse("2025-12", TODAY)
    assert dec.next_start == date(2026, 1, 1) and dec.previous.key == "2025-11"
    assert MonthRange.parse("2026-01", TODAY).previous.key == "2025-12"  # year boundary
    assert dec.bounds() == (datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.parametrize("bad", ["2026-12", "2027-01", "2020-01"])
def test_month_range_rejects_future_and_too_old(bad):
    with pytest.raises(BadRequestError):
        MonthRange.parse(bad, TODAY)


def test_month_options_always_include_current_newest_first():
    opts = month_options(["2026-09", "2026-10"], TODAY)
    assert opts.current == "2026-11"
    assert [(o.month, o.has_data, o.is_current) for o in opts.months] == [
        ("2026-11", False, True), ("2026-10", True, False), ("2026-09", True, False)]


def _row(day, topic, tokens, cost, runs):
    return {"day": day, "topic": topic, "prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens,
            "cost_usd": cost, "llm_calls": 1, "runs": runs, "cached_runs": 0}


def test_daily_series_has_no_gaps_and_sums_topics():
    rows = [_row("2026-11-02", "finance", 100, 0.02, 1), _row("2026-11-02", "ev_research", 50, 0.01, 1)]
    points = fill_daily(rows, date(2026, 11, 1), date(2026, 11, 3))
    assert [p.day.day for p in points] == [1, 2, 3]
    assert points[0].total_tokens == 0 and points[0].by_topic == {}
    assert points[1].total_tokens == 150 and points[1].cost_usd == 0.03 and set(points[1].by_topic) == {"finance", "ev_research"}


def test_breakdown_sorted_by_cost_with_shares_and_display_names():
    def t(cost, runs):
        return {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_usd": cost, "llm_calls": 2,
                "runs": runs, "cached_runs": 0}
    items = breakdown({"ev_research": t(0.01, 2), "finance": t(0.03, 1), "routing": t(0.0, 0)})
    assert [i.topic for i in items] == ["finance", "ev_research", "routing"]
    assert items[0].share_of_cost == 0.75 and items[1].avg_cost_per_run == 0.005
    assert items[2].kind == "routing" and items[2].display_name == "Topic routing (/chat)" and items[2].avg_cost_per_run is None
    assert items[1].display_name != "ev_research"  # from config/topics.yaml


def test_change_and_run_stats():
    assert pct_change(150, 100) == 50.0 and pct_change(5, 0) is None
    c = change({"total_tokens": 200, "cost_usd": 1.0, "runs": 4}, {"total_tokens": 100, "cost_usd": 0.0, "runs": 4})
    assert (c.total_tokens_pct, c.cost_usd_pct, c.runs_pct) == (100.0, None, 0.0)
    stats = run_stats({"runs": 4, "successful": 3, "failed": 1, "cached": 1, "avg_latency_ms": 1234.56,
                       "positive_feedback": 2, "negative_feedback": 0})
    assert (stats.success_rate, stats.cache_hit_rate, stats.avg_latency_ms) == (0.75, 0.25, 1234.6)
    assert run_stats({}).success_rate is None
