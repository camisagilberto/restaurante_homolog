from __future__ import annotations

import base64
import io
from typing import Any

import qrcode
from PIL import Image, ImageDraw, ImageFont

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


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = []

    if bold:
        candidates.extend(
            [
                'DejaVuSans-Bold.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
                'arialbd.ttf',
            ]
        )
    else:
        candidates.extend(
            [
                'DejaVuSans.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/usr/share/fonts/dejavu/DejaVuSans.ttf',
                'arial.ttf',
            ]
        )

    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue

    return ImageFont.load_default()


def _fit_brand_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int) -> ImageFont.ImageFont:
    size = max(14, start_size)

    while size >= 14:
        font = _load_font(size, bold=True)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        if text_width <= max_width:
            return font
        size -= 1

    return _load_font(14, bold=True)


def build_qr_code_data_uri(url: str, table_number: int | str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    qr_image = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    qr_width, qr_height = qr_image.size

    outer_padding = 18
    footer_height = 72
    canvas_width = qr_width + (outer_padding * 2)
    canvas_height = qr_height + (outer_padding * 2) + footer_height

    canvas = Image.new('RGB', (canvas_width, canvas_height), 'white')
    qr_x = outer_padding
    qr_y = outer_padding
    canvas.paste(qr_image, (qr_x, qr_y))

    draw = ImageDraw.Draw(canvas)

    brand_text = 'QR Totem'
    mesa_text = f'Mesa {table_number}'

    max_brand_width = int(qr_width * 0.40)
    brand_font = _fit_brand_font(draw, brand_text, max_brand_width, max(18, qr_width // 10))
    mesa_font = _load_font(max(18, qr_width // 16), bold=True)

    brand_bbox = draw.textbbox((0, 0), brand_text, font=brand_font)
    brand_text_width = brand_bbox[2] - brand_bbox[0]
    brand_text_height = brand_bbox[3] - brand_bbox[1]

    brand_pad_x = 18
    brand_pad_y = 10
    brand_box_width = brand_text_width + (brand_pad_x * 2)
    brand_box_height = brand_text_height + (brand_pad_y * 2)

    brand_box_x1 = qr_x + (qr_width - brand_box_width) // 2
    brand_box_y1 = qr_y + (qr_height - brand_box_height) // 2
    brand_box_x2 = brand_box_x1 + brand_box_width
    brand_box_y2 = brand_box_y1 + brand_box_height

    draw.rounded_rectangle(
        (brand_box_x1, brand_box_y1, brand_box_x2, brand_box_y2),
        radius=18,
        fill='white',
        outline='black',
        width=2,
    )

    brand_text_x = brand_box_x1 + (brand_box_width - brand_text_width) // 2
    brand_text_y = brand_box_y1 + (brand_box_height - brand_text_height) // 2 - 1

    draw.text(
        (brand_text_x, brand_text_y),
        brand_text,
        fill='black',
        font=brand_font,
    )

    mesa_bbox = draw.textbbox((0, 0), mesa_text, font=mesa_font)
    mesa_text_width = mesa_bbox[2] - mesa_bbox[0]
    mesa_text_height = mesa_bbox[3] - mesa_bbox[1]

    mesa_text_x = canvas_width - outer_padding - mesa_text_width
    mesa_text_y = qr_y + qr_height + ((footer_height - mesa_text_height) // 2)

    draw.text(
        (mesa_text_x, mesa_text_y),
        mesa_text,
        fill='black',
        font=mesa_font,
    )

    buffer = io.BytesIO()
    canvas.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
    return f'data:image/png;base64,{encoded}'
