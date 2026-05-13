from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

from ..errors import ValidationError
from ..utils import normalize_text, parse_price

PRICE_RE = re.compile(r'(?:R\$\s*)?(\d{1,4}(?:[.,]\d{2}))')
HEADER_RE = re.compile(r'^[A-ZÀ-Ü0-9][A-ZÀ-Ü0-9\s&/\-]{2,}$')
NOISE_RE = re.compile(r'^[\W_]+$')
RESAMPLING = getattr(Image, 'Resampling', Image)


@dataclass(slots=True)
class ImportedMenuItem:
    name: str
    category: str
    price: float
    description: str | None = None
    active: int = 1
    sort_order: int = 0


def _filename(file_storage: Any) -> str:
    return str(getattr(file_storage, 'filename', '') or 'arquivo')


def _read_bytes(file_storage: Any) -> bytes:
    if hasattr(file_storage, 'stream') and hasattr(file_storage.stream, 'seek'):
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass

    if hasattr(file_storage, 'read'):
        data = file_storage.read()
        try:
            file_storage.seek(0)
        except Exception:
            pass
        return data

    raise ValidationError(f'Não foi possível ler o arquivo "{_filename(file_storage)}".')


def _prepare_image(file_storage: Any) -> Image.Image:
    raw = _read_bytes(file_storage)
    if not raw:
        raise ValidationError(f'O arquivo "{_filename(file_storage)}" está vazio.')

    try:
        image = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise ValidationError(f'Não foi possível abrir a imagem "{_filename(file_storage)}".') from exc

    image = ImageOps.exif_transpose(image).convert('L')
    image = ImageOps.autocontrast(image)

    width, height = image.size
    target = max(width, height, 1600)
    if max(width, height) < target:
        if width >= height:
            new_width = target
            new_height = max(1, round(height * (target / width)))
        else:
            new_height = target
            new_width = max(1, round(width * (target / height)))
        image = image.resize((new_width, new_height), RESAMPLING.LANCZOS)

    image = ImageEnhance.Contrast(image).enhance(1.8)
    image = image.filter(ImageFilter.SHARPEN)
    return image


def _ocr_text(image: Image.Image) -> str:
    outputs: list[str] = []
    for psm in (6, 11):
        try:
            text = pytesseract.image_to_string(image, lang='por+eng', config=f'--oem 3 --psm {psm}')
        except pytesseract.TesseractNotFoundError as exc:
            raise ValidationError(
                'O mecanismo de OCR não está disponível no ambiente. '
                'Verifique se o Tesseract foi instalado no Railway.'
            ) from exc
        except Exception:
            continue

        text = normalize_text(text)
        if text:
            outputs.append(text)

    if not outputs:
        return ''

    merged_lines: list[str] = []
    seen: set[str] = set()
    for text in outputs:
        for raw_line in text.splitlines():
            line = normalize_text(raw_line.replace('•', ' ').replace('–', '-').replace('—', '-'))
            if not line or NOISE_RE.fullmatch(line):
                continue
            if line not in seen:
                seen.add(line)
                merged_lines.append(line)
    return '\n'.join(merged_lines)


def extract_text_from_upload(file_storage: Any) -> str:
    image = _prepare_image(file_storage)
    return _ocr_text(image)


def _is_category_line(line: str) -> bool:
    if not line or PRICE_RE.search(line):
        return False
    if len(line) < 3 or len(line) > 45:
        return False
    if sum(ch.isalpha() for ch in line) < 3:
        return False
    if line.endswith(':'):
        line = line[:-1]
    return bool(HEADER_RE.match(line)) and line == line.upper()


def _clean_line(line: str) -> str:
    line = normalize_text(line)
    line = line.replace('•', ' ').replace('–', '-').replace('—', '-')
    return normalize_text(line)


