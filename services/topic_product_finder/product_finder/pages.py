"""Page parsing: schema.org JSON-LD offers (exact, free) and visible text (for
LLM extraction + price verification)."""
import json
from dataclasses import dataclass
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

_CONDITIONS = {"newcondition": "new", "usedcondition": "used", "refurbishedcondition": "refurbished",
               "damagedcondition": "used"}


@dataclass(frozen=True)
class StructuredOffer:
    title: str | None
    price: float
    currency: str
    condition: str | None
    in_stock: bool | None


def visible_text(html: str, max_chars: int) -> str:
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, svg, iframe, template"):
        tag.decompose()
    body = tree.body or tree.root
    text = body.text(separator=" ", strip=True) if body else ""
    return " ".join(text.split())[:max_chars]


def page_title(html: str) -> str | None:
    node = HTMLParser(html).css_first("title")
    return node.text(strip=True)[:300] if node else None


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {s.lower() for s in ([t] if isinstance(t, str) else t if isinstance(t, list) else [])}


def _to_float(value: Any) -> float | None:
    try:
        f = float(str(value).replace(",", "")) if value not in (None, "") else None
    except ValueError:
        return None
    return f if f and f > 0 else None


def structured_offers(html: str) -> list[StructuredOffer]:
    """Offers declared in schema.org Product JSON-LD (what retailers publish for Google Shopping)."""
    found: list[StructuredOffer] = []
    for script in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(strip=True))
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk(data):
            if "product" not in _types(node):
                continue
            name = node.get("name") if isinstance(node.get("name"), str) else None
            offers = node.get("offers")
            for offer in _walk(offers) if offers else []:
                if not ({"offer", "aggregateoffer"} & _types(offer)):
                    continue
                price = _to_float(offer.get("price")) or _to_float(offer.get("lowPrice"))
                currency = offer.get("priceCurrency")
                if price is None or not isinstance(currency, str) or len(currency) != 3:
                    continue
                availability = str(offer.get("availability", "")).lower()
                condition = str(offer.get("itemCondition", "")).lower().rsplit("/", 1)[-1]
                found.append(StructuredOffer(
                    title=name,
                    price=price,
                    currency=currency.upper(),
                    condition=_CONDITIONS.get(condition),
                    in_stock=None if not availability else ("instock" in availability or "limitedavailability" in availability),
                ))
    return found
