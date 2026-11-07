"""Search external merchant products through the official Channel3 API.

This module calls POST https://api.trychannel3.com/v1/search. It does not
price a NuLixPay cart and it does not call PayPal. Missing prices, stock,
and merchant fields stay empty. They are never filled in with guesses.
"""

import logging
import os
import re
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from settings import DEFAULT_ENV_FILE

logger = logging.getLogger("nulixpay.channel3")

CHANNEL3_ORIGIN = "https://api.trychannel3.com"
CHANNEL3_SEARCH_URL = CHANNEL3_ORIGIN + "/v1/search"
CHANNEL3_HOST = "api.trychannel3.com"
REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
_CURRENCY = re.compile(r"^[A-Z]{3}$")

EXTERNAL_NOTICE = (
    "These are products from external merchants, found through Channel3. "
    "NuLixPay does not sell them. Do not send a Channel3 product id to "
    "POST /payments/create-order."
)


class Channel3Error(Exception):
    """A Channel3 call failed. The message is safe to show to a shopper."""

    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"message": message}


class ExternalOffer(BaseModel):
    """One merchant offer copied from a Channel3 product."""

    model_config = ConfigDict(extra="forbid")

    merchant_domain: str
    purchase_url: str
    price: float
    currency: str
    availability: Literal["InStock", "OutOfStock"] | None = None
    compare_at_price: float | None = None


