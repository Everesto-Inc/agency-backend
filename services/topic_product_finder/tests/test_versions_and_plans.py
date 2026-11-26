"""The iPhone conversation: storage matters, carrier prices with a plan aren't device prices, "from" prices aren't
an offer's price, follow-ups about shown results don't start a new search, and nobody blames the shopper."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("PROMPTS_DIR", str(ROOT / "prompts"))

from agency_platform.security.ssrf import FetchedPage
from agency_platform.testing import FakeLLM, fake_context
from product_finder import offers as pipeline
from product_finder.agent import ProductFinderAgent
from product_finder.models import Offer, SearchPlan
from product_finder.pages import storage_in, structured_offers
from product_finder.ranking import rank, render_answer

PLAN = SearchPlan(product="iPhone 18 Pro", must_match=["iPhone 18 Pro"], country_code="FR", currency="EUR",
                  queries=["iphone 18 pro prix"], variant_options=["storage"])


def offer(i, retailer, price, variant=None, requires_plan=None):
    return Offer(id=str(i), retailer=retailer, url=f"https://{retailer}/x", title="iPhone 18 Pro", price=price,
                 currency="EUR", source="llm_verified", total_in_target=price, target_currency="EUR",
                 verdict="exact", variant=variant, requires_plan=requires_plan)


def test_storage_is_read_from_titles():
    assert storage_in("Apple iPhone 18 Pro 256 Go Titane") == "256 GB"
    assert storage_in("iPhone 18 Pro 1TB") == "1 TB"
    assert storage_in("iPhone 18 Pro") is None
    assert storage_in("256 GB or 512 GB") is None          # ambiguous → unknown, never a guess


def test_a_from_price_is_marked_and_never_used_as_the_offer_price():
    html = """<script type="application/ld+json">{"@type":"Product","name":"iPhone 18 Pro",
    "offers":{"@type":"AggregateOffer","lowPrice":"1229.00","priceCurrency":"EUR"}}</script>"""
    [o] = structured_offers(html)
    assert o.from_price is True


def test_plan_prices_are_left_out_and_counted_storage_shown_and_asked():
    found = [offer(1, "sfr.fr", 1479, "256 GB"), offer(2, "bouyguestelecom.fr", 229.90, requires_plan=True),
             offer(3, "fnac.com", 1529, None)]
    ranked = rank(found, PLAN)
    assert [o.retailer for o in ranked] == ["sfr.fr", "fnac.com"]
    text = render_answer(PLAN, ranked, 12, [], found)
    assert "| Version |" in text and "| 256 GB |" in text and "| not shown |" in text
    assert "1 carrier offer(s) were left out" in text
    assert "tell me (e.g. *256 GB*)" in text


def test_no_result_is_explained_without_blaming_the_shopper():
    text = render_answer(PLAN, [], 12, [], [offer(2, "bouyguestelecom.fr", 229.90, requires_plan=True)])
    assert "more specific model name" not in text and "block automated reading" in text
    assert "carrier offer(s) were left out" in text and "storage" in text


class FollowUpCrews:
    async def plan(self, request, default_country, ctx):
        return PLAN.model_copy(update={"intent": "about_previous"})

    async def match(self, plan, offers, ctx):
        raise AssertionError("a follow-up must not search again")


async def test_a_question_about_shown_results_is_answered_from_them():
    agent = ProductFinderAgent(crews=FollowUpCrews())
    agent.bind(object(), object())  # type: ignore[arg-type]   # search / FX must not be used
    llm = FakeLLM({"direct": "The SFR offer didn't show its storage. Shall I search for the 256 GB version?"})
    history = [{"role": "user", "content": "iphone 18 pro price?"},
               {"role": "assistant", "content": "| 1 | sfr.fr | 1,479.00 EUR | exact |"}]
    ctx = fake_context("product_finder", llm, ROOT / "prompts", history=history)
    events = [e async for e in agent.run("which storage is that?", ctx)]
    answer = next(e for e in events if e.type == "token").content
    assert "256 GB" in answer and [c["task"] for c in llm.calls] == ["direct"]
    assert "sfr.fr" in llm.calls[0]["messages"][2]["content"]          # it saw the earlier answer


CARRIER_PAGE = "<html><head><title>iPhone 18 Pro - Bouygues</title><script type='application/ld+json'>" \
    '{"@type":"Product","name":"iPhone 18 Pro","offers":{"@type":"Offer","price":"229.90","priceCurrency":"EUR"}}' \
    "</script></head><body>" + "Forfait 5G 24 mois. " * 20 + " iPhone 18 Pro 256 Go à 229,90 € avec forfait</body></html>"


async def test_carrier_pages_are_read_by_the_model_not_the_json_shortcut(monkeypatch):
    async def fake_fetch(url, *, client, max_bytes, timeout_s):
        return FetchedPage(url=url, status_code=200, content_type="text/html", text=CARRIER_PAGE, truncated=False)
    monkeypatch.setattr(pipeline, "safe_fetch", fake_fetch)
    llm = FakeLLM({"extraction": {"is_product_page": True, "title": "iPhone 18 Pro 256 Go", "price": 229.90,
                                  "currency": "EUR", "requires_plan": True, "variant": "256 GB"}})
    o = await pipeline.extract_offer("https://www.bouyguestelecom.fr/iphone", fake_context("product_finder", llm, ROOT / "prompts"))
    assert o is not None and o.requires_plan is True and o.variant == "256 GB" and o.source == "llm_verified"


def test_planner_prompt_keeps_the_named_product():
    from agency_platform.config.prompts import PromptStore
    text = PromptStore(ROOT / "prompts").get("product_finder", "planner_task").system
    assert "never replace it with" in text and "variant_options" in text and "about_previous" in text

