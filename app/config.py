from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def _env_bool(name: str, default: str = '0') -> bool:
    value = str(os.getenv(name, default)).strip().lower()
    return value in {'1', 'true', 'yes', 'on'}


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-only-change-me')
    DATABASE = os.getenv('DATABASE_PATH', str(BASE_DIR / 'instance' / 'app.sqlite3'))

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', '0')
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024
    TEMPLATES_AUTO_RELOAD = True

    # URL pública do sistema. Será usada nos próximos blocos para montar callbacks,
    # webhooks e links enviados para provedores externos.
    BASE_URL = os.getenv('BASE_URL', '').rstrip('/')

    # Mercado Pago OAuth / Pix. Neste Bloco 2 essas variáveis são apenas lidas;
    # a conexão com Mercado Pago será implementada nos próximos blocos.
    MP_CLIENT_ID = os.getenv('MP_CLIENT_ID', '').strip()
    MP_CLIENT_SECRET = os.getenv('MP_CLIENT_SECRET', '').strip()
    MP_REDIRECT_URI = os.getenv('MP_REDIRECT_URI', '').strip()
    MP_WEBHOOK_SECRET = os.getenv('MP_WEBHOOK_SECRET', '').strip()

    # Chave usada para criptografar tokens sensíveis antes de salvar no banco.
    # Gere com: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    PAYMENT_ENCRYPTION_KEY = os.getenv('PAYMENT_ENCRYPTION_KEY', '').strip()

    @classmethod
    def mercado_pago_oauth_ready(cls) -> bool:
        """Retorna True quando o app já tem dados mínimos para iniciar OAuth."""
        return bool(cls.MP_CLIENT_ID and cls.MP_CLIENT_SECRET and cls.MP_REDIRECT_URI)

    @classmethod
    def payment_crypto_ready(cls) -> bool:
        """Retorna True quando há chave configurada para criptografar tokens."""
        return bool(cls.PAYMENT_ENCRYPTION_KEY)
