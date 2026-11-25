"""EV knowledge base: official / open data loaded ahead of time, so most questions need no web search.

    python -m ev_research.kb            refresh everything (CronJob ev-kb-refresh, weekly; also on first start)

| Data                                   | Source                                   | Table               |
|----------------------------------------|------------------------------------------|---------------------|
| household electricity price, all taxes | Eurostat nrg_pc_204 (twice a year)       | country_level_info  |
| petrol 95 + diesel price, all taxes    | EU Weekly Oil Bulletin (weekly)          | country_level_info  |
| EV specs: usable battery, consumption, | Open EV Data — MIT + attribution         | ev_catalog          |
| AC/DC charging power, charging curve   | (github.com/KilowattApp/open-ev-data)    |                     |

The EV Helper looks here FIRST (graph → sources: verified cache → this → web search as the last resort).
Parsers are pure (bytes/JSON in, rows out) and tested; a source that fails keeps its last good data.
"""
import asyncio
import io
import json
import logging
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EUROSTAT = "Eurostat · household electricity prices (nrg_pc_204), all taxes"
OIL_BULLETIN = "EU Weekly Oil Bulletin · prices with taxes"
OPEN_EV_DATA = "Open EV Data (github.com/KilowattApp/open-ev-data)"
OPEN_EV_DATA_URL = "https://github.com/KilowattApp/open-ev-data"


@dataclass(frozen=True)
class PriceRow:
    country_code: str
    metric: str          # electricity_price_per_kwh | petrol_price_per_liter | diesel_price_per_liter
    value: float
    currency: str
    period: str          # 2026-S1 · 2026-11-23
    source: str
    source_url: str


# ---------------------------------------------------------------- Eurostat (JSON-stat 2.0)
def parse_eurostat(doc: dict[str, Any], countries: list[str], url: str) -> list[PriceRow]:
    """Pure: the newest period that has a value, per country."""
    ids, sizes = doc["id"], doc["size"]
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    geo = doc["dimension"]["geo"]["category"]["index"]
    time = sorted(doc["dimension"]["time"]["category"]["index"].items(), key=lambda kv: kv[1], reverse=True)
    values = doc.get("value", {})
    gi, ti = ids.index("geo"), ids.index("time")
    out = []
    for cc in countries:
        if cc not in geo:
            continue
        for period, t in time:                      # newest first
            v = values.get(str(geo[cc] * strides[gi] + t * strides[ti]))
            if v is not None:
                out.append(PriceRow(cc, "electricity_price_per_kwh", round(float(v), 4), "EUR", period, f"{EUROSTAT} · {period}", url))
                break
    return out


# ---------------------------------------------------------------- Oil Bulletin (xlsx, read without a library)
_CELL = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
_VALUE = re.compile(r"<v>(.*?)</v>", re.S)
_TEXT = re.compile(r"<t[^>]*>(.*?)</t>", re.S)
_SI = re.compile(r"<si>(.*?)</si>", re.S)


