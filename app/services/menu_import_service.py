from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

from ..errors import ValidationError
from ..utils import normalize_text, parse_price

PRICE_RE = re.compile(
    r'(?:(?:R\$|RS|R\s*\$)\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,4}[,.]\d{2}|\d{1,4}\s*[,.]\s*\d{2})',
    re.IGNORECASE,
)
PRICE_ONLY_RE = re.compile(
    r'^\s*(?:(?:R\$|RS|R\s*\$)\s*)?(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,4}[,.]\d{2}|\d{1,4}\s*[,.]\s*\d{2})\s*$',
    re.IGNORECASE,
)
NOISE_RE = re.compile(r'^[\W_]+$')

CATEGORY_KEYWORDS = {
    'hamburguer', 'hamburgueres', 'burger', 'burgers', 'lanche', 'lanches',
    'pizza', 'pizzas', 'bebida', 'bebidas', 'suco', 'sucos', 'sobremesa',
    'sobremesas', 'porcao', 'porcoes', 'entrada', 'entradas', 'combo', 'combos',
    'prato', 'pratos', 'massa', 'massas', 'salada', 'saladas', 'cafe', 'cafes',
    'drink', 'drinks', 'cerveja', 'cervejas', 'vinho', 'vinhos', 'executivo',
    'promocao', 'promocoes', 'especial', 'especiais', 'adicional', 'adicionais',
}

STOP_WORDS = {
    'cardapio', 'menu', 'delivery', 'telefone', 'whatsapp', 'instagram', 'facebook',
    'endereco', 'rua', 'avenida', 'aberto', 'funcionamento', 'pedido', 'pedidos',
    'taxa', 'entrega', 'consulte', 'imagem', 'foto', 'page', 'pagina', 'qr',
}

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


