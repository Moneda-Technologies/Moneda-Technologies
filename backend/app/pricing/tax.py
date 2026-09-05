from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


MONEY = Decimal("0.01")
ALLOWED_RATES = {Decimal("0"), Decimal("5"), Decimal("12"), Decimal("18")}
ALLOWED_MODES = {"exclusive", "inclusive", "no_tax"}


def money(value: Decimal | str | int | float) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TaxResult:
    net: Decimal
    tax: Decimal
    total: Decimal
    rate: Decimal
    mode: str


def calculate_tax(amount: Decimal | str | int | float, rate: Decimal | str | int | float, mode: str) -> TaxResult:
    gross_or_net = money(amount)
    tax_rate = Decimal(str(rate))
    if tax_rate not in ALLOWED_RATES:
        raise ValueError("Unsupported tax rate")
    if mode not in ALLOWED_MODES:
        raise ValueError("Unsupported tax mode")
    if mode == "no_tax" or tax_rate == 0:
        return TaxResult(gross_or_net, Decimal("0.00"), gross_or_net, tax_rate, mode)
    fraction = tax_rate / Decimal("100")
    if mode == "inclusive":
        net = money(gross_or_net / (Decimal("1") + fraction))
        tax = money(gross_or_net - net)
        return TaxResult(net, tax, gross_or_net, tax_rate, mode)
    tax = money(gross_or_net * fraction)
    return TaxResult(gross_or_net, tax, money(gross_or_net + tax), tax_rate, mode)

