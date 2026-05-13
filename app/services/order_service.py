from __future__ import annotations

from datetime import datetime

ORDER_STATUS_LABELS = {
    'novo': 'Novo',
    'preparando': 'Preparando',
    'pronto': 'Pronto',
    'entregue': 'Entregue',
    'cancelado': 'Cancelado',
}

ACTIVE_ORDER_STATUSES = ('novo', 'preparando', 'pronto')


def _format_created_at(value) -> str:
    if not value:
        return ''

    text = str(value)

    for candidate in (text, text.replace(' ', 'T')):
        try:
            return datetime.fromisoformat(candidate).strftime('%d/%m/%Y %H:%M')
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
    table_number: str,
    cart: list[dict],
    customer_name: str,
    notes: str | None = None
) -> int:
    now = datetime.utcnow().isoformat(timespec='seconds')

    cursor = db.execute(
        '''
        INSERT INTO orders (
            table_number,
            customer_name,
            status,
            notes,
            total_amount,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (
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
        quantity = int(item['quantity'])
        unit_price = float(item['price'])

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
                item['product_id'],
                item['name'],
                quantity,
                unit_price,
            ),
        )

    db.execute(
        '''
        UPDATE orders
        SET total_amount = ?
        WHERE id = ?
        ''',
        (
            round(total, 2),
            order_id,
        ),
    )

    db.commit()
    return order_id


def list_orders_for_table(db, table_number: str):
    placeholders = ', '.join('?' for _ in ACTIVE_ORDER_STATUSES)

    orders = db.execute(
        f'''
        SELECT *
        FROM orders
        WHERE table_number = ?
          AND status IN ({placeholders})
        ORDER BY id DESC
        ''',
        (str(table_number), *ACTIVE_ORDER_STATUSES),
    ).fetchall()

    return [_decorate_order(db, order) for order in orders]


def list_orders_for_kitchen(db):
    orders = db.execute(
        '''
        SELECT *
        FROM orders
        ORDER BY id DESC
        '''
    ).fetchall()

    return [_decorate_order(db, order) for order in orders]


def update_order_status(db, order_id: int, status: str):
    db.execute(
        '''
        UPDATE orders
        SET status = ?
        WHERE id = ?
        ''',
        (
            status,
            order_id,
        ),
    )
    db.commit()


def delete_all_orders(db):
    db.execute('DELETE FROM order_items')
    db.execute('DELETE FROM orders')
    db.commit()
