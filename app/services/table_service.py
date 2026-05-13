from __future__ import annotations

import base64
import io
from typing import Any

import qrcode

from ..errors import ValidationError


def parse_table_count(value: Any) -> int:
    try:
        table_count = int(str(value or '').strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError('Informe uma quantidade válida de mesas.') from exc

    if table_count < 1:
        raise ValidationError('Informe pelo menos 1 mesa.')

    if table_count > 300:
        raise ValidationError('Informe no máximo 300 mesas por vez.')

    return table_count


def save_table_count(db, admin_id: int | None, table_count: int) -> None:
    if not admin_id:
        raise ValidationError('Sessão inválida. Faça login novamente.')

    db.execute(
        '''
        UPDATE restaurant_profiles
           SET table_count = ?
         WHERE admin_id = ?
        ''',
        (table_count, admin_id),
    )
    db.commit()


def build_qr_code_data_uri(url: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)

    image = qr.make_image(fill_color='black', back_color='white')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
    return f'data:image/png;base64,{encoded}'
