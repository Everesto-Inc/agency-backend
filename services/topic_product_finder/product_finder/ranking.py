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
        and o.requires_plan is not True                                  # a price with a contract isn't comparable
        and (o.condition is None or o.condition in allowed_conditions)  # planner defaults to new-only
    ]
    # exact matches before variants at equal price; then cheapest total
    eligible.sort(key=lambda o: (o.total_in_target, 0 if o.verdict == "exact" else 1))
    best_per_retailer: dict[str, Offer] = {}
    for o in eligible:
        best_per_retailer.setdefault(o.retailer, o)
    return list(best_per_retailer.values())[: cfg.top_n]


def _ask_variant(plan: SearchPlan) -> list[str]:
    if not plan.variant_options:
        return []
    opts = " / ".join(plan.variant_options)
    return ["", f"Prices depend on the **{opts}**, which you didn't give — tell me (e.g. *256 GB*) and I'll compare "
                "exactly that version."]


def render_answer(plan: SearchPlan, ranked: list[Offer], checked: int, caveats: list[str],
                  found: list[Offer] | None = None) -> str:
    with_plan = sum(1 for o in found or [] if o.requires_plan)
    plan_note = ([f"{with_plan} carrier offer(s) were left out: their price is only valid with a phone plan or "
                  "contract."] if with_plan else [])
    country_note = ([f"_Searched in {plan.country_code} (no country given — tell me if you shop elsewhere)._", ""]
                    if not plan.country_stated else [])
    if not ranked:
        return "\n".join([
            *country_note,
            f"I checked {checked} shop pages for **{plan.product}** in {plan.country_code}, but none showed a price I "
            f"could verify for this exact product ({len(found or [])} pages had a readable price). Shops often block "
            "automated reading, list several versions on one page, or show the price only after choosing options.",
            *(["", *plan_note] if plan_note else []),
            *_ask_variant(plan),
        ])
    show_variant = any(o.variant for o in ranked)
    lines = [
        f"### Cheapest verified offers for {plan.product} ({plan.country_code})",
        "",
        *country_note,
        f"| # | Retailer |{' Version |' if show_variant else ''} Price (as listed) | Shipping | Total ≈ {plan.currency} | Match |",
        f"|---|---|{'---|' if show_variant else ''}---|---|---|---|",
    ]
    for i, o in enumerate(ranked, 1):
        shipping = "unknown" if o.shipping is None else ("free" if o.shipping == 0 else f"{o.shipping:,.2f} {o.currency}")
        version = f" {o.variant or 'not shown'} |" if show_variant else ""
        lines.append(
            f"| {i} | [{o.retailer}]({o.url}) |{version} {o.price:,.2f} {o.currency} | {shipping} | "
            f"{o.total_in_target:,.2f} | {o.verdict} |"
        )
    lines += ["", f"Every price above was verified on the retailer page at fetch time ({checked} pages checked). "
                  "Converted totals use ECB reference rates and are for comparison only."]
    if plan_note:
        lines += ["", *plan_note]
    if plan.variant_options and not all(o.variant for o in ranked):
        lines += _ask_variant(plan)
    if caveats:
        lines += ["", "**Notes:**", *[f"- {c}" for c in caveats]]
    return "\n".join(lines)
