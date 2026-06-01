from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from flask import current_app

from .payment_crypto import decrypt_value, encrypt_value, is_payment_crypto_ready

PROVIDER_MERCADO_PAGO = 'mercadopago'
PAYMENT_ACCOUNT_STATUSES = {'not_connected', 'connected', 'error', 'disabled'}
SERVICE_MODE_DIGITAL_MENU = 'digital_menu'
SERVICE_MODE_FULL_ORDER_PAYMENT = 'full_order_payment'

MP_AUTHORIZATION_URL = 'https://auth.mercadopago.com/authorization'
MP_OAUTH_TOKEN_URL = 'https://api.mercadopago.com/oauth/token'
MP_PAYMENT_URL = 'https://api.mercadopago.com/v1/payments'


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


def _webhook_notification_url() -> str:
    base_url = str(current_app.config.get('BASE_URL') or '').strip().rstrip('/')
    if not base_url:
        return ''
    return f'{base_url}/pagamentos/mercadopago/webhook'


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




def humanize_payment_error(error_message: Any) -> str:
    """Converte erros técnicos do Mercado Pago em mensagens úteis para cliente/restaurante."""
    raw = str(error_message or '').strip()
    lowered = raw.lower()

    if not raw:
        return 'Não foi possível concluir a operação de pagamento agora. Tente novamente ou fale com o restaurante.'

    if 'collector user without key enabled for qr render' in lowered:
        return (
            'Não foi possível gerar o Pix porque a conta Mercado Pago do restaurante ainda não está pronta para receber Pix. '
            'O responsável precisa cadastrar e ativar uma chave Pix na conta Mercado Pago conectada.'
        )

    if 'mercado pago não conectado' in lowered or 'mercado pago nao conectado' in lowered:
        return 'Este restaurante ainda não conectou a conta Mercado Pago. Avise o responsável antes de finalizar o pedido.'

    if 'token mercado pago ausente' in lowered or 'reconecte a conta' in lowered or 'invalid_token' in lowered or 'unauthorized' in lowered:
        return 'A conexão Mercado Pago do restaurante precisa ser refeita. Avise o responsável pelo restaurante.'

    if 'não foi possível conectar ao mercado pago' in lowered or 'nao foi possivel conectar ao mercado pago' in lowered:
        return 'Não foi possível conectar ao Mercado Pago agora. Tente novamente em alguns instantes.'

    if 'valor do pedido inválido' in lowered or 'valor maior que zero' in lowered:
        return 'Não foi possível gerar o Pix porque o valor do pedido é inválido. Revise o carrinho.'

    if raw.startswith('Falha ao gerar Pix no Mercado Pago:') or raw.startswith('Falha ao consultar pagamento no Mercado Pago:'):
        return 'O Mercado Pago não conseguiu processar esta solicitação agora. Tente novamente ou fale com o restaurante.'

    # Mantém mensagens já amigáveis criadas pelo próprio QRTotem.
    return raw


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



def get_connected_mercadopago_access_token(db, restaurant_id: int | None) -> str:
    """Retorna o access_token descriptografado da conta Mercado Pago conectada."""
    account = get_payment_account(db, restaurant_id)

    if not account or normalize_payment_account_status(_row_get(account, 'status')) != 'connected':
        raise RuntimeError('Mercado Pago não conectado para este restaurante.')

    encrypted_token = _row_get(account, 'access_token_encrypted', '')
    if not encrypted_token:
        raise RuntimeError('Token Mercado Pago ausente. Reconecte a conta do restaurante.')

    return decrypt_value(encrypted_token)


