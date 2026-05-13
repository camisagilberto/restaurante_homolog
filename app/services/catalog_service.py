from __future__ import annotations

from ..errors import ValidationError
from ..utils import normalize_text, parse_price


def _require_restaurant_id(restaurant_id: int | None) -> int:
    if not restaurant_id:
        raise ValidationError('Restaurante não identificado.')

    return int(restaurant_id)


def list_products(db, restaurant_id: int, *, active_only: bool = False, query: str | None = None):
    restaurant_id = _require_restaurant_id(restaurant_id)

    sql = 'SELECT * FROM products WHERE restaurant_id = ?'
    params: list[object] = [restaurant_id]

    if active_only:
        sql += ' AND active = 1'

    if query:
        sql += ' AND (name LIKE ? OR category LIKE ? OR COALESCE(description, "") LIKE ?)'
        like = f'%{query.strip()}%'
        params.extend([like, like, like])

    sql += ' ORDER BY active DESC, category ASC, sort_order ASC, name ASC'
    return db.execute(sql, params).fetchall()


def get_product(db, product_id: int, restaurant_id: int):
    restaurant_id = _require_restaurant_id(restaurant_id)

    return db.execute(
        'SELECT * FROM products WHERE id = ? AND restaurant_id = ?',
        (product_id, restaurant_id),
    ).fetchone()


def validate_product_payload(payload: dict) -> dict:
    name = normalize_text(payload.get('name'))
    category = normalize_text(payload.get('category'))
    description = normalize_text(payload.get('description'))

    if not name:
        raise ValidationError('Informe o nome do produto.')
    if not category:
        raise ValidationError('Informe a categoria.')
    if len(name) < 2 or len(name) > 80:
        raise ValidationError('O nome deve ter entre 2 e 80 caracteres.')
    if len(category) < 2 or len(category) > 50:
        raise ValidationError('A categoria deve ter entre 2 e 50 caracteres.')

    price = parse_price(payload.get('price'))

    if price <= 0:
        raise ValidationError('O preço deve ser maior que zero.')

    sort_order = payload.get('sort_order') or 0

    try:
        sort_order = int(sort_order)
    except (TypeError, ValueError):
        sort_order = 0

    return {
        'name': name,
        'description': description or None,
        'price': price,
        'category': category,
        'active': 1 if str(payload.get('active', '1')).lower() in {'1', 'true', 'on', 'yes'} else 0,
        'sort_order': sort_order,
    }


def create_product(db, payload: dict, restaurant_id: int) -> int:
    restaurant_id = _require_restaurant_id(restaurant_id)
    data = validate_product_payload(payload)

    cursor = db.execute(
        '''
        INSERT INTO products (
            restaurant_id,
            name,
            description,
            price,
            category,
            active,
            sort_order
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            restaurant_id,
            data['name'],
            data['description'],
            data['price'],
            data['category'],
            data['active'],
            data['sort_order'],
        ),
    )
    db.commit()
    return cursor.lastrowid


def update_product(db, product_id: int, payload: dict, restaurant_id: int) -> None:
    restaurant_id = _require_restaurant_id(restaurant_id)
    data = validate_product_payload(payload)

    db.execute(
        '''
        UPDATE products
           SET name = ?,
               description = ?,
               price = ?,
               category = ?,
               active = ?,
               sort_order = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (
            data['name'],
            data['description'],
            data['price'],
            data['category'],
            data['active'],
            data['sort_order'],
            product_id,
            restaurant_id,
        ),
    )
    db.commit()


def toggle_product(db, product_id: int, restaurant_id: int) -> None:
    restaurant_id = _require_restaurant_id(restaurant_id)

    db.execute(
        '''
        UPDATE products
           SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
           AND restaurant_id = ?
        ''',
        (product_id, restaurant_id),
    )
    db.commit()


def delete_product(db, product_id: int, restaurant_id: int) -> tuple[bool, str]:
    restaurant_id = _require_restaurant_id(restaurant_id)

    used = db.execute(
        '''
        SELECT COUNT(*)
          FROM order_items oi
          JOIN orders o ON o.id = oi.order_id
         WHERE oi.product_id = ?
           AND o.restaurant_id = ?
        ''',
        (product_id, restaurant_id),
    ).fetchone()[0]

    if used:
        db.execute(
            '''
            UPDATE products
               SET active = 0,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND restaurant_id = ?
            ''',
            (product_id, restaurant_id),
        )
        db.commit()
        return False, 'Produto já aparece em pedidos históricos; ele foi desativado em vez de excluído.'

    db.execute(
        'DELETE FROM products WHERE id = ? AND restaurant_id = ?',
        (product_id, restaurant_id),
    )
    db.commit()
    return True, 'Produto removido com sucesso.'
