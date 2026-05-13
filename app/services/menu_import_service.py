from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

from ..errors import ValidationError
from ..utils import normalize_text, parse_price
from .catalog_service import validate_product_payload


PRICE_RE = re.compile(r'(?:R\$\\s*)?(\\d{1,4}(?:[.,]\\d{2}))')
HEADER_RE = re.compile(r'^[A-ZÀ-Ü0-9][A-ZÀ-Ü0-9\\s&/\\-]{2,}$')


@dataclass(slots=True)
class ImportedMenuItem:
    name: str
    category: str
    price: float
    description: str | None = None
    active: int = 1
    sort_order: int = 0


def _prepare_image(file_storage) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(file_storage.read()))
    except Exception as exc:
        raise ValidationError(f'Não foi possível ler a imagem "{getattr(file_storage, "filename", "arquivo")}".') from exc

    image = ImageOps.exif_transpose(image).convert('L')
    image = ImageEnhance.Contrast(image).enhance(2.0)
    image = image.filter(ImageFilter.SHARPEN)
    return image


def extract_text_from_upload(file_storage) -> str:
    image = _prepare_image(file_storage)
    try:
        return pytesseract.image_to_string(image, lang='por+eng')
    except pytesseract.TesseractNotFoundError as exc:
        raise ValidationError(
            'O mecanismo de OCR não está disponível no ambiente. Instale o Tesseract no deploy.'
        ) from exc
    except Exception as exc:
        raise ValidationError('Não foi possível ler o cardápio enviado.') from exc


def _clean_line(line: str) -> str:
    line = normalize_text(line)
    line = line.replace(' | ', ' ')
    line = line.replace('•', ' ')
    line = line.replace('–', '-')
    return normalize_text(line)


def parse_menu_text(text: str, *, default_category: str = 'Cardápio') -> list[ImportedMenuItem]:
    items: list[ImportedMenuItem] = []
    current_category = default_category
    seen: set[tuple[str, str, float]] = set()

    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue

        if HEADER_RE.match(line) and not PRICE_RE.search(line) and len(line.split()) <= 6:
            current_category = line.title()
            continue

        matches = list(PRICE_RE.finditer(line))
        if not matches:
            continue

        price_match = matches[-1]
        name = normalize_text(line[:price_match.start()].strip(' -:;,.'))
        if not name or len(name) < 2:
            continue

        try:
            price = parse_price(price_match.group(1))
        except ValueError:
            continue

        key = (name.lower(), current_category.lower(), price)
        if key in seen:
            continue
        seen.add(key)

        items.append(
            ImportedMenuItem(
                name=name[:80],
                category=current_category[:50],
                price=price,
                description=None,
                sort_order=len(items),
            )
        )

    return items


def _product_exists(db, name: str, category: str, price: float) -> bool:
    row = db.execute(
        '''
        SELECT 1
          FROM products
         WHERE lower(name) = lower(?)
           AND lower(category) = lower(?)
           AND abs(price - ?) < 0.01
         LIMIT 1
        ''',
        (name, category, price),
    ).fetchone()
    return row is not None


def import_menu_uploads(db, uploads: list[Any], *, default_category: str = 'Cardápio') -> dict[str, Any]:
    if not uploads:
        raise ValidationError('Envie pelo menos uma imagem do cardápio.')

    extracted_blocks: list[str] = []
    parsed_items: list[ImportedMenuItem] = []

    for upload in uploads:
        filename = getattr(upload, 'filename', '') or ''
        if not filename:
            continue
        text = extract_text_from_upload(upload)
        if text.strip():
            extracted_blocks.append(text)
            parsed_items.extend(parse_menu_text(text, default_category=default_category))

    if not extracted_blocks:
        raise ValidationError('Não foi possível ler nenhuma imagem do cardápio.')

    created = 0
    skipped = 0
    for item in parsed_items:
        if _product_exists(db, item.name, item.category, item.price):
            skipped += 1
            continue

        payload = {
            'name': item.name,
            'category': item.category or default_category,
            'price': f'{item.price:.2f}',
            'description': item.description or '',
            'active': item.active,
            'sort_order': item.sort_order,
        }
        data = validate_product_payload(payload)
        db.execute(
            'INSERT INTO products (name, description, price, category, active, sort_order) VALUES (?, ?, ?, ?, ?, ?)',
            (data['name'], data['description'], data['price'], data['category'], data['active'], data['sort_order']),
        )
        created += 1

    db.commit()

    return {
        'created': created,
        'skipped': skipped,
        'detected': len(parsed_items),
        'blocks': extracted_blocks,
    }
