"""All SQL for the EV topic: crawled-fact caches and the run audit log."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from agency_platform.db import get_db
from ev_research.params import params


def _norm(text: str) -> str:
    return text.strip().lower()


def _dec(value: float | Decimal | None) -> Decimal | None:
    """Store via a string-built Decimal: raw floats bake binary noise into DECIMAL."""
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.0001"))


def _fresh(fetched_at: datetime, days: int) -> bool:
    return datetime.now(UTC) - fetched_at <= timedelta(days=days)


async def get_cached_price(country_code: str, metric: str) -> dict[str, Any] | None:
    row = await get_db().fetchrow(
        "SELECT value, currency, source_url, fetched_at FROM country_level_info WHERE country_code = %s AND metric = %s",
        (country_code, metric),
    )
    if row is None or not _fresh(row["fetched_at"], params().country_data.staleness_days):
        return None
    return row


async def store_price(country_code: str, metric: str, value: float, currency: str, source_url: str | None) -> None:
    await get_db().execute(
        """INSERT INTO country_level_info (country_code, metric, value, currency, source_url, fetched_at)
           VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6)) AS new
           ON DUPLICATE KEY UPDATE
               value = new.value, currency = new.currency, source_url = new.source_url, fetched_at = new.fetched_at""",
        (country_code, metric, _dec(value), currency, source_url),
    )


_SPEC_COLS = ("battery_kwh", "range_km", "max_charging_kw", "efficiency_wh_km", "base_price")


async def get_cached_specs(make: str, model: str, year: int, trim: str = "") -> dict[str, Any] | None:
    row = await get_db().fetchrow(
        """SELECT battery_kwh, range_km, max_charging_kw, efficiency_wh_km, base_price, price_currency,
                  image_url, source_url, fetched_at
           FROM ev_model_specs WHERE make = %s AND model = %s AND year = %s AND `trim` = %s""",
        (_norm(make), _norm(model), year, _norm(trim)),
    )
    if row is None or not _fresh(row["fetched_at"], params().ev_specs.staleness_days):
        return None
    data = row
    for col in _SPEC_COLS:
        if data[col] is not None:
            data[col] = float(data[col])
    return data


async def store_specs(make: str, model: str, year: int, trim: str, data: dict[str, Any]) -> None:
    await get_db().execute(
        """INSERT INTO ev_model_specs (make, model, year, `trim`, battery_kwh, range_km, max_charging_kw,
               efficiency_wh_km, base_price, price_currency, image_url, source_url, fetched_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6)) AS new
           ON DUPLICATE KEY UPDATE
               battery_kwh = new.battery_kwh, range_km = new.range_km, max_charging_kw = new.max_charging_kw,
               efficiency_wh_km = new.efficiency_wh_km, base_price = new.base_price,
               price_currency = new.price_currency, image_url = new.image_url, source_url = new.source_url,
               fetched_at = new.fetched_at""",
        (_norm(make), _norm(model), year, _norm(trim),
         *[_dec(data.get(c)) for c in _SPEC_COLS],
         data.get("price_currency"), data.get("image_url"), data.get("source_url")),
    )


async def set_image_url(make: str, model: str, year: int, trim: str, image_url: str) -> None:
    """Remember a picture found by image search with the cached specs (next time: no search)."""
    await get_db().execute(
        """UPDATE ev_model_specs SET image_url = %s
           WHERE make = %s AND model = %s AND year = %s AND `trim` = %s AND image_url IS NULL""",
        (image_url, _norm(make), _norm(model), year, _norm(trim)),
    )


async def log_run(
    *, request_id: str, user_id: str, session_id: str, query: str, parsed_model: str | None,
    parsed_year: int | None, specs_source_url: str | None, tco_inputs: dict, tco_result: dict, final_output: str,
) -> None:
    await get_db().execute(
        """INSERT INTO ev_research_runs (request_id, user_id, session_id, query_text, parsed_model, parsed_year,
               specs_source_url, tco_inputs, tco_result, final_output)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (request_id, user_id, session_id, query, parsed_model, parsed_year,
         specs_source_url, tco_inputs, tco_result, final_output),
    )
