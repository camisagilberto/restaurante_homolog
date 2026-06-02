from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..db import get_db
from ..security import csrf_token
from ..utils import format_currency, normalize_text

owner_admin_bp = Blueprint('owner_admin', __name__, url_prefix='/ops-qrtotem')

MONTHLY_PRICE = 149.99
REFERRAL_MONTHLY_AMOUNT = 40.0
REFERRAL_MONTHS_TOTAL = 3


COUPON_TYPE_LABELS = {
    'global': 'Global QRTotem',
    'referral': 'Indicação',
    'restaurant_credit': 'Crédito do restaurante',
}


def _parse_money(value: str) -> float:
    normalized = str(value or '').strip().replace('.', '').replace(',', '.')
    try:
        parsed = float(normalized)
    except (TypeError, ValueError):
        return 0.0
    return round(parsed, 2)


def _parse_int(value: str) -> int:
    try:
        return max(0, int(str(value or '').strip()))
    except (TypeError, ValueError):
        return 0



def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _expire_restaurant_credit_allocations(db) -> None:
    db.execute(
        """
        UPDATE qrtotem_restaurant_credit_allocations
           SET status = 'expired',
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'available'
           AND expires_at IS NOT NULL
           AND datetime(expires_at) <= datetime('now')
        """
    )


