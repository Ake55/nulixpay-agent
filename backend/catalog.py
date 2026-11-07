"""Load the local product catalog and search it.

The catalog is a JSON file, not a database. This module checks that every
product has the expected fields, then filters that list in memory.
"""

import json
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

# Resolve from this file's location so the catalog loads whether Uvicorn is
# started from the project root or from the backend folder.
CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "products.json"


class CatalogError(Exception):
    """Raised when the catalog file is missing or its contents are unusable."""


class Product(BaseModel):
    """One product from data/products.json."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    category: str
    description: str
    price_eur: float
    features: list[str]
    stock: int

    @field_validator("id", "name", "category", "description", mode="before")
    @classmethod
    def text_must_be_present(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("must be text")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("price_eur", mode="before")
    @classmethod
    def price_must_be_positive(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("must be a finite number")
        if value <= 0:
            raise ValueError("must be greater than 0")
        return float(value)

    @field_validator("stock", mode="before")
    @classmethod
    def stock_must_be_whole(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be a whole number")
        if value < 0:
            raise ValueError("must be 0 or greater")
        return value

    @field_validator("features", mode="before")
    @classmethod
    def features_must_be_text(cls, value: object) -> list[str]:
        if not isinstance(value, list) or len(value) == 0:
            raise ValueError("must be a non-empty list of text")
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("must be a non-empty list of text")
            cleaned.append(item.strip())
        return cleaned


def load_products(path: Path | None = None) -> list[Product]:
    """Read and validate every product in the catalog file."""
    catalog_path = CATALOG_PATH if path is None else Path(path)
    if not catalog_path.is_file():
        raise CatalogError(f"Product catalog not found: {catalog_path}")

    try:
        raw_text = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"Could not read product catalog: {catalog_path}") from exc

    try:
        raw_products = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CatalogError("Product catalog is not valid JSON") from exc

    if not isinstance(raw_products, list):
        raise CatalogError("Product catalog must be a JSON array")

    products: list[Product] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_products, start=1):
        try:
            product = Product.model_validate(item)
        except ValidationError as exc:
            details = _format_validation_error(exc)
            raise CatalogError(f"Invalid product {index}: {details}") from exc
        if product.id in seen_ids:
            raise CatalogError(f"Duplicate product id: {product.id}")
        seen_ids.add(product.id)
        products.append(product)
    return products


def search_products(
    products: list[Product],
    query: str | None = None,
    max_price: float | None = None,
) -> list[Product]:
    """Return products that match an optional text query and budget.

    Text matching ignores letter case. The query is looked up inside the
    name, category, description, and each feature. A product is kept when
    any of those fields contains the query. When max_price is provided, the
    price must also be less than or equal to that amount. Catalog order is
    preserved.
    """
    matches = products
    if query is not None and query.strip():
        needle = query.strip().casefold()
        matches = [product for product in matches if _contains_query(product, needle)]
    if max_price is not None:
        matches = [product for product in matches if product.price_eur <= max_price]
    return matches


def _contains_query(product: Product, needle: str) -> bool:
    fields = [product.name, product.category, product.description, *product.features]
    return any(needle in field.casefold() for field in fields)


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "product"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)
