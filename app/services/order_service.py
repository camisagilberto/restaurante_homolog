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

PAYMENT_STATUS_LABELS = {
    'not_required': 'Pagamento não exigido',
    'pending': 'Pagamento pendente',
    'approved': 'Pagamento aprovado',
    'rejected': 'Pagamento recusado',
    'cancelled': 'Pagamento cancelado',
    'expired': 'Pagamento expirado',
    'error': 'Erro no pagamento',
    'offline_pending': 'Aguardando confirmação do atendente',
}

OFFLINE_PAYMENT_PROVIDER = 'offline'


ACTIVE_ORDER_STATUSES = ('novo', 'preparando', 'pronto', 'entregue')
OPEN_ORDER_STATUSES = ('novo', 'preparando', 'pronto')
BRASILIA_TZ = ZoneInfo('America/Sao_Paulo')
PAYMENT_RELEASE_FILTER = """
           AND (
                 COALESCE(payment_required, 0) = 0
              OR COALESCE(payment_status, 'not_required') = 'approved'
           )
"""


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


def _row_get(row, key: str, default=None):
    if not row:
        return default

    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass

    return default


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

    payment_status = _row_get(order, 'payment_status', 'not_required')

    return {
        'order': order,
        'items': items,
        'status_label': ORDER_STATUS_LABELS.get(order['status'], order['status']),
        'payment_status_label': PAYMENT_STATUS_LABELS.get(payment_status, 'Pagamento não exigido'),
        'created_at_display': _format_created_at(order['created_at']),
    }


def create_order_from_cart(
    db,
    restaurant_id: int,
    table_number: str,
    cart: list[dict],
    customer_name: str,
    notes: str | None = None,
    *,
    payment_required: bool = False,
    payment_status: str = 'not_required',
    payment_provider: str = '',
) -> int:
    restaurant_id = _require_restaurant_id(restaurant_id)
    now = _now_iso()
    payment_status = str(payment_status or 'not_required')

    if payment_status not in PAYMENT_STATUS_LABELS:
        raise ValidationError('Status de pagamento inválido.')

    cursor = db.execute(
        '''
        INSERT INTO orders (
            restaurant_id,
            table_number,
            customer_name,
            status,
            notes,
            total_amount,
            payment_required,
            payment_status,
            payment_provider,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            restaurant_id,
            str(table_number),
            customer_name,
            'novo',
            notes,
            0,
            1 if payment_required else 0,
            payment_status,
            str(payment_provider or ''),
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

    if total <= 0:
        db.execute('DELETE FROM order_items WHERE order_id = ?', (order_id,))
        db.execute('DELETE FROM orders WHERE id = ? AND restaurant_id = ?', (order_id, restaurant_id))
        db.commit()
        raise ValidationError('Carrinho vazio ou produtos indisponíveis.')

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
        f'''
        SELECT *
          FROM orders
         WHERE restaurant_id = ?
           {PAYMENT_RELEASE_FILTER}
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
        f'''
        SELECT
            COUNT(*) AS total_orders,
            COALESCE(MAX(id), 0) AS last_order_id,
            COALESCE(MAX(updated_at), '') AS last_update
          FROM orders
         WHERE restaurant_id = ?
           {PAYMENT_RELEASE_FILTER}
        ''',
        (restaurant_id,),
    ).fetchone()

    status_rows = db.execute(
        f'''
        SELECT
            id,
            status,
            COALESCE(payment_status, 'not_required') AS payment_status,
            COALESCE(updated_at, '') AS updated_at
          FROM orders
         WHERE restaurant_id = ?
           {PAYMENT_RELEASE_FILTER}
         ORDER BY id ASC
        ''',
        (restaurant_id,),
    ).fetchall()

    status_signature = '|'.join(
        f"{row['id']}:{row['status']}:{row['payment_status']}:{row['updated_at']}"
        for row in status_rows
    )

    return (
        f"{summary['total_orders']}:"
        f"{summary['last_order_id']}:"
        f"{summary['last_update']}:"
        f"{status_signature}"
    )



def list_orders_for_attendant(db, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)

    orders = db.execute(
        '''
        SELECT *
          FROM orders
         WHERE restaurant_id = ?
           AND COALESCE(payment_provider, '') = ?
           AND COALESCE(payment_required, 0) = 1
           AND COALESCE(payment_status, 'pending') = 'pending'
           AND status = 'novo'
         ORDER BY id ASC
        ''',
        (restaurant_id, OFFLINE_PAYMENT_PROVIDER),
    ).fetchall()

    return [_decorate_order(db, order) for order in orders]


def get_attendant_orders_signature(db, restaurant_id: int) -> str:
    restaurant_id = _require_restaurant_id(restaurant_id)

    rows = db.execute(
        '''
        SELECT id, status, payment_status, updated_at
          FROM orders
         WHERE restaurant_id = ?
           AND COALESCE(payment_provider, '') = ?
           AND COALESCE(payment_required, 0) = 1
           AND COALESCE(payment_status, 'pending') = 'pending'
           AND status = 'novo'
         ORDER BY id ASC
        ''',
        (restaurant_id, OFFLINE_PAYMENT_PROVIDER),
    ).fetchall()

    return '|'.join(f"{row['id']}:{row['status']}:{row['payment_status']}:{row['updated_at']}" for row in rows)


