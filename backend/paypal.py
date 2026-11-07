"""PayPal Orders API v2 client for the sandbox only.

This module obtains an OAuth access token and calls the Orders API. It never
logs the token, the client secret, or payer details.
"""

import base64
import logging
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

import httpx2

from money import euros_from_paypal, format_euros
from settings import SANDBOX_API_ORIGIN, Settings

logger = logging.getLogger("nulixpay.paypal")

SANDBOX_API_HOST = "api-m.sandbox.paypal.com"
SANDBOX_APPROVAL_HOSTS = {"www.sandbox.paypal.com", "sandbox.paypal.com"}
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class PayPalError(Exception):
    """Base class for sandbox call failures."""


class PayPalAuthError(PayPalError):
    """Sandbox rejected the server credentials."""


class PayPalTimeout(PayPalError):
    """The sandbox did not answer before the timeout."""


class PayPalUnavailable(PayPalError):
    """The sandbox could not be reached."""


class PayPalSecurityError(PayPalError):
    """A request or link was not on the sandbox host."""


class PayPalResponseError(PayPalError):
    """The sandbox answered with a body this server will not trust."""


class PayPalRequestError(PayPalError):
    """The sandbox rejected an order call."""

    def __init__(
        self,
        message: str,
        *,
        issue: str | None = None,
        debug_id: str | None = None,
        paypal_name: str | None = None,
    ):
        super().__init__(message)
        self.issue = issue
        self.debug_id = debug_id
        self.paypal_name = paypal_name


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    payload: dict | None


@dataclass(frozen=True)
class RemoteOrder:
    order_id: str
    status: str
    currency: str
    amount: str
    approval_url: str | None
    capture_id: str | None
    capture_status: str | None


class HttpxTransport:
    """HTTP calls with a hard timeout and no redirect following."""

    def __init__(self, timeout_seconds: float, client: object | None = None):
        self._owns_client = client is None
        self._client = client or httpx2.Client(
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict | None = None,
        form_body: dict[str, str] | None = None,
    ) -> HttpResult:
        assert_sandbox_api_url(url)
        request_kwargs: dict[str, object] = {}
        if json_body is not None:
            request_kwargs["json"] = json_body
        if form_body is not None:
            request_kwargs["data"] = form_body
        try:
            response = self._client.request(method, url, headers=headers, **request_kwargs)
        except httpx2.TimeoutException as exc:
            raise PayPalTimeout("PayPal Sandbox did not respond in time.") from exc
        except httpx2.TransportError as exc:
            raise PayPalUnavailable("Could not reach PayPal Sandbox.") from exc
        payload = _json_object(response)
        return HttpResult(response.status_code, payload)


