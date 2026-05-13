from __future__ import annotations

from ..utils import parse_positive_int

CART_KEY = 'cart'


def normalize_cart_item(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    try:
        product_id = int(item.get('product_id', item.get('id')))
        quantity = parse_positive_int(item.get('quantity', item.get('quantidade', 0)), default=0, minimum=1, maximum=999)
        price = float(item.get('price', item.get('preco')))
        name = str(item.get('name', item.get('nome'))).strip()
    except (TypeError, ValueError):
        return None
    if not product_id or not name or price < 0:
        return None
    return {
        'product_id': product_id,
        'name': name,
        'price': round(price, 2),
        'quantity': quantity,
    }


def get_cart(session) -> list[dict]:
    raw = session.get(CART_KEY, []) or []
    cart = []
    changed = False
    for item in raw:
        normalized = normalize_cart_item(item)
        if normalized:
            cart.append(normalized)
            changed = changed or normalized != item
        else:
            changed = True
    if changed:
        session[CART_KEY] = cart
        session.modified = True
    return cart


def save_cart(session, cart: list[dict]) -> None:
    session[CART_KEY] = cart
    session.modified = True


def clear_cart(session) -> None:
    session.pop(CART_KEY, None)
    session.modified = True


def find_item(cart: list[dict], product_id: int) -> dict | None:
    return next((item for item in cart if int(item['product_id']) == int(product_id)), None)


def totals(cart: list[dict]) -> tuple[float, int]:
    total = 0.0
    quantity = 0
    for item in cart:
        qty = int(item['quantity'])
        total += float(item['price']) * qty
        quantity += qty
    return round(total, 2), quantity


def add_item(cart: list[dict], product: dict, quantity: int) -> list[dict]:
    item = find_item(cart, product['id'])
    if item:
        item['quantity'] += quantity
    else:
        cart.append({'product_id': int(product['id']), 'name': product['name'], 'price': float(product['price']), 'quantity': quantity})
    return cart


def update_item(cart: list[dict], product_id: int, quantity: int) -> tuple[list[dict], bool]:
    item = find_item(cart, product_id)
    if not item:
        return cart, False
    if quantity <= 0:
        return [row for row in cart if int(row['product_id']) != int(product_id)], True
    item['quantity'] = quantity
    return cart, False


def remove_item(cart: list[dict], product_id: int) -> list[dict]:
    return [row for row in cart if int(row['product_id']) != int(product_id)]
