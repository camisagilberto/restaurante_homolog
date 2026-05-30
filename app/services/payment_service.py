from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import current_app

from .payment_crypto import is_payment_crypto_ready

PROVIDER_MERCADO_PAGO = 'mercadopago'
PAYMENT_ACCOUNT_STATUSES = {'not_connected', 'connected', 'error', 'disabled'}
SERVICE_MODE_DIGITAL_MENU = 'digital_menu'
SERVICE_MODE_FULL_ORDER_PAYMENT = 'full_order_payment'


@dataclass(frozen=True)
class PaymentEnvironmentStatus:
    oauth_ready: bool
    crypto_ready: bool
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.oauth_ready and self.crypto_ready


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec='seconds')


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if not row:
        return default

    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        pass

    if isinstance(row, dict):
        return row.get(key, default)

    return default


def normalize_payment_account_status(status: Any) -> str:
    value = str(status or '').strip()
    return value if value in PAYMENT_ACCOUNT_STATUSES else 'not_connected'


def payment_environment_status() -> PaymentEnvironmentStatus:
    """Valida somente a configuração local necessária para iniciar OAuth nos próximos blocos."""
    missing: list[str] = []
    config = current_app.config

    if not str(config.get('MP_CLIENT_ID') or '').strip():
        missing.append('MP_CLIENT_ID')
    if not str(config.get('MP_CLIENT_SECRET') or '').strip():
        missing.append('MP_CLIENT_SECRET')
    if not str(config.get('MP_REDIRECT_URI') or '').strip():
        missing.append('MP_REDIRECT_URI')
    if not str(config.get('PAYMENT_ENCRYPTION_KEY') or '').strip():
        missing.append('PAYMENT_ENCRYPTION_KEY')

    crypto_ready = is_payment_crypto_ready()
    if 'PAYMENT_ENCRYPTION_KEY' not in missing and not crypto_ready:
        missing.append('PAYMENT_ENCRYPTION_KEY inválida')

    oauth_ready = not any(item in missing for item in ('MP_CLIENT_ID', 'MP_CLIENT_SECRET', 'MP_REDIRECT_URI'))
    return PaymentEnvironmentStatus(oauth_ready=oauth_ready, crypto_ready=crypto_ready, missing=tuple(missing))


def get_payment_account(db, restaurant_id: int | None, provider: str = PROVIDER_MERCADO_PAGO):
    if not restaurant_id:
        return None

    return db.execute(
        '''
        SELECT *
          FROM restaurant_payment_accounts
         WHERE restaurant_id = ?
           AND provider = ?
         LIMIT 1
        ''',
        (restaurant_id, provider),
    ).fetchone()


def ensure_payment_account(db, restaurant_id: int | None, provider: str = PROVIDER_MERCADO_PAGO):
    if not restaurant_id:
        return None

    account = get_payment_account(db, restaurant_id, provider)
    if account:
        return account

    db.execute(
        '''
        INSERT INTO restaurant_payment_accounts (
            restaurant_id,
            provider,
            status,
            updated_at
        )
        VALUES (?, ?, 'not_connected', ?)
        ''',
        (restaurant_id, provider, _now_iso()),
    )
    db.commit()
    return get_payment_account(db, restaurant_id, provider)


def update_payment_account_status(
    db,
    restaurant_id: int | None,
    *,
    status: str,
    last_error: str = '',
    provider: str = PROVIDER_MERCADO_PAGO,
):
    if not restaurant_id:
        raise ValueError('Restaurante inválido para atualização de pagamento.')

    normalized_status = normalize_payment_account_status(status)
    ensure_payment_account(db, restaurant_id, provider)

    db.execute(
        '''
        UPDATE restaurant_payment_accounts
           SET status = ?,
               last_error = ?,
               updated_at = ?,
               connected_at = CASE WHEN ? = 'connected' THEN COALESCE(connected_at, ?) ELSE connected_at END
         WHERE restaurant_id = ?
           AND provider = ?
        ''',
        (normalized_status, str(last_error or ''), _now_iso(), normalized_status, _now_iso(), restaurant_id, provider),
    )
    db.commit()
    return get_payment_account(db, restaurant_id, provider)


def disable_payment_account(db, restaurant_id: int | None, provider: str = PROVIDER_MERCADO_PAGO):
    if not restaurant_id:
        raise ValueError('Restaurante inválido para desconectar pagamento.')

    ensure_payment_account(db, restaurant_id, provider)
    db.execute(
        '''
        UPDATE restaurant_payment_accounts
           SET status = 'disabled',
               provider_user_id = '',
               access_token_encrypted = '',
               refresh_token_encrypted = '',
               token_expires_at = NULL,
               public_key = '',
               last_error = '',
               updated_at = ?
         WHERE restaurant_id = ?
           AND provider = ?
        ''',
        (_now_iso(), restaurant_id, provider),
    )
    db.commit()
    return get_payment_account(db, restaurant_id, provider)


def payment_connection_summary(db, restaurant_profile) -> dict[str, Any]:
    restaurant_id = _row_get(restaurant_profile, 'id')
    service_mode = _row_get(restaurant_profile, 'service_mode', SERVICE_MODE_FULL_ORDER_PAYMENT)
    account = get_payment_account(db, restaurant_id)
    env = payment_environment_status()
    status = normalize_payment_account_status(_row_get(account, 'status', 'not_connected'))

    labels = {
        'not_connected': 'Não conectado',
        'connected': 'Conectado',
        'error': 'Com erro',
        'disabled': 'Desconectado',
    }

    descriptions = {
        'not_connected': 'A conta Mercado Pago ainda não foi conectada a este restaurante.',
        'connected': 'A conta Mercado Pago está marcada como conectada para este restaurante.',
        'error': _row_get(account, 'last_error', '') or 'A última tentativa de conexão retornou erro.',
        'disabled': 'A conexão Mercado Pago foi desativada para este restaurante.',
    }

    account_public = None
    if account:
        account_public = {
            'id': _row_get(account, 'id'),
            'restaurant_id': _row_get(account, 'restaurant_id'),
            'provider': _row_get(account, 'provider', PROVIDER_MERCADO_PAGO),
            'provider_user_id': _row_get(account, 'provider_user_id', ''),
            'status': status,
            'connected_at': _row_get(account, 'connected_at', ''),
            'updated_at': _row_get(account, 'updated_at', ''),
            'last_error': _row_get(account, 'last_error', ''),
        }

    return {
        'provider': PROVIDER_MERCADO_PAGO,
        'service_mode': service_mode,
        'is_full_order_mode': service_mode == SERVICE_MODE_FULL_ORDER_PAYMENT,
        'account': account_public,
        'status': status,
        'status_label': labels.get(status, 'Não conectado'),
        'description': descriptions.get(status, descriptions['not_connected']),
        'env_ready': env.ready,
        'oauth_ready': env.oauth_ready,
        'crypto_ready': env.crypto_ready,
        'missing_config': list(env.missing),
        'connected_at': _row_get(account, 'connected_at', ''),
        'updated_at': _row_get(account, 'updated_at', ''),
        'last_error': _row_get(account, 'last_error', ''),
    }
