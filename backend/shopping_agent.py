"""Recommend a cart from the local catalog.

Prices and stock come from the catalog and from SQLite order records.
This module does not create, approve, or capture a PayPal payment.
"""

from dataclasses import dataclass
from decimal import Decimal

from catalog import Product, load_products
from money import euros_from_catalog, format_euros, line_total
from order_store import OrderStore

from agent_schema import (
    AgentReply,
    ExtractedShoppingRequest,
    ProductNeed,
    RecommendedItem,
)

NEXT_STEP = (
    "This reply does not create a PayPal order. To pay for these product ids, "
    "call POST /payments/create-order yourself."
)


@dataclass(frozen=True)
class _Candidate:
    product: Product
    unit: Decimal
    line: Decimal
    hits: list[str]
    available: int


class ShoppingAgent:
    def __init__(self, store: OrderStore, extractor, catalog_loader=load_products):
        self._store = store
        self._extractor = extractor
        self._catalog_loader = catalog_loader

    def chat(self, message: str, history: list[str] | None = None) -> AgentReply:
        extracted = self._extractor.extract(message, history or [])
        if extracted.ambiguous or not extracted.needs:
            question = extracted.clarifying_question or (
                "Which product category and budget should I use?"
            )
            return _reply(
                status="clarification",
                question=question,
                items=[],
                total=Decimal("0.00"),
                explanation=question,
                unmet=[],
                extracted=extracted,
            )
        return self._recommend(extracted)

    def _recommend(self, extracted: ExtractedShoppingRequest) -> AgentReply:
        products = self._catalog_loader()
        remaining = extracted.budget_eur
        chosen: list[RecommendedItem] = []
        reasons: list[str] = []
        unmet: list[str] = []
        reserved: dict[str, int] = {}
        for need in extracted.needs:
            winner, runner_up, problem = self._select(products, need, remaining, reserved)
            if winner is None:
                unmet.append(problem or "No catalog product matches that part of the request.")
                continue
            reserved[winner.product.id] = reserved.get(winner.product.id, 0) + need.quantity
            if remaining is not None:
                remaining -= winner.line
            reason = _tradeoff(winner, runner_up)
            reasons.append(reason)
            chosen.append(
                RecommendedItem(
                    product_id=winner.product.id,
                    name=winner.product.name,
                    category=winner.product.category,
                    quantity=need.quantity,
                    unit_price_eur=format_euros(winner.unit),
                    line_total_eur=format_euros(winner.line),
                    matched_features=winner.hits,
                    available_stock=winner.available,
                    reason=reason,
                )
            )
        total = sum((Decimal(item.line_total_eur) for item in chosen), Decimal("0.00"))
        if extracted.budget_eur is not None and total > extracted.budget_eur:
            raise RuntimeError("recommended cart exceeded the catalog budget")
        if chosen:
            explanation = " ".join(reasons)
            if unmet:
                explanation = f"{explanation} Unmet: {' '.join(unmet)}"
            return _reply(
                status="recommendation",
                question=None,
                items=chosen,
                total=total,
                explanation=explanation,
                unmet=unmet,
                extracted=extracted,
            )
        explanation = " ".join(unmet) or "No catalog product matches that request."
        return _reply(
            status="no_match",
            question=None,
            items=[],
            total=Decimal("0.00"),
            explanation=explanation,
            unmet=unmet,
            extracted=extracted,
        )

    def _select(
        self,
        products: list[Product],
        need: ProductNeed,
        remaining: Decimal | None,
        reserved: dict[str, int],
    ) -> tuple[_Candidate | None, _Candidate | None, str | None]:
        matched_out_of_stock: list[Product] = []
        over_budget: list[tuple[Product, Decimal]] = []
        candidates: list[_Candidate] = []
        for product in products:
            if need.category is not None and product.category != need.category:
                continue
            hits = _feature_hits(product, need.features)
            if need.features and not hits:
                continue
            unit = euros_from_catalog(product.price_eur)
            available = (
                product.stock
                - self._store.committed_units(product.id)
                - reserved.get(product.id, 0)
            )
            if available < need.quantity:
                matched_out_of_stock.append(product)
                continue
            line = line_total(unit, need.quantity)
            if need.max_unit_price_eur is not None and unit > need.max_unit_price_eur:
                over_budget.append((product, unit))
                continue
            if remaining is not None and line > remaining:
                over_budget.append((product, unit))
                continue
            candidates.append(
                _Candidate(
                    product=product,
                    unit=unit,
                    line=line,
                    hits=hits,
                    available=available,
                )
            )
        if candidates:
            ranked = sorted(candidates, key=lambda item: _sort_key(item, need))
            runner_up = ranked[1] if len(ranked) > 1 else None
            return ranked[0], runner_up, None
        label = need.category or "that request"
        if over_budget:
            cheapest = min(price for _, price in over_budget)
            budget_text = format_euros(remaining) if remaining is not None else "the requested limit"
            return (
                None,
                None,
                (
                    f"No {label} in the catalog fit the remaining budget of {budget_text} EUR. "
                    f"The lowest matching catalog price is {format_euros(cheapest)} EUR."
                ),
            )
        if matched_out_of_stock:
            names = ", ".join(product.name for product in matched_out_of_stock)
            verb = "have" if len(matched_out_of_stock) > 1 else "has"
            return None, None, f"{names} {verb} no available stock."
        feature_text = ", ".join(need.features) if need.features else "the requested features"
        return None, None, f"No catalog product in {label} includes {feature_text}."


