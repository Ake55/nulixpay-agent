"""Tests for Channel3 search. The HTTP client is fake, so no credits are used.

Run from the project root:

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient

from channel3_client import (
    CHANNEL3_SEARCH_URL,
    Channel3Client,
    Channel3Error,
    assert_channel3_search_url,
)
from checkout import CheckoutError, CheckoutService
from main import app, get_channel3


SECRET_KEY = "channel3-test-key"
SAMPLE_PAYLOAD = {
    "products": [
        {
            "id": "ch3-prod-1",
            "title": "Harbor Wool Sweater",
            "description": "A real description from Channel3.",
            "brands": [{"id": "brand-1", "name": "Harbor"}],
            "images": [
                {
                    "url": "https://cdn.example.test/sweater.jpg",
                    "cleaned_url": "https://cdn.example.test/sweater-clean.jpg",
                    "is_main_image": True,
                }
            ],
            "offers": [
                {
                    "url": "https://shop.example.test/sweater",
                    "domain": "shop.example.test",
                    "price": {"price": 79.5, "compare_at_price": 90, "currency": "EUR"},
                    "availability": "InStock",
                },
                {
                    "url": "https://other.example.test/sweater",
                    "domain": "other.example.test",
                    "price": {"price": 82, "currency": "USD"},
                    "availability": "OutOfStock",
                },
            ],
        }
    ],
    "next_page_token": None,
}


class RecordingTransport:
    def __init__(self, status_code: int = 200, payload: object = None, error: Exception | None = None):
        self.status_code = status_code
        self.payload = SAMPLE_PAYLOAD if payload is None else payload
        self.error = error
        self.calls: list[tuple[str, dict, dict]] = []

    def post_json(self, url: str, headers: dict[str, str], body: dict) -> tuple[int, object]:
        self.calls.append((url, headers, body))
        if self.error is not None:
            raise self.error
        return self.status_code, self.payload


class ExplodingPayPal:
    def create_order(self, **_kwargs: object) -> None:
        raise AssertionError("PayPal was called for an external product")


class Channel3SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = RecordingTransport()
        self.channel3 = Channel3Client(SECRET_KEY, self.transport)
        app.dependency_overrides[get_channel3] = lambda: self.channel3
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        import main

        main._channel3 = None

    def test_search_preserves_merchant_offers(self) -> None:
        response = self.client.get(
            "/products/external/search",
            params={"query": "wool sweater", "max_price": 100, "limit": 5},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["source"], "channel3")
        self.assertFalse(body["checkout_supported"])
        self.assertIn("does not sell", body["notice"])
        self.assertNotIn(SECRET_KEY, response.text)
        product = body["results"][0]
        self.assertEqual(product["source"], "external_merchant")
        self.assertEqual(product["label"], "External merchant product")
        self.assertEqual(product["channel3_product_id"], "ch3-prod-1")
        self.assertEqual(product["name"], "Harbor Wool Sweater")
        self.assertEqual(product["brands"], ["Harbor"])
        self.assertEqual(product["image_url"], "https://cdn.example.test/sweater-clean.jpg")
        self.assertFalse(product["sold_by_nulixpay"])
        self.assertEqual(len(product["offers"]), 2)
        self.assertEqual(product["offers"][0]["merchant_domain"], "shop.example.test")
        self.assertEqual(product["offers"][0]["purchase_url"], "https://shop.example.test/sweater")
        self.assertEqual(product["offers"][0]["price"], 79.5)
        self.assertEqual(product["offers"][0]["currency"], "EUR")
        self.assertEqual(product["offers"][0]["availability"], "InStock")
        self.assertEqual(product["offers"][0]["compare_at_price"], 90)
        self.assertEqual(product["offers"][1]["currency"], "USD")
        self.assertEqual(product["offers"][1]["availability"], "OutOfStock")

        url, headers, sent = self.transport.calls[0]
        self.assertEqual(url, CHANNEL3_SEARCH_URL)
        self.assertEqual(headers["x-api-key"], SECRET_KEY)
        self.assertEqual(sent["query"], "wool sweater")
        self.assertEqual(sent["limit"], 5)
        self.assertEqual(sent["config"], {"currency": "EUR", "mode": "default"})
        self.assertEqual(sent["filters"]["price"], {"max_price": 100})

    def test_missing_price_is_not_invented(self) -> None:
        self.transport.payload = {
            "products": [
                {
                    "id": "ch3-prod-2",
                    "title": "Unknown Price Jacket",
                    "offers": [
                        {
                            "url": "https://shop.example.test/jacket",
                            "domain": "shop.example.test",
                            "price": {"currency": "EUR"},
                            "availability": "InStock",
                        },
                        {
                            "url": "javascript:alert(1)",
                            "domain": "shop.example.test",
                            "price": {"price": 10, "currency": "EUR"},
                            "availability": "InStock",
                        },
                    ],
                }
            ]
        }
        response = self.client.get("/products/external/search", params={"query": "jacket"})
        self.assertEqual(response.status_code, 200)
        product = response.json()["results"][0]
        self.assertEqual(product["offers"], [])
        self.assertIsNone(product["image_url"])

    def test_unknown_availability_stays_empty(self) -> None:
        self.transport.payload = {
            "products": [
                {
                    "id": "ch3-prod-3",
                    "title": "Plain Hat",
                    "offers": [
                        {
                            "url": "https://shop.example.test/hat",
                            "domain": "shop.example.test",
                            "price": {"price": 12, "currency": "EUR"},
                        }
                    ],
                }
            ]
        }
        response = self.client.get("/products/external/search", params={"query": "hat"})
        offer = response.json()["results"][0]["offers"][0]
        self.assertIsNone(offer["availability"])
        self.assertNotIn("InStock", response.text)

    def test_auth_failure_hides_the_key_and_the_response_body(self) -> None:
        self.transport.status_code = 401
        self.transport.payload = {"detail": f"invalid {SECRET_KEY} buyer@example.com"}
        response = self.client.get("/products/external/search", params={"query": "shoes"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["message"], "Channel3 rejected the API key.")
        self.assertNotIn(SECRET_KEY, response.text)
        self.assertNotIn("buyer@example.com", response.text)

    def test_rate_limit_and_timeout(self) -> None:
        self.transport.status_code = 429
        response = self.client.get("/products/external/search", params={"query": "shoes"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("rate limiting", response.json()["detail"]["message"])

        self.transport.status_code = 200
        self.transport.error = Channel3Error("Channel3 did not respond in time.", status_code=504)
        timed_out = self.client.get("/products/external/search", params={"query": "shoes"})
        self.assertEqual(timed_out.status_code, 504)

    def test_upstream_unavailable(self) -> None:
        self.transport.status_code = 503
        self.transport.payload = {"detail": "secret upstream body"}
        response = self.client.get("/products/external/search", params={"query": "shoes"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["message"], "Channel3 is unavailable.")
        self.assertNotIn("secret upstream", response.text)

    def test_missing_key_does_not_call_channel3(self) -> None:
        app.dependency_overrides.clear()
        import main

        main._channel3 = None
        with patch("main.load_channel3_api_key", return_value=""):
            response = self.client.get("/products/external/search", params={"query": "shoes"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("CHANNEL3_API_KEY", response.text)
        self.assertEqual(self.transport.calls, [])

    def test_local_catalog_search_stays_independent(self) -> None:
        response = self.client.get("/products/search", params={"query": "laptop"})
        self.assertEqual(response.status_code, 200)
        names = [product["name"] for product in response.json()]
        self.assertIn("Northwind Book 14", names)
        self.assertEqual(self.transport.calls, [])

    def test_external_id_is_rejected_by_checkout(self) -> None:
        service = CheckoutService(ExplodingPayPal(), object())
        with self.assertRaises(CheckoutError) as caught:
            service.create_order([("ch3-prod-1", 1)])
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("Unknown product id", caught.exception.detail["message"])

    def test_only_the_official_search_url_is_allowed(self) -> None:
        assert_channel3_search_url(CHANNEL3_SEARCH_URL)
        for url in (
            "http://api.trychannel3.com/v1/search",
            "https://api.trychannel3.com/v1/products/ch3-prod-1",
            "https://evil.example/v1/search",
            "https://user:secret@api.trychannel3.com/v1/search",
        ):
            with self.assertRaises(Channel3Error):
                assert_channel3_search_url(url)

    def test_client_caps_the_result_count(self) -> None:
        self.channel3.search(query="shoes", limit=50)
        self.assertEqual(self.transport.calls[0][2]["limit"], 10)


if __name__ == "__main__":
    unittest.main()
