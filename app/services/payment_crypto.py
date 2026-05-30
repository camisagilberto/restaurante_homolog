from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class PaymentCryptoError(RuntimeError):
    """Erro controlado de criptografia de dados de pagamento."""


def generate_payment_encryption_key() -> str:
    """Gera uma chave válida para PAYMENT_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode('utf-8')


def _configured_key() -> str:
    key = str(current_app.config.get('PAYMENT_ENCRYPTION_KEY') or '').strip()

    if not key:
        raise PaymentCryptoError(
            'PAYMENT_ENCRYPTION_KEY não configurada. Configure a variável de ambiente antes de conectar pagamentos.'
        )

    return key


def _fernet() -> Fernet:
    key = _configured_key()

    try:
        return Fernet(key.encode('utf-8'))
    except Exception as exc:
        raise PaymentCryptoError(
            'PAYMENT_ENCRYPTION_KEY inválida. Gere uma chave Fernet válida antes de conectar pagamentos.'
        ) from exc


def is_payment_crypto_ready() -> bool:
    """Verifica se a criptografia está pronta sem derrubar o site."""
    try:
        _fernet()
        return True
    except PaymentCryptoError:
        return False


def encrypt_value(value: Any) -> str:
    """Criptografa texto sensível para armazenamento no banco."""
    text = str(value or '')

    if not text:
        return ''

    return _fernet().encrypt(text.encode('utf-8')).decode('utf-8')


def decrypt_value(value: Any) -> str:
    """Descriptografa texto sensível salvo no banco."""
    token = str(value or '')

    if not token:
        return ''

    try:
        return _fernet().decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken as exc:
        raise PaymentCryptoError('Não foi possível descriptografar o valor informado.') from exc