def _feature_hits(product: Product, features: list[str]) -> list[str]:
    haystack = _haystack(product)
    return [feature for feature in features if feature.casefold().replace("-", " ") in haystack]


def _haystack(product: Product) -> str:
    raw = " ".join([product.name, product.category, product.description, *product.features])
    return raw.casefold().replace("-", " ")


def _sort_key(candidate: _Candidate, need: ProductNeed) -> tuple:
    price_first = "lower price" in need.priorities and "more matching features" not in need.priorities
    if price_first:
        return (candidate.unit, -len(candidate.hits), candidate.product.name)
    return (-len(candidate.hits), candidate.unit, candidate.product.name)


def _tradeoff(winner: _Candidate, runner_up: _Candidate | None) -> str:
    price = format_euros(winner.unit)
    if runner_up is None:
        if winner.hits:
            matched = ", ".join(winner.hits)
            return (
                f"{winner.product.name} is the only catalog match for {matched}. "
                f"It costs {price} EUR."
            )
        return f"{winner.product.name} is the lowest priced catalog match at {price} EUR."
    other_price = format_euros(runner_up.unit)
    winner_only = [feature for feature in winner.hits if feature not in runner_up.hits]
    if winner_only:
        return (
            f"{winner.product.name} costs {price} EUR and includes {', '.join(winner_only)}. "
            f"{runner_up.product.name} costs {other_price} EUR and does not include that."
        )
    if winner.unit < runner_up.unit:
        return (
            f"{winner.product.name} and {runner_up.product.name} match the same requested features. "
            f"{winner.product.name} costs {price} EUR, against {other_price} EUR for {runner_up.product.name}."
        )
    return (
        f"{winner.product.name} costs {price} EUR and matches more of the request than "
        f"{runner_up.product.name} at {other_price} EUR."
    )


def _reply(
    *,
    status: str,
    question: str | None,
    items: list[RecommendedItem],
    total: Decimal,
    explanation: str,
    unmet: list[str],
    extracted: ExtractedShoppingRequest,
) -> AgentReply:
    categories = []
    for need in extracted.needs:
        if need.category and need.category not in categories:
            categories.append(need.category)
    return AgentReply(
        status=status,
        clarifying_question=question,
        items=items,
        total_eur=format_euros(total),
        explanation=explanation,
        unmet_requirements=unmet,
        understood_budget_eur=format_euros(extracted.budget_eur) if extracted.budget_eur is not None else None,
        understood_categories=categories,
        payment_created=False,
        next_step=NEXT_STEP,
    )
