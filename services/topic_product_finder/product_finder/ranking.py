"""Deterministic filtering and ranking: the LLM never decides the order or the numbers."""
from product_finder.models import Offer, SearchPlan
from product_finder.params import params


def rank(offers: list[Offer], plan: SearchPlan) -> list[Offer]:
    cfg = params().ranking
    allowed_conditions = set(plan.acceptable_conditions)
    eligible = [
        o for o in offers
        if o.verdict in cfg.allowed_verdicts
        and o.total_in_target is not None
        and o.in_stock is not False
        and (o.condition is None or o.condition in allowed_conditions)  # planner defaults to new-only
    ]
    # exact matches before variants at equal price; then cheapest total
    eligible.sort(key=lambda o: (o.total_in_target, 0 if o.verdict == "exact" else 1))
    best_per_retailer: dict[str, Offer] = {}
    for o in eligible:
        best_per_retailer.setdefault(o.retailer, o)
    return list(best_per_retailer.values())[: cfg.top_n]


def render_answer(plan: SearchPlan, ranked: list[Offer], checked: int, caveats: list[str]) -> str:
    if not ranked:
        return (
            f"I checked {checked} retailer pages for **{plan.product}** in {plan.country_code} but couldn't "
            "verify any matching offer with a price shown on the page. Try a more specific model name."
        )
    lines = [
        f"### Cheapest verified offers for {plan.product} ({plan.country_code})",
        "",
        f"| # | Retailer | Price (as listed) | Shipping | Total ≈ {plan.currency} | Match |",
        "|---|---|---|---|---|---|",
    ]
    for i, o in enumerate(ranked, 1):
        shipping = "unknown" if o.shipping is None else ("free" if o.shipping == 0 else f"{o.shipping:,.2f} {o.currency}")
        lines.append(
            f"| {i} | [{o.retailer}]({o.url}) | {o.price:,.2f} {o.currency} | {shipping} | "
            f"{o.total_in_target:,.2f} | {o.verdict} |"
        )
    lines += ["", f"Every price above was verified on the retailer page at fetch time ({checked} pages checked). "
                  "Converted totals use ECB reference rates and are for comparison only."]
    if caveats:
        lines += ["", "**Notes:**", *[f"- {c}" for c in caveats]]
    return "\n".join(lines)