def approve_attendant_order(db, order_id: int, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)
    now = _now_iso()
    cursor = db.execute(
        '''
        UPDATE orders
           SET payment_status = 'approved',
               payment_approved_at = COALESCE(payment_approved_at, ?),
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
           AND COALESCE(payment_provider, '') = ?
           AND COALESCE(payment_required, 0) = 1
           AND COALESCE(payment_status, 'pending') = 'pending'
           AND status = 'novo'
        ''',
        (now, now, order_id, restaurant_id, OFFLINE_PAYMENT_PROVIDER),
    )
    if cursor.rowcount == 0:
        raise ValidationError('Pedido não encontrado ou já confirmado.')
    db.commit()


def reject_attendant_order(db, order_id: int, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)
    now = _now_iso()
    cursor = db.execute(
        '''
        UPDATE orders
           SET status = 'cancelado',
               payment_status = 'cancelled',
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
           AND COALESCE(payment_provider, '') = ?
           AND COALESCE(payment_required, 0) = 1
           AND COALESCE(payment_status, 'pending') = 'pending'
        ''',
        (now, order_id, restaurant_id, OFFLINE_PAYMENT_PROVIDER),
    )
    if cursor.rowcount == 0:
        raise ValidationError('Pedido não encontrado ou já confirmado.')
    db.commit()

def get_order_for_payment(db, restaurant_id: int, order_id: int, table_number: str | None = None):
    restaurant_id = _require_restaurant_id(restaurant_id)
    params: list = [order_id, restaurant_id]
    table_filter = ''

    if table_number is not None:
        table_filter = ' AND table_number = ?'
        params.append(str(table_number))

    return db.execute(
        f'''
        SELECT *
          FROM orders
         WHERE id = ?
           AND restaurant_id = ?
           {table_filter}
         LIMIT 1
        ''',
        tuple(params),
    ).fetchone()


def update_order_payment_pending(
    db,
    *,
    restaurant_id: int,
    order_id: int,
    provider: str,
    external_id: str,
    external_reference: str,
    qr_code: str,
    qr_code_base64: str = '',
    ticket_url: str = '',
):
    restaurant_id = _require_restaurant_id(restaurant_id)
    now = _now_iso()

    cursor = db.execute(
        '''
        UPDATE orders
           SET payment_required = 1,
               payment_status = 'pending',
               payment_provider = ?,
               payment_external_id = ?,
               payment_external_reference = ?,
               payment_qr_code = ?,
               payment_qr_code_base64 = ?,
               payment_ticket_url = ?,
               payment_created_at = COALESCE(payment_created_at, ?),
               payment_error = '',
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (
            provider,
            str(external_id or ''),
            str(external_reference or ''),
            str(qr_code or ''),
            str(qr_code_base64 or ''),
            str(ticket_url or ''),
            now,
            now,
            order_id,
            restaurant_id,
        ),
    )

    if cursor.rowcount == 0:
        raise ValidationError('Pedido não encontrado para atualizar pagamento.')

    db.commit()


def mark_order_payment_error(db, *, restaurant_id: int, order_id: int, error: str):
    restaurant_id = _require_restaurant_id(restaurant_id)
    db.execute(
        '''
        UPDATE orders
           SET payment_status = 'error',
               payment_error = ?,
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (str(error or '')[:500], _now_iso(), order_id, restaurant_id),
    )
    db.commit()




def update_order_payment_status(
    db,
    *,
    restaurant_id: int,
    order_id: int,
    payment_status: str,
    approved_at: str | None = None,
    payment_error: str = '',
):
    restaurant_id = _require_restaurant_id(restaurant_id)
    normalized_status = str(payment_status or '').strip().lower()

    if normalized_status not in PAYMENT_STATUS_LABELS:
        raise ValidationError('Status de pagamento inválido.')

    now = _now_iso()
    approved_value = approved_at or now if normalized_status == 'approved' else None

    cursor = db.execute(
        """
        UPDATE orders
           SET payment_status = ?,
               payment_approved_at = CASE WHEN ? = 'approved' THEN COALESCE(NULLIF(?, ''), ?) ELSE payment_approved_at END,
               payment_error = ?,
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
        """,
        (
            normalized_status,
            normalized_status,
            str(approved_value or ''),
            now,
            str(payment_error or '')[:500],
            now,
            order_id,
            restaurant_id,
        ),
    )

    if cursor.rowcount == 0:
        raise ValidationError('Pedido não encontrado para atualizar pagamento.')

    db.commit()


def update_order_status(db, order_id: int, status: str, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)

    if status not in ORDER_STATUS_LABELS:
        raise ValidationError('Status inválido.')

    cursor = db.execute(
        f'''
        UPDATE orders
           SET status = ?,
               updated_at = ?
         WHERE id = ?
           AND restaurant_id = ?
           {PAYMENT_RELEASE_FILTER}
        ''',
        (
            status,
            _now_iso(),
            order_id,
            restaurant_id,
        ),
    )

    if cursor.rowcount == 0:
        pending_payment = db.execute(
            '''
            SELECT id
              FROM orders
             WHERE id = ?
               AND restaurant_id = ?
               AND COALESCE(payment_required, 0) = 1
               AND COALESCE(payment_status, 'not_required') != 'approved'
             LIMIT 1
            ''',
            (order_id, restaurant_id),
        ).fetchone()

        if pending_payment:
            raise ValidationError('Este pedido ainda não foi liberado para a cozinha porque o pagamento Pix não foi aprovado.')

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
