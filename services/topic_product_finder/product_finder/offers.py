"""Reading one page: safe fetch → visible text + the page's own offer with a VERIFIED price.

The price of an offer comes from schema.org data (exact, free) or the cheap model reading the page — and in that case
it must literally appear on the page, or the offer is dropped (an injected or hallucinated price never gets through).
Review and comparison pages have no single offer: their text goes back to the research agent (to discover products).
"""
import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from agency_platform.agents.base import RunContext
from agency_platform.errors import AppError
from agency_platform.security.content import UNTRUSTED_DATA_RULES, fence_untrusted
from agency_platform.security.grounding import money_appears, number_appears
from agency_platform.security.guardrails import guardrail
from agency_platform.security.ssrf import UnsafeURLError, safe_fetch
from product_finder.models import Offer, PageExtraction
from product_finder.pages import page_title, storage_in, structured_offers, visible_text
from product_finder.params import params

logger = logging.getLogger(__name__)


def domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def excluded(url: str) -> bool:
    d = domain(url)
    return any(d == x or d.endswith("." + x) for x in params().search.exclude_domains)


def is_carrier(retailer: str) -> bool:
    return any(retailer == d or retailer.endswith("." + d) for d in params().search.carrier_domains)


@dataclass
class PageRead:
    url: str
    shop: str
    title: str = ""
    text: str = ""
    offer: Offer | None = None
    note: str = ""          # why there is no text / no offer (shown to the agent, kept in the trace)


async def read_page(url: str, ctx: RunContext) -> PageRead:
    f = params().fetch
    shop = domain(url)
    if excluded(url):
        return PageRead(url, shop, note="skipped: social / reference site")
    try:
        page = await safe_fetch(url, client=ctx.http, max_bytes=f.max_bytes, timeout_s=f.timeout_s)
    except UnsafeURLError as exc:
        await guardrail("unsafe_url", "blocked", topic="product_finder", detail=str(exc)[:200])
        return PageRead(url, shop, note="blocked: not a public address")
    except Exception as exc:
        logger.info("fetch failed", extra={"url": url, "error": type(exc).__name__})
        return PageRead(url, shop, note=f"could not be fetched ({type(exc).__name__})")
    if page.status_code in (401, 403, 429, 503):
        return PageRead(url, shop, note=f"the site blocks automated reading (HTTP {page.status_code})")
    if page.status_code != 200 or "html" not in page.content_type:
        return PageRead(url, shop, note=f"not a readable page (HTTP {page.status_code}, {page.content_type[:40]})")
    shop = domain(page.url)
    title = page_title(page.text) or shop
    text = visible_text(page.text, f.max_text_chars)
    read = PageRead(page.url, shop, title, text)
    read.offer = await _offer(page.url, page.text, title, text, ctx)
    if read.offer is None:
        read.note = "no single price on this page (a list, a review or options to choose first)"
    return read


async def _offer(url: str, html: str, title: str, text: str, ctx: RunContext) -> Offer | None:
    oid = hashlib.sha256(url.encode()).hexdigest()[:8]
    shop = domain(url)
    carrier = is_carrier(shop)   # phone shops: the model must read whether the price needs a plan
    if params().extraction.prefer_structured_data and not carrier:
        structured = [s for s in structured_offers(html) if not s.from_price]   # "from" prices: not this offer's
        if len(structured) == 1 or (structured and len({o.price for o in structured}) == 1):
            s = structured[0]
            return Offer(id=oid, retailer=shop, url=url, title=(s.title or title)[:300], price=s.price,
                         currency=s.currency, condition=s.condition, in_stock=s.in_stock, source="structured_data",
                         variant=storage_in(s.title or title))
    if len(text) < 200:
        return None
    p = ctx.prompt("extractor")
    try:
        ex = await ctx.llm.structured(
            [{"role": "system", "content": p.system.format(untrusted_rules=UNTRUSTED_DATA_RULES)},
             {"role": "user", "content": fence_untrusted(f"TITLE: {title}\n\n{text}", url)}],
            PageExtraction, task="extraction", prompt=p, max_escalations=0)
    except AppError:
        return None
    if not ex.is_product_page or ex.price is None or ex.price <= 0 or not ex.currency or len(ex.currency) != 3:
        return None
    if params().extraction.verify_price_on_page and not money_appears(ex.price, ex.currency, text):
        logger.warning("extracted price not on page; dropped", extra={"url": url})
        await guardrail("grounding", "dropped", topic="product_finder", detail=f"price not on {urlsplit(url).hostname}")
        return None
    shipping = ex.shipping if ex.shipping is not None and (ex.shipping == 0 or number_appears(ex.shipping, text)) else None
    return Offer(id=oid, retailer=shop, url=url, title=(ex.title or title)[:300], price=ex.price,
                 currency=ex.currency.upper(), shipping=shipping, condition=ex.condition, in_stock=ex.in_stock,
                 source="llm_verified", variant=(ex.variant or storage_in(ex.title or title) or None),
                 requires_plan=ex.requires_plan if ex.requires_plan is not None else (True if carrier else None))
