"""Deterministic grounding checks: is an LLM-extracted number really on the page?

LLM extraction can hallucinate or be steered by injected page text. Before an
extracted figure is cached or shown, we require it to literally appear in the
source text (allowing common formatting: 1,299.00 / 1.299,00 / 1 299 / 77.4 / 77,4).
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_SPACE = "[\\s  ]"


def _variants(value: float) -> set[str]:
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return set()
    out: set[str] = set()
    for places in (0, 1, 2):
        q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if places and q != d.quantize(Decimal(1).scaleb(-places)) and q.normalize() != d.normalize():
            continue
        if places == 0 and q != d:
            continue  # don't let 77.4 match "77"
        int_part, _, frac = f"{q:f}".partition(".")
        digits = int_part.lstrip("-")
        groups = []
        while digits:
            groups.insert(0, digits[-3:])
            digits = digits[:-3]
        for sep in ("", ",", ".", " "):
            grouped = sep.join(groups) if len(groups) > 1 else (groups[0] if groups else "0")
            for dec in (".", ","):
                if dec == sep and len(groups) > 1:
                    continue
                out.add(grouped + (dec + frac if frac else ""))
    return out


_CURRENCY_SYMBOLS: dict[str, list[str]] = {
    "USD": ["$", "US$", "USD"], "EUR": ["€", "EUR"], "GBP": ["£", "GBP"], "INR": ["₹", "Rs.", "Rs", "INR"],
    "NPR": ["रू", "Rs.", "Rs", "NPR"], "JPY": ["¥", "円", "JPY"], "CNY": ["¥", "元", "CNY", "RMB"],
    "CAD": ["$", "CA$", "C$", "CAD"], "AUD": ["$", "A$", "AU$", "AUD"], "CHF": ["CHF", "Fr."],
    "SEK": ["kr", "SEK"], "NOK": ["kr", "NOK"], "DKK": ["kr", "DKK"], "PLN": ["zł", "PLN"],
}


def money_appears(value: float | int | None, currency: str | None, text: str) -> bool:
    """Stricter than number_appears: the amount must sit next to its currency
    marker ("$349.99", "329,00 €", "USD 1,299"). Stops trivial matches like a
    stray "1" in injected text from 'verifying' a fabricated price."""
    if value is None or not currency:
        return False
    markers = [*_CURRENCY_SYMBOLS.get(currency.upper(), []), currency.upper()]
    marker_re = "(?:" + "|".join(re.escape(m) for m in sorted(set(markers), key=len, reverse=True)) + ")"
    normalised = re.sub(_SPACE, " ", text)
    for v in _variants(float(value)):
        num = r"(?<![\d.,])" + re.escape(v).replace(r"\ ", " ") + r"(?![\d]|[.,]\d)"
        if re.search(marker_re + r" ?" + num, normalised) or re.search(num + r" ?" + marker_re, normalised):
            return True
    return False


def number_appears(value: float | int | None, text: str) -> bool:
    if value is None:
        return False
    normalised = re.sub(_SPACE, " ", text)
    for v in _variants(float(value)):
        pattern = r"(?<![\d.,])" + re.escape(v).replace(r"\ ", " ") + r"(?![\d]|[.,]\d)"
        if re.search(pattern, normalised):
            return True
    return False
