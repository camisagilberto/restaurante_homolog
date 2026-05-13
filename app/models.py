from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Product:
    id: int
    name: str
    price: float
    category: str
    active: bool
    description: str | None = None


@dataclass(slots=True)
class OrderItem:
    product_id: int | None
    name: str
    quantity: int
    unit_price: float


@dataclass(slots=True)
class OrderSummary:
    id: int
    table_number: str
    status: str
    created_at: str
    total_amount: float
