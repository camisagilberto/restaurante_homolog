from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from flask import current_app

from .payment_crypto import encrypt_value, is_payment_crypto_ready

PROVIDER_MERCADO_PAGO = 'mercadopago'
PAYMENT_ACCOUNT_STATUSES = {'not_connected', 'connected', 'error', 'disabled'}
SERVICE_MODE_DIGITAL_MENU = 'digital_menu'
SERVICE_MODE_FULL_ORDER_PAYMENT = 'full_order_payment'

MP_AUTHORIZATION_URL = 'https://auth.mercadopago.com/authorization'
MP_OAUTH_TOKEN_URL = 'https://api.mercadopago.com/oauth/token'


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
    """Valida a configuração local necessária para iniciar o OAuth do Mercado Pago."""
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


def build_mercadopago_authorization_url(state: str) -> str:
    """Monta a URL oficial de autorização OAuth do Mercado Pago."""
    env = payment_environment_status()
    if not env.ready:
        missing = ', '.join(env.missing) or 'configurações de pagamento'
        raise RuntimeError(f'Configuração Mercado Pago incompleta: {missing}.')

    query = urlencode(
        {
            'client_id': current_app.config['MP_CLIENT_ID'],
            'response_type': 'code',
            'platform_id': 'mp',
            'state': state,
            'redirect_uri': current_app.config['MP_REDIRECT_URI'],
        }
    )
    return f'{MP_AUTHORIZATION_URL}?{query}'


def exchange_authorization_code_for_token(code: str) -> dict[str, Any]:
    """Troca o authorization code retornado pelo Mercado Pago por access token."""
    clean_code = str(code or '').strip()
    if not clean_code:
        raise RuntimeError('Código de autorização Mercado Pago não recebido.')

    payload = {
        'client_secret': current_app.config['MP_CLIENT_SECRET'],
        'client_id': current_app.config['MP_CLIENT_ID'],
        'grant_type': 'authorization_code',
        'code': clean_code,
        'redirect_uri': current_app.config['MP_REDIRECT_URI'],
    }

    try:
        response = requests.post(
            MP_OAUTH_TOKEN_URL,
            json=payload,
            headers={
                'accept': 'application/json',
                'content-type': 'application/json',
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RuntimeError('Não foi possível conectar ao Mercado Pago para finalizar a autorização.') from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError('O Mercado Pago retornou uma resposta inválida na autorização.') from exc

    if response.status_code >= 400:
        error = str(data.get('error') or 'erro_desconhecido')
        description = str(data.get('message') or data.get('error_description') or '').strip()
        detail = f'{error}: {description}' if description else error
        raise RuntimeError(f'Falha ao obter token Mercado Pago: {detail}')

    if not data.get('access_token'):
        raise RuntimeError('O Mercado Pago não retornou access_token na autorização.')

    return data


def _token_expiration_iso(token_response: dict[str, Any]) -> str | None:
    expires_in = token_response.get('expires_in')
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None

    if seconds <= 0:
        return None

    return (datetime.utcnow() + timedelta(seconds=seconds)).isoformat(timespec='seconds')


def save_mercadopago_token_response(db, restaurant_id: int | None, token_response: dict[str, Any]):
    """Criptografa e salva os tokens OAuth do restaurante."""
    if not restaurant_id:
        raise ValueError('Restaurante inválido para salvar conexão Mercado Pago.')

    access_token = str(token_response.get('access_token') or '').strip()
    if not access_token:
        raise RuntimeError('Access token Mercado Pago ausente.')

    refresh_token = str(token_response.get('refresh_token') or '').strip()
    provider_user_id = str(token_response.get('user_id') or '').strip()
    public_key = str(token_response.get('public_key') or '').strip()
    token_expires_at = _token_expiration_iso(token_response)

    ensure_payment_account(db, restaurant_id)
    db.execute(
        '''
        UPDATE restaurant_payment_accounts
           SET provider_user_id = ?,
               access_token_encrypted = ?,
               refresh_token_encrypted = ?,
               token_expires_at = ?,
               public_key = ?,
               status = 'connected',
               connected_at = COALESCE(connected_at, ?),
               updated_at = ?,
               last_error = ''
         WHERE restaurant_id = ?
           AND provider = ?
        ''',
        (
            provider_user_id,
            encrypt_value(access_token),
            encrypt_value(refresh_token),
            token_expires_at,
            public_key,
            _now_iso(),
            _now_iso(),
            restaurant_id,
            PROVIDER_MERCADO_PAGO,
        ),
    )
    db.commit()
    return get_payment_account(db, restaurant_id)


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
        'connected': 'A conta Mercado Pago está conectada a este restaurante.',
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
