from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def format_currency(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0')
    return f'R$ {amount.quantize(Decimal("0.01")):.2f}'.replace('.', ',', 1)


def normalize_text(value: Any) -> str:
    return ' '.join(str(value or '').strip().split())


def parse_positive_int(value: Any, *, default: int = 1, minimum: int = 1, maximum: int = 999) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def parse_price(value: Any) -> float:
    text = str(value or '').strip().replace('.', '').replace(',', '.') if isinstance(value, str) else str(value)
    try:
        number = float(text)
    except (TypeError, ValueError):
        raise ValueError('Preço inválido.')
    if number < 0:
        raise ValueError('Preço inválido.')
    return round(number, 2)