def _xml_text(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def xlsx_rows(data: bytes, max_bytes: int = 20_000_000) -> list[list[Any]]:
    """Pure: the first sheet of an .xlsx as rows of values (shared strings resolved). Enough for simple
    statistical sheets; no formulas, no styles. Regex instead of an XML parser: no entity expansion at all."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if sum(i.file_size for i in z.infolist()) > max_bytes:
            raise ValueError("xlsx too large")
        shared = [_xml_text("".join(_TEXT.findall(si))) for si in _SI.findall(z.read("xl/sharedStrings.xml").decode())] \
            if "xl/sharedStrings.xml" in names else []
        sheet = next(n for n in sorted(names) if n.startswith("xl/worksheets/sheet"))
        xml = z.read(sheet).decode()
    rows: dict[int, dict[int, Any]] = {}
    for col, row, attrs, inner in _CELL.findall(xml):
        inner = inner or ""
        ci = 0
        for ch in col:
            ci = ci * 26 + (ord(ch) - 64)
        if 't="s"' in attrs:
            m = _VALUE.search(inner)
            value: Any = shared[int(m.group(1))] if m else None
        elif 't="inlineStr"' in attrs or 't="str"' in attrs:
            value = _xml_text("".join(_TEXT.findall(inner))) or (_VALUE.search(inner).group(1) if _VALUE.search(inner) else None)
        else:
            m = _VALUE.search(inner)
            value = float(m.group(1)) if m else None
        rows.setdefault(int(row), {})[ci - 1] = value
    return [[cells.get(i) for i in range(max(cells) + 1)] for _, cells in sorted(rows.items()) if cells]


def parse_oil_bulletin(rows: list[list[Any]], names: dict[str, str], url: str) -> list[PriceRow]:
    """Pure: Euro-super 95 + diesel per country, € per litre (the sheet says € per 1000 l)."""
    header = next((r for r in rows if r and isinstance(r[0], str) and "EUR" in r[0].upper()), None)
    if header is None:
        raise ValueError("Oil Bulletin: header row not found")
    col_petrol = next(i for i, h in enumerate(header) if isinstance(h, str) and "super 95" in h.lower())
    col_diesel = next(i for i, h in enumerate(header) if isinstance(h, str) and "automotive gas oil" in h.lower())
    serial = next((r[0] for r in rows if r and isinstance(r[0], float)), None)
    period = (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat() if serial else "latest"
    by_name = {r[0].strip().lower(): r for r in rows if r and isinstance(r[0], str)}
    out = []
    for cc, name in names.items():
        r = by_name.get(name.lower())
        if r is None:
            continue
        for metric, col in (("petrol_price_per_liter", col_petrol), ("diesel_price_per_liter", col_diesel)):
            v = r[col] if col < len(r) else None
            if isinstance(v, int | float) and v > 0:
                out.append(PriceRow(cc, metric, round(v / 1000, 4), "EUR", period, f"{OIL_BULLETIN} · week of {period}", url))
    return out


# ---------------------------------------------------------------- Open EV Data
def parse_open_ev_data(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: cars with a usable battery → catalogue rows (range estimated = battery ÷ consumption)."""
    out = []
    for r in doc.get("data", []):
        if r.get("vehicle_type", "car") != "car":
            continue
        battery = r.get("usable_battery_size")
        cons = (r.get("energy_consumption") or {}).get("average_consumption")   # kWh / 100 km
        if not battery:
            continue
        dc = r.get("dc_charger") or {}
        out.append({
            "source_id": str(r["id"]), "make": r["brand"].strip(), "model": r["model"].strip(),
            "variant": (r.get("variant") or "").strip(), "release_year": r.get("release_year"),
            "usable_battery_kwh": float(battery),
            "consumption_kwh_100km": float(cons) if cons else None,
            "est_range_km": round(float(battery) / float(cons) * 100) if cons else None,
            "dc_max_kw": dc.get("max_power"), "ac_max_kw": (r.get("ac_charger") or {}).get("max_power"),
            "charging_curve": dc.get("charging_curve") or [],
        })
    return out


# ---------------------------------------------------------------- matching a question's car to the catalogue
_ALIASES = {"vw": "volkswagen", "mercedesbenz": "mercedes", "merc": "mercedes", "chevy": "chevrolet",
            "opel": "opelvauxhall", "vauxhall": "opelvauxhall", "landrover": "landrover", "skodaauto": "skoda"}


def _compact(s: str) -> str:
    """'Citroën' → 'citroen' · 'Mercedes-Benz' → 'mercedesbenz'"""
    plain = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", plain.lower())


def brand_key(make: str) -> str:
    c = _compact(make)
    return _ALIASES.get(c, c)


_NOISE = {"electric", "elektro", "electrique", "ev", "bev"}   # "Corsa Electric" = "Corsa-e"


def _tokens(s: str) -> list[str]:
    """'ID.4 Pro' → ['id4', 'pro'] · 'e-tron GT' → ['etron', 'gt'] · 'Model 3' → ['model', '3']"""
    return [t for t in (_compact(w) for w in re.split(r"\s+", s.replace("-", "").replace(".", "")))
            if t and t not in _NOISE]


def _token_in(w: str, have: list[str]) -> bool:
    """'corsa' ~ 'corsae' · '208' ~ 'e208' (the e- / -e naming of electric versions)."""
    return w in have or f"e{w}" in have or f"{w}e" in have or (w.startswith("e") and w[1:] in have)


def match_catalog(rows: list[dict[str, Any]], make: str, model: str, year: int | None, trim: str = "") -> dict[str, Any] | None:
    """Pure: best catalogue row for the asked car, + the other variants of that model. None if the model isn't there.

    Every token of the asked model must be in the row's model + variant; then: trim words matched, model year
    closest (not newer than asked if possible), fewest extra words (the base version)."""
    brand = brand_key(make)
    want = _tokens(model)
    if not want:
        return None
    trim_t = set(_tokens(trim))
    cands = []
    for r in rows:
        if brand_key(r["make"]) != brand:
            continue
        have = _tokens(f"{r['model']} {r['variant']}")
        if not all(_token_in(w, have) for w in want):
            continue
        ry = r.get("release_year") or 0
        year_gap = abs((year or ry) - ry) + (0.5 if year and ry > year else 0)
        extra = len(set(have) - set(want) - trim_t)
        cands.append(((-len(trim_t & set(have)), year_gap, extra, -(r["usable_battery_kwh"] or 0)), r))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    # equally good matches (no version named): take the typical one — the middle battery size
    top = [r for k, r in cands if k[:2] == cands[0][0][:2]]          # same trim match + same model year
    top.sort(key=lambda r: r["usable_battery_kwh"] or 0)
    best = top[(len(top) - 1) // 2]
    cands = [c for c in cands if c[1] is not best]
    cands.insert(0, (None, best))
    variants = sorted({f"{r['model']} {r['variant']}".strip() + (f" ({r['release_year']})" if r.get("release_year") else "")
                       for _, r in cands[1:8]})
    return {**best, "variants": variants}


def catalog_specs(row: dict[str, Any], asked_year: int | None) -> dict[str, Any]:
    """Catalogue row → the specs shape the EV graph uses (with where each number comes from)."""
    name = f"{row['make']} {row['model']} {row['variant']}".strip()
    year = row.get("release_year")
    return {
        "battery_kwh": row["usable_battery_kwh"],
        "range_km": row.get("est_range_km"),
        "max_charging_kw": row.get("dc_max_kw"),
        "efficiency_wh_km": round(row["consumption_kwh_100km"] * 10, 1) if row.get("consumption_kwh_100km") else None,
        "base_price": None, "price_currency": None, "image_url": None,
        "source_url": OPEN_EV_DATA_URL,
        "source": OPEN_EV_DATA,
        "matched_vehicle": f"{name} ({year})" if year else name,
        "battery_is_usable_capacity": True,
        "range_estimated": row.get("est_range_km") is not None,   # battery ÷ average consumption, not WLTP
        "model_year_note": (f"closest model year in our data: {year}" if asked_year and year and year != asked_year else None),
        "other_variants": row.get("variants") or [],
        "from_cache": True, "from_catalog": True,
    }


# ---------------------------------------------------------------- refresh
async def _get(http: httpx.AsyncClient, url: str, **params: Any) -> httpx.Response:
    r = await http.get(url, params=params or None, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r


async def refresh(http: httpx.AsyncClient, cfg, names: dict[str, str]) -> dict[str, Any]:
    """Fetch every source; store what worked. Returns a summary (also logged)."""
    from ev_research import repository as repo
    summary: dict[str, Any] = {}

    async def electricity():
        params = {"format": "JSON", "lang": "EN", "nrg_cons": "KWH2500-4999", "unit": "KWH", "tax": "I_TAX",
                  "currency": "EUR", "lastTimePeriod": "4"}
        r = await http.get(cfg.eurostat_url, params=[*params.items(), *(("geo", c) for c in cfg.countries)],
                           timeout=60, follow_redirects=True)
        r.raise_for_status()
        return parse_eurostat(r.json(), cfg.countries, cfg.eurostat_url)

    async def fuel():
        r = await _get(http, cfg.oil_bulletin_url)
        return parse_oil_bulletin(xlsx_rows(r.content), {c: names[c] for c in cfg.countries}, cfg.oil_bulletin_url)

    async def catalog():
        r = await _get(http, cfg.open_ev_data_url)
        return parse_open_ev_data(json.loads(r.content))

    results = await asyncio.gather(electricity(), fuel(), catalog(), return_exceptions=True)
    for name, res in zip(("electricity", "fuel"), results[:2], strict=True):
        if isinstance(res, BaseException):
            logger.error("kb source failed; keeping last data", extra={"source": name, "error": repr(res)[:300]})
            summary[name] = f"failed: {type(res).__name__}"
            continue
        for row in res:
            await repo.store_official_price(row)
        summary[name] = {f"{r.country_code}.{r.metric}": f"{r.value} {r.currency} ({r.period})" for r in res}
    cat = results[2]
    if isinstance(cat, BaseException):
        logger.error("kb source failed; keeping last data", extra={"source": "catalog", "error": repr(cat)[:300]})
        summary["catalog"] = f"failed: {type(cat).__name__}"
    else:
        summary["catalog"] = await repo.replace_catalog("open_ev_data", cat)
    summary["at"] = datetime.now(UTC).isoformat()
    logger.info("ev knowledge base refreshed", extra={"summary": json.dumps(summary)[:2000]})
    return summary


def country_names(countries: list[str], overrides: dict[str, str]) -> dict[str, str]:
    """Country code → the name the Oil Bulletin uses."""
    import pycountry
    out = {}
    for c in countries:
        country = pycountry.countries.get(alpha_2=c)
        out[c] = overrides.get(c) or getattr(country, "common_name", None) or (country.name if country else c)
    return out


async def main() -> int:
    from agency_platform.db import close_db, init_db
    from agency_platform.observability.logging import configure_logging
    from ev_research.params import params, settings
    s = settings()
    configure_logging("ev-kb-refresh", s.log_level, s.log_json)
    cfg = params().kb
    await init_db(s)
    try:
        async with httpx.AsyncClient(headers={"user-agent": "agency-ev-kb/1.0"}) as http:
            summary = await refresh(http, cfg, country_names(cfg.countries, cfg.country_names))
    finally:
        await close_db()
    print(json.dumps(summary, indent=2))
    failed = [k for k, v in summary.items() if isinstance(v, str) and v.startswith("failed")]
    return 1 if len(failed) == 3 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