def _normalize_ocr_text(text: str) -> str:
    replacements = {
        '—': '-',
        '–': '-',
        '•': ' ',
        '·': ' ',
        '|': ' ',
        'R§': 'R$',
        'RS ': 'R$ ',
        'R5': 'R$',
        'S$': 'R$',
        'O,': '0,',
        'o,': '0,',
        'l,': '1,',
        'I,': '1,',
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def _prepare_base_image(file_storage: Any) -> Image.Image:
    raw = _read_bytes(file_storage)

    if not raw:
        raise ValidationError(f'O arquivo "{_filename(file_storage)}" está vazio.')

    try:
        image = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise ValidationError(f'Não foi possível abrir a imagem "{_filename(file_storage)}".') from exc

    image = ImageOps.exif_transpose(image).convert('L')

    width, height = image.size
    largest_side = max(width, height)

    if largest_side < 1800:
        scale = 1800 / largest_side
        image = image.resize((round(width * scale), round(height * scale)), RESAMPLING.LANCZOS)
    elif largest_side > 3200:
        scale = 3200 / largest_side
        image = image.resize((round(width * scale), round(height * scale)), RESAMPLING.LANCZOS)

    return image


def _image_variants(image: Image.Image) -> list[Image.Image]:
    variants: list[Image.Image] = []

    auto = ImageOps.autocontrast(image)
    variants.append(auto)

    high_contrast = ImageEnhance.Contrast(auto).enhance(2.2).filter(ImageFilter.SHARPEN)
    variants.append(high_contrast)

    threshold = high_contrast.point(lambda pixel: 255 if pixel > 165 else 0)
    variants.append(threshold)

    inverted = ImageOps.invert(auto)
    variants.append(ImageEnhance.Contrast(inverted).enhance(1.8).filter(ImageFilter.SHARPEN))

    return variants


def _run_tesseract(image: Image.Image) -> str:
    texts: list[str] = []

    configs = [
        '--oem 3 --psm 6',
        '--oem 3 --psm 4',
        '--oem 3 --psm 11',
        '--oem 3 --psm 3',
    ]

    for variant in _image_variants(image):
        for config in configs:
            try:
                text = pytesseract.image_to_string(variant, lang='por+eng', config=config)
            except pytesseract.TesseractNotFoundError as exc:
                raise ValidationError(
                    'O mecanismo de OCR não está disponível no ambiente. '
                    'Verifique se o Tesseract foi instalado pelo Dockerfile no Railway.'
                ) from exc
            except Exception:
                continue

            text = normalize_text(_normalize_ocr_text(text))

            if text:
                texts.append(text)

    return '\n'.join(texts)


def extract_text_from_upload(file_storage: Any) -> str:
    image = _prepare_base_image(file_storage)
    text = _run_tesseract(image)
    return _dedupe_lines(text)


def _dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []

    for raw_line in text.splitlines():
        line = _clean_line(raw_line)

        if not line:
            continue

        key = line.lower()

        if key in seen:
            continue

        seen.add(key)
        lines.append(line)

    return '\n'.join(lines)


def _strip_accents_for_compare(text: str) -> str:
    table = str.maketrans(
        'áàãâäéèêëíìîïóòõôöúùûüçÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇ',
        'aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC',
    )
    return text.translate(table).lower()


def _clean_line(line: str) -> str:
    line = _normalize_ocr_text(str(line or ''))
    line = re.sub(r'\s+', ' ', line).strip()
    line = line.strip(' _*~`"\'')
    return normalize_text(line)


def _clean_name(name: str) -> str:
    name = _clean_line(name)
    name = re.sub(r'^[\-–—•·\d\.)\s]+', '', name)
    name = re.sub(r'\s{2,}', ' ', name)
    name = name.strip(' -:;,.')
    name = re.sub(r'(?i)\bR\$\b$', '', name).strip()
    return name


def _looks_like_category(line: str) -> bool:
    line = _clean_line(line).rstrip(':')

    if not line or PRICE_RE.search(line):
        return False

    if len(line) < 3 or len(line) > 45:
        return False

    compare = _strip_accents_for_compare(line)
    words = [word for word in re.findall(r'[a-zA-ZÀ-ÿ]+', compare)]

    if not words:
        return False

    if any(word in CATEGORY_KEYWORDS for word in words):
        return True

    alpha = [ch for ch in line if ch.isalpha()]

    if len(alpha) >= 3:
        uppercase_ratio = sum(1 for ch in alpha if ch.isupper()) / len(alpha)
        if uppercase_ratio > 0.75 and len(words) <= 5:
            return True

    return False


def _looks_like_noise_or_footer(line: str) -> bool:
    compare = _strip_accents_for_compare(line)

    if NOISE_RE.fullmatch(line):
        return True

    if len(line) <= 1:
        return True

    if any(word in compare for word in STOP_WORDS) and not PRICE_RE.search(line):
        return True

    return False


def _price_to_float(price_text: str) -> float | None:
    cleaned = normalize_text(price_text).replace(' ', '')

    try:
        return parse_price(cleaned)
    except ValueError:
        return None


def _add_item(
    items: list[ImportedMenuItem],
    seen: set[tuple[str, str, float]],
    *,
    name: str,
    category: str,
    price_text: str,
    description: str | None = None,
) -> None:
    name = _clean_name(name)
    category = _clean_line(category).rstrip(':') or 'Cardápio'

    if not name or len(name) < 2 or len(name) > 90:
        return

    if _looks_like_noise_or_footer(name):
        return

    price = _price_to_float(price_text)

    if price is None or price <= 0 or price > 9999:
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
            description=normalize_text(description)[:180] if description else None,
            active=1,
            sort_order=len(items),
        )
    )


def _parse_same_line(
    line: str,
    category: str,
    items: list[ImportedMenuItem],
    seen: set[tuple[str, str, float]],
) -> bool:
    matches = list(PRICE_RE.finditer(line))

    if not matches:
        return False

    price_match = matches[-1]
    name = line[:price_match.start()]

    if not _clean_name(name):
        after = line[price_match.end():]

        if _clean_name(after):
            name = after

    if not _clean_name(name):
        return False

    _add_item(
        items,
        seen,
        name=name,
        category=category,
        price_text=price_match.group(1),
    )

    return True


