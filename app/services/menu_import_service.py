from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

from ..errors import ValidationError
from ..utils import normalize_text, parse_price

PRICE_RE = re.compile(
    r'(?:(?:R\$|RS|S\$|\$)\s*)?'
    r'(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,4}[,.]\d{2}|\d{3,5})',
    re.IGNORECASE,
)

PRICE_ONLY_RE = re.compile(
    r'^\s*[\[\(\|]?\s*'
    r'(?:(?:R\$|RS|S\$|\$)\s*)?'
    r'(\d{1,3}(?:\.\d{3})*,\d{2}|\d{1,4}[,.]\d{2}|\d{3,5})'
    r'\s*[\]\)\|]?\s*$',
    re.IGNORECASE,
)

ITEM_NUMBER_RE = re.compile(r'^\s*(?:\d{1,2}\s*[-–—.)]\s*)+')
NOISE_RE = re.compile(r'^[\W_]+$')
RESAMPLING = getattr(Image, 'Resampling', Image)

CATEGORY_WORDS = {
    'entrada', 'entradas',
    'sobremesa', 'sobremesas',
    'prato', 'pratos',
    'principal', 'principais',
    'bebida', 'bebidas',
    'lanche', 'lanches',
    'hamburguer', 'hamburgueres',
    'burger', 'burgers',
    'combo', 'combos',
    'porcao', 'porcoes',
    'pizza', 'pizzas',
    'massa', 'massas',
    'salada', 'saladas',
    'executivo', 'executivos',
    'promocao', 'promocoes',
}

STOP_WORDS = {
    'telefone',
    'whatsapp',
    'instagram',
    'facebook',
    'endereco',
    'delivery',
    'pedido',
    'pedidos',
    'funcionamento',
}


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


def _mime_type(filename: str) -> str:
    filename = filename.lower()
    if filename.endswith('.png'):
        return 'image/png'
    if filename.endswith('.webp'):
        return 'image/webp'
    return 'image/jpeg'


def _normalize_ocr_text(text: str) -> str:
    replacements = {
        '—': '-',
        '–': '-',
        '•': ' ',
        '·': ' ',
        '|': ' | ',
        'R§': 'R$',
        'S$': 'R$',
        'R5': 'R$',
        'O,': '0,',
        'o,': '0,',
        'l,': '1,',
        'I,': '1,',
        'ºº': '90',
        '°°': '90',
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r'(?<=\d)\s+(?=,\d{2}\b)', '', text)
    return text


def _clean_line(line: str) -> str:
    line = _normalize_ocr_text(str(line or ''))
    line = re.sub(r'\s+', ' ', line).strip()
    line = line.strip(' _*~`"\'')
    return normalize_text(line)


def _strip_accents_for_compare(text: str) -> str:
    table = str.maketrans(
        'áàãâäéèêëíìîïóòõôöúùûüçÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇ',
        'aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC',
    )
    return text.translate(table).lower()


def _prepare_base_image(file_storage: Any) -> Image.Image:
    raw = _read_bytes(file_storage)

    if not raw:
        raise ValidationError(f'O arquivo "{_filename(file_storage)}" está vazio.')

    try:
        image = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise ValidationError(f'Não foi possível abrir a imagem "{_filename(file_storage)}".') from exc

    image = ImageOps.exif_transpose(image).convert('RGB')

    width, height = image.size
    largest_side = max(width, height)

    if largest_side < 1900:
        scale = 1900 / largest_side
        image = image.resize((round(width * scale), round(height * scale)), RESAMPLING.LANCZOS)
    elif largest_side > 3400:
        scale = 3400 / largest_side
        image = image.resize((round(width * scale), round(height * scale)), RESAMPLING.LANCZOS)

    return image


def _image_variants(image: Image.Image) -> list[Image.Image]:
    gray = ImageOps.grayscale(image)
    auto = ImageOps.autocontrast(gray)

    high_contrast = ImageEnhance.Contrast(auto).enhance(2.4)
    high_contrast = high_contrast.filter(ImageFilter.SHARPEN)

    threshold = high_contrast.point(lambda pixel: 255 if pixel > 155 else 0)

    inverted = ImageOps.invert(auto)
    inverted = ImageEnhance.Contrast(inverted).enhance(2.0)
    inverted = inverted.filter(ImageFilter.SHARPEN)

    return [auto, high_contrast, threshold, inverted]


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


def extract_text_from_upload(file_storage: Any) -> str:
    image = _prepare_base_image(file_storage)
    texts: list[str] = []

    for variant in _image_variants(image):
        for config in ('--oem 3 --psm 6', '--oem 3 --psm 4', '--oem 3 --psm 11'):
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

    return _dedupe_lines('\n'.join(texts))