def create_pix_payment_for_order(
    db,
    *,
    restaurant_id: int,
    order_id: int,
    amount: float,
    customer_name: str,
    customer_email: str,
    external_reference: str,
) -> dict[str, Any]:
    """Cria uma cobrança Pix no Mercado Pago usando a conta conectada do restaurante."""
    try:
        transaction_amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise RuntimeError('Valor do pedido inválido para gerar Pix.')

    if transaction_amount <= 0:
        raise RuntimeError('O pedido precisa ter valor maior que zero para gerar Pix.')

    clean_email = str(customer_email or '').strip().lower()
    if '@' not in clean_email or '.' not in clean_email.split('@')[-1]:
        raise RuntimeError('E-mail técnico inválido para gerar o Pix.')

    access_token = get_connected_mercadopago_access_token(db, restaurant_id)

    payer_name = str(customer_name or 'Cliente').strip() or 'Cliente'
    payload: dict[str, Any] = {
        'transaction_amount': transaction_amount,
        'description': f'QRTotem - Pedido #{order_id}',
        'payment_method_id': 'pix',
        'external_reference': external_reference,
        'payer': {
            'email': clean_email,
            'first_name': payer_name[:60],
        },
    }

    notification_url = _webhook_notification_url()
    if notification_url:
        payload['notification_url'] = notification_url

    try:
        response = requests.post(
            MP_PAYMENT_URL,
            json=payload,
            headers={
                'accept': 'application/json',
                'content-type': 'application/json',
                'Authorization': f'Bearer {access_token}',
                'X-Idempotency-Key': f'qrtotem-order-{restaurant_id}-{order_id}',
            },
            timeout=25,
        )
    except requests.RequestException as exc:
        raise RuntimeError('Não foi possível conectar ao Mercado Pago para gerar o Pix.') from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError('O Mercado Pago retornou uma resposta inválida ao gerar o Pix.') from exc

    if response.status_code >= 400:
        error = str(data.get('error') or data.get('status') or 'erro_desconhecido')
        cause = data.get('cause')
        if isinstance(cause, list) and cause:
            cause_messages = []
            for item in cause[:3]:
                if isinstance(item, dict):
                    cause_messages.append(str(item.get('description') or item.get('message') or item.get('code') or '').strip())
                else:
                    cause_messages.append(str(item).strip())
            cause_text = '; '.join(text for text in cause_messages if text)
        else:
            cause_text = str(cause or '').strip()
        description = str(data.get('message') or data.get('error_description') or cause_text or '').strip()
        detail = f'{error}: {description}' if description else error
        raise RuntimeError(humanize_payment_error(f'Falha ao gerar Pix no Mercado Pago: {detail}'))

    payment_id = str(data.get('id') or '').strip()
    transaction_data = (data.get('point_of_interaction') or {}).get('transaction_data') or {}
    qr_code = str(transaction_data.get('qr_code') or '').strip()
    qr_code_base64 = str(transaction_data.get('qr_code_base64') or '').strip()
    ticket_url = str(transaction_data.get('ticket_url') or '').strip()

    if not payment_id or not qr_code:
        raise RuntimeError('O Mercado Pago não retornou os dados Pix necessários.')

    return {
        'id': payment_id,
        'status': str(data.get('status') or 'pending'),
        'external_reference': str(data.get('external_reference') or external_reference),
        'qr_code': qr_code,
        'qr_code_base64': qr_code_base64,
        'ticket_url': ticket_url,
        'raw': data,
    }


def fetch_mercadopago_payment_status(db, *, restaurant_id: int, payment_external_id: str) -> dict[str, Any]:
    """Consulta o pagamento no Mercado Pago usando a conta conectada do restaurante."""
    payment_id = str(payment_external_id or '').strip()
    if not payment_id:
        raise RuntimeError('Pagamento Mercado Pago não encontrado para este pedido.')

    access_token = get_connected_mercadopago_access_token(db, restaurant_id)

    try:
        response = requests.get(
            f'{MP_PAYMENT_URL}/{payment_id}',
            headers={
                'accept': 'application/json',
                'Authorization': f'Bearer {access_token}',
            },
            timeout=25,
        )
    except requests.RequestException as exc:
        raise RuntimeError('Não foi possível consultar o Mercado Pago agora. Tente novamente em alguns segundos.') from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError('O Mercado Pago retornou uma resposta inválida ao consultar o pagamento.') from exc

    if response.status_code >= 400:
        error = str(data.get('error') or data.get('status') or 'erro_desconhecido')
        cause = data.get('cause')
        if isinstance(cause, list) and cause:
            cause_messages = []
            for item in cause[:3]:
                if isinstance(item, dict):
                    cause_messages.append(str(item.get('description') or item.get('message') or item.get('code') or '').strip())
                else:
                    cause_messages.append(str(item).strip())
            cause_text = '; '.join(text for text in cause_messages if text)
        else:
            cause_text = str(cause or '').strip()
        description = str(data.get('message') or data.get('error_description') or cause_text or '').strip()
        detail = f'{error}: {description}' if description else error
        raise RuntimeError(humanize_payment_error(f'Falha ao consultar pagamento no Mercado Pago: {detail}'))

    mp_status = str(data.get('status') or '').strip().lower()
    status_detail = str(data.get('status_detail') or '').strip()

    if mp_status == 'approved':
        normalized_status = 'approved'
    elif mp_status in {'rejected'}:
        normalized_status = 'rejected'
    elif mp_status in {'cancelled', 'canceled'}:
        normalized_status = 'cancelled'
    elif mp_status in {'expired'}:
        normalized_status = 'expired'
    elif mp_status in {'pending', 'in_process', 'authorized'}:
        normalized_status = 'pending'
    else:
        normalized_status = 'pending'

    return {
        'id': str(data.get('id') or payment_id),
        'provider_status': mp_status or 'unknown',
        'status': normalized_status,
        'status_detail': status_detail,
        'approved_at': str(data.get('date_approved') or ''),
        'external_reference': str(data.get('external_reference') or ''),
        'raw': data,
    }

