from __future__ import annotations

from ..errors import ValidationError
from ..utils import normalize_text, parse_price


def _require_restaurant_id(restaurant_id: int | None) -> int:
    if not restaurant_id:
        raise ValidationError('Restaurante não identificado.')

    return int(restaurant_id)


def _normalize_kind(kind: str | None = 'menu') -> str:
    value = str(kind or 'menu').strip().lower()
    return value if value in {'menu', 'coupon'} else 'menu'


def list_products(db, restaurant_id: int, *, active_only: bool = False, query: str | None = None, kind: str | None = 'menu'):
    restaurant_id = _require_restaurant_id(restaurant_id)
    kind = _normalize_kind(kind)

    sql = 'SELECT * FROM products WHERE restaurant_id = ? AND kind = ?'
    params: list[object] = [restaurant_id, kind]

    if active_only:
        sql += ' AND active = 1'

    if query:
        sql += ' AND (name LIKE ? OR category LIKE ? OR COALESCE(description, "") LIKE ?)'
        like = f'%{query.strip()}%'
        params.extend([like, like, like])

    sql += ' ORDER BY active DESC, category ASC, sort_order ASC, name ASC'
    return db.execute(sql, params).fetchall()


def get_product(db, product_id: int, restaurant_id: int, *, kind: str | None = None):
    restaurant_id = _require_restaurant_id(restaurant_id)

    sql = 'SELECT * FROM products WHERE id = ? AND restaurant_id = ?'
    params: list[object] = [product_id, restaurant_id]

    if kind is not None:
        sql += ' AND kind = ?'
        params.append(_normalize_kind(kind))

    return db.execute(sql, params).fetchone()


def _requested_sort_order(payload: dict) -> int | None:
    raw_value = payload.get('sort_order')

    if raw_value is None or str(raw_value).strip() == '':
        return None

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def _last_sort_order(db, restaurant_id: int, category: str, *, kind: str = 'menu', exclude_product_id: int | None = None) -> int:
    params: list[object] = [restaurant_id, category, _normalize_kind(kind)]

    sql = '''
        SELECT COALESCE(MAX(sort_order), 0)
          FROM products
         WHERE restaurant_id = ?
           AND category = ?
           AND kind = ?
    '''

    if exclude_product_id is not None:
        sql += ' AND id <> ?'
        params.append(exclude_product_id)

    return int(db.execute(sql, params).fetchone()[0] or 0)


def _shift_category_from(db, restaurant_id: int, category: str, target_order: int, *, kind: str = 'menu', exclude_product_id: int | None = None) -> None:
    params: list[object] = [restaurant_id, category, _normalize_kind(kind), target_order]

    sql = '''
        UPDATE products
           SET sort_order = sort_order + 1,
               updated_at = CURRENT_TIMESTAMP
         WHERE restaurant_id = ?
           AND category = ?
           AND kind = ?
           AND sort_order >= ?
    '''

    if exclude_product_id is not None:
        sql += ' AND id <> ?'
        params.append(exclude_product_id)

    db.execute(sql, params)


def _reindex_category(db, restaurant_id: int, category: str, *, kind: str = 'menu') -> None:
    rows = db.execute(
        '''
        SELECT id
          FROM products
         WHERE restaurant_id = ?
           AND category = ?
           AND kind = ?
         ORDER BY sort_order ASC, name ASC, id ASC
        ''',
        (restaurant_id, category, _normalize_kind(kind)),
    ).fetchall()

    for index, row in enumerate(rows, start=1):
        db.execute(
            '''
            UPDATE products
               SET sort_order = ?,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND restaurant_id = ?
            ''',
            (index, row['id'], restaurant_id),
        )


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

    return {
        'name': name,
        'description': description or None,
        'price': price,
        'category': category,
        'active': 1 if str(payload.get('active', '1')).lower() in {'1', 'true', 'on', 'yes'} else 0,
        'sort_order': _requested_sort_order(payload),
    }


def create_product(db, payload: dict, restaurant_id: int, *, kind: str = 'menu') -> int:
    restaurant_id = _require_restaurant_id(restaurant_id)
    data = validate_product_payload(payload)
    kind = _normalize_kind(kind)

    target_order = data['sort_order'] or (_last_sort_order(db, restaurant_id, data['category'], kind=kind) + 1)
    target_order = max(1, int(target_order))

    _shift_category_from(db, restaurant_id, data['category'], target_order, kind=kind)

    cursor = db.execute(
        '''
        INSERT INTO products (
            restaurant_id,
            name,
            description,
            price,
            category,
            active,
            sort_order,
            kind
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            restaurant_id,
            data['name'],
            data['description'],
            data['price'],
            data['category'],
            data['active'],
            target_order,
            kind,
        ),
    )

    _reindex_category(db, restaurant_id, data['category'], kind=kind)
    db.commit()
    return cursor.lastrowid


def update_product(db, product_id: int, payload: dict, restaurant_id: int, *, kind: str = 'menu') -> None:
    restaurant_id = _require_restaurant_id(restaurant_id)
    data = validate_product_payload(payload)
    kind = _normalize_kind(kind)
    current_product = get_product(db, product_id, restaurant_id, kind=kind)

    if not current_product:
        raise ValidationError('Produto não encontrado.')

    old_category = current_product['category']
    target_order = data['sort_order'] or (_last_sort_order(db, restaurant_id, data['category'], kind=kind, exclude_product_id=product_id) + 1)
    target_order = max(1, int(target_order))

    _shift_category_from(db, restaurant_id, data['category'], target_order, kind=kind, exclude_product_id=product_id)

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
            target_order,
            product_id,
            restaurant_id,
        ),
    )

    _reindex_category(db, restaurant_id, data['category'], kind=kind)

    if old_category != data['category']:
        _reindex_category(db, restaurant_id, old_category, kind=kind)

    db.commit()


def toggle_product(db, product_id: int, restaurant_id: int, *, kind: str = 'menu') -> None:
    restaurant_id = _require_restaurant_id(restaurant_id)

    db.execute(
        '''
        UPDATE products
           SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END,
               updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
           AND restaurant_id = ?
           AND kind = ?
        ''',
        (product_id, restaurant_id, _normalize_kind(kind)),
    )
    db.commit()


def delete_product(db, product_id: int, restaurant_id: int, *, kind: str = 'menu') -> tuple[bool, str]:
    restaurant_id = _require_restaurant_id(restaurant_id)
    kind = _normalize_kind(kind)
    product = get_product(db, product_id, restaurant_id, kind=kind)

    if not product:
        return False, 'Produto não encontrado.'

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
               AND kind = ?
            ''',
            (product_id, restaurant_id, kind),
        )
        db.commit()
        return False, 'Produto já aparece em pedidos históricos; ele foi desativado em vez de excluído.'

    db.execute(
        'DELETE FROM products WHERE id = ? AND restaurant_id = ? AND kind = ?',
        (product_id, restaurant_id, kind),
    )

    _reindex_category(db, restaurant_id, product['category'], kind=kind)
    db.commit()
    return True, 'Produto removido com sucesso.'
