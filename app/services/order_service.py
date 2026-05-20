from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..errors import ValidationError

ORDER_STATUS_LABELS = {
    'novo': 'Novo',
    'preparando': 'Preparando',
    'pronto': 'Pronto',
    'entregue': 'Entregue',
    'cancelado': 'Cancelado',
}

ACTIVE_ORDER_STATUSES = ('novo', 'preparando', 'pronto', 'entregue')
OPEN_ORDER_STATUSES = ('novo', 'preparando', 'pronto')
BRASILIA_TZ = ZoneInfo('America/Sao_Paulo')


def _require_restaurant_id(restaurant_id: int | None) -> int:
    if not restaurant_id:
        raise ValidationError('Restaurante não identificado.')

    return int(restaurant_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _format_created_at(value) -> str:
    if not value:
        return ''

    text = str(value)

    for candidate in (text, text.replace(' ', 'T')):
        try:
            created_at = datetime.fromisoformat(candidate)

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            return created_at.astimezone(BRASILIA_TZ).strftime('%d/%m/%Y %H:%M')
        except ValueError:
            continue

    return text


def _decorate_order(db, order):
    items = db.execute(
        '''
        SELECT
            oi.id,
            oi.order_id,
            oi.product_id,
            COALESCE(NULLIF(oi.product_name_snapshot, ''), p.name, 'Item') AS name,
            oi.quantity,
            oi.unit_price
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
        ORDER BY oi.id ASC
        ''',
        (order['id'],),
    ).fetchall()

    return {
        'order': order,
        'items': items,
        'status_label': ORDER_STATUS_LABELS.get(order['status'], order['status']),
        'created_at_display': _format_created_at(order['created_at']),
    }


def create_order_from_cart(
    db,
    restaurant_id: int,
    table_number: str,
    cart: list[dict],
    customer_name: str,
    notes: str | None = None,
) -> int:
    restaurant_id = _require_restaurant_id(restaurant_id)
    now = _now_iso()

    cursor = db.execute(
        '''
        INSERT INTO orders (
            restaurant_id,
            table_number,
            customer_name,
            status,
            notes,
            total_amount,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            restaurant_id,
            str(table_number),
            customer_name,
            'novo',
            notes,
            0,
            now,
            now,
        ),
    )

    order_id = cursor.lastrowid
    total = 0.0

    for item in cart:
        product_id = int(item['product_id'])

        product = db.execute(
            '''
            SELECT id, name, price
              FROM products
             WHERE id = ?
               AND restaurant_id = ?
               AND active = 1
            ''',
            (product_id, restaurant_id),
        ).fetchone()

        if not product:
            continue

        quantity = int(item['quantity'])
        unit_price = float(product['price'])

        total += quantity * unit_price

        db.execute(
            '''
            INSERT INTO order_items (
                order_id,
                product_id,
                product_name_snapshot,
                quantity,
                unit_price
            )
            VALUES (?, ?, ?, ?, ?)
            ''',
            (
                order_id,
                product['id'],
                product['name'],
                quantity,
                unit_price,
            ),
        )

    db.execute(
        '''
        UPDATE orders
           SET total_amount = ?,
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (
            round(total, 2),
            _now_iso(),
            order_id,
            restaurant_id,
        ),
    )

    db.commit()
    return order_id


def list_orders_for_table(db, restaurant_id: int, table_number: str):
    restaurant_id = _require_restaurant_id(restaurant_id)
    placeholders = ', '.join('?' for _ in ACTIVE_ORDER_STATUSES)

    orders = db.execute(
        f'''
        SELECT *
          FROM orders
         WHERE restaurant_id = ?
           AND table_number = ?
           AND status IN ({placeholders})
         ORDER BY id DESC
        ''',
        (restaurant_id, str(table_number), *ACTIVE_ORDER_STATUSES),
    ).fetchall()

    return [_decorate_order(db, order) for order in orders]


def list_orders_for_kitchen(db, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)

    orders = db.execute(
        '''
        SELECT *
          FROM orders
         WHERE restaurant_id = ?
         ORDER BY id DESC
        ''',
        (restaurant_id,),
    ).fetchall()

    return [_decorate_order(db, order) for order in orders]

def count_open_orders_for_table(db, restaurant_id: int, table_number: str) -> int:
    restaurant_id = _require_restaurant_id(restaurant_id)
    placeholders = ', '.join('?' for _ in OPEN_ORDER_STATUSES)

    return int(
        db.execute(
            f'''
            SELECT COUNT(*)
              FROM orders
             WHERE restaurant_id = ?
               AND table_number = ?
               AND status IN ({placeholders})
            ''',
            (restaurant_id, str(table_number), *OPEN_ORDER_STATUSES),
        ).fetchone()[0]
        or 0
    )

def get_kitchen_orders_signature(db, restaurant_id: int) -> str:
    restaurant_id = _require_restaurant_id(restaurant_id)

    summary = db.execute(
        '''
        SELECT
            COUNT(*) AS total_orders,
            COALESCE(MAX(id), 0) AS last_order_id,
            COALESCE(MAX(updated_at), '') AS last_update
          FROM orders
         WHERE restaurant_id = ?
        ''',
        (restaurant_id,),
    ).fetchone()

    status_rows = db.execute(
        '''
        SELECT id, status, COALESCE(updated_at, '') AS updated_at
          FROM orders
         WHERE restaurant_id = ?
         ORDER BY id ASC
        ''',
        (restaurant_id,),
    ).fetchall()

    status_signature = '|'.join(
        f"{row['id']}:{row['status']}:{row['updated_at']}"
        for row in status_rows
    )

    return (
        f"{summary['total_orders']}:"
        f"{summary['last_order_id']}:"
        f"{summary['last_update']}:"
        f"{status_signature}"
    )


def update_order_status(db, order_id: int, status: str, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)

    if status not in ORDER_STATUS_LABELS:
        raise ValidationError('Status inválido.')

    cursor = db.execute(
        '''
        UPDATE orders
           SET status = ?,
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (
            status,
            _now_iso(),
            order_id,
            restaurant_id,
        ),
    )

    if cursor.rowcount == 0:
        raise ValidationError('Pedido não encontrado para este restaurante.')

    db.commit()


def delete_all_orders(db, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)

    order_ids = [
        row['id']
        for row in db.execute(
            'SELECT id FROM orders WHERE restaurant_id = ?',
            (restaurant_id,),
        ).fetchall()
    ]

    for order_id in order_ids:
        db.execute('DELETE FROM order_items WHERE order_id = ?', (order_id,))

    db.execute(
        'DELETE FROM orders WHERE restaurant_id = ?',
        (restaurant_id,),
    )
    db.commit()
