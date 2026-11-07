"""Create a PayPal order and capture it only after two separate steps.

The server prices the cart from the catalog. The buyer approves that order
on PayPal. Capture runs only when this API is called again with confirm=true.
"""

import re
import uuid
from decimal import Decimal

from catalog import load_products
from money import euros_from_catalog, euros_from_paypal, format_euros, line_total
from order_store import OrderConflict, OrderNotFound, OrderStore, StoredOrder
from paypal import (
    PayPalAuthError,
    PayPalClient,
    PayPalError,
    PayPalRequestError,
    PayPalResponseError,
    PayPalSecurityError,
    PayPalTimeout,
    PayPalUnavailable,
    RemoteOrder,
)

ORDER_ID_RE = re.compile(r"^[A-Z0-9]{1,36}$")
MAX_QUANTITY = 99


class CheckoutError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        issue: str | None = None,
        debug_id: str | None = None,
        payment_status: str | None = None,
        order_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        detail: dict[str, str] = {"message": message}
        if issue:
            detail["issue"] = issue
        if debug_id:
            detail["debug_id"] = debug_id
        if payment_status:
            detail["payment_status"] = payment_status
        if order_id:
            detail["order_id"] = order_id
        self.detail = detail


class CheckoutService:
    def __init__(
        self,
        paypal: PayPalClient,
        store: OrderStore,
        catalog_loader=load_products,
    ):
        self._paypal = paypal
        self._store = store
        self._catalog_loader = catalog_loader

    def create_order(self, items: list[tuple[str, int]]) -> dict[str, object]:
        lines, total = self._price_cart(items)
        request_id = str(uuid.uuid4())
        remote = self._call_paypal(
            lambda: self._paypal.create_order(total=total, lines=lines, request_id=request_id)
        )
        if not ORDER_ID_RE.fullmatch(remote.order_id):
            raise CheckoutError(
                "PayPal Sandbox returned an unexpected order id.",
                status_code=502,
            )
        if remote.approval_url is None:
            raise CheckoutError(
                "PayPal Sandbox did not return an approval link.",
                status_code=502,
            )
        stored = StoredOrder(
            order_id=remote.order_id,
            payment_status="NOT_PAID",
            paypal_status=remote.status,
            currency="EUR",
            amount=format_euros(total),
            items=lines,
            approval_url=remote.approval_url,
            capture_id=None,
            capture_request_id=str(uuid.uuid4()),
            capture_state="IDLE",
        )
        self._store.insert(stored)
        return _public_view(
            stored,
            "Order created. The buyer still needs to approve it on PayPal. "
            "Money has not been captured.",
            amount_verified=True,
        )

    def capture(self, order_id: str, confirm: bool) -> dict[str, object]:
        if confirm is not True:
            raise CheckoutError(
                "Capture requires explicit confirmation. Send confirm=true after the buyer approves the order.",
                status_code=400,
            )
        self._require_order_id(order_id)
        reserved = self._reserve(order_id)
        try:
            return self._capture_reserved(reserved)
        except CheckoutError:
            self._store.release_reservation(order_id)
            raise
        except PayPalError as exc:
            self._store.release_reservation(order_id)
            raise _translate_paypal(exc) from exc
        except Exception:
            self._store.release_reservation(order_id)
            raise

    def get_order(self, order_id: str) -> dict[str, object]:
        self._require_order_id(order_id)
        stored = self._require_local_order(order_id)
        remote = self._call_paypal(lambda: self._paypal.get_order(order_id))
        verified = _amounts_match(stored, remote)
        if not verified:
            return _public_view(
                stored,
                "PayPal reported an amount that does not match this server. "
                "The payment was not marked as completed.",
                amount_verified=False,
            )
        if remote.status == "COMPLETED" and remote.capture_status == "COMPLETED":
            if stored.payment_status != "PAID":
                stored = self._store.mark_paid(order_id, remote.capture_id)
            return _public_view(stored, "Payment completed.", amount_verified=True)
        if remote.capture_status == "DECLINED":
            stored = self._store.mark_declined(order_id, "DECLINED")
            return _public_view(
                stored,
                "The payment was declined. No money was captured.",
                amount_verified=True,
            )
        if stored.paypal_status != remote.status and stored.payment_status != "PAID":
            stored = self._store.update_paypal_status(order_id, remote.status)
        return _public_view(stored, _status_message(stored), amount_verified=True)

    def _capture_reserved(self, stored: StoredOrder) -> dict[str, object]:
        remote = self._call_paypal(lambda: self._paypal.get_order(stored.order_id))
        if not _amounts_match(stored, remote):
            raise CheckoutError(
                "The PayPal order amount does not match the amount calculated by the server. Capture was refused.",
                status_code=409,
                payment_status="NOT_PAID",
                order_id=stored.order_id,
            )
        if remote.status == "COMPLETED" and remote.capture_status == "COMPLETED":
            paid = self._store.mark_paid(stored.order_id, remote.capture_id)
            return _public_view(
                paid,
                "Payment was already completed. It was not captured again.",
                amount_verified=True,
            )
        if remote.status != "APPROVED":
            raise CheckoutError(
                "The buyer has not approved this order, so it was not captured.",
                status_code=409,
                payment_status="NOT_PAID",
                order_id=stored.order_id,
            )
        try:
            captured = self._paypal.capture_order(stored.order_id, stored.capture_request_id)
        except PayPalRequestError as exc:
            return self._handle_capture_error(stored, exc)
        if not _amounts_match(stored, captured):
            reviewed = self._store.mark_review(stored.order_id)
            raise CheckoutError(
                "PayPal captured an unexpected amount. The payment is not marked as completed.",
                status_code=502,
                payment_status=reviewed.payment_status,
                order_id=stored.order_id,
            )
        if captured.status == "COMPLETED" and captured.capture_status == "COMPLETED" and captured.capture_id:
            paid = self._store.mark_paid(stored.order_id, captured.capture_id)
            return _public_view(paid, "Payment completed.", amount_verified=True)
        if captured.capture_status == "DECLINED" or captured.status in {"DECLINED", "VOIDED"}:
            declined = self._store.mark_declined(stored.order_id, captured.status)
            raise CheckoutError(
                "The payment was declined. No money was captured.",
                status_code=402,
                payment_status="DECLINED",
                order_id=declined.order_id,
            )
        raise CheckoutError(
            "PayPal Sandbox did not complete the capture.",
            status_code=502,
            payment_status="NOT_PAID",
            order_id=stored.order_id,
        )

    def _handle_capture_error(self, stored: StoredOrder, exc: PayPalRequestError) -> dict[str, object]:
        if exc.issue == "INSTRUMENT_DECLINED":
            self._store.mark_declined(stored.order_id, "DECLINED")
            raise CheckoutError(
                "The buyer's funding source was declined. No money was captured.",
                status_code=402,
                issue=exc.issue,
                debug_id=exc.debug_id,
                payment_status="DECLINED",
                order_id=stored.order_id,
            ) from exc
        if exc.issue in {"ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"}:
            raise CheckoutError(
                "The buyer has not approved this order, so it was not captured.",
                status_code=409,
                issue=exc.issue,
                debug_id=exc.debug_id,
                payment_status="NOT_PAID",
                order_id=stored.order_id,
            ) from exc
        if exc.issue == "ORDER_ALREADY_CAPTURED":
            remote = self._call_paypal(lambda: self._paypal.get_order(stored.order_id))
            if (
                _amounts_match(stored, remote)
                and remote.status == "COMPLETED"
                and remote.capture_status == "COMPLETED"
            ):
                paid = self._store.mark_paid(stored.order_id, remote.capture_id)
                raise CheckoutError(
                    "This order was already captured. It was not captured again.",
                    status_code=409,
                    issue=exc.issue,
                    debug_id=exc.debug_id,
                    payment_status="PAID",
                    order_id=paid.order_id,
                ) from exc
            self._store.mark_review(stored.order_id)
            raise CheckoutError(
                "PayPal reported this order as already captured, but the amount did not match.",
                status_code=502,
                issue=exc.issue,
                debug_id=exc.debug_id,
                payment_status="REVIEW",
                order_id=stored.order_id,
            ) from exc
        raise CheckoutError(
            "PayPal Sandbox rejected the capture.",
            status_code=502,
            issue=exc.issue,
            debug_id=exc.debug_id,
            payment_status="NOT_PAID",
            order_id=stored.order_id,
        ) from exc

    def _price_cart(self, items: list[tuple[str, int]]) -> tuple[list[dict[str, object]], Decimal]:
        if not items:
            raise CheckoutError("Add at least one product.", status_code=400)
        catalog = {product.id: product for product in self._catalog_loader()}
        seen: set[str] = set()
        lines: list[dict[str, object]] = []
        total = Decimal("0.00")
        for product_id, quantity in items:
            if product_id in seen:
                raise CheckoutError(
                    f"Product {product_id} was sent more than once. Combine the quantity into one line.",
                    status_code=400,
                )
            seen.add(product_id)
            if quantity < 1 or quantity > MAX_QUANTITY:
                raise CheckoutError(
                    f"Quantity for {product_id} must be a whole number from 1 to {MAX_QUANTITY}.",
                    status_code=400,
                )
            product = catalog.get(product_id)
            if product is None:
                raise CheckoutError(f"Unknown product id: {product_id}.", status_code=400)
            available = product.stock - self._store.committed_units(product_id)
            if quantity > available:
                raise CheckoutError(
                    f"Not enough stock for product {product_id}. Available: {max(available, 0)}.",
                    status_code=409,
                    payment_status="NOT_PAID",
                )
            unit = euros_from_catalog(product.price_eur)
            total_line = line_total(unit, quantity)
            total += total_line
            lines.append(
                {
                    "product_id": product.id,
                    "name": product.name,
                    "quantity": quantity,
                    "unit_price": format_euros(unit),
                    "line_total": format_euros(total_line),
                }
            )
        return lines, total

    def _reserve(self, order_id: str) -> StoredOrder:
        stock = {product.id: product.stock for product in self._catalog_loader()}
        try:
            return self._store.reserve_for_capture(order_id, stock)
        except OrderNotFound as exc:
            raise CheckoutError("Unknown order id.", status_code=404) from exc
        except OrderConflict as exc:
            raise CheckoutError(
                str(exc),
                status_code=409,
                payment_status=exc.payment_status,
                order_id=exc.order_id,
            ) from exc

    def _require_local_order(self, order_id: str) -> StoredOrder:
        stored = self._store.get(order_id)
        if stored is None:
            raise CheckoutError("Unknown order id.", status_code=404)
        return stored

    def _require_order_id(self, order_id: str) -> None:
        if not ORDER_ID_RE.fullmatch(order_id):
            raise CheckoutError("The order id is not a PayPal order id.", status_code=400)

    def _call_paypal(self, action):
        try:
            return action()
        except PayPalError as exc:
            raise _translate_paypal(exc) from exc


