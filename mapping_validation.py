"""Feature-based validation of supplier names against the normalized SKU catalog."""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import openpyxl


COLOR_ALIASES = {
    "черный": "black", "черная": "black", "black": "black",
    "белый": "white", "белая": "white", "white": "white",
    "серый": "gray", "серебристый": "silver", "серебро": "silver",
    "silver": "silver", "space grey": "space gray", "space gray": "space gray",
    "midnight": "black", "синий": "blue", "голубой": "blue", "blue": "blue",
    "зеленый": "green", "green": "green", "красный": "red", "red": "red",
    "розовый": "pink", "pink": "pink", "фиолетовый": "purple", "purple": "purple",
    "желтый": "yellow", "yellow": "yellow", "золотой": "gold", "gold": "gold",
    "natural titanium": "natural", "blue titanium": "blue",
    "black titanium": "black", "desert titanium": "desert",
    "starlight": "starlight", "teal": "teal", "ultramarine": "ultramarine",
}
BRANDS = {
    "apple": "Apple", "samsung": "Samsung", "xiaomi": "Xiaomi", "redmi": "Xiaomi",
    "poco": "Xiaomi", "google": "Google", "huawei": "Huawei", "honor": "Honor",
}
CRITICAL_FIELDS = {
    "smartphone": ("brand", "model", "variant", "ram", "storage", "network", "sim"),
    "laptop": ("brand", "model", "screen", "chip", "ram", "storage"),
    "tablet": ("brand", "model", "screen", "generation", "chip", "storage", "connectivity"),
    "smartwatch": ("brand", "model", "generation", "watch_size", "connectivity"),
    "headphones": ("brand", "model", "generation", "case_type"),
    "other": ("brand", "model", "variant", "storage"),
}
FIELD_LABELS = {
    "brand": "Бренд", "category": "Категория", "model": "Модель", "variant": "Версия",
    "screen": "Диагональ", "chip": "Чип", "ram": "Оперативная память",
    "storage": "Память", "network": "Сеть", "sim": "SIM", "connectivity": "Подключение",
    "generation": "Поколение", "watch_size": "Размер корпуса", "case_type": "Кейс", "color": "Цвет",
}


class ValidationStatus:
    OK = "OK"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    SKU_NOT_FOUND = "SKU_NOT_FOUND"


@dataclass(frozen=True)
class Difference:
    field: str
    source: str | None
    catalog: str | None
    reason: str = ""

    def display(self) -> str:
        if self.reason:
            return f"{FIELD_LABELS.get(self.field, self.field)}: {self.reason}"
        return f"{FIELD_LABELS.get(self.field, self.field)}: {self.source or 'не указано'} ≠ {self.catalog or 'не указано'}"


@dataclass(frozen=True)
class ProductFeatures:
    raw_name: str
    normalized_name: str
    brand: str | None = None
    category: str | None = None
    model: str | None = None
    variant: str | None = None
    screen: str | None = None
    chip: str | None = None
    ram: str | None = None
    storage: str | None = None
    network: str | None = None
    sim: str | None = None
    connectivity: str | None = None
    generation: str | None = None
    watch_size: str | None = None
    case_type: str | None = None
    color: str | None = None

    def value(self, field: str) -> str | None:
        return getattr(self, field)


@dataclass(frozen=True)
class ValidationResult:
    status: str
    source_name: str
    sku: str
    catalog_name: str | None
    conflicts: tuple[Difference, ...] = ()
    reviews: tuple[Difference, ...] = ()
    matches: tuple[Difference, ...] = ()
    mapping_signals: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        differences = self.conflicts or self.reviews
        if differences:
            return "; ".join(item.display() for item in differences)
        return "; ".join(self.mapping_signals) if self.mapping_signals else "—"


