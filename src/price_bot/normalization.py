from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from .models import ProductFeatures


COLOR_ALIASES = {
    "черный": "black",
    "чёрный": "black",
    "черная": "black",
    "черный": "black",
    "белый": "white",
    "белая": "white",
    "серый": "gray",
    "серебристый": "silver",
    "серебро": "silver",
    "синий": "blue",
    "голубой": "blue",
    "зеленый": "green",
    "зелёный": "green",
    "красный": "red",
    "розовый": "pink",
    "фиолетовый": "purple",
    "жёлтый": "yellow",
    "желтый": "yellow",
    "золотой": "gold",
    "оранжевый": "orange",
    "space grey": "space gray",
    "cosmic black": "cosmic black",
    "space black": "space black",
    "starlight": "starlight",
    "midnight": "midnight",
    "natural titanium": "natural titanium",
    "desert titanium": "desert titanium",
    "blue titanium": "blue titanium",
    "black titanium": "black titanium",
}

BRANDS = {
    "apple": "Apple",
    "samsung": "Samsung",
    "xiaomi": "Xiaomi",
    "redmi": "Xiaomi",
    "poco": "Xiaomi",
    "google": "Google",
    "huawei": "Huawei",
    "honor": "Honor",
    "sony": "Sony",
    "dyson": "Dyson",
}

CATEGORY_PREFIX = re.compile(
    r"^(?:смартфон|телефон|планшет|ноутбук|смарт[- ]?часы|часы|наушники|гарнитура)\s+",
    flags=re.IGNORECASE,
)
MEMORY_PAIR = re.compile(
    r"\b(\d{1,3})\s*(?:gb)?\s*[/+]\s*(\d{1,4})(?:\s*(gb|tb)\b)?", re.IGNORECASE
)
GB = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:гб|gb)\b", re.IGNORECASE)
TB = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:тб|tb)\b", re.IGNORECASE)


