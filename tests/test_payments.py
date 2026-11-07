"""Payment tests. PayPal is mocked, so these tests make no network calls."""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx2
from fastapi.testclient import TestClient

from catalog import Product
from checkout import CheckoutService
from main import app, get_checkout
from order_store import OrderStore
from paypal import (
    HttpResult,
    HttpxTransport,
    PayPalClient,
    PayPalSecurityError,
    PayPalTimeout,
    assert_sandbox_api_url,
)
from settings import Settings, SettingsError, load_settings

TEST_SECRET = "sandbox-test-secret"
ORDER_ID = "5O190127TN364715T"
SECOND_ORDER_ID = "6O190127TN364715U"
CAPTURE_ID = "3C679366HH908993F"


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class FakePayPal:
    """Records PayPal HTTP calls and returns scripted responses."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.queues: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.token_status = 200

    def push(self, method: str, path: str, status: int, payload: object) -> None:
        self.queues.setdefault((method, path), []).append((status, payload))

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict | None = None,
        form_body: dict[str, str] | None = None,
    ) -> HttpResult:
        assert_sandbox_api_url(url)
        path = urlsplit(url).path
        self.calls.append(
            {
                "method": method,
                "path": path,
                "url": url,
                "headers": dict(headers),
                "json": json_body,
                "form": form_body,
            }
        )
        if path == "/v1/oauth2/token":
            if self.token_status != 200:
                return HttpResult(self.token_status, {"error": "invalid_client"})
            return HttpResult(
                200,
                {"access_token": "test-access-token", "expires_in": 320, "token_type": "Bearer"},
            )
        queued = self.queues.get((method, path), [])
        if not queued:
            raise AssertionError(f"no scripted PayPal response for {method} {path}")
        status, payload = queued.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return HttpResult(status, payload if isinstance(payload, dict) else None)


def order_payload(order_id: str, amount: str, status: str, capture_status: str | None = None) -> dict:
    unit: dict[str, object] = {"amount": {"currency_code": "EUR", "value": amount}}
    if capture_status is not None:
        unit["payments"] = {
            "captures": [
                {
                    "id": CAPTURE_ID,
                    "status": capture_status,
                    "amount": {"currency_code": "EUR", "value": amount},
                }
            ]
        }
    return {
        "id": order_id,
        "status": status,
        "purchase_units": [unit],
        "links": [
            {
                "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}",
                "rel": "payer-action",
                "method": "GET",
            }
        ],
    }


def one_unit_catalog() -> list[Product]:
    return [
        Product(
            id="hp-001",
            name="Auralis Quiet 40",
            category="headphones",
            description="A fictional headphone used by the payment tests.",
            price_eur=129.0,
            features=["bluetooth"],
            stock=1,
        )
    ]


class PaymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakePayPal()
        self.db_path = Path(self.temp.name) / "orders.sqlite"
        self.store = OrderStore(self.db_path)
        self.store.initialize()
        settings = Settings(
            client_id="sandbox-test-id",
            client_secret=TEST_SECRET,
            mode="sandbox",
            return_url="http://127.0.0.1:8000/payments/return",
            cancel_url="http://127.0.0.1:8000/payments/cancel",
            database_path=self.db_path,
        )
        paypal = PayPalClient(settings, self.fake)
        self.service = CheckoutService(paypal, self.store)
        app.dependency_overrides[get_checkout] = lambda: self.service
        self.client = TestClient(app)
        self.logs = ListHandler()
        self.logger = logging.getLogger("nulixpay.paypal")
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.logs)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.logger.removeHandler(self.logs)

    def assert_no_secrets(self, response_text: str) -> None:
        for secret in (TEST_SECRET, "test-access-token", "sandbox-test-id", "Bearer ", "Basic "):
            self.assertNotIn(secret, response_text)
            for message in self.logs.messages:
                self.assertNotIn(secret, message)

    def capture_calls(self) -> list[dict[str, object]]:
        return [call for call in self.fake.calls if str(call["path"]).endswith("/capture")]

    def test_create_order_prices_from_catalog(self) -> None:
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(ORDER_ID, "56.70", "PAYER_ACTION_REQUIRED"))
        response = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "ac-003", "quantity": 3}]},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["amount"], "56.70")
        self.assertEqual(body["currency"], "EUR")
        self.assertEqual(body["payment_status"], "NOT_PAID")
        self.assertFalse(body["payment_completed"])
        self.assertTrue(body["approval_url"].startswith("https://www.sandbox.paypal.com/"))
        paypal_body = next(
            call["json"] for call in self.fake.calls if call["path"] == "/v2/checkout/orders"
        )
        amount = paypal_body["purchase_units"][0]["amount"]
        self.assertEqual(amount["currency_code"], "EUR")
        self.assertEqual(amount["value"], "56.70")
        self.assertEqual(paypal_body["purchase_units"][0]["items"][0]["unit_amount"]["value"], "18.90")
        self.assertEqual(paypal_body["purchase_units"][0]["items"][0]["quantity"], "3")
        self.assert_no_secrets(response.text)
        reloaded = OrderStore(self.db_path)
        self.assertEqual(reloaded.get(ORDER_ID).amount, "56.70")

    def test_browser_amount_is_rejected(self) -> None:
        response = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}], "amount": "0.01"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fake.calls, [])

    def test_unknown_product_and_bad_quantity(self) -> None:
        unknown = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "missing", "quantity": 1}]},
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("Unknown product", unknown.json()["detail"]["message"])
        duplicate = self.client.post(
            "/payments/create-order",
            json={
                "items": [
                    {"product_id": "hp-001", "quantity": 1},
                    {"product_id": "hp-001", "quantity": 1},
                ]
            },
        )
        self.assertEqual(duplicate.status_code, 400)
        zero = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 0}]},
        )
        self.assertEqual(zero.status_code, 422)
        self.assertEqual(self.fake.calls, [])

    def test_insufficient_stock_does_not_call_paypal(self) -> None:
        response = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "sw-003", "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Not enough stock", response.json()["detail"]["message"])
        self.assertEqual(self.fake.calls, [])

    def test_auth_failure_hides_the_secret(self) -> None:
        self.fake.token_status = 401
        response = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("credentials", response.json()["detail"]["message"])
        self.assert_no_secrets(response.text)
        self.assertFalse(any(call["path"] == "/v2/checkout/orders" for call in self.fake.calls))

    def test_capture_requires_approval_and_confirmation(self) -> None:
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"))
        created = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        self.assertEqual(created.status_code, 201)
        missing = self.client.post(f"/payments/{ORDER_ID}/capture", json={})
        rejected = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": False})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(rejected.status_code, 422)
        self.fake.push(
            "GET",
            f"/v2/checkout/orders/{ORDER_ID}",
            200,
            order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"),
        )
        too_early = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(too_early.status_code, 409)
        self.assertFalse(too_early.json()["detail"]["payment_status"] == "PAID")
        self.assertEqual(self.capture_calls(), [])
        self.assertEqual(self.store.get(ORDER_ID).payment_status, "NOT_PAID")

    def test_capture_after_approval_and_duplicate_capture(self) -> None:
        self._script_paid_order(ORDER_ID, "129.00", "hp-001", 1)
        first = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["payment_completed"])
        self.assertEqual(first.json()["payment_status"], "PAID")
        self.assertEqual(first.json()["order_status"], "COMPLETED")
        self.assertEqual(first.json()["capture_id"], CAPTURE_ID)
        self.assert_no_secrets(first.text)
        second = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"]["payment_status"], "PAID")
        self.assertEqual(len(self.capture_calls()), 1)

    def test_same_capture_request_id_is_reused_after_timeout(self) -> None:
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"))
        self.client.post("/payments/create-order", json={"items": [{"product_id": "hp-001", "quantity": 1}]})
        for _ in range(2):
            self.fake.push(
                "GET",
                f"/v2/checkout/orders/{ORDER_ID}",
                200,
                order_payload(ORDER_ID, "129.00", "APPROVED"),
            )
        self.fake.push(
            "POST",
            f"/v2/checkout/orders/{ORDER_ID}/capture",
            0,
            PayPalTimeout("PayPal Sandbox did not respond in time."),
        )
        self.fake.push(
            "POST",
            f"/v2/checkout/orders/{ORDER_ID}/capture",
            201,
            order_payload(ORDER_ID, "129.00", "COMPLETED", "COMPLETED"),
        )
        timed_out = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(timed_out.status_code, 504)
        completed = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(completed.status_code, 200)
        request_ids = [call["headers"]["PayPal-Request-Id"] for call in self.capture_calls()]
        self.assertEqual(request_ids[0], request_ids[1])

    def test_amount_mismatch_blocks_capture(self) -> None:
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"))
        self.client.post("/payments/create-order", json={"items": [{"product_id": "hp-001", "quantity": 1}]})
        self.fake.push(
            "GET",
            f"/v2/checkout/orders/{ORDER_ID}",
            200,
            order_payload(ORDER_ID, "1.00", "APPROVED"),
        )
        response = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.capture_calls(), [])
        self.assertEqual(self.store.get(ORDER_ID).payment_status, "NOT_PAID")

    def test_declined_payment_does_not_consume_stock(self) -> None:
        self.service = CheckoutService(self.service._paypal, self.store, one_unit_catalog)
        app.dependency_overrides[get_checkout] = lambda: self.service
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"))
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(SECOND_ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"))
        self.fake.push(
            "GET",
            f"/v2/checkout/orders/{ORDER_ID}",
            200,
            order_payload(ORDER_ID, "129.00", "APPROVED"),
        )
        self.fake.push(
            "POST",
            f"/v2/checkout/orders/{ORDER_ID}/capture",
            422,
            {
                "name": "UNPROCESSABLE_ENTITY",
                "message": "The requested action could not be performed.",
                "debug_id": "dbg123",
                "details": [{"issue": "INSTRUMENT_DECLINED"}],
            },
        )
        created = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        declined = self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.assertEqual(declined.status_code, 402)
        self.assertEqual(declined.json()["detail"]["payment_status"], "DECLINED")
        self.assertEqual(declined.json()["detail"]["issue"], "INSTRUMENT_DECLINED")
        self.assertEqual(declined.json()["detail"]["debug_id"], "dbg123")
        self.assert_no_secrets(declined.text)
        again = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(again.status_code, 201)
        self.assertEqual(again.json()["order_id"], SECOND_ORDER_ID)

    def test_paid_order_consumes_stock(self) -> None:
        self.service = CheckoutService(self.service._paypal, self.store, one_unit_catalog)
        app.dependency_overrides[get_checkout] = lambda: self.service
        self._script_paid_order(ORDER_ID, "129.00", "hp-001", 1)
        self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        sold_out = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        self.assertEqual(sold_out.status_code, 409)

    def test_get_distinguishes_created_order_from_completed_payment(self) -> None:
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"))
        self.client.post("/payments/create-order", json={"items": [{"product_id": "hp-001", "quantity": 1}]})
        self.fake.push(
            "GET",
            f"/v2/checkout/orders/{ORDER_ID}",
            200,
            order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED"),
        )
        pending = self.client.get(f"/payments/{ORDER_ID}")
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["payment_status"], "NOT_PAID")
        self.assertFalse(pending.json()["payment_completed"])
        self.fake.push(
            "GET",
            f"/v2/checkout/orders/{ORDER_ID}",
            200,
            order_payload(ORDER_ID, "129.00", "APPROVED"),
        )
        self.fake.push(
            "POST",
            f"/v2/checkout/orders/{ORDER_ID}/capture",
            201,
            order_payload(ORDER_ID, "129.00", "COMPLETED", "COMPLETED"),
        )
        self.client.post(f"/payments/{ORDER_ID}/capture", json={"confirm": True})
        self.fake.push(
            "GET",
            f"/v2/checkout/orders/{ORDER_ID}",
            200,
            order_payload(ORDER_ID, "129.00", "COMPLETED", "COMPLETED"),
        )
        paid = self.client.get(f"/payments/{ORDER_ID}")
        self.assertTrue(paid.json()["payment_completed"])
        self.assertEqual(paid.json()["payment_status"], "PAID")

    def test_paypal_server_error_is_reported_without_payer_data(self) -> None:
        self.fake.push(
            "POST",
            "/v2/checkout/orders",
            500,
            {
                "name": "INTERNAL_SERVER_ERROR",
                "message": "A server error occurred.",
                "debug_id": "dbg500",
                "payer": {"email_address": "buyer@example.com"},
            },
        )
        response = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["debug_id"], "dbg500")
        self.assertNotIn("buyer@example.com", response.text)
        self.assert_no_secrets(response.text)

    def test_unknown_and_invalid_order_ids(self) -> None:
        missing = self.client.get(f"/payments/{ORDER_ID}")
        invalid = self.client.post("/payments/not-a-paypal-id/capture", json={"confirm": True})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.fake.calls, [])

    def test_return_and_cancel_do_not_capture(self) -> None:
        def forbid_checkout() -> CheckoutService:
            raise AssertionError("buyer return must not start checkout")

        app.dependency_overrides[get_checkout] = forbid_checkout
        returned = self.client.get(
            "/payments/return",
            params={"token": ORDER_ID, "PayerID": "PAYER123"},
        )
        cancelled = self.client.get("/payments/cancel", params={"token": ORDER_ID})
        self.assertEqual(returned.status_code, 200)
        self.assertFalse(returned.json()["payment_completed"])
        self.assertEqual(returned.json()["payment_status"], "NOT_PAID")
        self.assertEqual(returned.json()["order_id"], ORDER_ID)
        self.assertNotIn("PAYER123", returned.text)
        self.assertFalse(cancelled.json()["payment_completed"])
        self.assertEqual(self.fake.calls, [])

    def test_browser_return_redirects_without_capturing_or_echoing_payer_id(self) -> None:
        returned = self.client.get(
            "/payments/return",
            params={"token": ORDER_ID, "PayerID": "PAYER123"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(returned.status_code, 302)
        self.assertEqual(
            returned.headers["location"],
            f"http://127.0.0.1:5173/return?token={ORDER_ID}",
        )
        self.assertNotIn("PAYER123", returned.headers["location"])
        self.assertEqual(self.fake.calls, [])

    def test_live_link_is_rejected(self) -> None:
        payload = order_payload(ORDER_ID, "129.00", "PAYER_ACTION_REQUIRED")
        payload["links"] = [
            {
                "href": "https://www.paypal.com/checkoutnow?token=5O190127TN364715T",
                "rel": "approve",
                "method": "GET",
            }
        ]
        self.fake.push("POST", "/v2/checkout/orders", 201, payload)
        response = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": "hp-001", "quantity": 1}]},
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("www.paypal.com", response.text)
        self.assertIsNone(self.store.get(ORDER_ID))

    def test_transport_blocks_live_api_before_any_request(self) -> None:
        class Spy:
            def request(self, *args, **kwargs):
                raise AssertionError("network was used")

        transport = HttpxTransport(10, client=Spy())
        with self.assertRaises(PayPalSecurityError):
            transport.send("GET", "https://api-m.paypal.com/v2/checkout/orders/ABC", {})

    def test_httpx_timeout_becomes_paypal_timeout(self) -> None:
        class TimeoutClient:
            def request(self, *args, **kwargs):
                raise httpx2.ReadTimeout("timed out")

        transport = HttpxTransport(10, client=TimeoutClient())
        with self.assertRaises(PayPalTimeout):
            transport.send("GET", "https://api-m.sandbox.paypal.com/v2/checkout/orders/ABC", {})

    def test_settings_reject_live_mode_and_missing_credentials(self) -> None:
        missing_file = Path(self.temp.name) / "missing.env"
        with patch.dict(
            os.environ,
            {"PAYPAL_MODE": "live", "PAYPAL_CLIENT_ID": "live-id", "PAYPAL_CLIENT_SECRET": TEST_SECRET},
        ):
            with self.assertRaises(SettingsError) as caught:
                load_settings(missing_file)
        self.assertNotIn(TEST_SECRET, str(caught.exception))
        self.assertIn("sandbox", str(caught.exception).lower())
        with patch.dict(
            os.environ,
            {"PAYPAL_MODE": "", "PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""},
        ):
            with self.assertRaises(SettingsError) as missing_mode:
                load_settings(missing_file)
        self.assertIn("PAYPAL_MODE is missing", str(missing_mode.exception))
        with patch.dict(
            os.environ,
            {"PAYPAL_MODE": "sandbox", "PAYPAL_CLIENT_ID": "", "PAYPAL_CLIENT_SECRET": ""},
        ):
            with self.assertRaises(SettingsError) as missing:
                load_settings(missing_file)
        self.assertIn("PAYPAL_CLIENT_ID", str(missing.exception))

    def _script_paid_order(self, order_id: str, amount: str, product_id: str, quantity: int) -> None:
        self.fake.push("POST", "/v2/checkout/orders", 201, order_payload(order_id, amount, "PAYER_ACTION_REQUIRED"))
        created = self.client.post(
            "/payments/create-order",
            json={"items": [{"product_id": product_id, "quantity": quantity}]},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.fake.push("GET", f"/v2/checkout/orders/{order_id}", 200, order_payload(order_id, amount, "APPROVED"))
        self.fake.push(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            201,
            order_payload(order_id, amount, "COMPLETED", "COMPLETED"),
        )
