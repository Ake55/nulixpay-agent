"""Euro amounts for checkout.

Catalog prices are converted once, then every total stays a Decimal with
cents. Checkout never trusts an amount sent by the browser.
"""

from decimal import Decimal, ROUND_HALF_UP

_CENTS = Decimal("0.01")


def euros_from_catalog(price: float) -> Decimal:
    """Turn a catalog price into exact cents."""
    amount = Decimal(format(price, ".2f"))
    if amount <= 0:
        raise ValueError("Catalog price must be greater than 0.")
    return amount


def euros_from_paypal(value: str) -> Decimal:
    """Parse a PayPal money string. Reject amounts that are not exact cents."""
    try:
        amount = Decimal(value)
    except Exception as exc:
        raise ValueError("PayPal amount is not a decimal number.") from exc
    cents = amount.quantize(_CENTS)
    if amount != cents:
        raise ValueError("PayPal amount is not an exact cent value.")
    if cents <= 0:
        raise ValueError("PayPal amount must be greater than 0.")
    return cents


def format_euros(amount: Decimal) -> str:
    """Format cents the way the PayPal Orders API expects, such as 18.90."""
    return f"{amount.quantize(_CENTS, rounding=ROUND_HALF_UP):.2f}"


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    return (unit_price * quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)