def _parse_column_like_lines(
    lines: list[str],
    category: str,
    items: list[ImportedMenuItem],
    seen: set[tuple[str, str, float]],
) -> bool:
    added = False

    for index, line in enumerate(lines):
        price_only = PRICE_ONLY_RE.match(line)

        if not price_only:
            continue

        candidates: list[str] = []

        for offset in (1, 2):
            if index - offset >= 0:
                candidates.append(lines[index - offset])

        for offset in (1,):
            if index + offset < len(lines):
                candidates.append(lines[index + offset])

        for candidate in candidates:
            if PRICE_RE.search(candidate) or _looks_like_category(candidate):
                continue

            before = len(items)

            _add_item(
                items,
                seen,
                name=candidate,
                category=category,
                price_text=price_only.group(1),
            )

            if len(items) > before:
                added = True
                break

    return added


def parse_menu_text(text: str, *, default_category: str = 'Cardápio') -> list[ImportedMenuItem]:
    lines = [_clean_line(raw_line) for raw_line in text.splitlines()]
    lines = [line for line in lines if line and not _looks_like_noise_or_footer(line)]

    items: list[ImportedMenuItem] = []
    seen: set[tuple[str, str, float]] = set()
    current_category = default_category

    index = 0

    while index < len(lines):
        line = lines[index]

        if _looks_like_category(line):
            current_category = line.rstrip(':').title()
            index += 1
            continue

        if _parse_same_line(line, current_category, items, seen):
            index += 1
            continue

        if index + 1 < len(lines):
            price_next = PRICE_ONLY_RE.match(lines[index + 1])

            if price_next and not PRICE_RE.search(line):
                _add_item(
                    items,
                    seen,
                    name=line,
                    category=current_category,
                    price_text=price_next.group(1),
                )
                index += 2
                continue

        if index + 2 < len(lines):
            price_after_description = PRICE_ONLY_RE.match(lines[index + 2])

            if (
                price_after_description
                and not PRICE_RE.search(line)
                and not PRICE_RE.search(lines[index + 1])
            ):
                _add_item(
                    items,
                    seen,
                    name=line,
                    category=current_category,
                    price_text=price_after_description.group(1),
                    description=lines[index + 1],
                )
                index += 3
                continue

        index += 1

    _parse_column_like_lines(lines, current_category, items, seen)

    return items


def import_menu_uploads(uploads: list[Any], *, default_category: str = 'Cardápio') -> dict[str, Any]:
    valid_uploads = [upload for upload in uploads if getattr(upload, 'filename', '')]

    if not valid_uploads:
        raise ValidationError('Envie pelo menos uma imagem do cardápio.')

    all_items: list[ImportedMenuItem] = []
    seen: set[tuple[str, str, float]] = set()
    extracted_texts: list[str] = []
    failures: list[str] = []

    for upload in valid_uploads:
        try:
            text = extract_text_from_upload(upload)
        except ValidationError as exc:
            failures.append(str(exc))
            continue

        if not text.strip():
            failures.append(f'Não foi possível extrair texto de "{_filename(upload)}".')
            continue

        extracted_texts.append(text)

        for item in parse_menu_text(text, default_category=default_category):
            key = (item.name.lower(), item.category.lower(), item.price)

            if key in seen:
                continue

            seen.add(key)
            item.sort_order = len(all_items)
            all_items.append(item)

    if not extracted_texts:
        details = ' '.join(failures[:2])
        raise ValidationError(f'Não foi possível ler as imagens enviadas. {details}'.strip())

    return {
        'items': [
            {
                'name': item.name,
                'category': item.category,
                'price': item.price,
                'description': item.description or '',
                'active': item.active,
                'sort_order': item.sort_order,
            }
            for item in all_items
        ],
        'processed_files': len(valid_uploads),
        'recognized_items': len(all_items),
        'failures': failures,
    }