def _coupon_type_label(coupon_type: str) -> str:
    return COUPON_TYPE_LABELS.get(coupon_type or 'global', 'Global QRTotem')


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
        'SELECT COUNT(*) FROM restaurant_profiles WHERE COALESCE(is_active, 1) = 1',
    ) if _table_exists(db, 'restaurant_profiles') else 0

    restaurants_with_orders_month = _safe_count(
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
                COALESCE(rp.is_active, 1) AS is_active,
                COALESCE(rp.service_mode, 'full_order_payment') AS service_mode,
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

    restaurants = []
    if _table_exists(db, 'restaurant_profiles'):
        restaurants = db.execute(
            '''
            SELECT
                rp.id,
                rp.restaurant_name,
                rp.owner_name,
                rp.email,
                COALESCE(rp.is_active, 1) AS is_active,
                COALESCE(rp.service_mode, 'full_order_payment') AS service_mode,
                COUNT(o.id) AS total_orders,
                MAX(o.created_at) AS last_order_at
              FROM restaurant_profiles rp
              LEFT JOIN orders o
                ON o.restaurant_id = rp.id
             GROUP BY rp.id
             ORDER BY COALESCE(rp.is_active, 1) DESC, rp.restaurant_name ASC
            '''
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

    monthly_revenue = active_restaurants * MONTHLY_PRICE
    critical_issues = stale_orders + restaurants_without_active_products
    max_growth = max([int(row['total_orders'] or 0) for row in growth], default=0)

    return {
        'monthly_price': MONTHLY_PRICE,
        'monthly_price_formatted': format_currency(MONTHLY_PRICE),
        'restaurant_count': restaurant_count,
        'active_restaurants': active_restaurants,
        'restaurants_with_orders_month': restaurants_with_orders_month,
        'orders_month': orders_month,
        'orders_variation': _percentage_change(orders_month, orders_previous_month),
        'monthly_revenue': monthly_revenue,
        'monthly_revenue_formatted': format_currency(monthly_revenue),
        'customers_total': customers_total,
        'radar_total': radar_total,
        'critical_issues': critical_issues,
        'top_restaurants': top_restaurants,
        'restaurants': restaurants,
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


@owner_admin_bp.route('/restaurantes/<int:restaurant_id>/toggle', methods=['POST'])
@_owner_login_required
def toggle_restaurant_status(restaurant_id: int):
    db = get_db()
    profile = db.execute(
        'SELECT id, restaurant_name, COALESCE(is_active, 1) AS is_active FROM restaurant_profiles WHERE id = ? LIMIT 1',
        (restaurant_id,),
    ).fetchone()

    if not profile:
        flash('Restaurante não encontrado.', 'error')
        return redirect(url_for('owner_admin.dashboard'))

    new_status = 0 if int(profile['is_active'] or 0) else 1
    db.execute(
        'UPDATE restaurant_profiles SET is_active = ? WHERE id = ?',
        (new_status, restaurant_id),
    )
    db.commit()

    action = 'ativado' if new_status else 'inativado'
    flash(f'Restaurante {profile["restaurant_name"]} {action} com sucesso.', 'success')
    return redirect(url_for('owner_admin.dashboard'))



@owner_admin_bp.route('/creditos-restaurantes', methods=['GET', 'POST'])
@_owner_login_required
def restaurant_credits():
    db = get_db()
    _expire_restaurant_credit_allocations(db)

    if request.method == 'POST':
        title = str(request.form.get('title') or '').strip()
        notes = str(request.form.get('notes') or '').strip()
        amount = _parse_money(request.form.get('amount_per_restaurant'))
        validity_days = 30

        restaurants = db.execute(
            """
            SELECT id,
                   restaurant_name,
                   owner_name,
                   email,
                   COALESCE(is_active, 1) AS is_active
              FROM restaurant_profiles
             ORDER BY restaurant_name ASC
            """
        ).fetchall()

        if not title:
            flash('Informe um nome para a distribuição de crédito.', 'error')
        elif amount <= 0:
            flash('Informe um valor de crédito maior que zero.', 'error')
        elif not restaurants:
            flash('Não há restaurantes cadastrados para receber crédito.', 'warning')
        else:
            now = datetime.utcnow()
            expires_at = now + timedelta(days=validity_days)
            active_count = sum(1 for row in restaurants if int(row['is_active'] or 0) == 1)
            inactive_count = len(restaurants) - active_count
            total_credit = round(active_count * amount, 2)

            cursor = db.execute(
                """
                INSERT INTO qrtotem_restaurant_credit_distributions (
                    title,
                    notes,
                    amount_per_restaurant,
                    validity_days,
                    expires_at,
                    active_restaurants_count,
                    inactive_restaurants_count,
                    total_credit_amount,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'owner', ?, ?)
                """,
                (
                    title,
                    notes,
                    amount,
                    validity_days,
                    _iso(expires_at),
                    active_count,
                    inactive_count,
                    total_credit,
                    _iso(now),
                    _iso(now),
                ),
            )
            distribution_id = cursor.lastrowid

            for row in restaurants:
                was_active = 1 if int(row['is_active'] or 0) == 1 else 0
                received_amount = amount if was_active else 0.0
                status = 'available' if was_active else 'not_received'
                reason = '' if was_active else 'Restaurante inativo no momento da distribuição.'

                db.execute(
                    """
                    INSERT INTO qrtotem_restaurant_credit_allocations (
                        distribution_id,
                        restaurant_id,
                        restaurant_name_snapshot,
                        owner_name_snapshot,
                        email_snapshot,
                        restaurant_was_active,
                        status,
                        initial_amount,
                        allocated_amount,
                        expires_at,
                        not_received_reason,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        distribution_id,
                        row['id'],
                        row['restaurant_name'] or '',
                        row['owner_name'] or '',
                        row['email'] or '',
                        was_active,
                        status,
                        received_amount,
                        _iso(expires_at) if was_active else None,
                        reason,
                        _iso(now),
                        _iso(now),
                    ),
                )

            db.commit()
            flash(
                f'Distribuição criada: {active_count} restaurante(s) ativo(s) receberam crédito e {inactive_count} inativo(s) ficaram registrados sem crédito.',
                'success',
            )
            return redirect(url_for('owner_admin.restaurant_credits'))

    db.commit()

    distributions = db.execute(
        """
        SELECT d.*,
               COUNT(a.id) AS total_restaurants,
               COALESCE(SUM(CASE WHEN a.restaurant_was_active = 1 THEN 1 ELSE 0 END), 0) AS received_count,
               COALESCE(SUM(CASE WHEN a.restaurant_was_active = 0 THEN 1 ELSE 0 END), 0) AS not_received_count,
               COALESCE(SUM(CASE WHEN a.status = 'available' THEN a.initial_amount - a.allocated_amount ELSE 0 END), 0) AS available_balance,
               COALESCE(SUM(CASE WHEN a.status = 'expired' THEN a.initial_amount - a.allocated_amount ELSE 0 END), 0) AS expired_balance
          FROM qrtotem_restaurant_credit_distributions d
          LEFT JOIN qrtotem_restaurant_credit_allocations a ON a.distribution_id = d.id
         GROUP BY d.id
         ORDER BY d.created_at DESC, d.id DESC
         LIMIT 40
        """
    ).fetchall()

    allocations = db.execute(
        """
        SELECT a.*,
               d.title AS distribution_title,
               d.amount_per_restaurant,
               d.created_at AS distribution_created_at
          FROM qrtotem_restaurant_credit_allocations a
          JOIN qrtotem_restaurant_credit_distributions d ON d.id = a.distribution_id
         ORDER BY d.created_at DESC, a.restaurant_name_snapshot ASC
         LIMIT 250
        """
    ).fetchall()

    totals = db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN status = 'available' THEN initial_amount - allocated_amount ELSE 0 END), 0) AS available_balance,
               COALESCE(SUM(CASE WHEN status = 'expired' THEN initial_amount - allocated_amount ELSE 0 END), 0) AS expired_balance,
               COALESCE(SUM(CASE WHEN status = 'available' THEN allocated_amount ELSE 0 END), 0) AS allocated_amount
          FROM qrtotem_restaurant_credit_allocations
        """
    ).fetchone()

    return render_template(
        'owner_admin/restaurant_credits.html',
        distributions=distributions,
        allocations=allocations,
        totals=totals,
        validity_days=30,
        csrf=csrf_token(),
    )


@owner_admin_bp.route('/indicacoes', methods=['GET'])
@_owner_login_required
def referrals():
    db = get_db()

    referrals = db.execute(
        """
        SELECT r.*, rp.restaurant_name AS approved_restaurant_name
          FROM qrtotem_referrals r
          LEFT JOIN restaurant_profiles rp ON rp.id = r.approved_restaurant_id
         ORDER BY CASE r.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                  r.created_at DESC,
                  r.id DESC
         LIMIT 200
        """
    ).fetchall()

    active_restaurants = db.execute(
        """
        SELECT id, restaurant_name, owner_name, email, COALESCE(is_active, 1) AS is_active
          FROM restaurant_profiles
         WHERE COALESCE(is_active, 1) = 1
         ORDER BY restaurant_name ASC
        """
    ).fetchall()

    stats = db.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count,
               COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) AS approved_count,
               COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0) AS rejected_count
          FROM qrtotem_referrals
        """
    ).fetchone()

    return render_template(
        'owner_admin/referrals.html',
        referrals=referrals,
        active_restaurants=active_restaurants,
        stats=stats,
        monthly_amount=REFERRAL_MONTHLY_AMOUNT,
        months_total=REFERRAL_MONTHS_TOTAL,
        csrf=csrf_token(),
    )


@owner_admin_bp.route('/indicacoes/<int:referral_id>/aprovar', methods=['POST'])
@_owner_login_required
def approve_referral(referral_id: int):
    db = get_db()
    referral = db.execute('SELECT * FROM qrtotem_referrals WHERE id = ? LIMIT 1', (referral_id,)).fetchone()

    if not referral:
        flash('Indicação não encontrada.', 'error')
        return redirect(url_for('owner_admin.referrals'))

    if referral['status'] != 'pending':
        flash('Essa indicação já foi analisada.', 'warning')
        return redirect(url_for('owner_admin.referrals'))

    try:
        approved_restaurant_id = int(request.form.get('approved_restaurant_id') or 0)
    except (TypeError, ValueError):
        approved_restaurant_id = 0

    restaurant = db.execute(
        """
        SELECT id, restaurant_name, COALESCE(is_active, 1) AS is_active
          FROM restaurant_profiles
         WHERE id = ?
         LIMIT 1
        """,
        (approved_restaurant_id,),
    ).fetchone()

    if not restaurant:
        flash('Selecione o restaurante que entrou no QRTotem.', 'error')
        return redirect(url_for('owner_admin.referrals'))

    if int(restaurant['is_active'] or 0) != 1:
        flash('A indicação só pode ser aprovada se o restaurante estiver ativo.', 'error')
        return redirect(url_for('owner_admin.referrals'))

    now = datetime.utcnow().replace(microsecond=0)
    customer_email = str(referral['customer_email'] or '').strip().lower()
    customer_name = str(referral['customer_name'] or '').strip()
    monthly_amount = REFERRAL_MONTHLY_AMOUNT
    months_total = REFERRAL_MONTHS_TOTAL

    for month_index in range(months_total):
        starts_at = now + timedelta(days=30 * month_index)
        expires_at = starts_at + timedelta(days=30)
        title = f'Indicação QRTotem R$40 - mês {month_index + 1}/{months_total}'
        description = (
            f'Benefício pela indicação do restaurante {referral["indicated_restaurant_name"]}. '
            'Uso único, código numérico e validação pelo atendente.'
        )
        db.execute(
            """
            INSERT INTO qrtotem_coupon_campaigns (
                title,
                description,
                coupon_type,
                value,
                min_purchase_amount,
                total_quantity,
                active,
                target_customer_id,
                target_customer_email,
                target_customer_name,
                referral_id,
                starts_at,
                ends_at,
                expires_at,
                created_by,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'referral', ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, 'owner', ?, ?)
            """,
            (
                title,
                description,
                monthly_amount,
                round(monthly_amount + 5, 2),
                referral['customer_id'],
                customer_email,
                customer_name,
                referral_id,
                _iso(starts_at),
                _iso(expires_at),
                _iso(expires_at),
                _iso(now),
                _iso(now),
            ),
        )

    db.execute(
        """
        UPDATE qrtotem_referrals
           SET status = 'approved',
               approved_restaurant_id = ?,
               approved_at = ?,
               monthly_amount = ?,
               months_total = ?,
               campaigns_created = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (
            approved_restaurant_id,
            _iso(now),
            monthly_amount,
            months_total,
            months_total,
            _iso(now),
            referral_id,
        ),
    )
    db.commit()
    flash('Indicação aprovada. Foram criados 3 cupons de R$40, liberados um por mês para o usuário indicado.', 'success')
    return redirect(url_for('owner_admin.referrals'))