def _translate_paypal(exc: PayPalError) -> CheckoutError:
    if isinstance(exc, PayPalAuthError):
        return CheckoutError(str(exc), status_code=502)
    if isinstance(exc, PayPalTimeout):
        return CheckoutError(str(exc), status_code=504)
    if isinstance(exc, PayPalSecurityError):
        return CheckoutError(str(exc), status_code=503)
    if isinstance(exc, PayPalRequestError):
        return CheckoutError(str(exc), status_code=502, issue=exc.issue, debug_id=exc.debug_id)
    if isinstance(exc, (PayPalUnavailable, PayPalResponseError)):
        return CheckoutError(str(exc), status_code=502)
    return CheckoutError("PayPal Sandbox could not complete the request.", status_code=502)


def _amounts_match(stored: StoredOrder, remote: RemoteOrder) -> bool:
    try:
        return remote.currency == "EUR" and euros_from_paypal(remote.amount) == euros_from_paypal(stored.amount)
    except ValueError:
        return False


def _status_message(stored: StoredOrder) -> str:
    if stored.payment_status == "PAID":
        return "Payment completed."
    if stored.payment_status == "DECLINED" and stored.paypal_status == "APPROVED":
        return (
            "The previous capture was declined. Approve another funding source on PayPal, "
            "then send confirm=true. Money has not been captured."
        )
    if stored.payment_status == "DECLINED":
        return "The payment was declined. No money was captured."
    if stored.payment_status == "REVIEW":
        return "PayPal returned an unexpected capture result. The payment is not marked as completed."
    if stored.paypal_status == "APPROVED":
        return (
            "The buyer approved this order. Money has not been captured. "
            "Send confirm=true to capture it."
        )
    return (
        "Order created. The buyer has not approved it on PayPal. Money has not been captured."
    )


def _public_view(stored: StoredOrder, message: str, *, amount_verified: bool) -> dict[str, object]:
    return {
        "order_id": stored.order_id,
        "order_status": stored.paypal_status,
        "payment_status": stored.payment_status,
        "payment_completed": stored.payment_status == "PAID",
        "amount": stored.amount,
        "currency": stored.currency,
        "approval_url": stored.approval_url,
        "capture_id": stored.capture_id,
        "items": stored.items,
        "message": message,
        "amount_verified": amount_verified,
    }