class ExternalProduct(BaseModel):
    """A Channel3 product. It is not a row in the local catalog."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["external_merchant"] = "external_merchant"
    label: Literal["External merchant product"] = "External merchant product"
    channel3_product_id: str
    name: str
    description: str | None = None
    brands: list[str] = Field(default_factory=list)
    image_url: str | None = None
    offers: list[ExternalOffer] = Field(default_factory=list)
    sold_by_nulixpay: Literal[False] = False

    @field_validator("sold_by_nulixpay")
    @classmethod
    def never_sold_here(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("external products are not sold by NuLixPay")
        return False


class ExternalSearchResponse(BaseModel):
    """The public search result. Checkout is always disabled for these items."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["channel3"] = "channel3"
    query: str
    currency_requested: Literal["EUR"] = "EUR"
    results: list[ExternalProduct]
    checkout_supported: Literal[False] = False
    notice: str = EXTERNAL_NOTICE

    @field_validator("checkout_supported")
    @classmethod
    def never_checkout(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("external products cannot be checked out")
        return False


def load_channel3_api_key(env_file=None) -> str:
    """Read CHANNEL3_API_KEY. The value is not logged."""
    load_dotenv(DEFAULT_ENV_FILE if env_file is None else env_file, override=False)
    return os.environ.get("CHANNEL3_API_KEY", "").strip()


class HttpxChannel3Transport:
    """POST the search body to the official Channel3 host only."""

    def __init__(self, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS, client: object | None = None):
        self._owns_client = client is None
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def post_json(self, url: str, headers: dict[str, str], body: dict) -> tuple[int, object]:
        assert_channel3_search_url(url)
        try:
            response = self._client.request("POST", url, headers=headers, json=body)
        except httpx2.TimeoutException as exc:
            raise Channel3Error(
                "Channel3 did not respond in time.",
                status_code=504,
            ) from exc
        except httpx2.TransportError as exc:
            raise Channel3Error(
                "Could not reach Channel3.",
                status_code=502,
            ) from exc
        return response.status_code, _json_value(response)


class Channel3Client:
    """Call Channel3 search and map the response into our public models."""

    def __init__(self, api_key: str, transport: HttpxChannel3Transport):
        self._api_key = api_key
        self._transport = transport

    def search(
        self,
        query: str,
        *,
        max_price: float | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> ExternalSearchResponse:
        if not self._api_key:
            raise Channel3Error(
                "CHANNEL3_API_KEY is missing. Add it to the .env file in the "
                "project root, then restart the API.",
                status_code=503,
            )
        bounded = min(max(limit, 1), MAX_LIMIT)
        body: dict[str, object] = {
            "query": query,
            "limit": bounded,
            "config": {"currency": "EUR", "mode": "default"},
        }
        if max_price is not None:
            body["filters"] = {
                "price": {"max_price": max_price},
                "conditions": ["new"],
                "availability": ["InStock"],
            }
        status_code, payload = self._transport.post_json(
            CHANNEL3_SEARCH_URL,
            {
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            body,
        )
        logger.info("Channel3 POST /v1/search -> %s", status_code)
        if status_code != 200:
            raise _error_for_status(status_code)
        return ExternalSearchResponse(
            query=query,
            results=_products(payload, bounded),
        )


def assert_channel3_search_url(url: str) -> None:
    """Refuse every host except the official Channel3 API."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != CHANNEL3_HOST
        or parsed.path != "/v1/search"
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        raise Channel3Error(
            "Blocked a Channel3 request that was not aimed at the official search API.",
            status_code=503,
        )


def _error_for_status(status_code: int) -> Channel3Error:
    """Map Channel3 HTTP statuses to a short message. The body is not copied."""
    if status_code in {401, 403}:
        return Channel3Error("Channel3 rejected the API key.", status_code=502)
    if status_code == 402:
        return Channel3Error(
            "Channel3 has no credits left for this key.",
            status_code=502,
        )
    if status_code == 429:
        return Channel3Error(
            "Channel3 is rate limiting requests. Wait and try again.",
            status_code=429,
        )
    if status_code in {408, 504}:
        return Channel3Error("Channel3 did not respond in time.", status_code=504)
    if status_code >= 500:
        return Channel3Error("Channel3 is unavailable.", status_code=502)
    return Channel3Error("Channel3 rejected the search.", status_code=502)


def _products(payload: object, limit: int) -> list[ExternalProduct]:
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        raise Channel3Error(
            "Channel3 returned an unexpected response.",
            status_code=502,
        )
    products: list[ExternalProduct] = []
    for item in payload["products"]:
        if len(products) >= limit:
            break
        mapped = _product(item)
        if mapped is not None:
            products.append(mapped)
    return products


def _product(item: object) -> ExternalProduct | None:
    if not isinstance(item, dict):
        return None
    product_id = item.get("id")
    title = item.get("title")
    if not isinstance(product_id, str) or not product_id.strip():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    description = item.get("description")
    if not isinstance(description, str) or not description.strip():
        description = None
    else:
        description = description.strip()
    offers = []
    raw_offers = item.get("offers")
    if isinstance(raw_offers, list):
        for raw_offer in raw_offers:
            offer = _offer(raw_offer)
            if offer is not None:
                offers.append(offer)
    return ExternalProduct(
        channel3_product_id=product_id.strip()[:200],
        name=title.strip(),
        description=description,
        brands=_brands(item.get("brands")),
        image_url=_image_url(item.get("images")),
        offers=offers,
    )


def _brands(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for brand in value:
        if not isinstance(brand, dict):
            continue
        name = brand.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _image_url(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    chosen: dict | None = None
    for image in value:
        if not isinstance(image, dict):
            continue
        if image.get("is_main_image") is True:
            chosen = image
            break
        if chosen is None:
            chosen = image
    if chosen is None:
        return None
    for key in ("cleaned_url", "url"):
        url = _public_url(chosen.get(key))
        if url is not None:
            return url
    return None


def _offer(value: object) -> ExternalOffer | None:
    if not isinstance(value, dict):
        return None
    purchase_url = _public_url(value.get("url"))
    domain = value.get("domain")
    price_info = value.get("price")
    if purchase_url is None or not isinstance(domain, str) or not domain.strip():
        return None
    if not isinstance(price_info, dict):
        return None
    price = _money(price_info.get("price"))
    currency = price_info.get("currency")
    if price is None or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        return None
    availability = value.get("availability")
    if availability not in {"InStock", "OutOfStock"}:
        availability = None
    compare_at = price_info.get("compare_at_price")
    return ExternalOffer(
        merchant_domain=domain.strip()[:253],
        purchase_url=purchase_url,
        price=price,
        currency=currency,
        availability=availability,
        compare_at_price=_money(compare_at) if compare_at is not None else None,
    )


def _money(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and value != value:
        return None
    if value < 0 or value == float("inf"):
        return None
    return float(value)


def _public_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2000:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return url


def _json_value(response: object) -> object:
    try:
        return response.json()
    except ValueError:
        return None