class NormalizedCatalog:
    def __init__(self, products: dict[str, str]) -> None:
        if not products:
            raise ValueError("Эталонная база SKU пуста")
        self.products = products

    @classmethod
    def from_xlsx(cls, path: str | Path) -> "NormalizedCatalog":
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"Не найден файл эталонной базы: {source}")
        workbook = openpyxl.load_workbook(source, read_only=True, data_only=True, keep_links=False)
        try:
            sheet = next((item for item in workbook.worksheets if item.sheet_state == "visible"), None)
            if sheet is None:
                raise ValueError("В эталонной базе нет видимого листа")
            rows = sheet.iter_rows(values_only=True)
            headers = [str(value or "").strip().casefold() for value in next(rows, ())]
            if "sku" not in headers or "наименование товара" not in headers:
                raise ValueError("В эталонной базе нужны колонки SKU и «Наименование товара»")
            sku_index, name_index = headers.index("sku"), headers.index("наименование товара")
            products: dict[str, str] = {}
            for row in rows:
                sku = str(row[sku_index] or "").strip() if sku_index < len(row) else ""
                name = str(row[name_index] or "").strip() if name_index < len(row) else ""
                if sku and name:
                    products[sku.casefold()] = name
            return cls(products)
        finally:
            workbook.close()

    def get(self, sku: str) -> tuple[str, str] | None:
        name = self.products.get(sku.strip().casefold())
        return (sku.strip(), name) if name else None