class PayPalClient:
    def __init__(self, settings: Settings, transport: HttpxTransport | object):
        if settings.mode != "sandbox":
            raise PayPalSecurityError(
                "PAYPAL_MODE must be sandbox. This server never calls the live PayPal API."
            )
        self._settings = settings
        self._transport = transport
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._token_deadline = 0.0

    def create_order(
        self,
        *,
        total: Decimal,
        lines: list[dict[str, object]],
        request_id: str,
    ) -> RemoteOrder:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": "nulixpay",
                    "description": "NuLixPay sandbox order",
                    "custom_id": request_id,
                    "amount": {
                        "currency_code": "EUR",
                        "value": format_euros(total),
                        "breakdown": {
                            "item_total": {
                                "currency_code": "EUR",
                                "value": format_euros(total),
                            }
                        },
                    },
                    "items": [_paypal_item(line) for line in lines],
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": "NuLixPay",
                        "shipping_preference": "NO_SHIPPING",
                        "user_action": "PAY_NOW",
                        "return_url": self._settings.return_url,
                        "cancel_url": self._settings.cancel_url,
                    }
                }
            },
        }
        payload = self._request(
            "POST",
            "/v2/checkout/orders",
            json_body=body,
            request_id=request_id,
            allowed=(200, 201),
        )
        remote = parse_remote_order(payload)
        if remote.currency != "EUR" or euros_from_paypal(remote.amount) != total:
            raise PayPalResponseError("PayPal Sandbox confirmed an unexpected amount.")
        if remote.approval_url is None:
            raise PayPalResponseError("PayPal Sandbox did not return an approval link.")
        return remote

    def get_order(self, order_id: str) -> RemoteOrder:
        payload = self._request(
            "GET",
            f"/v2/checkout/orders/{order_id}",
            allowed=(200,),
        )
        return parse_remote_order(payload)

    def capture_order(self, order_id: str, request_id: str) -> RemoteOrder:
        payload = self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            json_body={},
            request_id=request_id,
            allowed=(200, 201),
        )
        return parse_remote_order(payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        request_id: str | None = None,
        allowed: tuple[int, ...],
        retried: bool = False,
    ) -> dict:
        url = sandbox_url(path)
        headers = {
            "Authorization": f"Bearer {self._bearer_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if request_id is not None:
            headers["PayPal-Request-Id"] = request_id
        result = self._transport.send(method, url, headers, json_body=json_body)
        logger.info("PayPal sandbox %s %s -> %s", method, path, result.status_code)
        if result.status_code == 401 and not retried:
            with self._lock:
                self._access_token = None
            return self._request(
                method,
                path,
                json_body=json_body,
                request_id=request_id,
                allowed=allowed,
                retried=True,
            )
        if result.status_code not in allowed:
            raise _error_from_response(result, self._settings.client_secret)
        if result.payload is None:
            raise PayPalResponseError("PayPal Sandbox returned an unreadable response.")
        return result.payload

    def _bearer_token(self) -> str:
        with self._lock:
            if self._access_token and time.monotonic() < self._token_deadline:
                return self._access_token
            token, lifetime = self._request_token()
            self._access_token = token
            self._token_deadline = time.monotonic() + lifetime
            return token

    def _request_token(self) -> tuple[str, float]:
        raw = f"{self._settings.client_id}:{self._settings.client_secret}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        url = sandbox_url("/v1/oauth2/token")
        headers = {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        result = self._transport.send(
            "POST",
            url,
            headers,
            form_body={"grant_type": "client_credentials"},
        )
        logger.info("PayPal sandbox POST /v1/oauth2/token -> %s", result.status_code)
        if result.status_code != 200 or result.payload is None:
            raise PayPalAuthError("PayPal Sandbox rejected the client credentials.")
        token = result.payload.get("access_token")
        expires_in = result.payload.get("expires_in", 0)
        if not isinstance(token, str) or not token:
            raise PayPalAuthError("PayPal Sandbox rejected the client credentials.")
        if not isinstance(expires_in, int):
            expires_in = 60
        lifetime = float(max(expires_in - 30, 1))
        return token, lifetime


def assert_sandbox_api_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != SANDBOX_API_HOST:
        raise PayPalSecurityError(
            "Blocked a PayPal request that was not aimed at the sandbox API."
        )


def sandbox_url(path: str) -> str:
    if not path.startswith("/") or ".." in path or "?" in path:
        raise PayPalSecurityError("Blocked a PayPal request that was not aimed at the sandbox API.")
    return _checked_sandbox_url(SANDBOX_API_ORIGIN + path)


def _checked_sandbox_url(url: str) -> str:
    assert_sandbox_api_url(url)
    return url


def parse_remote_order(payload: dict) -> RemoteOrder:
    order_id = payload.get("id")
    status = payload.get("status")
    if not isinstance(order_id, str) or not isinstance(status, str):
        raise PayPalResponseError("PayPal Sandbox returned an incomplete order.")
    currency, amount = _amount(payload)
    capture_id, capture_status = _capture(payload)
    return RemoteOrder(
        order_id=order_id,
        status=status,
        currency=currency,
        amount=amount,
        approval_url=_approval_url(payload.get("links")),
        capture_id=capture_id,
        capture_status=capture_status,
    )


def _amount(payload: dict) -> tuple[str, str]:
    units = payload.get("purchase_units")
    if not isinstance(units, list) or not units or not isinstance(units[0], dict):
        raise PayPalResponseError("PayPal Sandbox did not include the order amount.")
    amount = units[0].get("amount")
    if not isinstance(amount, dict):
        raise PayPalResponseError("PayPal Sandbox did not include the order amount.")
    currency = amount.get("currency_code")
    value = amount.get("value")
    if currency != "EUR" or not isinstance(value, str):
        raise PayPalResponseError("PayPal Sandbox amount was not in EUR.")
    try:
        euros_from_paypal(value)
    except ValueError as exc:
        raise PayPalResponseError("PayPal Sandbox amount was not in EUR cents.") from exc
    return "EUR", format_euros(euros_from_paypal(value))


def _capture(payload: dict) -> tuple[str | None, str | None]:
    units = payload.get("purchase_units")
    if not isinstance(units, list) or not units or not isinstance(units[0], dict):
        return None, None
    payments = units[0].get("payments")
    if not isinstance(payments, dict):
        return None, None
    captures = payments.get("captures")
    if not isinstance(captures, list) or not captures or not isinstance(captures[0], dict):
        return None, None
    capture = captures[0]
    capture_id = capture.get("id")
    capture_status = capture.get("status")
    if capture_id is not None and not isinstance(capture_id, str):
        capture_id = None
    if capture_status is not None and not isinstance(capture_status, str):
        capture_status = None
    amount = capture.get("amount")
    if isinstance(amount, dict) and amount.get("currency_code") not in {None, "EUR"}:
        raise PayPalResponseError("PayPal Sandbox captured an amount that was not in EUR.")
    return capture_id, capture_status


def _approval_url(links: object) -> str | None:
    if links is None:
        return None
    if not isinstance(links, list):
        raise PayPalResponseError("PayPal Sandbox returned an unreadable approval link.")
    chosen: str | None = None
    for rel in ("payer-action", "approve"):
        for link in links:
            if not isinstance(link, dict) or link.get("rel") != rel:
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            parsed = urlsplit(href)
            if parsed.scheme != "https" or parsed.hostname not in SANDBOX_APPROVAL_HOSTS:
                raise PayPalSecurityError(
                    "PayPal Sandbox returned an approval link outside the sandbox."
                )
            chosen = href
            break
        if chosen:
            break
    return chosen


def _paypal_item(line: dict[str, object]) -> dict[str, object]:
    name = str(line["name"])[:127]
    return {
        "name": name,
        "sku": str(line["product_id"]),
        "quantity": str(line["quantity"]),
        "unit_amount": {
            "currency_code": "EUR",
            "value": str(line["unit_price"]),
        },
    }


def _json_object(response: object) -> dict | None:
    content_type = ""
    headers = getattr(response, "headers", {})
    if headers is not None:
        content_type = headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _error_from_response(result: HttpResult, secret: str) -> PayPalError:
    if result.status_code in {401, 403}:
        return PayPalAuthError("PayPal Sandbox rejected the client credentials.")
    payload = result.payload or {}
    issue = _safe_code(_nested_issue(payload))
    debug_id = _safe_code(payload.get("debug_id"))
    paypal_name = _safe_code(payload.get("name"))
    message = _safe_message(payload.get("message"), secret)
    if issue == "INSTRUMENT_DECLINED":
        text = "The buyer's funding source was declined. No money was captured."
    elif message:
        text = message
    else:
        text = "PayPal Sandbox rejected the request."
    return PayPalRequestError(text, issue=issue, debug_id=debug_id, paypal_name=paypal_name)


def _nested_issue(payload: dict) -> object:
    details = payload.get("details")
    if not isinstance(details, list) or not details or not isinstance(details[0], dict):
        return None
    return details[0].get("issue")


def _safe_code(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        return None
    return value


def _safe_message(value: object, secret: str) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > 240 or "@" in text or (secret and secret in text):
        return None
    return text
