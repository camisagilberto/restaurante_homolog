from __future__ import annotations

import os
import re
import secrets
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
    kitchen_password_hash TEXT,
    reset_token_hash TEXT,
    reset_token_expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS restaurant_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL UNIQUE,
    owner_name TEXT NOT NULL,
    age INTEGER NOT NULL,
    email TEXT NOT NULL,
    restaurant_name TEXT NOT NULL,
    cnpj TEXT NOT NULL DEFAULT '',
    restaurant_address TEXT NOT NULL,
    cell_phone TEXT NOT NULL DEFAULT '',
    order_payment_mode TEXT NOT NULL DEFAULT 'pay_after',
    service_mode TEXT NOT NULL DEFAULT 'full_order_payment',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    table_count INTEGER NOT NULL DEFAULT 0,    
    public_token TEXT,
    slug TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (admin_id) REFERENCES admins(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER,
    name TEXT NOT NULL,
    description TEXT,
    price REAL NOT NULL CHECK (price >= 0),
    category TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'menu' CHECK (kind IN ('menu', 'coupon')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER,
    table_number TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'novo',
    notes TEXT,
    total_amount REAL NOT NULL DEFAULT 0,
    payment_required INTEGER NOT NULL DEFAULT 0 CHECK (payment_required IN (0, 1)),
    payment_status TEXT NOT NULL DEFAULT 'not_required',
    payment_provider TEXT NOT NULL DEFAULT '',
    payment_external_id TEXT NOT NULL DEFAULT '',
    payment_external_reference TEXT NOT NULL DEFAULT '',
    payment_qr_code TEXT NOT NULL DEFAULT '',
    payment_qr_code_base64 TEXT NOT NULL DEFAULT '',
    payment_ticket_url TEXT NOT NULL DEFAULT '',
    payment_created_at TEXT,
    payment_approved_at TEXT,
    payment_expires_at TEXT,
    payment_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('novo', 'preparando', 'pronto', 'entregue', 'cancelado')),
    CHECK (payment_status IN ('not_required', 'pending', 'approved', 'rejected', 'cancelled', 'expired', 'error'))
);

CREATE TABLE IF NOT EXISTS restaurant_payment_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT 'mercadopago',
    provider_user_id TEXT NOT NULL DEFAULT '',
    access_token_encrypted TEXT NOT NULL DEFAULT '',
    refresh_token_encrypted TEXT NOT NULL DEFAULT '',
    token_expires_at TEXT,
    public_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'not_connected',
    connected_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
    UNIQUE (restaurant_id, provider),
    CHECK (status IN ('not_connected', 'connected', 'error', 'disabled'))
);

CREATE TABLE IF NOT EXISTS customer_coupon_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    username TEXT NOT NULL,
    cell_phone TEXT NOT NULL,
    email TEXT NOT NULL,
    cep TEXT NOT NULL,
    receive_whatsapp INTEGER NOT NULL DEFAULT 0 CHECK (receive_whatsapp IN (0, 1)),
    receive_email INTEGER NOT NULL DEFAULT 0 CHECK (receive_email IN (0, 1)),
    radar_enabled INTEGER NOT NULL DEFAULT 0 CHECK (radar_enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
    UNIQUE (restaurant_id, username)
);

