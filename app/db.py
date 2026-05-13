from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash

SCHEMA_SQL = '''
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    price REAL NOT NULL CHECK (price >= 0),
    category TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_number TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'novo',
    notes TEXT,
    total_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('novo', 'preparando', 'pronto', 'entregue', 'cancelado'))
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name_snapshot TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_products_active_category ON products(active, category, name);
CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
'''

DEFAULT_PRODUCTS = [
    ('Hambúrguer Artesanal', 24.90, 'Lanches'),
    ('Batata Frita', 12.90, 'Acompanhamentos'),
    ('Refrigerante', 8.00, 'Bebidas'),
    ('Combo da Casa', 39.90, 'Combos'),
]


def get_db():
    if 'db' not in g:
        db = sqlite3.connect(current_app.config['DATABASE'])
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        g.db = db
    return g.db


def close_db(_exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_info(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _ensure_column(db: sqlite3.Connection, table: str, column_def: str) -> None:
    column_name = column_def.split()[0]
    if column_name not in _table_info(db, table):
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column_def}')


def _migrate_admin_passwords(db: sqlite3.Connection) -> None:
    columns = _table_info(db, 'admins')

    if 'password_hash' not in columns:
        _ensure_column(db, 'admins', 'password_hash TEXT')
    if 'is_active' not in columns:
        _ensure_column(db, 'admins', 'is_active INTEGER NOT NULL DEFAULT 1')

    if 'password' in columns:
        rows = db.execute('SELECT id, password, password_hash FROM admins').fetchall()
        for row in rows:
            current_hash = row['password_hash'] or ''
            if current_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                continue
            raw = current_hash or row['password'] or os.getenv('ADMIN_PASSWORD', '123456')
            if not str(raw).startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                raw = generate_password_hash(str(raw))
            db.execute('UPDATE admins SET password_hash = ? WHERE id = ?', (raw, row['id']))
    else:
        rows = db.execute('SELECT id, password_hash FROM admins').fetchall()
        for row in rows:
            current_hash = row['password_hash'] or ''
            if not current_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                db.execute(
                    'UPDATE admins SET password_hash = ? WHERE id = ?',
                    (generate_password_hash(current_hash or os.getenv('ADMIN_PASSWORD', '123456')), row['id'])
                )


def _seed_defaults(db: sqlite3.Connection) -> None:
    default_admin_username = os.getenv('ADMIN_USERNAME', 'admin')
    default_admin_password = os.getenv('ADMIN_PASSWORD', '123456')

    if db.execute('SELECT COUNT(*) FROM admins').fetchone()[0] == 0:
        db.execute(
            'INSERT INTO admins (username, password_hash, is_active) VALUES (?, ?, 1)',
            (default_admin_username, generate_password_hash(default_admin_password)),
        )

    if db.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
        for index, (name, price, category) in enumerate(DEFAULT_PRODUCTS):
            db.execute(
                '''
                INSERT INTO products (name, price, category, active, sort_order)
                VALUES (?, ?, ?, 1, ?)
                ''',
                (name, price, category, index),
            )


def migrate_schema(db: sqlite3.Connection) -> None:
    if _table_exists(db, 'products'):
        for column_def in [
            'description TEXT',
            'sort_order INTEGER NOT NULL DEFAULT 0',
            'created_at TEXT',
            'updated_at TEXT',
        ]:
            _ensure_column(db, 'products', column_def)

    if _table_exists(db, 'orders'):
        for column_def in [
            'customer_name TEXT NOT NULL DEFAULT ""',
            'notes TEXT',
            'total_amount REAL NOT NULL DEFAULT 0',
            'updated_at TEXT',
        ]:
            _ensure_column(db, 'orders', column_def)
        db.execute('UPDATE orders SET customer_name = COALESCE(customer_name, "")')

    if _table_exists(db, 'order_items'):
        _ensure_column(db, 'order_items', 'product_name_snapshot TEXT NOT NULL DEFAULT ""')

    if _table_exists(db, 'admins'):
        _migrate_admin_passwords(db)


def _backfill_timestamps(db: sqlite3.Connection) -> None:
    now = datetime.utcnow().isoformat(timespec='seconds')

    if _table_exists(db, 'products'):
        db.execute('UPDATE products SET created_at = COALESCE(created_at, ?)', (now,))
        db.execute('UPDATE products SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'orders'):
        db.execute('UPDATE orders SET updated_at = COALESCE(updated_at, ?)', (now,))


def init_db(app):
    @app.teardown_appcontext
    def _close_db(exception):
        close_db(exception)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA_SQL)
        migrate_schema(db)
        _backfill_timestamps(db)
        _seed_defaults(db)
        db.commit()