"""Tests for catalog loading and the product endpoints.

Run from the project root:

    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient

from catalog import CatalogError, load_products, search_products
from main import app


def sample_product(**overrides: object) -> dict[str, object]:
    product: dict[str, object] = {
        "id": "test-001",
        "name": "Test Headphones",
        "category": "headphones",
        "description": "A fictional product used by the tests.",
        "price_eur": 10.0,
        "features": ["bluetooth"],
        "stock": 1,
    }
    product.update(overrides)
    return product


class CatalogLoadTests(unittest.TestCase):
    def write_catalog(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "products.json"
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        return path

    def test_real_catalog_contains_15_products(self) -> None:
        products = load_products()
        self.assertEqual(len(products), 15)
        self.assertEqual(
            {product.category for product in products},
            {"headphones", "smartphones", "laptops", "accessories", "smartwatches"},
        )
        self.assertEqual(len({product.id for product in products}), 15)

    def test_out_of_stock_product_is_still_loaded(self) -> None:
        products = load_products()
        dawn = next(product for product in products if product.id == "sw-003")
        self.assertEqual(dawn.stock, 0)

    def test_missing_file_raises_catalog_error(self) -> None:
        missing = Path(tempfile.gettempdir()) / "nulixpay-missing-catalog.json"
        if missing.exists():
            missing.unlink()
        with self.assertRaises(CatalogError) as caught:
            load_products(missing)
        self.assertIn("not found", str(caught.exception))

    def test_invalid_json_raises_catalog_error(self) -> None:
        path = self.write_catalog("{")
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("valid JSON", str(caught.exception))

    def test_catalog_must_be_an_array(self) -> None:
        path = self.write_catalog({"id": "test-001"})
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("JSON array", str(caught.exception))

    def test_missing_field_is_rejected(self) -> None:
        product = sample_product()
        del product["features"]
        path = self.write_catalog([product])
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("features", str(caught.exception))

    def test_negative_price_is_rejected(self) -> None:
        path = self.write_catalog([sample_product(price_eur=-1)])
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("price_eur", str(caught.exception))

    def test_price_must_be_numeric(self) -> None:
        path = self.write_catalog([sample_product(price_eur="10")])
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("price_eur", str(caught.exception))

    def test_boolean_stock_is_rejected(self) -> None:
        path = self.write_catalog([sample_product(stock=True)])
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("stock", str(caught.exception))

    def test_duplicate_id_is_rejected(self) -> None:
        payload = [sample_product(), sample_product(name="Another Name")]
        path = self.write_catalog(payload)
        with self.assertRaises(CatalogError) as caught:
            load_products(path)
        self.assertIn("Duplicate product id", str(caught.exception))

    def test_empty_array_is_valid(self) -> None:
        path = self.write_catalog([])
        self.assertEqual(load_products(path), [])


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.products = load_products()

    def ids(self, query: str | None = None, max_price: float | None = None) -> list[str]:
        found = search_products(self.products, query=query, max_price=max_price)
        return [product.id for product in found]

    def test_name_search_ignores_case(self) -> None:
        self.assertEqual(self.ids(query="fieldnote"), ["lp-003"])
        self.assertEqual(self.ids(query="FIELDNOTE"), ["lp-003"])

    def test_category_search(self) -> None:
        self.assertEqual(self.ids(query="smartphones"), ["ph-001", "ph-002", "ph-003"])

    def test_description_search(self) -> None:
        self.assertEqual(self.ids(query="one hand"), ["ph-003"])

    def test_feature_search_ignores_case(self) -> None:
        self.assertEqual(self.ids(query="hdmi"), ["ac-001"])

    def test_max_price_includes_exact_budget(self) -> None:
        self.assertEqual(self.ids(max_price=18.9), ["ac-003"])
        self.assertEqual(self.ids(max_price=18), [])

    def test_query_and_budget_together(self) -> None:
        self.assertEqual(self.ids(query="usb", max_price=20), ["ac-003"])

    def test_blank_query_returns_the_full_catalog(self) -> None:
        self.assertEqual(len(search_products(self.products, query="   ")), 15)

    def test_unknown_query_returns_no_products(self) -> None:
        self.assertEqual(self.ids(query="spaceship"), [])


class EndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    def test_local_frontend_can_call_the_api(self) -> None:
        response = self.client.get("/products", headers={"Origin": "http://127.0.0.1:5173"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://127.0.0.1:5173",
        )

    def test_unknown_origin_is_not_allowed(self) -> None:
        response = self.client.get("/products", headers={"Origin": "https://evil.example"})
        self.assertIsNone(response.headers.get("access-control-allow-origin"))

    def test_welcome_endpoint_still_runs(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")

    def test_list_products(self) -> None:
        response = self.client.get("/products")
        self.assertEqual(response.status_code, 200)
        products = response.json()
        self.assertEqual(len(products), 15)
        self.assertEqual(
            set(products[0].keys()),
            {"id", "name", "category", "description", "price_eur", "features", "stock"},
        )

    def test_search_endpoint(self) -> None:
        response = self.client.get(
            "/products/search",
            params={"query": "usb", "max_price": 20},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([product["id"] for product in response.json()], ["ac-003"])

    def test_search_without_filters_returns_all_products(self) -> None:
        response = self.client.get("/products/search")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 15)

    def test_negative_max_price_is_rejected(self) -> None:
        response = self.client.get("/products/search", params={"max_price": -1})
        self.assertEqual(response.status_code, 422)

    def test_non_numeric_max_price_is_rejected(self) -> None:
        response = self.client.get("/products/search", params={"max_price": "cheap"})
        self.assertEqual(response.status_code, 422)

    def test_catalog_error_returns_500(self) -> None:
        with patch("main.load_products", side_effect=CatalogError("Product catalog not found")):
            for path in ("/products", "/products/search"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json()["detail"], "Product catalog not found")
