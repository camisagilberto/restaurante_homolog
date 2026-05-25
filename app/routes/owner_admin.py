from __future__ import annotations

import os
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..db import get_db
from ..security import csrf_token
from ..utils import format_currency, normalize_text

owner_admin_bp = Blueprint('owner_admin', __name__, url_prefix='/ops-qrtotem')

MONTHLY_PRICE = 149.99


def _owner_credentials() -> tuple[str, str]:
    username = os.getenv('OWNER_ADMIN_USERNAME', 'dono')
    password = os.getenv('OWNER_ADMIN_PASSWORD', 'troque-esta-senha')
    return username, password


def _owner_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('owner_admin_logged_in'):
            return redirect(url_for('owner_admin.login'))
        return view(*args, **kwargs)

    return wrapped


@owner_admin_bp.after_request
def _noindex_owner_pages(response):
    response.headers.setdefault('X-Robots-Tag', 'noindex, nofollow, noarchive')
    response.headers.setdefault('Cache-Control', 'no-store')
    return response


def _table_exists(db, table_name: str) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _safe_count(db, sql: str, params: tuple = ()) -> int:
    try:
        row = db.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _current_month() -> str:
    return datetime.now().strftime('%Y-%m')


def _previous_month() -> str:
    now = datetime.now()
    year = now.year
    month = now.month - 1

    if month == 0:
        month = 12
        year -= 1

    return f'{year:04d}-{month:02d}'


def _percentage_change(current: int, previous: int) -> str:
    if previous <= 0 and current > 0:
        return '+100% vs mês anterior'

    if previous <= 0:
        return 'Sem base anterior'

    value = ((current - previous) / previous) * 100
    signal = '+' if value >= 0 else ''
    return f'{signal}{value:.0f}% vs mês anterior'


