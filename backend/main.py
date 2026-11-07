"""NuLixPay Agent API entry point.

Run this app with Uvicorn. FastAPI turns the functions below into HTTP
endpoints and builds the interactive docs at /docs automatically.
"""

import threading
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_llm import AgentConfigError, AgentLLMError, LLMShoppingExtractor, MockShoppingExtractor, load_agent_settings
from agent_schema import AgentReply
from catalog import CatalogError, Product, load_products, search_products
from channel3_client import (
    Channel3Client,
    Channel3Error,
    ExternalSearchResponse,
    HttpxChannel3Transport,
    load_channel3_api_key,
)
from checkout import ORDER_ID_RE, CheckoutError, CheckoutService
from order_store import OrderStore
from paypal import HttpxTransport, PayPalClient
from settings import SettingsError, load_settings
from shopping_agent import ShoppingAgent

app = FastAPI(
    title="NuLixPay Agent",
    description=(
        "Backend for the NuLixPay shopping assistant. "
        "The agent recommends catalog products and does not create payments. "
        "Checkout uses PayPal Sandbox and local catalog ids only. "
        "GET /products/external/search lists other merchants and cannot be paid here."
    ),
    version="0.1.0",
)

# The Vite app runs here during local development. Credentials stay on this API.
FRONTEND_ORIGIN = "http://127.0.0.1:5173"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:5173"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Content-Type"],
    allow_credentials=False,
)

_checkout: CheckoutService | None = None
_checkout_lock = threading.Lock()
_agent: ShoppingAgent | None = None
_agent_lock = threading.Lock()
_channel3: Channel3Client | None = None
_channel3_lock = threading.Lock()


class ChatIn(BaseModel):
    """A shopping request in natural language. This route never takes payment."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"message": "I need noise cancelling headphones and a charger under 200 euros"}
            ]
        },
    )

    message: str = Field(min_length=1, max_length=2000)
    history: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("message")
    @classmethod
    def message_text(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("message must not be empty")
        return cleaned

    @field_validator("history")
    @classmethod
    def history_text(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("history entries must be text")
            text = " ".join(item.split())
            if text:
                cleaned.append(text[:2000])
        return cleaned


class OrderLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=40)
    quantity: int = Field(ge=1, le=99)

    @field_validator("product_id", mode="before")
    @classmethod
    def product_id_text(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("must be text")
        return value.strip()

    @field_validator("quantity", mode="before")
    @classmethod
    def quantity_whole(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be a whole number")
        return value


class CreateOrderIn(BaseModel):
    """Product ids and quantities. The server calculates the euro total."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"items": [{"product_id": "hp-001", "quantity": 1}]}]
        },
    )

    items: list[OrderLineIn] = Field(min_length=1, max_length=20)


