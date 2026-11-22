"""Deterministic cost math -- no LLM involved, Decimal throughout."""
from decimal import Decimal

_CENT = Decimal("0.01")


def ev_energy_cost(annual_km: int, price_per_kwh: float, efficiency_wh_km: float) -> dict[str, float]:
    kwh = Decimal(str(efficiency_wh_km)) / Decimal(1000) * Decimal(annual_km)
    cost = kwh * Decimal(str(price_per_kwh))
    return {"annual_kwh": float(kwh.quantize(_CENT)), "annual_energy_cost": float(cost.quantize(_CENT))}


def fuel_cost(annual_km: int, price_per_liter: float, l_per_100km: float) -> dict[str, float]:
    liters = Decimal(str(l_per_100km)) / Decimal(100) * Decimal(annual_km)
    cost = liters * Decimal(str(price_per_liter))
    return {"annual_liters": float(liters.quantize(_CENT)), "annual_fuel_cost": float(cost.quantize(_CENT))}


def derived_efficiency(battery_kwh: float | None, range_km: float | None) -> float | None:
    """Wh/km from battery and range when the page didn't state efficiency (labelled as derived)."""
    if not battery_kwh or not range_km:
        return None
    return float((Decimal(str(battery_kwh)) * 1000 / Decimal(str(range_km))).quantize(Decimal("0.1")))
