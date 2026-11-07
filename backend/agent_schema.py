"""Structured shopping requirements and the agent reply.

The model may only describe what the shopper wants. Product names, prices,
and stock are filled later from the catalog.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATALOG_CATEGORIES = (
    "headphones",
    "smartphones",
    "laptops",
    "accessories",
    "smartwatches",
)
CATEGORY_ALIASES = {
    "headphone": "headphones",
    "headphones": "headphones",
    "earbuds": "headphones",
    "earbud": "headphones",
    "smartphone": "smartphones",
    "smartphones": "smartphones",
    "phone": "smartphones",
    "phones": "smartphones",
    "laptop": "laptops",
    "laptops": "laptops",
    "accessory": "accessories",
    "accessories": "accessories",
    "charger": "accessories",
    "smartwatch": "smartwatches",
    "smartwatches": "smartwatches",
    "watch": "smartwatches",
    "watches": "smartwatches",
}
ALLOWED_PRIORITIES = ("lower price", "more matching features")


class ProductNeed(BaseModel):
    """One product the shopper asked for."""

    model_config = ConfigDict(extra="ignore")

    category: str | None = None
    features: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    quantity: int = 1
    max_unit_price_eur: Decimal | None = None

    @field_validator("category", mode="before")
    @classmethod
    def known_category(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("category must be text")
        canonical = CATEGORY_ALIASES.get(value.strip().casefold())
        if canonical is None:
            raise ValueError("category is not in the catalog")
        return canonical

    @field_validator("features", mode="before")
    @classmethod
    def clean_features(cls, value: object) -> list[str]:
        return _clean_text_list(value, limit=8)

    @field_validator("priorities", mode="before")
    @classmethod
    def clean_priorities(cls, value: object) -> list[str]:
        cleaned: list[str] = []
        for item in _clean_text_list(value, limit=4):
            key = item.casefold()
            if key in ALLOWED_PRIORITIES and key not in cleaned:
                cleaned.append(key)
        return cleaned

    @field_validator("quantity", mode="before")
    @classmethod
    def whole_quantity(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("quantity must be a whole number")
        if value < 1 or value > 5:
            raise ValueError("quantity must be from 1 to 5")
        return value

    @field_validator("max_unit_price_eur", mode="before")
    @classmethod
    def unit_price_euros(cls, value: object) -> Decimal | None:
        return _euros(value)


class ExtractedShoppingRequest(BaseModel):
    """What the model understood. Prices are still unknown at this step."""

    model_config = ConfigDict(extra="ignore")

    budget_eur: Decimal | None = None
    needs: list[ProductNeed] = Field(default_factory=list)
    ambiguous: bool = False
    clarifying_question: str | None = None

    @field_validator("budget_eur", mode="before")
    @classmethod
    def budget_euros(cls, value: object) -> Decimal | None:
        return _euros(value)

    @field_validator("clarifying_question", mode="before")
    @classmethod
    def question_text(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("clarifying_question must be text")
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        return cleaned[:300]

    @model_validator(mode="after")
    def ambiguous_requests_ask_a_question(self) -> "ExtractedShoppingRequest":
        if self.ambiguous and not self.clarifying_question:
            raise ValueError("an ambiguous request needs a clarifying question")
        return self


class RecommendedItem(BaseModel):
    product_id: str
    name: str
    category: str
    quantity: int
    unit_price_eur: str
    line_total_eur: str
    matched_features: list[str]
    available_stock: int
    reason: str


class AgentReply(BaseModel):
    status: Literal["recommendation", "clarification", "no_match"]
    clarifying_question: str | None
    items: list[RecommendedItem]
    total_eur: str
    currency: Literal["EUR"] = "EUR"
    explanation: str
    unmet_requirements: list[str]
    understood_budget_eur: str | None
    understood_categories: list[str]
    payment_created: bool
    next_step: str

    @field_validator("payment_created")
    @classmethod
    def never_creates_a_payment(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("the shopping agent cannot create a payment")
        return False


def _euros(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("money must be a number")
    try:
        if isinstance(value, float):
            amount = Decimal(format(value, ".2f"))
        elif isinstance(value, int):
            amount = Decimal(value)
        elif isinstance(value, Decimal):
            amount = value
        elif isinstance(value, str):
            amount = Decimal(value.strip().replace(",", "."))
        else:
            raise ValueError("money must be a number")
    except Exception as exc:
        raise ValueError("money must be a number") from exc
    amount = amount.quantize(Decimal("0.01"))
    if amount <= 0:
        raise ValueError("money must be greater than 0")
    return amount


def _clean_text_list(value: object, *, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("expected a list of text")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("expected a list of text")
        text = " ".join(item.split())
        if text and text.casefold() not in {entry.casefold() for entry in cleaned}:
            cleaned.append(text[:40])
        if len(cleaned) == limit:
            break
    return cleaned
