from decimal import Decimal

import pytest

from app.pricing.tax import calculate_tax


def test_exclusive_eighteen_percent():
    result = calculate_tax("100", "18", "exclusive")
    assert result.net == Decimal("100.00")
    assert result.tax == Decimal("18.00")
    assert result.total == Decimal("118.00")


def test_inclusive_eighteen_percent():
    result = calculate_tax("118", "18", "inclusive")
    assert result.net == Decimal("100.00")
    assert result.tax == Decimal("18.00")
    assert result.total == Decimal("118.00")


def test_five_percent_and_no_tax():
    assert calculate_tax("100", 5, "exclusive").total == Decimal("105.00")
    assert calculate_tax("100", 18, "no_tax").tax == Decimal("0.00")


def test_rejects_unmanaged_tax_rate():
    with pytest.raises(ValueError, match="Unsupported tax rate"):
        calculate_tax("100", 7, "exclusive")


def test_money_rounding_is_half_up():
    result = calculate_tax("0.05", 5, "exclusive")
    assert result.tax == Decimal("0.00")
    assert result.total == Decimal("0.05")

