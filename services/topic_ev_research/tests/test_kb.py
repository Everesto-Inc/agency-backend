"""EV knowledge base: official prices (Eurostat, Oil Bulletin), the Open EV Data catalogue, matching, data-first."""
import io
import zipfile
from datetime import date

import pytest

from agency_platform.errors import UpstreamError
from ev_research import kb

EUROSTAT = {   # JSON-stat 2.0, as the API returns it (geo × time)
    "id": ["freq", "siec", "nrg_cons", "unit", "tax", "currency", "geo", "time"],
    "size": [1, 1, 1, 1, 1, 1, 2, 2],
    "dimension": {"geo": {"category": {"index": {"DE": 0, "FR": 1}}},
                  "time": {"category": {"index": {"2025-S2": 0, "2026-S1": 1}}}},
    "value": {"0": 0.3869, "1": 0.3618, "2": 0.2561},          # FR 2026-S1 missing → falls back to 2025-S2
}


def test_eurostat_newest_period_with_a_value():
    rows = {r.country_code: r for r in kb.parse_eurostat(EUROSTAT, ["DE", "FR", "IT"], "u")}
    assert (rows["DE"].value, rows["DE"].period) == (0.3618, "2026-S1")
    assert (rows["FR"].value, rows["FR"].period) == (0.2561, "2025-S2")
    assert "IT" not in rows and rows["DE"].metric == "electricity_price_per_kwh" and "Eurostat" in rows["DE"].source


def xlsx(rows: list[list]) -> bytes:
    """A tiny real .xlsx (shared strings + numbers), like the Oil Bulletin's."""
    strings: list[str] = []
    cells = []
    for ri, row in enumerate(rows, 1):
        out = []
        for ci, v in enumerate(row):
            ref = f"{chr(65 + ci)}{ri}"
            if isinstance(v, str):
                strings.append(v)
                out.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            elif v is not None:
                out.append(f'<c r="{ref}"><v>{v}</v></c>')
        cells.append(f'<row r="{ri}">{"".join(out)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", "<sst>" + "".join(f"<si><t>{s.replace('&', '&amp;')}</t></si>" for s in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{''.join(cells)}</sheetData></worksheet>")
    return buf.getvalue()


def test_oil_bulletin_from_xlsx():
    serial = (date(2026, 11, 23) - date(1899, 12, 30)).days
    data = xlsx([
        ["in EUR", "Euro-super 95  (I)", "Gas oil automobile Automotive gas oil Dieselkraftstoff (I)", "Heating"],
        [serial, "1000 l", "1000 l", "1000 l"],
        ["Germany", 1964, 2125, 1500],
        ["France", 2002.02, 2098.3, 1623.56],
    ])
    rows = kb.parse_oil_bulletin(kb.xlsx_rows(data), {"DE": "Germany", "FR": "France"}, "u")
    got = {(r.country_code, r.metric): (r.value, r.period) for r in rows}
    assert got[("DE", "petrol_price_per_liter")] == (1.964, "2026-11-23")
    assert got[("FR", "diesel_price_per_liter")] == (2.0983, "2026-11-23")


def test_oil_bulletin_without_its_header_is_an_error_not_bad_data():
    with pytest.raises(ValueError):
        kb.parse_oil_bulletin([["something else"], ["Germany", 1, 2]], {"DE": "Germany"}, "u")


OED = {"data": [
    {"id": "1", "brand": "Volkswagen", "model": "ID.3", "variant": "Pure", "release_year": 2024, "vehicle_type": "car",
     "usable_battery_size": 52, "energy_consumption": {"average_consumption": 16.3}, "dc_charger": {"max_power": 145},
     "ac_charger": {"max_power": 11}},
    {"id": "2", "brand": "Volkswagen", "model": "ID.3", "variant": "Pro S", "release_year": 2024, "vehicle_type": "car",
     "usable_battery_size": 77, "energy_consumption": {"average_consumption": 16.0}, "dc_charger": {"max_power": 175}},
    {"id": "3", "brand": "Volkswagen", "model": "ID.3", "variant": "GTX", "release_year": 2024, "vehicle_type": "car",
     "usable_battery_size": 79, "energy_consumption": {"average_consumption": 17.0}, "dc_charger": {"max_power": 185}},
    {"id": "4", "brand": "Opel / Vauxhall", "model": "Corsa-e", "variant": "", "release_year": 2021, "vehicle_type": "car",
     "usable_battery_size": 46.3, "energy_consumption": {"average_consumption": 15.7}},
    {"id": "5", "brand": "Mercedes", "model": "EQA", "variant": "250+", "release_year": 2023, "vehicle_type": "car",
     "usable_battery_size": 70.5, "energy_consumption": {"average_consumption": 17.0}},
    {"id": "6", "brand": "Zero Motorcycles", "model": "SR/F", "vehicle_type": "motorbike", "usable_battery_size": 15},
    {"id": "7", "brand": "Kia", "model": "EV6", "variant": "x", "vehicle_type": "car"},               # no battery → skipped
]}


def test_open_ev_data_rows_with_estimated_range():
    rows = {r["source_id"]: r for r in kb.parse_open_ev_data(OED)}
    assert set(rows) == {"1", "2", "3", "4", "5"}                                            # cars with a battery only
    assert rows["1"]["est_range_km"] == round(52 / 16.3 * 100) and rows["1"]["dc_max_kw"] == 145


@pytest.mark.parametrize("make,model,trim,expected", [
    ("VW", "ID.3", "", "Volkswagen ID.3 Pro S (2024)"),          # no version named → the middle battery
    ("VW", "ID.3", "GTX", "Volkswagen ID.3 GTX (2024)"),
    ("Opel", "Corsa Electric", "", "Opel / Vauxhall Corsa-e (2021)"),
    ("Mercedes-Benz", "EQA", "", "Mercedes EQA 250+ (2023)"),
    ("Tesla", "Model 3", "", None),                             # not in the data → web search later
])
def test_matching_a_question_to_the_catalogue(make, model, trim, expected):
    m = kb.match_catalog(kb.parse_open_ev_data(OED), make, model, 2024, trim)
    assert (kb.catalog_specs(m, 2024)["matched_vehicle"] if m else None) == expected


def test_catalog_specs_say_where_numbers_come_from():
    m = kb.match_catalog(kb.parse_open_ev_data(OED), "Opel", "Corsa-e", 2024)
    s = kb.catalog_specs(m, 2024)
    assert s["source"].startswith("Open EV Data") and s["range_estimated"] and s["battery_is_usable_capacity"]
    assert s["model_year_note"] == "closest model year in our data: 2021" and s["efficiency_wh_km"] == 157.0


async def test_specs_data_first_cache_then_catalogue_then_web(monkeypatch):
    from ev_research import repository as repo
    from ev_research import sources as src
    web_calls = []

    async def no_cache(*a):
        return None

    async def catalog():
        return kb.parse_open_ev_data(OED)

    class Search:
        async def search(self, q, max_results):
            web_calls.append(q)
            return []
    monkeypatch.setattr(repo, "get_cached_specs", no_cache)
    monkeypatch.setattr(repo, "catalog_rows", catalog)
    fs = src.FactSources(crawler=None, search=Search())
    specs = await fs.ev_specs("VW", "ID.3", 2024, "GTX", ctx=None)
    assert specs["from_catalog"] and specs["battery_kwh"] == 79 and web_calls == []        # no web at all
    with pytest.raises(UpstreamError):                                                      # not in the data → web
        await fs.ev_specs("Tesla", "Model 3", 2024, "", ctx=None)
    assert web_calls and "Model 3" in web_calls[0]