def _looks_like_category(line: str) -> bool:
    line = _clean_line(line).rstrip(':')

    if not line or PRICE_RE.search(line):
        return False

    if len(line) < 3 or len(line) > 55:
        return False

    compare = _strip_accents_for_compare(line)
    words = re.findall(r'[a-zA-ZÀ-ÿ]+', compare)

    if any(word in CATEGORY_WORDS for word in words):
        return True

    alpha = [ch for ch in line if ch.isalpha()]

    if len(alpha) >= 3:
        uppercase_ratio = sum(1 for ch in alpha if ch.isupper()) / len(alpha)
        if uppercase_ratio > 0.75 and len(words) <= 5:
            return True

    return False


def _looks_like_noise(line: str) -> bool:
    compare = _strip_accents_for_compare(line)

    if len(line) <= 1:
        return True

    if NOISE_RE.fullmatch(line):
        return True

    if any(word in compare for word in STOP_WORDS) and not PRICE_RE.search(line):
        return True

    return False


def _clean_name(name: str) -> str:
    name = _clean_line(name)
    name = ITEM_NUMBER_RE.sub('', name)
    name = re.sub(r'[\[\]{}()|]+', ' ', name)
    name = re.sub(r'(?i)\b(?:R\$|RS|S\$)\b', ' ', name)
    name = re.sub(r'\s+', ' ', name).strip(' -:;,.')
    return name


def _price_to_float(price_text: str) -> float | None:
    cleaned = normalize_text(price_text).replace(' ', '')

    if re.fullmatch(r'\d{3,5}', cleaned):
        return round(int(cleaned) / 100, 2)

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
) -> bool:
    name = _clean_name(name)
    category = _clean_line(category).rstrip(':') or 'Cardápio'

    if not name or len(name) < 2 or len(name) > 90:
        return False

    if _looks_like_noise(name):
        return False

    price = _price_to_float(price_text)

    if price is None or price <= 0 or price > 9999:
        return False

    key = (name.lower(), category.lower(), round(price, 2))

    if key in seen:
        return False

    seen.add(key)

    items.append(
        ImportedMenuItem(
            name=name[:80],
            category=category[:50],
            price=round(price, 2),
            description=normalize_text(description)[:180] if description else None,
            active=1,
            sort_order=len(items),
        )
    )

    return True


def parse_menu_text(text: str, *, default_category: str = 'Cardápio') -> list[ImportedMenuItem]:
    lines = [_clean_line(raw_line) for raw_line in text.splitlines()]
    lines = [line for line in lines if line and not _looks_like_noise(line)]

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

        matches = list(PRICE_RE.finditer(line))

        if matches:
            cursor = 0

            for match in matches:
                before_price = line[cursor:match.start()]
                candidate_name = before_price

                cleaned_candidate = _clean_name(candidate_name)

                if not cleaned_candidate or len(cleaned_candidate.split()) > 8:
                    if index > 0:
                        previous = lines[index - 1]
                        if previous and not PRICE_RE.search(previous) and not _looks_like_category(previous):
                            candidate_name = previous

                _add_item(
                    items,
                    seen,
                    name=candidate_name,
                    category=current_category,
                    price_text=match.group(1),
                )

                cursor = match.end()

            index += 1
            continue

        if index + 1 < len(lines):
            price_next = PRICE_ONLY_RE.match(lines[index + 1])

            if price_next:
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

            if price_after_description and not PRICE_RE.search(lines[index + 1]):
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

    return items


def _image_to_base64_data_url(raw: bytes, filename: str) -> str:
    encoded = base64.b64encode(raw).decode('utf-8')
    return f'data:{_mime_type(filename)};base64,{encoded}'


def _safe_json_loads(text: str) -> dict[str, Any]:
    text = text.strip()

    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?', '', text).strip()
        text = re.sub(r'```$', '', text).strip()

    start = text.find('{')
    end = text.rfind('}')

    if start >= 0 and end >= 0:
        text = text[start:end + 1]

    return json.loads(text)


def _normalize_ai_item(raw: dict[str, Any], index: int) -> ImportedMenuItem | None:
    name = _clean_name(raw.get('name') or raw.get('nome') or '')
    category = _clean_line(raw.get('category') or raw.get('categoria') or 'Cardápio')
    description = normalize_text(raw.get('description') or raw.get('descricao') or '')
    price_raw = raw.get('price') or raw.get('preco') or raw.get('valor')

    if price_raw is None:
        return None

    price = _price_to_float(str(price_raw))

    if not name or price is None:
        return None

    return ImportedMenuItem(
        name=name[:80],
        category=(category or 'Cardápio')[:50],
        price=round(price, 2),
        description=description[:180] if description else None,
        active=1,
        sort_order=index,
    )