def normalize_name(value: str) -> str:
    """Normalize spelling variants only; product-defining values remain untouched."""
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\be\s*[- ]?\s*sim\b", "esim", text)
    text = re.sub(r"(?<!\w)2\s*sim\b", "dual sim", text)
    text = re.sub(r"\bwi\s*[- ]?\s*fi\b", "wifi", text)
    text = re.sub(r"\bpro\s*max\b", "pro max", text)
    text = re.sub(r"\bpromax\b", "pro max", text)
    text = re.sub(r"\bair\s*pods\b", "airpods", text)
    text = re.sub(r"\bm\s*([1-9])\s*(pro|max|ultra)\b", r"m\1 \2", text)
    text = GB.sub(lambda match: f"{match.group(1)}gb", text)
    text = TB.sub(lambda match: f"{match.group(1)}tb", text)
    text = MEMORY_PAIR.sub(_normalize_memory_pair, text)
    for source, target in COLOR_ALIASES.items():
        text = re.sub(rf"\b{re.escape(source)}\b", target, text)
    text = CATEGORY_PREFIX.sub("", text)
    text = re.sub(r"[_,;|]", " ", text)
    text = re.sub(r"\s*-\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Region US identifies eSIM only for iPhone 14+.  Do this in the shared
    # normal form as well as in parsed features, while the parser separately
    # preserves ``region_original`` from the raw title.
    iphone = re.search(r"\biphone\s*(\d{1,2})\b", text)
    if iphone and int(iphone.group(1)) >= 14 and re.search(r"\b(?:us|usa)\b", text):
        text = re.sub(r"\b(?:us|usa)\b", "esim", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", "", normalize_name(value))


def extract_features(product_name: str) -> ProductFeatures:
    text = normalize_name(product_name)
    brand = _extract_brand(text)
    category = _extract_category(text)
    if category == "laptop":
        features = _extract_laptop(product_name, text, brand)
    elif category == "smartphone":
        features = _extract_phone(product_name, text, brand)
    elif category == "tablet":
        features = _extract_tablet(product_name, text, brand)
    elif category == "smartwatch":
        features = _extract_watch(product_name, text, brand)
    elif category == "headphones":
        features = _extract_headphones(product_name, text, brand)
    else:
        features = ProductFeatures(raw_name=product_name, normalized_name=text, brand=brand, category=category)
    return _with_common_features(features, text)


def _normalize_memory_pair(match: re.Match[str]) -> str:
    first, second, unit = match.groups()
    return f"{first}gb/{second}{(unit or 'gb').lower()}"


def _extract_brand(text: str) -> str | None:
    for token, brand in BRANDS.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            return brand
    if re.search(r"\b(iphone|ipad|macbook|airpods|apple watch)\b", text):
        return "Apple"
    return None


def _extract_category(text: str) -> str | None:
    if "macbook" in text or re.search(r"\b(laptop|notebook)\b", text):
        return "laptop"
    if "iphone" in text or re.search(r"\b(galaxy|pixel|redmi|poco|smartphone)\b", text):
        return "smartphone"
    if re.search(r"\bs\d{1,3}\b", text) and "samsung" in text:
        return "smartphone"
    if re.search(r"\b(?:xiaomi|redmi|poco)\s+\d{1,3}\b", text):
        return "smartphone"
    if "ipad" in text or "tablet" in text:
        return "tablet"
    if "apple watch" in text or re.search(r"\bwatch\b", text):
        return "smartwatch"
    if "airpods" in text or re.search(r"\b(headphones|buds|earbuds)\b", text):
        return "headphones"
    return None


def _extract_laptop(raw: str, text: str, brand: str | None) -> ProductFeatures:
    model: str | None = None
    if "macbook pro" in text:
        model = "MacBook Pro"
    elif "macbook air" in text:
        model = "MacBook Air"
    screen = _first_group(r"\b(?:macbook\s+(?:pro|air)\s+)?(13(?:\.3)?|14(?:\.2)?|15(?:\.3)?|16(?:\.2)?)\s*(?:\"|inch)?\b", text)
    if screen:
        screen = screen.split(".")[0]
    chip = _chip(text)
    ram, storage = _memory(text)
    return ProductFeatures(
        raw_name=raw,
        normalized_name=text,
        brand=brand,
        category="laptop",
        model=model,
        screen=screen,
        chip=chip,
        ram=ram,
        storage=storage,
    )


def _extract_phone(raw: str, text: str, brand: str | None) -> ProductFeatures:
    model: str | None = None
    variant: str | None = None
    generation: str | None = None
    iphone = re.search(r"\biphone\s*(\d{1,2}(?:\s*(?:se|mini))?)\b", text)
    if iphone:
        model = f"iPhone {iphone.group(1).upper()}"
        generation_match = re.match(r"\d+", iphone.group(1))
        generation = generation_match.group(0) if generation_match else None
        suffix = text[iphone.end() : iphone.end() + 20]
        variant = _variant(suffix)
    else:
        galaxy = re.search(r"\bgalaxy\s+(s\d{1,3}|a\d{1,3}|z\s*(?:flip|fold)\s*\d?)\b", text)
        if galaxy:
            galaxy_series = re.sub(r"\s+", " ", galaxy.group(1)).upper()
            model = f"Galaxy {galaxy_series}"
            variant = _variant(text[galaxy.end() : galaxy.end() + 20])
        else:
            pixel = re.search(r"\bpixel\s+(\d+(?:a|\s+pro)?)\b", text)
            if pixel:
                model = f"Pixel {pixel.group(1).upper()}"
                variant = _variant(text[pixel.end() : pixel.end() + 20])
            else:
                samsung = re.search(r"\b(?:samsung\s+)?(?:galaxy\s+)?(s\d{1,3}|a\d{1,3}|z\s*(?:flip|fold)\s*\d?)\b", text)
                if samsung and "samsung" in text:
                    samsung_series = re.sub(r"\s+", " ", samsung.group(1)).upper()
                    model = f"Galaxy {samsung_series}"
                    generation_match = re.search(r"\d+", samsung.group(1))
                    generation = generation_match.group(0) if generation_match else None
                    variant = _variant(text[samsung.end() : samsung.end() + 20])
                else:
                    xiaomi = re.search(r"\b(xiaomi|redmi|poco)\s+(\d{1,3})\b", text)
                    if xiaomi:
                        model = f"{xiaomi.group(1).title()} {xiaomi.group(2)}"
                        generation = xiaomi.group(2)
                        variant = _variant(text[xiaomi.end() : xiaomi.end() + 20])
    ram, storage = _memory(text)
    if storage is None:
        # In supplier phone prices memory is commonly written simply as
        # ``256 Black``.  Restrict values to real storage tiers so an iPhone
        # generation such as 16 never becomes a storage value.
        bare_storage = re.search(r"\b(64|128|256|512|1024|2048)\b", text)
        storage = f"{bare_storage.group(1)}GB" if bare_storage else None
    region = _region(raw.casefold())
    sim = _sim(text)
    # US is an eSIM marker only for iPhone 14 and newer.  Keeping the original
    # region lets exports and a reviewer see why eSIM was inferred.
    if brand == "Apple" and model and model.startswith("iPhone") and generation and int(generation) >= 14 and region == "US":
        sim = "eSIM"
    return ProductFeatures(
        raw_name=raw,
        normalized_name=text,
        brand=brand,
        category="smartphone",
        model=model,
        variant=variant,
        generation=generation,
        ram=ram,
        storage=storage,
        network=_network(text),
        sim=sim,
        region=region,
        region_original=region,
    )


def _extract_tablet(raw: str, text: str, brand: str | None) -> ProductFeatures:
    model: str | None = None
    if "ipad pro" in text:
        model = "iPad Pro"
    elif "ipad air" in text:
        model = "iPad Air"
    elif "ipad mini" in text:
        model = "iPad mini"
    elif "ipad" in text:
        model = "iPad"
    screen = _first_group(r"\b(8\.3|10\.2|10\.9|11|12\.9|13)\s*(?:\"|inch)?\b", text)
    generation = _first_group(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:gen|generation)\b", text)
    _, storage = _memory(text)
    return ProductFeatures(
        raw_name=raw,
        normalized_name=text,
        brand=brand,
        category="tablet",
        model=model,
        screen=screen,
        generation=generation,
        chip=_chip(text),
        storage=storage,
        connectivity=_connectivity(text),
    )


def _extract_watch(raw: str, text: str, brand: str | None) -> ProductFeatures:
    model: str | None = None
    if "apple watch ultra" in text:
        model = "Apple Watch Ultra"
    elif "apple watch se" in text:
        model = "Apple Watch SE"
    elif "apple watch" in text:
        model = "Apple Watch"
    generation = _first_group(r"\bseries\s*(\d+)\b", text)
    if not generation:
        generation = _first_group(r"\bultra\s*(\d+)\b", text)
    watch_size = _first_group(r"\b(40|41|42|44|45|46|49)\s*mm\b", text)
    return ProductFeatures(
        raw_name=raw,
        normalized_name=text,
        brand=brand,
        category="smartwatch",
        model=model,
        generation=generation,
        watch_size=watch_size,
        connectivity=_connectivity(text),
    )


def _extract_headphones(raw: str, text: str, brand: str | None) -> ProductFeatures:
    model: str | None = "AirPods" if "airpods" in text else None
    if "airpods pro" in text:
        model = "AirPods Pro"
    elif "airpods max" in text:
        model = "AirPods Max"
    generation = _first_group(r"\b(\d+)(?:st|nd|rd|th)?\s*(?:gen|generation)\b", text)
    case_type = None
    if "usb c" in text:
        case_type = "USB-C"
    elif "lightning" in text:
        case_type = "Lightning"
    return ProductFeatures(
        raw_name=raw,
        normalized_name=text,
        brand=brand,
        category="headphones",
        model=model,
        generation=generation,
        case_type=case_type,
    )


def _with_common_features(features: ProductFeatures, text: str) -> ProductFeatures:
    return replace(features, color=_color(text))


def _memory(text: str) -> tuple[str | None, str | None]:
    pair = re.search(r"\b(\d{1,3})gb\s*/\s*(\d{1,4})(gb|tb)\b", text)
    if pair:
        return f"{pair.group(1)}GB", f"{pair.group(2)}{pair.group(3).upper()}"
    values = re.findall(r"\b(\d+(?:\.\d+)?)(gb|tb)\b", text)
    if not values:
        return None, None
    storage_candidates = [f"{number}{unit.upper()}" for number, unit in values if unit == "tb" or int(float(number)) >= 64]
    return None, storage_candidates[-1] if storage_candidates else None


def _chip(text: str) -> str | None:
    chip = re.search(r"\b(m[1-9])(?:\s+(pro|max|ultra))?\b", text)
    if not chip:
        return None
    return " ".join(part.upper() if index == 0 else part.title() for index, part in enumerate(chip.groups()) if part)


def _variant(text: str) -> str | None:
    if re.search(r"\bpro\s+max\b", text):
        return "Pro Max"
    for value, label in (("ultra", "Ultra"), ("plus", "Plus"), ("pro", "Pro"), ("mini", "mini"), ("fe", "FE")):
        if re.search(rf"\b{value}\b", text):
            return label
    return None


def _network(text: str) -> str | None:
    match = re.search(r"\b([45])g\b", text)
    return f"{match.group(1)}G" if match else None


def _sim(text: str) -> str | None:
    if "esim" in text:
        return "eSIM"
    if re.search(r"\bdual\s*sim\b", text):
        return "Dual SIM"
    if re.search(r"\bsim\b", text):
        return "SIM"
    return None


def _region(text: str) -> str | None:
    if re.search(r"\b(?:us|usa)\b", text):
        return "US"
    if re.search(r"\b(?:eu|europe)\b", text):
        return "EU"
    if re.search(r"\b(?:cn|china)\b", text):
        return "CN"
    return None


def _connectivity(text: str) -> str | None:
    if "cellular" in text or "lte" in text:
        return "Cellular"
    if "wifi" in text:
        return "Wi-Fi"
    if "gps" in text:
        return "GPS"
    return None


def _color(text: str) -> str | None:
    for color in sorted(set(COLOR_ALIASES.values()), key=len, reverse=True):
        if re.search(rf"\b{re.escape(color)}\b", text):
            return color.title()
    return None


def _first_group(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None
