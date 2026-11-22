"""Dashboard assembly: turns repository rows into frontend-ready monthly views.

The pure helpers (MonthRange, fill_daily, breakdown, change, run_stats) hold
all the shaping logic and are unit-tested without a database; the async
builders only fetch and compose.
"""
import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from agency_platform.auth.accounts import Account
from agency_platform.billing.usage import UsageMeter
from agency_platform.config.registry import plans, topics
from agency_platform.db import Database
from agency_platform.errors import BadRequestError
from agency_platform.repositories import AdminMetrics, RunRepository, UsageRepository
from agency_platform.repositories.usage import TOTAL_KEYS, total
from edge_gateway import schemas as s
from edge_gateway.usage_service import live_today

PSEUDO_TOPICS = {"routing": "Topic routing (/chat)", "evals": "Quality evaluations"}
_HISTORY_MONTHS = 36
_ZERO = dict.fromkeys(TOTAL_KEYS, 0)


# ------------------------------------------------------------------ periods
@dataclass(frozen=True)
class MonthRange:
    year: int
    month: int
    today: date

    @classmethod
    def parse(cls, value: str | None, today: date | None = None) -> "MonthRange":
        today = today or datetime.now(UTC).date()
        if not value:
            return cls(today.year, today.month, today)
        year, month = (int(x) for x in value.split("-"))
        rng = cls(year, month, today)
        if rng.start > today:
            raise BadRequestError(f"Month {value} is in the future")
        if (today.year - year) * 12 + today.month - month >= _HISTORY_MONTHS:
            raise BadRequestError(f"Only the last {_HISTORY_MONTHS} months are available")
        return rng

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{calendar.month_name[self.month]} {self.year}"

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def next_start(self) -> date:
        return date(self.year + self.month // 12, self.month % 12 + 1, 1)

    @property
    def is_current(self) -> bool:
        return (self.year, self.month) == (self.today.year, self.today.month)

    @property
    def last_day(self) -> date:
        """Last day covered: today for the current month."""
        return self.today if self.is_current else self.next_start - timedelta(days=1)

    @property
    def previous(self) -> "MonthRange":
        prev_last = self.start - timedelta(days=1)
        return MonthRange(prev_last.year, prev_last.month, self.today)

    def bounds(self) -> tuple[datetime, datetime]:
        """[start, end) as UTC datetimes, for timestamp columns."""
        return (datetime.combine(self.start, time.min, tzinfo=UTC),
                datetime.combine(self.next_start, time.min, tzinfo=UTC))

    def period(self) -> s.Period:
        return s.Period(month=self.key, label=self.label, start=self.start, end=self.last_day,
                        days=(self.last_day - self.start).days + 1, is_current=self.is_current)


def month_options(with_data: list[str], today: date | None = None) -> s.MonthsResponse:
    current = MonthRange.parse(None, today)
    keys = sorted(set(with_data) | {current.key}, reverse=True)
    options = []
    for key in keys:
        try:
            m = MonthRange.parse(key, current.today)
        except BadRequestError:
            continue
        options.append(s.MonthOption(month=key, label=m.label, is_current=m.is_current, has_data=key in with_data))
    return s.MonthsResponse(current=current.key, months=options)


# ------------------------------------------------------------------ shaping
def display_name(topic: str) -> str:
    if topic in PSEUDO_TOPICS:
        return PSEUDO_TOPICS[topic]
    cfg = topics().topics.get(topic)
    return cfg.display_name if cfg else topic.replace("_", " ").capitalize()


def _kind(topic: str) -> str:
    return topic if topic in PSEUDO_TOPICS else "assistant"


def pct_change(current: float, previous: float) -> float | None:
    return round((current - previous) / previous * 100, 1) if previous else None


def change(cur: dict, prev: dict) -> s.Change:
    return s.Change(
        total_tokens_pct=pct_change(cur["total_tokens"], prev["total_tokens"]),
        cost_usd_pct=pct_change(cur["cost_usd"], prev["cost_usd"]),
        runs_pct=pct_change(cur["runs"], prev["runs"]),
    )


def breakdown(per_topic: dict[str, dict]) -> list[s.TopicBreakdown]:
    grand = sum(v["cost_usd"] for v in per_topic.values())
    items = [
        s.TopicBreakdown(
            topic=t, display_name=display_name(t), kind=_kind(t), totals=s.UsageTotals(**v),
            share_of_cost=round(v["cost_usd"] / grand, 4) if grand else 0.0,
            avg_cost_per_run=round(v["cost_usd"] / v["runs"], 6) if v["runs"] else None,
        )
        for t, v in per_topic.items()
    ]
    return sorted(items, key=lambda b: (-b.totals.cost_usd, b.topic))


def fill_daily(rows: list[dict], first: date, last: date) -> list[s.DailyPoint]:
    """One point per day from first to last (inclusive), zeros where nothing happened."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    points = []
    day = first
    while day <= last:
        rs = by_day.get(day.isoformat(), [])
        points.append(s.DailyPoint(
            day=day,
            total_tokens=sum(r["total_tokens"] for r in rs),
            cost_usd=round(sum(r["cost_usd"] for r in rs), 6),
            runs=sum(r["runs"] for r in rs),
            llm_calls=sum(r["llm_calls"] for r in rs),
            by_topic={r["topic"]: s.DailyTopicPoint(total_tokens=r["total_tokens"], cost_usd=r["cost_usd"], runs=r["runs"])
                      for r in rs},
        ))
        day += timedelta(days=1)
    return points


def run_stats(row: dict[str, Any]) -> s.RunStats:
    runs = int(row.get("runs") or 0)
    ok, failed, cached = (int(row.get(k) or 0) for k in ("successful", "failed", "cached"))
    latency = row.get("avg_latency_ms")
    return s.RunStats(
        runs=runs, successful=ok, failed=failed,
        success_rate=round(ok / runs, 4) if runs else None,
        cache_hit_rate=round(cached / runs, 4) if runs else None,
        avg_latency_ms=round(float(latency), 1) if latency is not None else None,
        positive_feedback=int(row.get("positive_feedback") or 0),
        negative_feedback=int(row.get("negative_feedback") or 0),
    )


def _totals(per_topic: dict[str, dict]) -> dict:
    return total(per_topic) if per_topic else dict(_ZERO)


# ------------------------------------------------------------------ builders
async def months_for(db: Database, user_id: str | None) -> s.MonthsResponse:
    return month_options(await UsageRepository(db).months(user_id, limit=_HISTORY_MONTHS))


async def user_dashboard(db: Database, meter: UsageMeter, account: Account, month: str | None) -> s.UserDashboard:
    m = MonthRange.parse(month)
    usage, runs = UsageRepository(db), RunRepository(db)
    cur = await usage.by_topic(from_day=m.start, to_day=m.next_start, user_id=account.id)
    prev = await usage.by_topic(from_day=m.previous.start, to_day=m.previous.next_start, user_id=account.id)
    daily = await usage.daily_series(from_day=m.start, to_day=m.next_start, user_id=account.id)
    start, end = m.bounds()
    recent = await runs.list_for_user(account.id, topic=None, limit=10, start=start, end=end)
    cur_t, prev_t = _totals(cur), _totals(prev)
    plan = account.effective_plan
    return s.UserDashboard(
        period=m.period(),
        account=s.AccountRef(id=account.id, email=account.email, plan=plan),
        totals=s.UsageTotals(**cur_t),
        previous_month=s.UsageTotals(**prev_t),
        change=change(cur_t, prev_t),
        by_topic=breakdown(cur),
        daily=fill_daily(daily, m.start, m.last_day),
        runs=run_stats(await runs.stats(start=start, end=end, user_id=account.id)),
        recent_runs=[
            s.RunSummary(**r, total_tokens=r["prompt_tokens"] + r["completion_tokens"])
            for r in recent
        ],
        today=await live_today(meter, account.id, plans().get(plan)) if m.is_current else None,
    )


async def admin_dashboard(db: Database, month: str | None) -> s.AdminDashboard:
    m = MonthRange.parse(month)
    usage, metrics = UsageRepository(db), AdminMetrics(db)
    cur = await usage.by_topic(from_day=m.start, to_day=m.next_start)
    prev = await usage.by_topic(from_day=m.previous.start, to_day=m.previous.next_start)
    daily = await usage.daily_series(from_day=m.start, to_day=m.next_start)
    start, end = m.bounds()
    models = await metrics.models_between(start, end)
    model_cost = sum(x["cost_usd"] for x in models)
    counts = await metrics.user_counts(m.start, m.next_start)
    cur_t, prev_t = _totals(cur), _totals(prev)
    return s.AdminDashboard(
        period=m.period(),
        totals=s.UsageTotals(**cur_t),
        previous_month=s.UsageTotals(**prev_t),
        change=change(cur_t, prev_t),
        by_topic=breakdown(cur),
        by_model=[s.ModelUsage(**x, share_of_cost=round(x["cost_usd"] / model_cost, 4) if model_cost else 0.0)
                  for x in models],
        daily=fill_daily(daily, m.start, m.last_day),
        users=s.UserCounts(**counts, active=await usage.active_users(from_day=m.start, to_day=m.next_start)),
        runs=run_stats(await RunRepository(db).stats(start=start, end=end)),
        top_users=await usage.top_users(from_day=m.start, to_day=m.next_start, limit=10),
    )