def _import_with_openai(raw_files: list[tuple[str, bytes]], *, default_category: str) -> list[ImportedMenuItem]:
    api_key = os.getenv('OPENAI_API_KEY', '').strip()

    if not api_key:
        return []

    model = os.getenv('OPENAI_MENU_MODEL', 'gpt-4o-mini').strip() or 'gpt-4o-mini'

    content: list[dict[str, Any]] = [
        {
            'type': 'text',
            'text': (
                'Você é um extrator de cardápios de restaurante. '
                'Analise as imagens e retorne SOMENTE JSON válido. '
                'Extraia produtos, categorias, preços e descrições curtas. '
                'Regras importantes: '
                '1) Em cardápios com categorias como Entradas, Sobremesas, Pratos principais e Bebidas, preserve a categoria correta. '
                '2) Se o item vier numerado, remova o número do nome. Exemplo: "01 - Fritas" vira "Fritas". '
                '3) Se houver descrição abaixo do nome, coloque em description. '
                '4) Em cardápios promocionais com imagem, extraia o nome principal e o preço perto dele. '
                '5) Textos como "Chá ou Refri" podem ir em description. '
                '6) Use preço numérico com ponto decimal, exemplo 25.90. '
                '7) Não invente produtos quando não houver preço visível. '
                'Formato obrigatório: '
                '{"items":[{"name":"Produto","category":"Categoria","price":25.90,"description":"Descrição opcional"}]}'
            ),
        }
    ]

    for filename, raw in raw_files:
        content.append(
            {
                'type': 'image_url',
                'image_url': {
                    'url': _image_to_base64_data_url(raw, filename)
                },
            }
        )

    payload = {
        'model': model,
        'messages': [
            {
                'role': 'user',
                'content': content,
            }
        ],
        'temperature': 0,
        'response_format': {'type': 'json_object'},
    }

    response = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=90,
    )

    if response.status_code >= 400:
        raise ValidationError(
            'A leitura por IA não conseguiu processar o cardápio. '
            'Verifique a variável OPENAI_API_KEY ou tente novamente.'
        )

    data = response.json()
    content_text = data['choices'][0]['message']['content']
    parsed = _safe_json_loads(content_text)

    raw_items = parsed.get('items') or parsed.get('produtos') or []

    if not isinstance(raw_items, list):
        return []

    items: list[ImportedMenuItem] = []
    seen: set[tuple[str, str, float]] = set()

    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            continue

        item = _normalize_ai_item(raw_item, index)

        if not item:
            continue

        key = (item.name.lower(), item.category.lower(), round(item.price, 2))

        if key in seen:
            continue

        seen.add(key)
        item.sort_order = len(items)
        items.append(item)

    return items


def _import_with_ocr(raw_files: list[tuple[str, bytes]], *, default_category: str) -> tuple[list[ImportedMenuItem], list[str]]:
    all_items: list[ImportedMenuItem] = []
    failures: list[str] = []
    seen: set[tuple[str, str, float]] = set()

    for filename, raw in raw_files:
        try:
            class UploadProxy(io.BytesIO):
                pass

            proxy = UploadProxy(raw)
            proxy.filename = filename

            text = extract_text_from_upload(proxy)
            items = parse_menu_text(text, default_category=default_category)
        except ValidationError as exc:
            failures.append(str(exc))
            continue

        if not items:
            failures.append(f'Nenhum item com preço foi identificado em "{filename}".')
            continue

        for item in items:
            key = (item.name.lower(), item.category.lower(), round(item.price, 2))

            if key in seen:
                continue

            seen.add(key)
            item.sort_order = len(all_items)
            all_items.append(item)

    return all_items, failures


def import_menu_uploads(uploads: list[Any], *, default_category: str = 'Cardápio') -> dict[str, Any]:
    valid_uploads = [upload for upload in uploads if getattr(upload, 'filename', '')]

    if not valid_uploads:
        raise ValidationError('Envie pelo menos uma imagem do cardápio.')

    raw_files: list[tuple[str, bytes]] = []

    for upload in valid_uploads:
        raw = _read_bytes(upload)

        if raw:
            raw_files.append((_filename(upload), raw))

    if not raw_files:
        raise ValidationError('Envie pelo menos uma imagem válida do cardápio.')

    failures: list[str] = []

    try:
        items = _import_with_openai(raw_files, default_category=default_category)
    except ValidationError as exc:
        failures.append(str(exc))
        items = []

    if not items:
        ocr_items, ocr_failures = _import_with_ocr(raw_files, default_category=default_category)
        items = ocr_items
        failures.extend(ocr_failures)

    if not items:
        raise ValidationError(
            'Não consegui identificar produtos com preço nas imagens. '
            'Para cardápios com imagens, fundos coloridos ou texto espalhado, cadastre uma OPENAI_API_KEY no Railway para usar leitura por IA.'
        )

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
            for item in items
        ],
        'processed_files': len(raw_files),
        'recognized_items': len(items),
        'failures': failures,
    }
