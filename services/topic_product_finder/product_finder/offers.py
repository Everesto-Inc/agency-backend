"""Search -> safe fetch -> extract (structured data first, else low-tier LLM) -> verify -> FX."""
import asyncio
import hashlib
import logging
from urllib.parse import urlsplit

from agency_platform.agents.base import RunContext
from agency_platform.errors import AppError
from agency_platform.security.content import UNTRUSTED_DATA_RULES, fence_untrusted
from agency_platform.security.grounding import money_appears, number_appears
from agency_platform.security.guardrails import guardrail
from agency_platform.security.ssrf import UnsafeURLError, safe_fetch
from agency_platform.tools.search import WebSearch
from product_finder.fx import FXRates
from product_finder.models import Offer, PageExtraction, SearchPlan
from product_finder.pages import page_title, storage_in, structured_offers, visible_text
from product_finder.params import params

logger = logging.getLogger(__name__)


def domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _excluded(url: str) -> bool:
    d = domain(url)
    return any(d == x or d.endswith("." + x) for x in params().search.exclude_domains)


def _is_carrier(retailer: str) -> bool:
    return any(retailer == d or retailer.endswith("." + d) for d in params().search.carrier_domains)


async def candidate_urls(plan: SearchPlan, search: WebSearch) -> list[str]:
    cfg = params().search
    jobs = [search.search(q, max_results=cfg.results_per_query) for q in plan.queries[: cfg.max_queries]]
    hints = cfg.retailer_hints.get(plan.country_code.upper())
    if hints:
        jobs.append(search.search(plan.product, max_results=cfg.results_per_query, include_domains=hints))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    seen: set[str] = set()
    urls: list[str] = []
    per_domain: dict[str, int] = {}
    for batch in results:
        if isinstance(batch, BaseException):
            logger.warning("search query failed", extra={"error": type(batch).__name__})
            continue
        for r in sorted(batch, key=lambda r: -r.score):
            url = r.url.split("#")[0]
            d = domain(url)
            if url in seen or _excluded(url) or per_domain.get(d, 0) >= 2:
                continue
            seen.add(url)
            per_domain[d] = per_domain.get(d, 0) + 1
            urls.append(url)
    return urls[: cfg.max_pages_to_check]


async def extract_offer(url: str, ctx: RunContext) -> Offer | None:
    f = params().fetch
    try:
        page = await safe_fetch(url, client=ctx.http, max_bytes=f.max_bytes, timeout_s=f.timeout_s)
    except UnsafeURLError as exc:
        logger.warning("blocked unsafe url", extra={"url": url})
        await guardrail("unsafe_url", "blocked", topic="product_finder", detail=str(exc)[:200])
        return None
    except Exception as exc:
        logger.info("fetch failed", extra={"url": url, "error": type(exc).__name__})
        return None
    if page.status_code != 200 or "html" not in page.content_type:
        return None

    oid = hashlib.sha256(page.url.encode()).hexdigest()[:8]
    retailer = domain(page.url)
    title = page_title(page.text) or retailer

    carrier = _is_carrier(retailer)   # phone shops: the model must read whether the price needs a plan
    if params().extraction.prefer_structured_data and not carrier:
        structured = [s for s in structured_offers(page.text) if not s.from_price]   # "from" prices: not this offer's
        if len(structured) == 1 or (structured and len({o.price for o in structured}) == 1):
            s = structured[0]
            return Offer(id=oid, retailer=retailer, url=page.url, title=(s.title or title)[:300], price=s.price,
                         currency=s.currency, condition=s.condition, in_stock=s.in_stock, source="structured_data",
                         variant=storage_in(s.title or title))

    text = visible_text(page.text, f.max_text_chars)
    if len(text) < 200:
        return None
    p = ctx.prompt("extractor")
    try:
        ex = await ctx.llm.structured(
            [
                {"role": "system", "content": p.system.format(untrusted_rules=UNTRUSTED_DATA_RULES)},
                {"role": "user", "content": fence_untrusted(f"TITLE: {title}\n\n{text}", page.url)},
            ],
            PageExtraction,
            task="extraction",
            prompt=p,
            max_escalations=0,  # a cheap model is plenty; a miss just drops the page
        )
    except AppError:
        return None
    if not ex.is_product_page or ex.price is None or ex.price <= 0 or not ex.currency or len(ex.currency) != 3:
        return None
    if params().extraction.verify_price_on_page and not money_appears(ex.price, ex.currency, text):
        logger.warning("extracted price not on page; dropped", extra={"url": page.url})
        await guardrail("grounding", "dropped", topic="product_finder",
                        detail=f"price not on {urlsplit(page.url).hostname}")
        return None
    shipping = ex.shipping if ex.shipping is not None and (ex.shipping == 0 or number_appears(ex.shipping, text)) else None
    return Offer(id=oid, retailer=retailer, url=page.url, title=(ex.title or title)[:300], price=ex.price,
                 currency=ex.currency.upper(), shipping=shipping, condition=ex.condition, in_stock=ex.in_stock,
                 source="llm_verified", variant=(ex.variant or storage_in(ex.title or title) or None),
                 requires_plan=ex.requires_plan if ex.requires_plan is not None else (True if carrier else None))


async def gather_offers(urls: list[str], ctx: RunContext) -> list[Offer]:
    sem = asyncio.Semaphore(params().fetch.concurrency)

    async def one(u: str) -> Offer | None:
        async with sem:
            return await extract_offer(u, ctx)

    results = await asyncio.gather(*(one(u) for u in urls))
    unique: dict[str, Offer] = {}
    for o in results:
        if o is not None:
            unique.setdefault(o.id, o)
    return list(unique.values())


async def normalise(offers: list[Offer], target_currency: str, fx: FXRates) -> None:
    for o in offers:
        total = o.price + (o.shipping or 0)
        try:
            o.total_in_target = await fx.convert(total, o.currency, target_currency)
        except AppError:
            o.total_in_target = total if o.currency == target_currency.upper() else None
        o.target_currency = target_currency.upper()
