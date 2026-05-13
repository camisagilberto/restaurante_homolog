from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import re


def format_currency(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal('0')

    formatted = f'{amount.quantize(Decimal("0.01")):.2f}'
    integer, cents = formatted.split('.')

    groups = []
    while integer:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]

    return f'R$ {".".join(groups)},{cents}'


def normalize_text(value: Any) -> str:
    return ' '.join(str(value or '').strip().split())


def parse_positive_int(value: Any, *, default: int = 1, minimum: int = 1, maximum: int = 999) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def parse_price(value: Any) -> float:
    if value is None:
        raise ValueError('Preço inválido.')

    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise ValueError('Preço inválido.')
    else:
        text = normalize_text(value)

        if not text:
            raise ValueError('Preço inválido.')

        text = text.upper()
        text = text.replace('R$', '')
        text = text.replace('RS', '')
        text = text.replace('$', '')
        text = text.replace(' ', '')

        text = re.sub(r'[^0-9,.]', '', text)

        if not text:
            raise ValueError('Preço inválido.')

        has_comma = ',' in text
        has_dot = '.' in text

        if has_comma and has_dot:
            last_comma = text.rfind(',')
            last_dot = text.rfind('.')

            if last_comma > last_dot:
                text = text.replace('.', '')
                text = text.replace(',', '.')
            else:
                text = text.replace(',', '')

        elif has_comma:
            text = text.replace('.', '')
            text = text.replace(',', '.')

        elif has_dot:
            parts = text.split('.')

            if len(parts) == 2 and len(parts[1]) in {1, 2}:
                text = f'{parts[0]}.{parts[1]}'
            else:
                text = text.replace('.', '')

        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValueError('Preço inválido.')

    if number < 0:
        raise ValueError('Preço inválido.')

    return float(number.quantize(Decimal('0.01')))