def _get_dashboard_data(db) -> dict:
    month = _current_month()
    previous_month = _previous_month()

    restaurant_count = _safe_count(
        db,
        'SELECT COUNT(*) FROM restaurant_profiles',
    ) if _table_exists(db, 'restaurant_profiles') else 0

    active_restaurants = _safe_count(
        db,
        '''
        SELECT COUNT(DISTINCT restaurant_id)
          FROM orders
         WHERE restaurant_id IS NOT NULL
           AND strftime('%Y-%m', created_at) = ?
        ''',
        (month,),
    ) if _table_exists(db, 'orders') else 0

    orders_month = _safe_count(
        db,
        "SELECT COUNT(*) FROM orders WHERE strftime('%Y-%m', created_at) = ?",
        (month,),
    ) if _table_exists(db, 'orders') else 0

    orders_previous_month = _safe_count(
        db,
        "SELECT COUNT(*) FROM orders WHERE strftime('%Y-%m', created_at) = ?",
        (previous_month,),
    ) if _table_exists(db, 'orders') else 0

    customers_total = _safe_count(
        db,
        'SELECT COUNT(*) FROM customer_coupon_users',
    ) if _table_exists(db, 'customer_coupon_users') else 0

    radar_total = _safe_count(
        db,
        'SELECT COUNT(*) FROM customer_coupon_users WHERE radar_enabled = 1',
    ) if _table_exists(db, 'customer_coupon_users') else 0

    open_orders = _safe_count(
        db,
        "SELECT COUNT(*) FROM orders WHERE status IN ('novo', 'preparando')",
    ) if _table_exists(db, 'orders') else 0

    stale_orders = _safe_count(
        db,
        '''
        SELECT COUNT(*)
          FROM orders
         WHERE status IN ('novo', 'preparando')
           AND datetime(created_at) <= datetime('now', '-30 minutes')
        ''',
    ) if _table_exists(db, 'orders') else 0

    restaurants_without_active_products = _safe_count(
        db,
        '''
        SELECT COUNT(*)
          FROM restaurant_profiles rp
         WHERE NOT EXISTS (
               SELECT 1
                 FROM products p
                WHERE p.restaurant_id = rp.id
                  AND p.kind = 'menu'
                  AND p.active = 1
         )
        ''',
    ) if _table_exists(db, 'restaurant_profiles') and _table_exists(db, 'products') else 0

    inactive_restaurants_7_days = _safe_count(
        db,
        '''
        SELECT COUNT(*)
          FROM restaurant_profiles rp
         WHERE NOT EXISTS (
               SELECT 1
                 FROM orders o
                WHERE o.restaurant_id = rp.id
                  AND datetime(o.created_at) >= datetime('now', '-7 days')
         )
        ''',
    ) if _table_exists(db, 'restaurant_profiles') and _table_exists(db, 'orders') else 0

    top_restaurants = []
    if _table_exists(db, 'restaurant_profiles'):
        top_restaurants = db.execute(
            '''
            SELECT
                rp.id,
                rp.restaurant_name,
                rp.owner_name,
                rp.email,
                COALESCE(COUNT(o.id), 0) AS orders_month,
                MAX(o.created_at) AS last_order_at,
                COALESCE(SUM(o.total_amount), 0) AS gross_volume
              FROM restaurant_profiles rp
              LEFT JOIN orders o
                ON o.restaurant_id = rp.id
               AND strftime('%Y-%m', o.created_at) = ?
             GROUP BY rp.id
             ORDER BY orders_month DESC, last_order_at DESC, rp.restaurant_name ASC
             LIMIT 5
            ''',
            (month,),
        ).fetchall()

    growth = []
    if _table_exists(db, 'orders'):
        growth = db.execute(
            '''
            WITH months(month_ref) AS (
                SELECT strftime('%Y-%m', date('now', '-5 months'))
                UNION ALL SELECT strftime('%Y-%m', date('now', '-4 months'))
                UNION ALL SELECT strftime('%Y-%m', date('now', '-3 months'))
                UNION ALL SELECT strftime('%Y-%m', date('now', '-2 months'))
                UNION ALL SELECT strftime('%Y-%m', date('now', '-1 month'))
                UNION ALL SELECT strftime('%Y-%m', date('now'))
            )
            SELECT
                months.month_ref,
                COALESCE(COUNT(orders.id), 0) AS total_orders
              FROM months
              LEFT JOIN orders
                ON strftime('%Y-%m', orders.created_at) = months.month_ref
             GROUP BY months.month_ref
             ORDER BY months.month_ref ASC
            '''
        ).fetchall()

    alerts = []

    if stale_orders > 0:
        alerts.append({
            'priority': 'Alta',
            'title': f'{stale_orders} pedido(s) parado(s) há mais de 30 minutos',
            'detail': 'Verifique a cozinha para evitar atraso no atendimento.',
            'tone': 'danger',
        })

    if restaurants_without_active_products > 0:
        alerts.append({
            'priority': 'Alta',
            'title': f'{restaurants_without_active_products} restaurante(s) sem produtos ativos',
            'detail': 'Sem produtos ativos, o cliente não consegue fazer pedido.',
            'tone': 'danger',
        })

    if inactive_restaurants_7_days > 0:
        alerts.append({
            'priority': 'Média',
            'title': f'{inactive_restaurants_7_days} restaurante(s) sem pedidos nos últimos 7 dias',
            'detail': 'Pode indicar baixo uso, operação parada ou risco de cancelamento.',
            'tone': 'warning',
        })

    if not alerts:
        alerts.append({
            'priority': 'OK',
            'title': 'Nenhum problema crítico encontrado',
            'detail': 'A operação geral não apresenta alertas prioritários neste momento.',
            'tone': 'success',
        })

    platform_status = [
        {
            'label': 'Site e painel',
            'status': 'Normal',
            'tone': 'success',
            'detail': 'Aplicação carregando e banco acessível.',
        },
        {
            'label': 'Pedidos em aberto',
            'status': 'Atenção' if stale_orders else 'Normal',
            'tone': 'warning' if stale_orders else 'success',
            'detail': f'{open_orders} pedido(s) em andamento.',
        },
        {
            'label': 'Cardápios ativos',
            'status': 'Atenção' if restaurants_without_active_products else 'Normal',
            'tone': 'warning' if restaurants_without_active_products else 'success',
            'detail': f'{restaurants_without_active_products} restaurante(s) sem item ativo.',
        },
        {
            'label': 'Pagamentos dos restaurantes',
            'status': 'Manual',
            'tone': 'neutral',
            'detail': 'Controle real entra quando integrar Mercado Pago/PIX.',
        },
    ]

    monthly_revenue = restaurant_count * MONTHLY_PRICE
    critical_issues = stale_orders + restaurants_without_active_products
    max_growth = max([int(row['total_orders'] or 0) for row in growth], default=0)

    return {
        'monthly_price': MONTHLY_PRICE,
        'monthly_price_formatted': format_currency(MONTHLY_PRICE),
        'restaurant_count': restaurant_count,
        'active_restaurants': active_restaurants,
        'orders_month': orders_month,
        'orders_variation': _percentage_change(orders_month, orders_previous_month),
        'monthly_revenue': monthly_revenue,
        'monthly_revenue_formatted': format_currency(monthly_revenue),
        'customers_total': customers_total,
        'radar_total': radar_total,
        'critical_issues': critical_issues,
        'top_restaurants': top_restaurants,
        'growth': growth,
        'max_growth': max_growth,
        'alerts': alerts,
        'platform_status': platform_status,
        'generated_at': datetime.now().strftime('%d/%m/%Y %H:%M'),
    }


@owner_admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('owner_admin_logged_in'):
        return redirect(url_for('owner_admin.dashboard'))

    expected_username, expected_password = _owner_credentials()

    if request.method == 'POST':
        username = normalize_text(request.form.get('username'))
        password = str(request.form.get('password') or '')

        username_ok = secrets.compare_digest(username, expected_username)
        password_ok = secrets.compare_digest(password, expected_password)

        if username_ok and password_ok:
            session.clear()
            session['owner_admin_logged_in'] = True
            session['owner_admin_username'] = expected_username
            flash('Acesso interno liberado.', 'success')
            return redirect(url_for('owner_admin.dashboard'))

        flash('Credenciais internas inválidas.', 'error')

    return render_template('owner_admin/login.html', csrf=csrf_token())


@owner_admin_bp.route('/logout')
@_owner_login_required
def logout():
    session.clear()
    flash('Sessão interna encerrada.', 'success')
    return redirect(url_for('owner_admin.login'))


@owner_admin_bp.route('/')
@_owner_login_required
def dashboard():
    db = get_db()
    dashboard_data = _get_dashboard_data(db)
    return render_template(
        'owner_admin/dashboard.html',
        data=dashboard_data,
        csrf=csrf_token(),
    )