class MappingValidator:
    """Central validation service. Catalog conflicts always override mapping aliases."""

    def __init__(self, catalog: NormalizedCatalog) -> None:
        self.catalog = catalog

    def validate_mapping(self, product_name: str, sku: str, mapping_names: tuple[str, ...] = ()) -> ValidationResult:
        catalog_row = self.catalog.get(sku)
        if catalog_row is None:
            return ValidationResult(ValidationStatus.SKU_NOT_FOUND, product_name, sku.strip(), None)
        resolved_sku, catalog_name = catalog_row
        source, target = extract_features(product_name), extract_features(catalog_name)
        source = _infer_abbreviated_source(source, target)
        conflicts, reviews, matches = self._compare(source, target)
        signals = self._mapping_signals(mapping_names, target)
        status = ValidationStatus.BLOCKED if conflicts else ValidationStatus.REVIEW if reviews else ValidationStatus.OK
        return ValidationResult(status, product_name, resolved_sku, catalog_name, tuple(conflicts), tuple(reviews), tuple(matches), tuple(signals))

    def _compare(self, source: ProductFeatures, target: ProductFeatures):
        category = target.category or source.category or "other"
        conflicts, reviews, matches = [], [], []
        for field in CRITICAL_FIELDS.get(category, CRITICAL_FIELDS["other"]):
            current, expected = source.value(field), target.value(field)
            if current and expected:
                if _same(current, expected):
                    matches.append(Difference(field, current, expected))
                else:
                    conflicts.append(Difference(field, current, expected))
            elif expected and not current:
                reviews.append(Difference(field, None, expected, "в названии поставщика отсутствует критичная характеристика"))
        if source.color and target.color and not _same(source.color, target.color):
            reviews.append(Difference("color", source.color, target.color))
        elif target.color and not source.color:
            reviews.append(Difference("color", None, target.color, "цвет не указан в названии поставщика"))
        elif source.color and target.color:
            matches.append(Difference("color", source.color, target.color))
        return conflicts, reviews, matches

    def _mapping_signals(self, names: tuple[str, ...], target: ProductFeatures) -> list[str]:
        invalid = 0
        fields: dict[str, Counter[str]] = {}
        for name in names:
            source = _infer_abbreviated_source(extract_features(name), target)
            conflicts, _, _ = self._compare(source, target)
            if conflicts:
                invalid += 1
                continue
            for field in CRITICAL_FIELDS.get(target.category or "other", CRITICAL_FIELDS["other"]):
                value = source.value(field)
                if value:
                    fields.setdefault(field, Counter())[value] += 1
        signals = []
        if invalid:
            signals.append(f"В существующем mapping найдено подозрительных записей для SKU: {invalid}")
        for field, values in fields.items():
            if len(values) > 1:
                signals.append(f"В mapping нет единого значения «{FIELD_LABELS.get(field, field)}»")
        return signals


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    text = re.sub(r"(?<=\d)е\b", "e", text)  # Cyrillic «Е» in compact names such as 16Е.
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\be\s*[- ]?\s*sim\b", "esim", text)
    text = re.sub(r"(?<!\w)2\s*sim\b", "dual sim", text)
    text = re.sub(r"\bwi\s*[- ]?\s*fi\b", "wifi", text)
    text = re.sub(r"\bpro\s*max\b", "pro max", text)
    text = re.sub(r"\bpromax\b", "pro max", text)
    text = re.sub(r"\bm\s*([1-9])\s*(pro|max|ultra)\b", r"m\1 \2", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:гб|gb)\b", r"\1gb", text)
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s*(?:тб|tb)\b", r"\1tb", text)
    text = re.sub(r"\b(\d{1,3})\s*(?:gb)?\s*[/+]\s*(\d{1,4})\s*(gb|tb)?\b", lambda m: f"{m.group(1)}gb/{m.group(2)}{m.group(3) or 'gb'}", text)
    for source, target in sorted(COLOR_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(rf"\b{re.escape(source)}\b", target, text)
    text = re.sub(r"^(?:смартфон|телефон|планшет|ноутбук|смарт[- ]?часы|часы|наушники)\s+", "", text)
    return re.sub(r"\s+", " ", re.sub(r"[_,;|]", " ", text)).strip()


def extract_features(raw_name: str) -> ProductFeatures:
    text = normalize_name(raw_name)
    brand = _brand(text)
    category = _category(text)
    common = {"raw_name": raw_name, "normalized_name": text, "brand": brand, "category": category}
    if category == "laptop":
        model = "MacBook Pro" if "macbook pro" in text else "MacBook Air" if "macbook air" in text else None
        screen = _first(r"\b(?:macbook\s+(?:pro|air)\s+)?(13(?:\.3)?|14(?:\.2)?|15(?:\.3)?|16(?:\.2)?)\s*(?:\"|inch)?\b", text)
        return ProductFeatures(**common, model=model, screen=(screen or "").split(".")[0] or None, chip=_chip(text), ram=_memory(text)[0], storage=_memory(text)[1], color=_color(text))
    if category == "smartphone":
        model, variant = _phone_model(text)
        ram, storage = _memory(text)
        return ProductFeatures(**common, model=model, variant=variant, ram=ram, storage=storage, network=_network(text), sim=_sim(text), color=_color(text))
    if category == "tablet":
        model = "iPad Pro" if "ipad pro" in text else "iPad Air" if "ipad air" in text else "iPad mini" if "ipad mini" in text else "iPad" if "ipad" in text else None
        _, storage = _memory(text)
        return ProductFeatures(**common, model=model, screen=_first(r"\b(8\.3|10\.2|10\.9|11|12\.9|13)\s*(?:\"|inch)?\b", text), generation=_first(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:gen|generation)\b", text), chip=_chip(text), storage=storage, connectivity=_connectivity(text), color=_color(text))
    if category == "smartwatch":
        model = "Apple Watch Ultra" if "apple watch ultra" in text else "Apple Watch SE" if "apple watch se" in text else "Apple Watch" if "apple watch" in text else None
        return ProductFeatures(**common, model=model, generation=_first(r"\bseries\s*(\d+)\b", text) or _first(r"\bultra\s*(\d+)\b", text), watch_size=_first(r"\b(40|41|42|44|45|46|49)\s*mm\b", text), connectivity=_connectivity(text), color=_color(text))
    if category == "headphones":
        model = "AirPods Pro" if "airpods pro" in text else "AirPods Max" if "airpods max" in text else "AirPods" if "airpods" in text else None
        case_type = "USB-C" if "usb c" in text else "Lightning" if "lightning" in text else None
        return ProductFeatures(**common, model=model, generation=_first(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:gen|generation)\b", text), case_type=case_type, color=_color(text))
    return ProductFeatures(**common, color=_color(text))


def _infer_abbreviated_source(source: ProductFeatures, target: ProductFeatures) -> ProductFeatures:
    """Existing mapping abbreviates Apple names (e.g. «15 Pro 256»); retain its valid convention."""
    if source.category or not target.category:
        return source
    text = source.normalized_name
    if target.brand == "Apple" and target.model and target.model.startswith("iPhone "):
        number = target.model.split(" ", 1)[1].casefold()
        if re.match(rf"^{re.escape(number)}\b", text):
            storage = _memory(text)[1] or _short_storage(text, target.storage)
            return replace(source, brand="Apple", category="smartphone", model=target.model, variant=_variant(text), storage=storage, sim=_sim(text), network=_network(text))
    if target.model in {"MacBook Pro", "MacBook Air"} and re.match(r"^(?:macbook\s+)?(?:pro|air)\b", text):
        return replace(source, brand="Apple", category="laptop", model=target.model, screen=_first(r"\b(13|14|15|16)\b", text), chip=_chip(text), ram=_memory(text)[0], storage=_memory(text)[1])
    return source


def _brand(text: str) -> str | None:
    for token, brand in BRANDS.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            return brand
    return "Apple" if re.search(r"\b(iphone|ipad|macbook|airpods|apple watch)\b", text) else None


def _category(text: str) -> str | None:
    if "macbook" in text:
        return "laptop"
    if re.search(r"\b(iphone|galaxy|pixel|redmi|poco)\b", text):
        return "smartphone"
    if "ipad" in text:
        return "tablet"
    if "apple watch" in text:
        return "smartwatch"
    if "airpods" in text:
        return "headphones"
    return None


def _phone_model(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"\biphone\s*(\d{1,2}(?:e|se|\s+mini)?)\b", text)
    if match:
        return f"iPhone {match.group(1).upper()}", _variant(text[match.end():match.end() + 24])
    match = re.search(r"\bgalaxy\s+(s\d{1,3}|a\d{1,3}|z\s*(?:flip|fold)\s*\d?)\b", text)
    if match:
        return "Galaxy " + re.sub(r"\s+", " ", match.group(1)).upper(), _variant(text[match.end():match.end() + 24])
    return None, None


def _memory(text: str) -> tuple[str | None, str | None]:
    pair = re.search(r"\b(\d{1,3})gb\s*/\s*(\d{1,4})(gb|tb)\b", text)
    if pair:
        return f"{pair.group(1)}GB", f"{pair.group(2)}{pair.group(3).upper()}"
    values = re.findall(r"\b(\d+(?:\.\d+)?)(gb|tb)\b", text)
    storage = [f"{number}{unit.upper()}" for number, unit in values if unit == "tb" or int(float(number)) >= 64]
    return None, storage[-1] if storage else None


def _short_storage(text: str, expected: str | None) -> str | None:
    """Compact mapping names often have «15 Pro 256» rather than «256GB»."""
    if not expected:
        return None
    for value in re.findall(r"\b\d+(?:tb)?\b", text):
        candidate = f"{value.upper()}" if value.casefold().endswith("tb") else f"{value}GB"
        if _same(candidate, expected):
            return expected
    return None


def _chip(text: str) -> str | None:
    match = re.search(r"\b(m[1-9])(?:\s+(pro|max|ultra))?\b", text)
    return " ".join((match.group(1).upper(), match.group(2).title())) if match and match.group(2) else match.group(1).upper() if match else None


def _variant(text: str) -> str | None:
    if re.search(r"\bpro\s+max\b", text): return "Pro Max"
    for token, label in (("ultra", "Ultra"), ("plus", "Plus"), ("pro", "Pro"), ("mini", "mini"), ("fe", "FE")):
        if re.search(rf"\b{token}\b", text): return label
    return None


def _network(text: str) -> str | None:
    match = re.search(r"\b([45])g\b", text)
    return f"{match.group(1)}G" if match else None


def _sim(text: str) -> str | None:
    if "esim" in text: return "eSIM"
    if re.search(r"\bdual\s*sim\b", text): return "Dual SIM"
    if re.search(r"\bsim\b", text): return "SIM"
    return None


def _connectivity(text: str) -> str | None:
    if "cellular" in text or "lte" in text: return "Cellular"
    if "wifi" in text: return "Wi-Fi"
    if "gps" in text: return "GPS"
    return None


def _color(text: str) -> str | None:
    for color in sorted(set(COLOR_ALIASES.values()), key=len, reverse=True):
        if re.search(rf"\b{re.escape(color)}\b", text): return color.title()
    return None


def _first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _same(left: str, right: str) -> bool:
    left_capacity, right_capacity = _capacity_in_gb(left), _capacity_in_gb(right)
    if left_capacity is not None and right_capacity is not None:
        return left_capacity == right_capacity
    return left.casefold().replace(" ", "") == right.casefold().replace(" ", "")


def _capacity_in_gb(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(gb|tb)\s*", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    return round(number * (1024 if match.group(2).casefold() == "tb" else 1))


@lru_cache(maxsize=1)
def get_runtime_validator() -> MappingValidator:
    path = Path(os.getenv("CATALOG_PATH", Path(__file__).with_name("catalog_normalized.xlsx")))
    return MappingValidator(NormalizedCatalog.from_xlsx(path))
