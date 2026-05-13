from __future__ import annotations

from ..errors import ValidationError
from ..utils import normalize_text, parse_price


def list_products(db, *, active_only: bool = False, query: str | None = None):
    sql = 'SELECT * FROM products'
    params: list[object] = []
    clauses = []
    if active_only:
        clauses.append('active = 1')
    if query:
        clauses.append('(name LIKE ? OR category LIKE ? OR COALESCE(description, "") LIKE ?)')
        like = f'%{query.strip()}%'
        params.extend([like, like, like])
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY active DESC, category ASC, sort_order ASC, name ASC'
    return db.execute(sql, params).fetchall()


def get_product(db, product_id: int):
    return db.execute('SELECT * FROM products WHERE id = ?', (product_id,)).fetchone()


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


def create_product(db, payload: dict) -> int:
    data = validate_product_payload(payload)
    cursor = db.execute(
        'INSERT INTO products (name, description, price, category, active, sort_order) VALUES (?, ?, ?, ?, ?, ?)',
        (data['name'], data['description'], data['price'], data['category'], data['active'], data['sort_order']),
    )
    db.commit()
    return cursor.lastrowid


def update_product(db, product_id: int, payload: dict) -> None:
    data = validate_product_payload(payload)
    db.execute(
        '''
        UPDATE products
           SET name = ?, description = ?, price = ?, category = ?, active = ?, sort_order = ?, updated_at = CURRENT_TIMESTAMP
         WHERE id = ?
        ''',
        (data['name'], data['description'], data['price'], data['category'], data['active'], data['sort_order'], product_id),
    )
    db.commit()


def toggle_product(db, product_id: int) -> None:
    db.execute(
        'UPDATE products SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (product_id,),
    )
    db.commit()


def delete_product(db, product_id: int) -> tuple[bool, str]:
    used = db.execute('SELECT COUNT(*) FROM order_items WHERE product_id = ?', (product_id,)).fetchone()[0]
    if used:
        db.execute('UPDATE products SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (product_id,))
        db.commit()
        return False, 'Produto já aparece em pedidos históricos; ele foi desativado em vez de excluído.'
    db.execute('DELETE FROM products WHERE id = ?', (product_id,))
    db.commit()
    return True, 'Produto removido com sucesso.'
