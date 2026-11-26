"""All SQL for the EV topic: crawled-fact caches and the run audit log."""
import json
import time
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
    """Official rows (knowledge base) stay valid longer than ones crawled from the web."""
    row = await get_db().fetchrow(
        """SELECT value, currency, source_url, source, period, official, fetched_at
           FROM country_level_info WHERE country_code = %s AND metric = %s""",
        (country_code, metric),
    )
    p = params()
    days = p.kb.official_staleness_days if row and row["official"] else p.country_data.staleness_days
    if row is None or not _fresh(row["fetched_at"], days):
        return None
    row["official"] = bool(row["official"])
    return row


async def store_official_price(row) -> None:
    """Knowledge base: an official price (Eurostat, Oil Bulletin) replaces whatever was cached."""
    await get_db().execute(
        """INSERT INTO country_level_info (country_code, metric, value, currency, source_url, source, period, official,
                                          fetched_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, 1, UTC_TIMESTAMP(6)) AS new
           ON DUPLICATE KEY UPDATE value = new.value, currency = new.currency, source_url = new.source_url,
               source = new.source, period = new.period, official = 1, fetched_at = new.fetched_at""",
        (row.country_code, row.metric, _dec(row.value), row.currency, row.source_url, row.source[:160], row.period),
    )


async def replace_catalog(source: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Upsert the catalogue from one source; rows the source dropped are removed."""
    db = get_db()
    ids = []
    for r in rows:
        ids.append(r["source_id"])
        await db.execute(
            """INSERT INTO ev_catalog (source, source_id, make, model, variant, release_year, usable_battery_kwh,
                   consumption_kwh_100km, est_range_km, dc_max_kw, ac_max_kw, charging_curve, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6)) AS new
               ON DUPLICATE KEY UPDATE make = new.make, model = new.model, variant = new.variant,
                   release_year = new.release_year, usable_battery_kwh = new.usable_battery_kwh,
                   consumption_kwh_100km = new.consumption_kwh_100km, est_range_km = new.est_range_km,
                   dc_max_kw = new.dc_max_kw, ac_max_kw = new.ac_max_kw, charging_curve = new.charging_curve,
                   updated_at = new.updated_at""",
            (source, r["source_id"], r["make"][:100], r["model"][:150], r["variant"][:150], r["release_year"],
             _dec(r["usable_battery_kwh"]), _dec(r["consumption_kwh_100km"]), r["est_range_km"],
             _dec(r["dc_max_kw"]), _dec(r["ac_max_kw"]), json.dumps(r["charging_curve"])),
        )
    removed = 0
    if ids:
        existing = await db.fetch("SELECT source_id FROM ev_catalog WHERE source = %s", (source,))
        keep = set(ids)
        for row in existing:
            if row["source_id"] not in keep:
                removed += await db.execute("DELETE FROM ev_catalog WHERE source = %s AND source_id = %s",
                                            (source, row["source_id"]))
    return {"vehicles": len(rows), "removed": removed}


_CATALOG: dict[str, Any] = {"at": 0.0, "rows": []}
_CATALOG_TTL_S = 600


async def catalog_rows() -> list[dict[str, Any]]:
    """The whole catalogue (~1,300 rows), kept in memory 10 min — no database query per question."""
    if time.monotonic() - _CATALOG["at"] > _CATALOG_TTL_S or not _CATALOG["rows"]:
        rows = await get_db().fetch(
            """SELECT make, model, variant, release_year, usable_battery_kwh, consumption_kwh_100km, est_range_km,
                      dc_max_kw, ac_max_kw, charging_curve FROM ev_catalog""")
        for r in rows:
            curve = r.get("charging_curve")
            r["charging_curve"] = json.loads(curve) if isinstance(curve, str | bytes) else (curve or [])
            for k in ("usable_battery_kwh", "consumption_kwh_100km", "dc_max_kw", "ac_max_kw"):
                r[k] = float(r[k]) if r[k] is not None else None
        _CATALOG.update(at=time.monotonic(), rows=rows)
    return _CATALOG["rows"]


async def kb_status() -> dict[str, Any]:
    """Is the knowledge base loaded? (startup: refresh in the background if not)"""
    db = get_db()
    return {"catalog": int(await db.fetchval("SELECT COUNT(*) FROM ev_catalog") or 0),
            "official_prices": int(await db.fetchval("SELECT COUNT(*) FROM country_level_info WHERE official = 1") or 0)}


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
