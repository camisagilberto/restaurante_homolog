from __future__ import annotations

    _reindex_category(db, restaurant_id, data['category'])

    if old_category != data['category']:
        _reindex_category(db, restaurant_id, old_category)

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
    product = get_product(db, product_id, restaurant_id)

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
            ''',
            (product_id, restaurant_id),
        )
        db.commit()
        return False, 'Produto já aparece em pedidos históricos; ele foi desativado em vez de excluído.'

    db.execute(
        'DELETE FROM products WHERE id = ? AND restaurant_id = ?',
        (product_id, restaurant_id),
    )
    _reindex_category(db, restaurant_id, product['category'])
    db.commit()
    return True, 'Produto removido com sucesso.'