class CaptureIn(BaseModel):
    """Explicit confirmation. The return URL from PayPal does not send this."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"confirm": True}]},
    )

    confirm: Literal[True] = Field(
        description=(
            "Must be true. Capture runs only after the buyer has approved the "
            "PayPal order and this confirmation is sent."
        )
    )


@app.exception_handler(CheckoutError)
async def checkout_error_handler(_request, exc: CheckoutError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Channel3Error)
async def channel3_error_handler(_request, exc: Channel3Error) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def get_checkout() -> CheckoutService:
    """Build the sandbox checkout once. Tests can override this dependency."""
    global _checkout
    if _checkout is not None:
        return _checkout
    with _checkout_lock:
        if _checkout is not None:
            return _checkout
        try:
            settings = load_settings()
        except SettingsError as exc:
            raise HTTPException(status_code=503, detail={"message": str(exc)}) from exc
        store = OrderStore(settings.database_path)
        store.initialize()
        paypal = PayPalClient(settings, HttpxTransport(settings.timeout_seconds))
        _checkout = CheckoutService(paypal, store)
        return _checkout


def get_agent() -> ShoppingAgent:
    """Build the shopping agent once. Tests can override this dependency."""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        try:
            settings = load_agent_settings()
        except AgentConfigError as exc:
            raise HTTPException(status_code=503, detail={"message": str(exc)}) from exc
        store = OrderStore(settings.database_path)
        store.initialize()
        if settings.mode == "mock":
            extractor = MockShoppingExtractor()
        else:
            extractor = LLMShoppingExtractor(settings)
        _agent = ShoppingAgent(store, extractor)
        return _agent


def get_channel3() -> Channel3Client:
    """Build the Channel3 client once. Tests can override this dependency."""
    global _channel3
    if _channel3 is not None:
        return _channel3
    with _channel3_lock:
        if _channel3 is not None:
            return _channel3
        api_key = load_channel3_api_key()
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": (
                        "CHANNEL3_API_KEY is missing. Add it to the .env file in the "
                        "project root, then restart the API."
                    )
                },
            )
        _channel3 = Channel3Client(api_key, HttpxChannel3Transport())
        return _channel3


@app.get("/")
def read_root() -> dict[str, str]:
    """Confirm the API process is up."""
    return {
        "message": "Welcome to NuLixPay Agent",
        "status": "running",
    }


def _load_catalog() -> list[Product]:
    """Load the catalog, or turn a data problem into an HTTP 500 response."""
    try:
        return load_products()
    except CatalogError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/products")
def list_products() -> list[Product]:
    """Return every product in the catalog."""
    return _load_catalog()


@app.get("/products/search")
def find_products(
    query: str | None = Query(
        default=None,
        description=(
            "Text to find in the name, category, description, or features. "
            "Letter case is ignored. Leave this empty to skip text matching."
        ),
    ),
    max_price: float | None = Query(
        default=None,
        ge=0,
        description=(
            "Highest price in euros to include. A product at this exact price "
            "is included. Leave this empty to ignore budget."
        ),
    ),
) -> list[Product]:
    """Return products matching an optional text query and maximum price."""
    return search_products(_load_catalog(), query=query, max_price=max_price)


@app.get("/products/external/search")
def search_external_products(
    query: str = Query(
        min_length=1,
        max_length=200,
        description="Text sent to Channel3 product search.",
    ),
    max_price: float | None = Query(
        default=None,
        ge=0,
        le=1_000_000,
        description=(
            "Optional maximum price sent to Channel3 while requesting EUR offers. "
            "Each result keeps the currency Channel3 returned."
        ),
    ),
    limit: int = Query(
        default=5,
        ge=1,
        le=10,
        description="How many external products to return. The maximum is 10.",
    ),
    channel3: Channel3Client = Depends(get_channel3),
) -> ExternalSearchResponse:
    """Search other merchants through Channel3.

    These products are not in the local catalog. Their ids cannot be paid
    through PayPal Sandbox checkout.
    """
    cleaned = " ".join(query.split())
    if not cleaned:
        raise HTTPException(status_code=422, detail={"message": "query must not be empty"})
    return channel3.search(query=cleaned, max_price=max_price, limit=limit)


@app.post("/agent/chat")
def agent_chat(body: ChatIn, agent: ShoppingAgent = Depends(get_agent)) -> AgentReply:
    """Recommend catalog products for a natural-language shopping request.

    The reply explains trade-offs and a cart total. It does not call PayPal.
    """
    try:
        return agent.chat(body.message, body.history)
    except AgentLLMError as exc:
        raise HTTPException(status_code=502, detail={"message": str(exc)}) from exc
    except CatalogError as exc:
        raise HTTPException(status_code=500, detail={"message": str(exc)}) from exc


@app.post("/payments/create-order", status_code=201)
def create_payment_order(
    body: CreateOrderIn,
    checkout: CheckoutService = Depends(get_checkout),
) -> dict[str, object]:
    """Create a PayPal Sandbox order from catalog prices.

    The response is an unpaid order. It includes the approval URL and does not
    capture money.
    """
    items = [(line.product_id, line.quantity) for line in body.items]
    return checkout.create_order(items)


def _frontend_redirect(request: Request, path: str) -> RedirectResponse | None:
    """Send a browser to the Vite app. API clients still receive JSON."""
    if "text/html" not in request.headers.get("accept", ""):
        return None
    return RedirectResponse(f"{FRONTEND_ORIGIN}{path}", status_code=302)


@app.get("/payments/return")
def payment_return(request: Request, token: str | None = Query(default=None)):
    """Landing page after buyer approval. This route never captures a payment."""
    order_id = token if isinstance(token, str) and ORDER_ID_RE.fullmatch(token) else None
    query = f"?token={order_id}" if order_id else ""
    redirect = _frontend_redirect(request, f"/return{query}")
    if redirect is not None:
        return redirect
    return {
        "order_id": order_id,
        "payment_status": "NOT_PAID",
        "payment_completed": False,
        "message": (
            "PayPal returned the buyer to NuLixPay. No payment was captured. "
            "To capture an approved order, send POST /payments/{order_id}/capture "
            "with confirm set to true."
        ),
    }


@app.get("/payments/cancel")
def payment_cancel(request: Request, token: str | None = Query(default=None)):
    """Landing page after the buyer cancels approval. Nothing is captured."""
    order_id = token if isinstance(token, str) and ORDER_ID_RE.fullmatch(token) else None
    query = "?cancelled=1"
    if order_id:
        query += f"&token={order_id}"
    redirect = _frontend_redirect(request, f"/return{query}")
    if redirect is not None:
        return redirect
    return {
        "order_id": order_id,
        "payment_status": "NOT_PAID",
        "payment_completed": False,
        "message": "The buyer cancelled PayPal approval. No payment was captured.",
    }


@app.get("/payments/{order_id}")
def get_payment(
    order_id: str,
    checkout: CheckoutService = Depends(get_checkout),
) -> dict[str, object]:
    """Read the order from PayPal Sandbox and from the local record."""
    return checkout.get_order(order_id)


@app.post("/payments/{order_id}/capture")
def capture_payment(
    order_id: str,
    body: CaptureIn,
    checkout: CheckoutService = Depends(get_checkout),
) -> dict[str, object]:
    """Capture an approved order after explicit confirmation.

    This is the only capture route. A created order is not a completed payment.
    """
    return checkout.capture(order_id, body.confirm)
