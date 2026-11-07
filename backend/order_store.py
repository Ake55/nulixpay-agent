"""SQLite record of PayPal orders created by this server.

Pending and completed orders live in the database so a restart cannot forget
an unpaid order or capture the same order twice. The access token is not stored.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class OrderNotFound(Exception):
    """Raised when the order id is not one this server created."""


class OrderConflict(Exception):
    """Raised when the order cannot be captured again."""

    def __init__(self, message: str, *, payment_status: str, order_id: str):
        super().__init__(message)
        self.payment_status = payment_status
        self.order_id = order_id


@dataclass(frozen=True)
class StoredOrder:
    order_id: str
    payment_status: str
    paypal_status: str
    currency: str
    amount: str
    items: list[dict[str, object]]
    approval_url: str
    capture_id: str | None
    capture_request_id: str
    capture_state: str


class OrderStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    payment_status TEXT NOT NULL,
                    paypal_status TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    approval_url TEXT NOT NULL,
                    capture_id TEXT,
                    capture_request_id TEXT NOT NULL,
                    capture_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def insert(self, order: StoredOrder) -> None:
        now = _now()
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    order_id, payment_status, paypal_status, currency, amount,
                    items_json, approval_url, capture_id, capture_request_id,
                    capture_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    order.payment_status,
                    order.paypal_status,
                    order.currency,
                    order.amount,
                    json.dumps(order.items),
                    order.approval_url,
                    order.capture_id,
                    order.capture_request_id,
                    order.capture_state,
                    now,
                    now,
                ),
            )

    def get(self, order_id: str) -> StoredOrder | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return _from_row(row)

    def committed_units(self, product_id: str) -> int:
        """Units already paid, under review, or reserved by an in-progress capture."""
        with self._session() as connection:
            return _units_for(connection, product_id)

    def reserve_for_capture(self, order_id: str, catalog_stock: dict[str, int]) -> StoredOrder:
        """Lock the order and reserve stock for one capture attempt."""
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise OrderNotFound(order_id)
            order = _from_row(row)
            if order.payment_status == "PAID" or order.capture_state == "DONE":
                raise OrderConflict(
                    "This order is already paid. It was not captured again.",
                    payment_status=order.payment_status,
                    order_id=order.order_id,
                )
            if order.capture_state == "IN_PROGRESS":
                raise OrderConflict(
                    "A capture for this order is already in progress.",
                    payment_status=order.payment_status,
                    order_id=order.order_id,
                )
            for item in order.items:
                product_id = str(item["product_id"])
                quantity = int(item["quantity"])
                available = catalog_stock.get(product_id, 0) - _units_for(connection, product_id)
                if quantity > available:
                    raise OrderConflict(
                        f"Not enough stock for product {product_id}. Available: {max(available, 0)}.",
                        payment_status="NOT_PAID",
                        order_id=order.order_id,
                    )
            connection.execute(
                """
                UPDATE orders
                SET capture_state = 'IN_PROGRESS', updated_at = ?
                WHERE order_id = ?
                """,
                (_now(), order_id),
            )
            connection.commit()
        reserved = self.get(order_id)
        if reserved is None:
            raise OrderNotFound(order_id)
        return reserved

    def release_reservation(self, order_id: str) -> None:
        """Clear an in-progress capture that did not take payment."""
        with self._session() as connection:
            connection.execute(
                """
                UPDATE orders
                SET capture_state = 'IDLE', updated_at = ?
                WHERE order_id = ?
                  AND capture_state = 'IN_PROGRESS'
                  AND payment_status NOT IN ('PAID', 'REVIEW')
                """,
                (_now(), order_id),
            )

    def mark_paid(self, order_id: str, capture_id: str | None) -> StoredOrder:
        return self._update_outcome(
            order_id,
            payment_status="PAID",
            paypal_status="COMPLETED",
            capture_state="DONE",
            capture_id=capture_id,
        )

    def mark_declined(self, order_id: str, paypal_status: str) -> StoredOrder:
        return self._update_outcome(
            order_id,
            payment_status="DECLINED",
            paypal_status=paypal_status,
            capture_state="IDLE",
            capture_id=None,
        )

    def mark_review(self, order_id: str) -> StoredOrder:
        return self._update_outcome(
            order_id,
            payment_status="REVIEW",
            paypal_status="COMPLETED",
            capture_state="DONE",
            capture_id=None,
        )

    def update_paypal_status(self, order_id: str, paypal_status: str) -> StoredOrder:
        with self._session() as connection:
            connection.execute(
                """
                UPDATE orders
                SET paypal_status = ?, updated_at = ?
                WHERE order_id = ?
                """,
                (paypal_status, _now(), order_id),
            )
        updated = self.get(order_id)
        if updated is None:
            raise OrderNotFound(order_id)
        return updated

    def _update_outcome(
        self,
        order_id: str,
        *,
        payment_status: str,
        paypal_status: str,
        capture_state: str,
        capture_id: str | None,
    ) -> StoredOrder:
        with self._session() as connection:
            connection.execute(
                """
                UPDATE orders
                SET payment_status = ?, paypal_status = ?, capture_state = ?,
                    capture_id = COALESCE(?, capture_id), updated_at = ?
                WHERE order_id = ?
                """,
                (payment_status, paypal_status, capture_state, capture_id, _now(), order_id),
            )
        updated = self.get(order_id)
        if updated is None:
            raise OrderNotFound(order_id)
        return updated

    def _connect(self) -> sqlite3.Connection:
        # isolation_level None leaves transaction control to BEGIN/COMMIT below.
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()


def _units_for(connection: sqlite3.Connection, product_id: str) -> int:
    rows = connection.execute(
        "SELECT items_json, payment_status, capture_state FROM orders"
    ).fetchall()
    total = 0
    for row in rows:
        if row["payment_status"] not in {"PAID", "REVIEW"} and row["capture_state"] != "IN_PROGRESS":
            continue
        for item in json.loads(row["items_json"]):
            if item["product_id"] == product_id:
                total += int(item["quantity"])
    return total


def _from_row(row: sqlite3.Row) -> StoredOrder:
    return StoredOrder(
        order_id=row["order_id"],
        payment_status=row["payment_status"],
        paypal_status=row["paypal_status"],
        currency=row["currency"],
        amount=row["amount"],
        items=json.loads(row["items_json"]),
        approval_url=row["approval_url"],
        capture_id=row["capture_id"],
        capture_request_id=row["capture_request_id"],
        capture_state=row["capture_state"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