def _add_item(
    items: list[ImportedMenuItem],
    seen: set[tuple[str, str, float]],
    *,
    name: str,
    category: str,
    price_text: str,
    sort_order: int,
) -> None:
    name = normalize_text(name).strip(' -:;,.')
    category = normalize_text(category) or 'Cardápio'
    if not name or len(name) < 2:
        return

    try:
        price = parse_price(price_text)
    except ValueError:
        return

    key = (name.lower(), category.lower(), price)
    if key in seen:
        return

    seen.add(key)
    items.append(
        ImportedMenuItem(
            name=name[:80],
            category=category[:50],
            price=price,
            description=None,
            active=1,
            sort_order=sort_order,
        )
    )


def parse_menu_text(text: str, *, default_category: str = 'Cardápio') -> list[ImportedMenuItem]:
    lines = [_clean_line(raw_line) for raw_line in text.splitlines()]
    lines = [line for line in lines if line and not NOISE_RE.fullmatch(line)]

    items: list[ImportedMenuItem] = []
    seen: set[tuple[str, str, float]] = set()
    current_category = default_category

    i = 0
    while i < len(lines):
        line = lines[i]

        if _is_category_line(line):
            current_category = line.rstrip(':').title()
            i += 1
            continue

        direct_price = PRICE_RE.fullmatch(line)
        if direct_price:
            if i > 0:
                previous = lines[i - 1]
                if previous and not PRICE_RE.search(previous) and not _is_category_line(previous):
                    _add_item(
                        items,
                        seen,
                        name=previous,
                        category=current_category,
                        price_text=direct_price.group(1),
                        sort_order=len(items),
                    )
            i += 1
            continue

        price_match = PRICE_RE.search(line)
        if price_match:
            _add_item(
                items,
                seen,
                name=line[:price_match.start()],
                category=current_category,
                price_text=price_match.group(1),
                sort_order=len(items),
            )
            i += 1
            continue

        if i + 1 < len(lines) and PRICE_RE.fullmatch(lines[i + 1]):
            _add_item(
                items,
                seen,
                name=line,
                category=current_category,
                price_text=PRICE_RE.fullmatch(lines[i + 1]).group(1),
                sort_order=len(items),
            )
            i += 2
            continue

        if i + 2 < len(lines) and PRICE_RE.fullmatch(lines[i + 2]) and not PRICE_RE.search(lines[i + 1]):
            merged_name = normalize_text(f'{line} {lines[i + 1]}')
            _add_item(
                items,
                seen,
                name=merged_name,
                category=current_category,
                price_text=PRICE_RE.fullmatch(lines[i + 2]).group(1),
                sort_order=len(items),
            )
            i += 3
            continue

        i += 1

    return items


def import_menu_uploads(uploads: list[Any], *, default_category: str = 'Cardápio') -> dict[str, Any]:
    if not uploads:
        raise ValidationError('Envie pelo menos uma imagem do cardápio.')

    extracted_texts: list[str] = []
    processed_files = 0
    failures: list[str] = []

    for upload in uploads:
        filename = _filename(upload)
        if not filename or filename == 'arquivo':
            continue

        processed_files += 1
        try:
            text = extract_text_from_upload(upload)
        except ValidationError as exc:
            failures.append(str(exc))
            continue

        if text:
            extracted_texts.append(text)

    if not processed_files:
        raise ValidationError('Envie pelo menos uma imagem do cardápio.')

    if not extracted_texts:
        if failures:
            raise ValidationError('Não foi possível ler as imagens enviadas. ' + ' '.join(failures[:2]))
        raise ValidationError('Não foi possível ler texto suficiente nas imagens enviadas.')

    merged_text = '\n'.join(extracted_texts)
    items = parse_menu_text(merged_text, default_category=default_category)

    return {
        'items': [
            {
                'name': item.name,
                'category': item.category,
                'price': item.price,
                'description': item.description,
                'active': item.active,
                'sort_order': item.sort_order,
            }
            for item in items
        ],
        'processed_files': processed_files,
        'recognized_items': len(items),
        'failures': failures,
        'extracted_text': merged_text,
    }