CREATE TABLE IF NOT EXISTS coupon_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    coupon_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved',
    code TEXT NOT NULL DEFAULT '',
    reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    code_generated_at TEXT,
    code_expires_at TEXT,
    used_at TEXT,
    validated_by_admin_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (coupon_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (customer_id) REFERENCES customer_coupon_users(id) ON DELETE CASCADE,
    FOREIGN KEY (validated_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
    CHECK (status IN ('reserved', 'code_generated', 'used', 'expired', 'cancelled'))
);


CREATE TABLE IF NOT EXISTS qrtotem_coupon_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    coupon_type TEXT NOT NULL DEFAULT 'global',
    value REAL NOT NULL CHECK (value > 0),
    min_purchase_amount REAL NOT NULL CHECK (min_purchase_amount > 0),
    total_quantity INTEGER NOT NULL DEFAULT 0 CHECK (total_quantity >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    restaurant_id INTEGER,
    credit_allocation_id INTEGER,
    target_customer_id INTEGER,
    target_customer_email TEXT NOT NULL DEFAULT '',
    target_customer_name TEXT NOT NULL DEFAULT '',
    referral_id INTEGER,
    starts_at TEXT,
    ends_at TEXT,
    expires_at TEXT,
    created_by TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (coupon_type IN ('global', 'referral', 'restaurant_credit'))
);

CREATE TABLE IF NOT EXISTS qrtotem_coupon_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    customer_id INTEGER,
    customer_name TEXT NOT NULL DEFAULT '',
    customer_email TEXT NOT NULL DEFAULT '',
    customer_username TEXT NOT NULL DEFAULT '',
    generated_restaurant_id INTEGER,
    used_restaurant_id INTEGER,
    status TEXT NOT NULL DEFAULT 'code_generated',
    code TEXT NOT NULL DEFAULT '',
    code_generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    code_expires_at TEXT NOT NULL,
    used_at TEXT,
    validated_by_admin_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (campaign_id) REFERENCES qrtotem_coupon_campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (customer_id) REFERENCES customer_coupon_users(id) ON DELETE SET NULL,
    FOREIGN KEY (generated_restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE SET NULL,
    FOREIGN KEY (used_restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE SET NULL,
    FOREIGN KEY (validated_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
    CHECK (status IN ('code_generated', 'used', 'expired', 'cancelled'))
);


CREATE TABLE IF NOT EXISTS qrtotem_restaurant_credit_distributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    amount_per_restaurant REAL NOT NULL CHECK (amount_per_restaurant > 0),
    validity_days INTEGER NOT NULL DEFAULT 30 CHECK (validity_days > 0),
    expires_at TEXT NOT NULL,
    active_restaurants_count INTEGER NOT NULL DEFAULT 0,
    inactive_restaurants_count INTEGER NOT NULL DEFAULT 0,
    total_credit_amount REAL NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS qrtotem_restaurant_credit_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    distribution_id INTEGER NOT NULL,
    restaurant_id INTEGER NOT NULL,
    restaurant_name_snapshot TEXT NOT NULL DEFAULT '',
    owner_name_snapshot TEXT NOT NULL DEFAULT '',
    email_snapshot TEXT NOT NULL DEFAULT '',
    restaurant_was_active INTEGER NOT NULL DEFAULT 0 CHECK (restaurant_was_active IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'not_received',
    initial_amount REAL NOT NULL DEFAULT 0 CHECK (initial_amount >= 0),
    allocated_amount REAL NOT NULL DEFAULT 0 CHECK (allocated_amount >= 0),
    expires_at TEXT,
    not_received_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (distribution_id) REFERENCES qrtotem_restaurant_credit_distributions(id) ON DELETE CASCADE,
    FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
    CHECK (status IN ('available', 'not_received', 'expired', 'consumed'))
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
    if not _table_exists(db, table):
        return set()
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _ensure_column(db: sqlite3.Connection, table: str, column_def: str) -> None:
    column_name = column_def.split()[0]
    if column_name not in _table_info(db, table):
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column_def}')


def _slugify(value: str) -> str:
    value = str(value or '').strip().lower()

    accents = str.maketrans(
        'áàãâäéèêëíìîïóòõôöúùûüçñ',
        'aaaaaeeeeiiiiooooouuuucn',
    )
    value = value.translate(accents)

    value = re.sub(r'[^a-z0-9]+', '-', value)
    value = re.sub(r'-+', '-', value).strip('-')
    return value or 'restaurante'


def _unique_slug(db: sqlite3.Connection, base: str, current_id: int | None = None) -> str:
    if 'slug' not in _table_info(db, 'restaurant_profiles'):
        return _slugify(base)

    slug = _slugify(base)
    candidate = slug
    counter = 2

    while True:
        if current_id:
            row = db.execute(
                'SELECT id FROM restaurant_profiles WHERE slug = ? AND id <> ?',
                (candidate, current_id),
            ).fetchone()
        else:
            row = db.execute(
                'SELECT id FROM restaurant_profiles WHERE slug = ?',
                (candidate,),
            ).fetchone()

        if not row:
            return candidate

        candidate = f'{slug}-{counter}'
        counter += 1


def _unique_token(db: sqlite3.Connection) -> str:
    columns = _table_info(db, 'restaurant_profiles')

    while True:
        token = secrets.token_urlsafe(10).replace('-', '').replace('_', '')[:12]

        if 'public_token' not in columns:
            return token

        row = db.execute(
            'SELECT id FROM restaurant_profiles WHERE public_token = ?',
            (token,),
        ).fetchone()

        if not row:
            return token


def _migrate_admin_passwords(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'admins'):
        return

    columns = _table_info(db, 'admins')

    if 'password_hash' not in columns:
        _ensure_column(db, 'admins', 'password_hash TEXT')

    if 'is_active' not in columns:
        _ensure_column(db, 'admins', 'is_active INTEGER NOT NULL DEFAULT 1')

    if 'kitchen_password_hash' not in columns:
        _ensure_column(db, 'admins', 'kitchen_password_hash TEXT')

    if 'reset_token_hash' not in columns:
        _ensure_column(db, 'admins', 'reset_token_hash TEXT')

    if 'reset_token_expires_at' not in columns:
        _ensure_column(db, 'admins', 'reset_token_expires_at TEXT')

    columns = _table_info(db, 'admins')
    
    if 'password' in columns:
        rows = db.execute('SELECT id, password, password_hash FROM admins').fetchall()
        for row in rows:
            current_hash = row['password_hash'] or ''
            if current_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                continue

            raw = current_hash or row['password'] or os.getenv('ADMIN_PASSWORD', '123456')
            if not str(raw).startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                raw = generate_password_hash(str(raw))

            db.execute(
                'UPDATE admins SET password_hash = ?, kitchen_password_hash = COALESCE(kitchen_password_hash, ?) WHERE id = ?',
                (raw, raw, row['id']),
            )
            
    else:
        rows = db.execute('SELECT id, password_hash FROM admins').fetchall()
        for row in rows:
            current_hash = row['password_hash'] or ''
            if not current_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                new_hash = generate_password_hash(current_hash or os.getenv('ADMIN_PASSWORD', '123456'))
                db.execute(
                    'UPDATE admins SET password_hash = ?, kitchen_password_hash = COALESCE(kitchen_password_hash, ?) WHERE id = ?',
                    (new_hash, new_hash, row['id']),
                )

def _migrate_restaurant_profiles(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'restaurant_profiles'):
        return

    _ensure_column(db, 'restaurant_profiles', 'table_count INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'restaurant_profiles', 'public_token TEXT')
    _ensure_column(db, 'restaurant_profiles', 'slug TEXT')
    _ensure_column(db, 'restaurant_profiles', "order_payment_mode TEXT NOT NULL DEFAULT 'pay_after'")
    _ensure_column(db, 'restaurant_profiles', "service_mode TEXT NOT NULL DEFAULT 'full_order_payment'")
    _ensure_column(db, 'restaurant_profiles', 'is_active INTEGER NOT NULL DEFAULT 1')

    db.execute('''
        UPDATE restaurant_profiles
           SET order_payment_mode = 'pay_after'
         WHERE order_payment_mode IS NULL
            OR order_payment_mode = ''
            OR order_payment_mode NOT IN ('pay_before', 'pay_after')
    ''')

    db.execute('''
        UPDATE restaurant_profiles
           SET service_mode = 'full_order_payment'
         WHERE service_mode IS NULL
            OR service_mode = ''
            OR service_mode NOT IN ('digital_menu', 'full_order_payment')
    ''')

    db.execute('''
        UPDATE restaurant_profiles
           SET is_active = 1
         WHERE is_active IS NULL
            OR is_active NOT IN (0, 1)
    ''')

    rows = db.execute(
        'SELECT id, restaurant_name, public_token, slug FROM restaurant_profiles'
    ).fetchall()

    for row in rows:
        token = row['public_token'] or _unique_token(db)
        slug = row['slug'] or _unique_slug(db, row['restaurant_name'], row['id'])
        db.execute(
            '''
            UPDATE restaurant_profiles
               SET public_token = ?,
                   slug = ?
             WHERE id = ?
            ''',
            (token, slug, row['id']),
        )


def _ensure_default_profile(db: sqlite3.Connection) -> int:
    admin = db.execute(
        'SELECT id, username FROM admins ORDER BY id ASC LIMIT 1'
    ).fetchone()

    if not admin:
        return 0

    profile = db.execute(
        'SELECT id FROM restaurant_profiles WHERE admin_id = ?',
        (admin['id'],),
    ).fetchone()

    if profile:
        return profile['id']

    cursor = db.execute(
        '''
        INSERT INTO restaurant_profiles (
            admin_id,
            owner_name,
            age,
            email,
            restaurant_name,
            cnpj,
            restaurant_address,
            cell_phone,
            table_count,
            service_mode,
            is_active,
            public_token,
            slug
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'full_order_payment', 1, ?, ?)
        ''',
        (
            admin['id'],
            'Administrador',
            18,
            'admin@example.com',
            'Restaurante Demo',
            '00000000000000',
            'Endereço não informado',
            '00000000000',
            _unique_token(db),
            _unique_slug(db, 'Restaurante Demo'),
        ),
    )
    return cursor.lastrowid


def _migrate_products(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'products'):
        return

    _ensure_column(db, 'products', 'restaurant_id INTEGER')
    _ensure_column(db, 'products', 'description TEXT')
    _ensure_column(db, 'products', 'sort_order INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'products', "kind TEXT NOT NULL DEFAULT 'menu'")
    _ensure_column(db, 'products', 'created_at TEXT')
    _ensure_column(db, 'products', 'updated_at TEXT')

    default_profile = db.execute(
        'SELECT id FROM restaurant_profiles ORDER BY id ASC LIMIT 1'
    ).fetchone()

    if default_profile:
        db.execute(
            'UPDATE products SET restaurant_id = COALESCE(restaurant_id, ?)',
            (default_profile['id'],),
        )

    db.execute("UPDATE products SET kind = COALESCE(NULLIF(kind, ''), 'menu')")


def _migrate_customer_coupon_users(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'customer_coupon_users'):
        return

    _ensure_column(db, 'customer_coupon_users', 'receive_whatsapp INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'customer_coupon_users', 'receive_email INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'customer_coupon_users', 'radar_enabled INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'customer_coupon_users', 'updated_at TEXT')


def _ensure_coupon_redemptions(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS coupon_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            coupon_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            code TEXT NOT NULL DEFAULT '',
            reserved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            code_generated_at TEXT,
            code_expires_at TEXT,
            used_at TEXT,
            validated_by_admin_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (coupon_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customer_coupon_users(id) ON DELETE CASCADE,
            FOREIGN KEY (validated_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
            CHECK (status IN ('reserved', 'code_generated', 'used', 'expired', 'cancelled'))
        )
    """)

    _ensure_column(db, 'coupon_redemptions', 'restaurant_id INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'coupon_redemptions', 'coupon_id INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'coupon_redemptions', 'customer_id INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'coupon_redemptions', "status TEXT NOT NULL DEFAULT 'reserved'")
    _ensure_column(db, 'coupon_redemptions', "code TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'coupon_redemptions', 'reserved_at TEXT')
    _ensure_column(db, 'coupon_redemptions', 'expires_at TEXT')
    _ensure_column(db, 'coupon_redemptions', 'code_generated_at TEXT')
    _ensure_column(db, 'coupon_redemptions', 'code_expires_at TEXT')
    _ensure_column(db, 'coupon_redemptions', 'used_at TEXT')
    _ensure_column(db, 'coupon_redemptions', 'validated_by_admin_id INTEGER')
    _ensure_column(db, 'coupon_redemptions', 'created_at TEXT')
    _ensure_column(db, 'coupon_redemptions', 'updated_at TEXT')

    now = datetime.utcnow().isoformat(timespec='seconds')
    db.execute("""
        UPDATE coupon_redemptions
           SET status = CASE
                   WHEN status IN ('reserved', 'code_generated', 'used', 'expired', 'cancelled') THEN status
                   ELSE 'reserved'
               END,
               code = COALESCE(code, ''),
               reserved_at = COALESCE(reserved_at, ?),
               expires_at = COALESCE(expires_at, datetime(?, '+3 hours')),
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)
    """, (now, now, now, now))



def _ensure_qrtotem_coupon_tables(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS qrtotem_coupon_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            coupon_type TEXT NOT NULL DEFAULT 'global',
            value REAL NOT NULL CHECK (value > 0),
            min_purchase_amount REAL NOT NULL CHECK (min_purchase_amount > 0),
            total_quantity INTEGER NOT NULL DEFAULT 0 CHECK (total_quantity >= 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            starts_at TEXT,
            ends_at TEXT,
            created_by TEXT NOT NULL DEFAULT 'owner',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (coupon_type IN ('global', 'referral', 'restaurant_credit'))
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS qrtotem_coupon_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            customer_id INTEGER,
            customer_name TEXT NOT NULL DEFAULT '',
            customer_email TEXT NOT NULL DEFAULT '',
            customer_username TEXT NOT NULL DEFAULT '',
            generated_restaurant_id INTEGER,
            used_restaurant_id INTEGER,
            status TEXT NOT NULL DEFAULT 'code_generated',
            code TEXT NOT NULL DEFAULT '',
            code_generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            code_expires_at TEXT NOT NULL,
            used_at TEXT,
            validated_by_admin_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (campaign_id) REFERENCES qrtotem_coupon_campaigns(id) ON DELETE CASCADE,
            FOREIGN KEY (customer_id) REFERENCES customer_coupon_users(id) ON DELETE SET NULL,
            FOREIGN KEY (generated_restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (used_restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (validated_by_admin_id) REFERENCES admins(id) ON DELETE SET NULL,
            CHECK (status IN ('code_generated', 'used', 'expired', 'cancelled'))
        )
    """)

    for column_def in [
        "title TEXT NOT NULL DEFAULT ''",
        "description TEXT NOT NULL DEFAULT ''",
        "coupon_type TEXT NOT NULL DEFAULT 'global'",
        "value REAL NOT NULL DEFAULT 0",
        "min_purchase_amount REAL NOT NULL DEFAULT 0",
        "total_quantity INTEGER NOT NULL DEFAULT 0",
        "active INTEGER NOT NULL DEFAULT 1",
        "restaurant_id INTEGER",
        "credit_allocation_id INTEGER",
        "target_customer_id INTEGER",
        "target_customer_email TEXT NOT NULL DEFAULT ''",
        "target_customer_name TEXT NOT NULL DEFAULT ''",
        "referral_id INTEGER",
        "starts_at TEXT",
        "ends_at TEXT",
        "expires_at TEXT",
        "created_by TEXT NOT NULL DEFAULT 'owner'",
        "created_at TEXT",
        "updated_at TEXT",
    ]:
        _ensure_column(db, 'qrtotem_coupon_campaigns', column_def)

    for column_def in [
        "campaign_id INTEGER NOT NULL DEFAULT 0",
        "customer_id INTEGER",
        "customer_name TEXT NOT NULL DEFAULT ''",
        "customer_email TEXT NOT NULL DEFAULT ''",
        "customer_username TEXT NOT NULL DEFAULT ''",
        "generated_restaurant_id INTEGER",
        "used_restaurant_id INTEGER",
        "status TEXT NOT NULL DEFAULT 'code_generated'",
        "code TEXT NOT NULL DEFAULT ''",
        "code_generated_at TEXT",
        "code_expires_at TEXT",
        "used_at TEXT",
        "validated_by_admin_id INTEGER",
        "created_at TEXT",
        "updated_at TEXT",
    ]:
        _ensure_column(db, 'qrtotem_coupon_redemptions', column_def)

    now = datetime.utcnow().isoformat(timespec='seconds')
    db.execute("""
        UPDATE qrtotem_coupon_campaigns
           SET min_purchase_amount = CASE
                   WHEN COALESCE(min_purchase_amount, 0) < COALESCE(value, 0) + 5 THEN COALESCE(value, 0) + 5
                   ELSE min_purchase_amount
               END,
               coupon_type = CASE
                   WHEN coupon_type IN ('global', 'referral', 'restaurant_credit') THEN coupon_type
                   ELSE 'global'
               END,
               active = CASE WHEN active IN (0, 1) THEN active ELSE 1 END,
               target_customer_email = lower(COALESCE(target_customer_email, '')),
               target_customer_name = COALESCE(target_customer_name, ''),
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)
    """, (now, now))

    db.execute("""
        UPDATE qrtotem_coupon_redemptions
           SET status = CASE
                   WHEN status IN ('code_generated', 'used', 'expired', 'cancelled') THEN status
                   ELSE 'code_generated'
               END,
               code = COALESCE(code, ''),
               customer_name = COALESCE(customer_name, ''),
               customer_email = lower(COALESCE(customer_email, '')),
               customer_username = COALESCE(customer_username, ''),
               code_generated_at = COALESCE(code_generated_at, ?),
               code_expires_at = COALESCE(code_expires_at, datetime(?, '+10 minutes')),
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)
    """, (now, now, now, now))



def _ensure_referral_tables(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS qrtotem_referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_name TEXT NOT NULL DEFAULT '',
            customer_email TEXT NOT NULL DEFAULT '',
            customer_username TEXT NOT NULL DEFAULT '',
            indicated_restaurant_name TEXT NOT NULL DEFAULT '',
            indicated_contact_name TEXT NOT NULL DEFAULT '',
            indicated_contact_phone TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            approved_restaurant_id INTEGER,
            approved_by TEXT NOT NULL DEFAULT 'owner',
            approved_at TEXT,
            rejected_at TEXT,
            rejection_reason TEXT NOT NULL DEFAULT '',
            monthly_amount REAL NOT NULL DEFAULT 40,
            months_total INTEGER NOT NULL DEFAULT 3,
            campaigns_created INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customer_coupon_users(id) ON DELETE SET NULL,
            FOREIGN KEY (approved_restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE SET NULL,
            CHECK (status IN ('pending', 'approved', 'rejected'))
        )
    """)

    for column_def in [
        "customer_id INTEGER",
        "customer_name TEXT NOT NULL DEFAULT ''",
        "customer_email TEXT NOT NULL DEFAULT ''",
        "customer_username TEXT NOT NULL DEFAULT ''",
        "indicated_restaurant_name TEXT NOT NULL DEFAULT ''",
        "indicated_contact_name TEXT NOT NULL DEFAULT ''",
        "indicated_contact_phone TEXT NOT NULL DEFAULT ''",
        "notes TEXT NOT NULL DEFAULT ''",
        "status TEXT NOT NULL DEFAULT 'pending'",
        "approved_restaurant_id INTEGER",
        "approved_by TEXT NOT NULL DEFAULT 'owner'",
        "approved_at TEXT",
        "rejected_at TEXT",
        "rejection_reason TEXT NOT NULL DEFAULT ''",
        "monthly_amount REAL NOT NULL DEFAULT 40",
        "months_total INTEGER NOT NULL DEFAULT 3",
        "campaigns_created INTEGER NOT NULL DEFAULT 0",
        "created_at TEXT",
        "updated_at TEXT",
    ]:
        _ensure_column(db, 'qrtotem_referrals', column_def)

    now = datetime.utcnow().isoformat(timespec='seconds')
    db.execute("""
        UPDATE qrtotem_referrals
           SET customer_name = COALESCE(customer_name, ''),
               customer_email = lower(COALESCE(customer_email, '')),
               customer_username = COALESCE(customer_username, ''),
               indicated_restaurant_name = COALESCE(indicated_restaurant_name, ''),
               indicated_contact_name = COALESCE(indicated_contact_name, ''),
               indicated_contact_phone = COALESCE(indicated_contact_phone, ''),
               notes = COALESCE(notes, ''),
               status = CASE WHEN status IN ('pending', 'approved', 'rejected') THEN status ELSE 'pending' END,
               monthly_amount = COALESCE(monthly_amount, 40),
               months_total = COALESCE(months_total, 3),
               campaigns_created = COALESCE(campaigns_created, 0),
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)
    """, (now, now))


def _ensure_restaurant_credit_tables(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS qrtotem_restaurant_credit_distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            amount_per_restaurant REAL NOT NULL CHECK (amount_per_restaurant > 0),
            validity_days INTEGER NOT NULL DEFAULT 30 CHECK (validity_days > 0),
            expires_at TEXT NOT NULL,
            active_restaurants_count INTEGER NOT NULL DEFAULT 0,
            inactive_restaurants_count INTEGER NOT NULL DEFAULT 0,
            total_credit_amount REAL NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'owner',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS qrtotem_restaurant_credit_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER NOT NULL,
            restaurant_id INTEGER NOT NULL,
            restaurant_name_snapshot TEXT NOT NULL DEFAULT '',
            owner_name_snapshot TEXT NOT NULL DEFAULT '',
            email_snapshot TEXT NOT NULL DEFAULT '',
            restaurant_was_active INTEGER NOT NULL DEFAULT 0 CHECK (restaurant_was_active IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'not_received',
            initial_amount REAL NOT NULL DEFAULT 0 CHECK (initial_amount >= 0),
            allocated_amount REAL NOT NULL DEFAULT 0 CHECK (allocated_amount >= 0),
            expires_at TEXT,
            not_received_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (distribution_id) REFERENCES qrtotem_restaurant_credit_distributions(id) ON DELETE CASCADE,
            FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
            CHECK (status IN ('available', 'not_received', 'expired', 'consumed'))
        )
    """)

    for column_def in [
        "title TEXT NOT NULL DEFAULT ''",
        "notes TEXT NOT NULL DEFAULT ''",
        "amount_per_restaurant REAL NOT NULL DEFAULT 0",
        "validity_days INTEGER NOT NULL DEFAULT 30",
        "expires_at TEXT",
        "active_restaurants_count INTEGER NOT NULL DEFAULT 0",
        "inactive_restaurants_count INTEGER NOT NULL DEFAULT 0",
        "total_credit_amount REAL NOT NULL DEFAULT 0",
        "created_by TEXT NOT NULL DEFAULT 'owner'",
        "created_at TEXT",
        "updated_at TEXT",
    ]:
        _ensure_column(db, 'qrtotem_restaurant_credit_distributions', column_def)

    for column_def in [
        "distribution_id INTEGER NOT NULL DEFAULT 0",
        "restaurant_id INTEGER NOT NULL DEFAULT 0",
        "restaurant_name_snapshot TEXT NOT NULL DEFAULT ''",
        "owner_name_snapshot TEXT NOT NULL DEFAULT ''",
        "email_snapshot TEXT NOT NULL DEFAULT ''",
        "restaurant_was_active INTEGER NOT NULL DEFAULT 0",
        "status TEXT NOT NULL DEFAULT 'not_received'",
        "initial_amount REAL NOT NULL DEFAULT 0",
        "allocated_amount REAL NOT NULL DEFAULT 0",
        "expires_at TEXT",
        "not_received_reason TEXT NOT NULL DEFAULT ''",
        "created_at TEXT",
        "updated_at TEXT",
    ]:
        _ensure_column(db, 'qrtotem_restaurant_credit_allocations', column_def)

    now = datetime.utcnow().isoformat(timespec='seconds')
    db.execute("""
        UPDATE qrtotem_restaurant_credit_distributions
           SET notes = COALESCE(notes, ''),
               amount_per_restaurant = COALESCE(amount_per_restaurant, 0),
               validity_days = COALESCE(validity_days, 30),
               expires_at = COALESCE(expires_at, datetime(COALESCE(created_at, ?), '+30 days')),
               active_restaurants_count = COALESCE(active_restaurants_count, 0),
               inactive_restaurants_count = COALESCE(inactive_restaurants_count, 0),
               total_credit_amount = COALESCE(total_credit_amount, 0),
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)
    """, (now, now, now))

    db.execute("""
        UPDATE qrtotem_restaurant_credit_allocations
           SET restaurant_name_snapshot = COALESCE(restaurant_name_snapshot, ''),
               owner_name_snapshot = COALESCE(owner_name_snapshot, ''),
               email_snapshot = COALESCE(email_snapshot, ''),
               restaurant_was_active = CASE WHEN restaurant_was_active IN (0, 1) THEN restaurant_was_active ELSE 0 END,
               status = CASE WHEN status IN ('available', 'not_received', 'expired', 'consumed') THEN status ELSE 'not_received' END,
               initial_amount = COALESCE(initial_amount, 0),
               allocated_amount = COALESCE(allocated_amount, 0),
               not_received_reason = COALESCE(not_received_reason, ''),
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)
    """, (now, now))

    db.execute("""
        UPDATE qrtotem_restaurant_credit_allocations
           SET status = 'expired',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'available'
           AND expires_at IS NOT NULL
           AND datetime(expires_at) <= datetime('now')
    """)


def _migrate_orders(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'orders'):
        return

    _ensure_column(db, 'orders', 'restaurant_id INTEGER')
    _ensure_column(db, 'orders', 'customer_name TEXT NOT NULL DEFAULT ""')
    _ensure_column(db, 'orders', 'notes TEXT')
    _ensure_column(db, 'orders', 'total_amount REAL NOT NULL DEFAULT 0')
    _ensure_column(db, 'orders', 'payment_required INTEGER NOT NULL DEFAULT 0')
    _ensure_column(db, 'orders', "payment_status TEXT NOT NULL DEFAULT 'not_required'")
    _ensure_column(db, 'orders', "payment_provider TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', "payment_external_id TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', "payment_external_reference TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', "payment_qr_code TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', "payment_qr_code_base64 TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', "payment_ticket_url TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', 'payment_created_at TEXT')
    _ensure_column(db, 'orders', 'payment_approved_at TEXT')
    _ensure_column(db, 'orders', 'payment_expires_at TEXT')
    _ensure_column(db, 'orders', "payment_error TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'orders', 'updated_at TEXT')

    default_profile = db.execute(
        'SELECT id FROM restaurant_profiles ORDER BY id ASC LIMIT 1'
    ).fetchone()

    if default_profile:
        db.execute(
            'UPDATE orders SET restaurant_id = COALESCE(restaurant_id, ?)',
            (default_profile['id'],),
        )

    db.execute('UPDATE orders SET customer_name = COALESCE(customer_name, "")')
    db.execute('UPDATE orders SET payment_required = COALESCE(payment_required, 0)')
    db.execute('''
        UPDATE orders
           SET payment_status = 'not_required'
         WHERE payment_status IS NULL
            OR payment_status = ''
            OR payment_status NOT IN ('not_required', 'pending', 'approved', 'rejected', 'cancelled', 'expired', 'error')
    ''')


def _ensure_restaurant_payment_accounts(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS restaurant_payment_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            provider TEXT NOT NULL DEFAULT 'mercadopago',
            provider_user_id TEXT NOT NULL DEFAULT '',
            access_token_encrypted TEXT NOT NULL DEFAULT '',
            refresh_token_encrypted TEXT NOT NULL DEFAULT '',
            token_expires_at TEXT,
            public_key TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'not_connected',
            connected_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (restaurant_id) REFERENCES restaurant_profiles(id) ON DELETE CASCADE,
            UNIQUE (restaurant_id, provider),
            CHECK (status IN ('not_connected', 'connected', 'error', 'disabled'))
        )
    ''')

    _ensure_column(db, 'restaurant_payment_accounts', "provider TEXT NOT NULL DEFAULT 'mercadopago'")
    _ensure_column(db, 'restaurant_payment_accounts', "provider_user_id TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'restaurant_payment_accounts', "access_token_encrypted TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'restaurant_payment_accounts', "refresh_token_encrypted TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'restaurant_payment_accounts', 'token_expires_at TEXT')
    _ensure_column(db, 'restaurant_payment_accounts', "public_key TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, 'restaurant_payment_accounts', "status TEXT NOT NULL DEFAULT 'not_connected'")
    _ensure_column(db, 'restaurant_payment_accounts', 'connected_at TEXT')
    _ensure_column(db, 'restaurant_payment_accounts', 'updated_at TEXT')
    _ensure_column(db, 'restaurant_payment_accounts', "last_error TEXT NOT NULL DEFAULT ''")

    db.execute('''
        UPDATE restaurant_payment_accounts
           SET provider = COALESCE(NULLIF(provider, ''), 'mercadopago'),
               provider_user_id = COALESCE(provider_user_id, ''),
               access_token_encrypted = COALESCE(access_token_encrypted, ''),
               refresh_token_encrypted = COALESCE(refresh_token_encrypted, ''),
               public_key = COALESCE(public_key, ''),
               status = CASE
                   WHEN status IN ('not_connected', 'connected', 'error', 'disabled') THEN status
                   ELSE 'not_connected'
               END,
               updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP),
               last_error = COALESCE(last_error, '')
    ''')


def _migrate_order_items(db: sqlite3.Connection) -> None:
    if not _table_exists(db, 'order_items'):
        return

    _ensure_column(db, 'order_items', 'product_name_snapshot TEXT NOT NULL DEFAULT ""')


def _seed_defaults(db: sqlite3.Connection) -> None:
    default_admin_username = os.getenv('ADMIN_USERNAME', 'admin')
    default_admin_password = os.getenv('ADMIN_PASSWORD', '123456')

    if db.execute('SELECT COUNT(*) FROM admins').fetchone()[0] == 0:
        default_hash = generate_password_hash(default_admin_password)
        db.execute(
            'INSERT INTO admins (username, password_hash, kitchen_password_hash, is_active) VALUES (?, ?, ?, 1)',
            (default_admin_username, default_hash, default_hash),
        )
        
    _migrate_restaurant_profiles(db)
    default_restaurant_id = _ensure_default_profile(db)

    if default_restaurant_id and db.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
        for index, (name, price, category) in enumerate(DEFAULT_PRODUCTS):
            db.execute(
                '''
                INSERT INTO products (
                    restaurant_id,
                    name,
                    price,
                    category,
                    active,
                    sort_order,
                    kind
                )
                VALUES (?, ?, ?, ?, 1, ?, 'menu')
                ''',
                (
                    default_restaurant_id,
                    name,
                    price,
                    category,
                    index,
                ),
            )


def _create_indexes(db: sqlite3.Connection) -> None:
    if _table_exists(db, 'restaurant_profiles'):
        columns = _table_info(db, 'restaurant_profiles')

        if 'public_token' in columns:
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurant_profiles_public_token ON restaurant_profiles(public_token)'
            )

        if 'slug' in columns:
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurant_profiles_slug ON restaurant_profiles(slug)'
            )

    if _table_exists(db, 'products'):
        columns = _table_info(db, 'products')

        if {'restaurant_id', 'active', 'category', 'name'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_products_restaurant_active_category ON products(restaurant_id, active, category, name)'
            )

        if {'restaurant_id', 'kind', 'active', 'category', 'name'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_products_restaurant_kind_active_category ON products(restaurant_id, kind, active, category, name)'
            )

    if _table_exists(db, 'customer_coupon_users'):
        columns = _table_info(db, 'customer_coupon_users')

        if {'restaurant_id', 'username'}.issubset(columns):
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_coupon_users_restaurant_username ON customer_coupon_users(restaurant_id, username)'
            )

        if {'restaurant_id', 'radar_enabled'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_customer_coupon_users_restaurant_radar ON customer_coupon_users(restaurant_id, radar_enabled)'
            )

    if _table_exists(db, 'coupon_redemptions'):
        columns = _table_info(db, 'coupon_redemptions')

        if {'restaurant_id', 'customer_id', 'coupon_id', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_coupon_redemptions_customer_coupon_status ON coupon_redemptions(restaurant_id, customer_id, coupon_id, status)'
            )

        if {'restaurant_id', 'code', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_coupon_redemptions_code_status ON coupon_redemptions(restaurant_id, code, status)'
            )

        if {'restaurant_id', 'created_at'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_coupon_redemptions_restaurant_created ON coupon_redemptions(restaurant_id, created_at)'
            )


    if _table_exists(db, 'orders'):
        columns = _table_info(db, 'orders')

        if {'restaurant_id', 'status', 'created_at'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_restaurant_status_created ON orders(restaurant_id, status, created_at)'
            )

        if {'restaurant_id', 'table_number', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_restaurant_table_status ON orders(restaurant_id, table_number, status)'
            )

        if {'restaurant_id', 'payment_required', 'payment_status', 'created_at'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_restaurant_payment_status_created ON orders(restaurant_id, payment_required, payment_status, created_at)'
            )

        if 'payment_external_reference' in columns:
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_payment_external_reference ON orders(payment_external_reference)'
            )

        if 'payment_external_id' in columns:
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_orders_payment_external_id ON orders(payment_external_id)'
            )

    if _table_exists(db, 'restaurant_payment_accounts'):
        columns = _table_info(db, 'restaurant_payment_accounts')

        if {'restaurant_id', 'provider'}.issubset(columns):
            db.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurant_payment_accounts_restaurant_provider ON restaurant_payment_accounts(restaurant_id, provider)'
            )

        if {'restaurant_id', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_restaurant_payment_accounts_restaurant_status ON restaurant_payment_accounts(restaurant_id, status)'
            )

    if _table_exists(db, 'order_items'):
        columns = _table_info(db, 'order_items')

        if 'order_id' in columns:
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)'
            )


    if _table_exists(db, 'qrtotem_coupon_campaigns'):
        columns = _table_info(db, 'qrtotem_coupon_campaigns')
        if {'active', 'coupon_type'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_coupon_campaigns_active_type ON qrtotem_coupon_campaigns(active, coupon_type)'
            )

    if _table_exists(db, 'qrtotem_coupon_redemptions'):
        columns = _table_info(db, 'qrtotem_coupon_redemptions')
        if {'campaign_id', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_coupon_redemptions_campaign_status ON qrtotem_coupon_redemptions(campaign_id, status)'
            )
        if 'code' in columns:
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_coupon_redemptions_code ON qrtotem_coupon_redemptions(code)'
            )
        if {'customer_email', 'campaign_id', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_coupon_redemptions_customer_campaign ON qrtotem_coupon_redemptions(customer_email, campaign_id, status)'
            )



    if _table_exists(db, 'qrtotem_referrals'):
        columns = _table_info(db, 'qrtotem_referrals')
        if {'status', 'created_at'}.issubset(columns):
            db.execute('CREATE INDEX IF NOT EXISTS idx_qrtotem_referrals_status_created ON qrtotem_referrals(status, created_at)')
        if 'customer_email' in columns:
            db.execute('CREATE INDEX IF NOT EXISTS idx_qrtotem_referrals_customer_email ON qrtotem_referrals(customer_email)')

    if _table_exists(db, 'qrtotem_restaurant_credit_distributions'):
        columns = _table_info(db, 'qrtotem_restaurant_credit_distributions')
        if 'created_at' in columns:
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_credit_distributions_created ON qrtotem_restaurant_credit_distributions(created_at)'
            )

    if _table_exists(db, 'qrtotem_restaurant_credit_allocations'):
        columns = _table_info(db, 'qrtotem_restaurant_credit_allocations')
        if {'distribution_id', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_credit_allocations_distribution_status ON qrtotem_restaurant_credit_allocations(distribution_id, status)'
            )
        if {'restaurant_id', 'status'}.issubset(columns):
            db.execute(
                'CREATE INDEX IF NOT EXISTS idx_qrtotem_credit_allocations_restaurant_status ON qrtotem_restaurant_credit_allocations(restaurant_id, status)'
            )


def migrate_schema(db: sqlite3.Connection) -> None:
    _migrate_admin_passwords(db)
    _migrate_restaurant_profiles(db)
    _migrate_products(db)
    _migrate_customer_coupon_users(db)
    _ensure_coupon_redemptions(db)
    _ensure_qrtotem_coupon_tables(db)
    _ensure_referral_tables(db)
    _ensure_restaurant_credit_tables(db)
    _migrate_orders(db)
    _ensure_restaurant_payment_accounts(db)
    _migrate_order_items(db)


def _backfill_timestamps(db: sqlite3.Connection) -> None:
    now = datetime.utcnow().isoformat(timespec='seconds')

    if _table_exists(db, 'products'):
        columns = _table_info(db, 'products')

        if 'created_at' in columns:
            db.execute('UPDATE products SET created_at = COALESCE(created_at, ?)', (now,))

        if 'updated_at' in columns:
            db.execute('UPDATE products SET updated_at = COALESCE(updated_at, ?)', (now,))

        if 'kind' in columns:
            db.execute("UPDATE products SET kind = COALESCE(NULLIF(kind, ''), 'menu')")

    if _table_exists(db, 'customer_coupon_users'):
        columns = _table_info(db, 'customer_coupon_users')

        if 'updated_at' in columns:
            db.execute('UPDATE customer_coupon_users SET updated_at = COALESCE(updated_at, ?)', (now,))

        if 'radar_enabled' in columns:
            db.execute('UPDATE customer_coupon_users SET radar_enabled = COALESCE(radar_enabled, 0)')

    if _table_exists(db, 'coupon_redemptions'):
        columns = _table_info(db, 'coupon_redemptions')

        if 'updated_at' in columns:
            db.execute('UPDATE coupon_redemptions SET updated_at = COALESCE(updated_at, ?)', (now,))


    if _table_exists(db, 'qrtotem_coupon_campaigns'):
        columns = _table_info(db, 'qrtotem_coupon_campaigns')
        if 'updated_at' in columns:
            db.execute('UPDATE qrtotem_coupon_campaigns SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'qrtotem_coupon_redemptions'):
        columns = _table_info(db, 'qrtotem_coupon_redemptions')
        if 'updated_at' in columns:
            db.execute('UPDATE qrtotem_coupon_redemptions SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'qrtotem_referrals'):
        columns = _table_info(db, 'qrtotem_referrals')
        if 'updated_at' in columns:
            db.execute('UPDATE qrtotem_referrals SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'qrtotem_restaurant_credit_distributions'):
        columns = _table_info(db, 'qrtotem_restaurant_credit_distributions')
        if 'updated_at' in columns:
            db.execute('UPDATE qrtotem_restaurant_credit_distributions SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'qrtotem_restaurant_credit_allocations'):
        columns = _table_info(db, 'qrtotem_restaurant_credit_allocations')
        if 'updated_at' in columns:
            db.execute('UPDATE qrtotem_restaurant_credit_allocations SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'orders'):
        columns = _table_info(db, 'orders')

        if 'updated_at' in columns:
            db.execute('UPDATE orders SET updated_at = COALESCE(updated_at, ?)', (now,))

    if _table_exists(db, 'restaurant_payment_accounts'):
        columns = _table_info(db, 'restaurant_payment_accounts')

        if 'updated_at' in columns:
            db.execute('UPDATE restaurant_payment_accounts SET updated_at = COALESCE(updated_at, ?)', (now,))


def init_db(app):
    @app.teardown_appcontext
    def _close_db(exception):
        close_db(exception)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        db = get_db()

        db.executescript(SCHEMA_SQL)

        migrate_schema(db)
        _seed_defaults(db)
        migrate_schema(db)
        _backfill_timestamps(db)
        _create_indexes(db)

        db.commit()