@owner_admin_bp.route('/indicacoes/<int:referral_id>/rejeitar', methods=['POST'])
@_owner_login_required
def reject_referral(referral_id: int):
    db = get_db()
    referral = db.execute('SELECT id, status FROM qrtotem_referrals WHERE id = ? LIMIT 1', (referral_id,)).fetchone()

    if not referral:
        flash('Indicação não encontrada.', 'error')
        return redirect(url_for('owner_admin.referrals'))

    if referral['status'] != 'pending':
        flash('Essa indicação já foi analisada.', 'warning')
        return redirect(url_for('owner_admin.referrals'))

    reason = str(request.form.get('rejection_reason') or '').strip()
    now = _iso(datetime.utcnow().replace(microsecond=0))
    db.execute(
        """
        UPDATE qrtotem_referrals
           SET status = 'rejected',
               rejected_at = ?,
               rejection_reason = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (now, reason, now, referral_id),
    )
    db.commit()
    flash('Indicação rejeitada.', 'success')
    return redirect(url_for('owner_admin.referrals'))


@owner_admin_bp.route('/cupons-qrtotem', methods=['GET', 'POST'])
@_owner_login_required
def qrtotem_coupons():
    db = get_db()

    if request.method == 'POST':
        name = str(request.form.get('name') or '').strip()
        description = str(request.form.get('description') or '').strip()
        coupon_type = str(request.form.get('coupon_type') or 'global').strip()
        discount_amount = _parse_money(request.form.get('discount_amount'))
        total_quantity = _parse_int(request.form.get('total_quantity'))
        active = 1 if request.form.get('active') == '1' else 0

        if coupon_type != 'global':
            coupon_type = 'global'

        min_order_amount = round(discount_amount + 5, 2) if discount_amount > 0 else 0

        if not name:
            flash('Informe o nome do cupom.', 'error')
        elif discount_amount <= 0:
            flash('Informe um valor de cupom maior que zero.', 'error')
        elif total_quantity <= 0:
            flash('Informe a quantidade de cupons disponíveis.', 'error')
        else:
            db.execute(
                """
                INSERT INTO qrtotem_coupon_campaigns (
                    title,
                    description,
                    coupon_type,
                    value,
                    min_purchase_amount,
                    total_quantity,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (name, description, coupon_type, discount_amount, min_order_amount, total_quantity, active),
            )
            db.commit()
            flash('Cupom rastreável criado com sucesso.', 'success')
            return redirect(url_for('owner_admin.qrtotem_coupons'))

    campaigns = db.execute(
        """
        SELECT c.*,
               COALESCE(SUM(CASE WHEN cl.status = 'used' THEN 1 ELSE 0 END), 0) AS used_count,
               COALESCE(SUM(CASE WHEN cl.status = 'code_generated' THEN 1 ELSE 0 END), 0) AS pending_count
          FROM qrtotem_coupon_campaigns c
          LEFT JOIN qrtotem_coupon_redemptions cl ON cl.campaign_id = c.id
         GROUP BY c.id
         ORDER BY c.created_at DESC, c.id DESC
        """
    ).fetchall()

    usage_history = db.execute(
        """
        SELECT cl.*,
               c.title AS campaign_title,
               c.value,
               c.min_purchase_amount,
               c.coupon_type,
               rp.restaurant_name,
               cu.name AS customer_name,
               cu.username AS customer_username,
               cu.email AS customer_email,
               cu.cell_phone AS customer_cell_phone
          FROM qrtotem_coupon_redemptions cl
          JOIN qrtotem_coupon_campaigns c ON c.id = cl.campaign_id
          JOIN customer_coupon_users cu ON cu.id = cl.customer_id
          LEFT JOIN restaurant_profiles rp ON rp.id = cl.used_restaurant_id
         WHERE cl.status = 'used'
         ORDER BY cl.used_at DESC, cl.id DESC
         LIMIT 80
        """
    ).fetchall()

    return render_template(
        'owner_admin/qrtotem_coupons.html',
        campaigns=campaigns,
        usage_history=usage_history,
        coupon_type_labels=COUPON_TYPE_LABELS,
        coupon_type_label=_coupon_type_label,
        csrf=csrf_token(),
    )


@owner_admin_bp.route('/cupons-qrtotem/<int:campaign_id>/toggle', methods=['POST'])
@_owner_login_required
def toggle_qrtotem_coupon(campaign_id: int):
    db = get_db()
    campaign = db.execute('SELECT id, active FROM qrtotem_coupon_campaigns WHERE id = ? LIMIT 1', (campaign_id,)).fetchone()

    if not campaign:
        flash('Cupom rastreável não encontrado.', 'error')
        return redirect(url_for('owner_admin.qrtotem_coupons'))

    new_status = 0 if int(campaign['active'] or 0) else 1
    db.execute('UPDATE qrtotem_coupon_campaigns SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (new_status, campaign_id))
    db.commit()
    flash('Status do cupom atualizado.', 'success')
    return redirect(url_for('owner_admin.qrtotem_coupons'))
