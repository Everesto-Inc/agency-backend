"""All SQL for the product finder: the verified-offer price history."""
from collections.abc import Sequence

from agency_platform.db import get_db
from product_finder.models import Offer

_INSERT = """INSERT INTO product_offers (request_id, query_key, country_code, retailer, title, url, price, currency,
                 shipping, total_in_target, target_currency, `condition`, in_stock, match_verdict)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""


def query_key(product: str) -> str:
    return " ".join(product.lower().split())


async def record_offers(*, request_id: str, product: str, country_code: str, offers: Sequence[Offer]) -> None:
    key = query_key(product)
    await get_db().executemany(
        _INSERT,
        [(request_id, key, country_code, o.retailer, o.title, o.url, o.price, o.currency, o.shipping,
          o.total_in_target, o.target_currency, o.condition, o.in_stock, o.verdict) for o in offers],
    )
