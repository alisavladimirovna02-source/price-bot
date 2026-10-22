import base64
import csv
import json
import os
import re

import requests

from mapping_validation import ValidationStatus, normalize_name


PRODUCT_CODE_RE = re.compile(r"\b(?:[a-z]{2,}\d[a-z0-9]*|[a-z]\d[a-z0-9]{2,})\b", re.IGNORECASE)


flag_map = {
    "🇷🇺": "RU",
    "🇦🇪": "AE",
    "🇺🇸": "US",
    "🇪🇺": "EU",
    "🇨🇳": "CN",
    "🇭🇰": "HK",
}

region_map = {
    "US": "esim",
    "CN": "2sim",
    "HK": "2sim",
}


IPHONE_GENERATION_RE = re.compile(r"\biphone\s*(\d{1,2})\b", re.IGNORECASE)
IPHONE_SKU_RE = re.compile(r"\biphone\s*(\d{1,2})", re.IGNORECASE)


def clean_text(text):
    """Одинаковая очистка названия в прайсе и слева от '=' в mapping."""
    return re.sub(r"[^\w\s/.,+]", "", text)


def normalize_text(text):
    return " ".join(clean_text(text).lower().split())


def flexible_mapping_key(text):
    """A conservative lookup key for harmless supplier-name variations.

    This key is used only to find a mapping candidate.  It never authorizes a
    SKU by itself: a candidate found this way is always checked against the
    normalized SKU catalog by ``validate_mapping``.
    """
    key = normalize_name(text)
    key = re.sub(r"\bmbp\b", "macbook pro", key)
    key = re.sub(r"\bmba\b", "macbook air", key)
    key = re.sub(r"\bmac\s+book\b", "macbook", key)
    key = re.sub(r"\bair\s+pods\b", "airpods", key)
    key = re.sub(r"\b1\s*sim\s*\+\s*esim\b", "1sim+esim", key)

    # These are presentation words rather than product differences.  Critical
    # words such as Pro, Max, Air, screen size, chip, memory, SIM and network
    # deliberately remain in the key.
    key = re.sub(
        r"\b(?:apple|samsung|xiaomi|redmi|poco|google|huawei|honor|"
        r"iphone|ipad|galaxy|smartphone|phone|tablet|notebook|laptop)\b",
        " ",
        key,
    )
    # eSIM is relevant to phone SKUs but not to Wi-Fi iPad variants.  It is
    # deliberately removed only from the *lookup* key: the mandatory catalog
    # validation below still compares eSIM/Dual SIM before accepting a phone.
    key = re.sub(r"\besim\b", " ", key)
    # Existing mapping often omits GB in compact names (e.g. 256 instead of
    # 256GB).  TB remains explicit, so 1TB cannot turn into a bare 1.
    key = re.sub(r"(?<=\d)gb\b", "", key)
    return " ".join(key.split())


def product_codes(text):
    """Extract stable manufacturer article codes, not product characteristics.

    Apple codes such as ``MDHE4`` or ``MHFA4`` are a strong lookup anchor even
    when a supplier appends RAM, storage or a chip.  Short chip labels (M5,
    M5 Pro) are explicitly excluded because they identify a configuration, not
    one exact product.
    """
    codes = set()
    for code in PRODUCT_CODE_RE.findall(normalize_text(text)):
        token = code.casefold()
        if len(token) < 5 or len(token) > 8:
            continue
        if re.fullmatch(r"m[1-9](?:pro|max|ultra)?", token):
            continue
        codes.add(token)
    return codes


def _without_sim_for_region_lookup(name):
    """Return a lookup key for the same product regardless of SIM wording.

    SIM markers must be removed BEFORE generic flexible normalization.
    Otherwise ``1Sim+eSim`` can turn into ``1sim+`` and fail to match a
    supplier line where SIM wording is omitted.
    """
    key = normalize_name(name)

    key = re.sub(r"\bmbp\b", "macbook pro", key)
    key = re.sub(r"\bmba\b", "macbook air", key)
    key = re.sub(r"\bmac\s+book\b", "macbook", key)
    key = re.sub(r"\bair\s+pods\b", "airpods", key)

    key = re.sub(
        r"(?<!\w)(?:1\s*sim\s*\+\s*e\s*sim|2\s*sim|1\s*sim|e\s*sim)(?!\w)",
        " ",
        key,
        flags=re.I,
    )

    key = re.sub(
        r"\b(?:apple|samsung|xiaomi|redmi|poco|google|huawei|honor|"
        r"iphone|ipad|galaxy|smartphone|phone|tablet|notebook|laptop)\b",
        " ",
        key,
    )

    key = re.sub(r"(?<=\d)gb\b", "", key)
    key = re.sub(r"\s*\+\s*", " ", key)
    return " ".join(key.split())


def _iphone_generation(text):
    """Extract a numbered iPhone model, without mistaking storage for a model."""
    match = IPHONE_GENERATION_RE.search(text)
    return int(match.group(1)) if match else None


class MappingIndex(dict):
    """Mapping dictionary plus a precomputed conservative alias index."""

    def __init__(self):
        super().__init__()
        self.flexible = {}
        self.model_codes = {}
        self.region_lookup = {}

    def build_flexible_index(self):
        flexible = {}
        region_lookup = {}
        for alias, skus in self.items():
            key = flexible_mapping_key(alias)
            if key:
                flexible.setdefault(key, set()).update(skus)
            region_key = _without_sim_for_region_lookup(alias)
            if region_key:
                region_lookup.setdefault(region_key, set()).update(skus)
            for code in product_codes(alias):
                self.model_codes.setdefault(code, set()).update(skus)
        self.flexible = flexible
        self.region_lookup = region_lookup

    def flexible_candidates(self, name):
        return self.flexible.get(flexible_mapping_key(name), set())

    def model_code_candidates(self, name):
        candidates = set()
        for code in product_codes(name):
            candidates.update(self.model_codes.get(code, set()))
        return candidates

    def region_lookup_candidates(self, name):
        return self.region_lookup.get(_without_sim_for_region_lookup(name), set())


def parse_mapping(content):
    """Сохраняем все SKU одного названия, чтобы обнаруживать конфликты."""
    mapping = MappingIndex()
    for line in content.splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        name = normalize_text(left)
        sku = right.strip()
        if name and sku:
            mapping.setdefault(name, set()).add(sku)
    mapping.build_flexible_index()
    return mapping


def load_mapping_from_github():
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")

    if not token or not repo:
        raise Exception("Не заданы GITHUB_TOKEN или GITHUB_REPO")

    url = f"https://api.github.com/repos/{repo}/contents/mapping.txt"
    response = requests.get(
        url, headers={"Authorization": f"token {token}"}, timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if "content" not in data:
        raise Exception("Не удалось получить mapping.txt из GitHub")

    content = base64.b64decode(data["content"]).decode("utf-8-sig")
    return parse_mapping(content)


def _flexible_candidates(name, mapping):
    if isinstance(mapping, MappingIndex):
        return mapping.flexible_candidates(name)
    # Keep the public function compatible with callers that pass a plain dict.
    index = {}
    for alias, skus in mapping.items():
        key = flexible_mapping_key(alias)
        if key:
            index.setdefault(key, set()).update(skus)
    return index.get(flexible_mapping_key(name), set())


def _model_code_candidates(name, mapping):
    if isinstance(mapping, MappingIndex):
        return mapping.model_code_candidates(name)
    index = {}
    for alias, skus in mapping.items():
        for code in product_codes(alias):
            index.setdefault(code, set()).update(skus)
    candidates = set()
    for code in product_codes(name):
        candidates.update(index.get(code, set()))
    return candidates


def _region_lookup_candidates(name, mapping):
    """Find the same product in mapping ignoring only SIM text.

    This is used as a fallback when a supplier omits SIM configuration.
    Example: "17 256 Sage" can match mapping aliases
    "17 256 Sage eSim" and "17 256 Sage 1Sim+eSim"; region filtering
    then chooses the correct SKU variant.
    """
    if isinstance(mapping, MappingIndex):
        return mapping.region_lookup_candidates(name)

    key = _without_sim_for_region_lookup(name)
    candidates = set()
    for alias, skus in mapping.items():
        if _without_sim_for_region_lookup(alias) == key:
            candidates.update(skus)
    return candidates


def _is_iphone_14_or_newer(name, mapping=None, validator=None):
    """Identify iPhone generation from the name or an already-known mapping.

    Suppliers often omit the word ``iPhone`` (for example, ``16 Plus``).  In
    that case a mapping may safely supply the missing context, but ambiguous
    names must not be treated as iPhones just because they have a US flag.
    """
    generation = _iphone_generation(name)
    if generation is not None:
        return generation >= 14

    if not mapping:
        return False

    lookup_key = _without_sim_for_region_lookup(name)
    if not lookup_key:
        return False

    if isinstance(mapping, MappingIndex):
        candidates = mapping.region_lookup_candidates(name)
    else:
        # Compatibility path for external callers that still pass a plain dict.
        candidates = set()
        for alias, skus in mapping.items():
            if _without_sim_for_region_lookup(alias) == lookup_key:
                candidates.update(skus)

    generations = set()
    for sku in candidates:
        match = IPHONE_SKU_RE.search(sku)
        if match:
            generations.add(int(match.group(1)))
        if validator is not None:
            catalog_row = validator.catalog.get(sku)
            if catalog_row:
                catalog_generation = _iphone_generation(catalog_row[1])
                if catalog_generation is not None:
                    generations.add(catalog_generation)

    # If the name points to several model generations, do not infer eSIM.
    return len(generations) == 1 and next(iter(generations)) >= 14


def _has_explicit_esim(name):
    return bool(re.search(r"(?<!\w)e\s*sim(?!\w)", name, re.I))


def _sku_is_esim_variant(sku):
    """Our SKU convention: an `esim` token marks the eSIM-only/US variant."""
    return bool(re.search(r"esim", sku, re.I))


def _has_explicit_2sim(name):
    return bool(re.search(r"(?<!\w)2\s*sim(?!\w)", name, re.I))


def _has_explicit_1sim_esim(name):
    return bool(
        re.search(r"(?<!\w)1\s*sim\s*\+\s*e\s*sim(?!\w)", name, re.I)
    )


def _sku_is_2sim_variant(sku):
    return bool(re.search(r"2sim", sku, re.I))


def _filter_candidates_by_region(candidates, name, country):
    """Select the correct SIM variant for the region.

    - explicit 2SIM -> only 2SIM SKU
    - explicit 1SIM+eSIM -> ordinary/non-eSIM-only SKU
    - explicit eSIM -> eSIM SKU
    - no SIM marker and non-US region -> exclude eSIM-only SKU
    - US iPhone 14+ receives an explicit ``esim`` marker in prepare_price()
    """
    if not candidates:
        return candidates

    candidates = set(candidates)
    country = (country or "").upper()

    # Region markers may select among variants of the same SKU family, but
    # must never hide an unrelated double mapping.  Those remain explicit
    # MAPPING_CONFLICT errors for a person to fix.
    if len(candidates) > 1 and not _same_sku_family(candidates):
        return candidates

    explicit_2sim = _has_explicit_2sim(name)
    explicit_1sim_esim = _has_explicit_1sim_esim(name)
    explicit_esim = _has_explicit_esim(name)

    if explicit_2sim:
        sim_candidates = {
            sku for sku in candidates if _sku_is_2sim_variant(sku)
        }
        # As with eSIM-only models, not every established SKU embeds the SIM
        # marker in its text.  Keep an exact candidate when suffix filtering
        # has no evidence, then let catalogue validation make the decision.
        return sim_candidates or candidates

    if explicit_1sim_esim:
        return {
            sku for sku in candidates
            if not _sku_is_esim_variant(sku) and not _sku_is_2sim_variant(sku)
        }

    if explicit_esim:
        esim_candidates = {
            sku for sku in candidates if _sku_is_esim_variant(sku)
        }
        # Some current catalogue SKUs describe an eSIM-only product without
        # carrying an ``esim`` suffix (notably iPhone 17 Air).  In that case
        # the explicit mapping alias is still a valid candidate; the mandatory
        # catalogue validation in match_from_mapping() remains the authority.
        return esim_candidates or candidates

    if country != "US":
        return {sku for sku in candidates if not _sku_is_esim_variant(sku)}

    return candidates


def _aliases_for_candidate(mapping, sku):
    """Return aliases for a SKU, treating case-only spellings as one identity."""
    folded = sku.casefold()
    return tuple(
        alias for alias, values in mapping.items()
        if any(value.casefold() == folded for value in values)
    )


def _preferred_case_spelling(values):
    """Choose a deterministic spelling after the catalogue proved one identity."""
    return min(values, key=lambda value: (value != value.casefold(), value))


def _same_sku_family(candidates):
    """Recognize legacy/current spellings without guessing unrelated SKUs."""
    identities = [
        re.sub(r"[^a-z0-9]", "", sku.casefold()) for sku in candidates
    ]
    if not identities or any(not value for value in identities):
        return False
    shortest = min(identities, key=len)
    return all(shortest in value or value in shortest for value in identities)


def _resolve_candidates_by_catalog(candidates, name, mapping, validator):
    """Remove stale conflicting mappings only when the catalogue proves a winner.

    ``mapping.txt`` can contain an old SKU beside a newer SKU for the same
    supplier name.  The catalogue is the source of truth, so one uniquely OK
    candidate wins over REVIEW/BLOCKED/SKU_NOT_FOUND.  Genuine ambiguity is
    preserved as MAPPING_CONFLICT.
    """
    if validator is None or len(candidates) <= 1:
        return set(candidates)

    # Do not use validation to arbitrate unrelated SKU values.  This keeps a
    # genuine double mapping blocked.  The production conflicts fixed here
    # are recognizable legacy/current forms such as ``pocof8...``/``f8...``
    # and a version suffix on the same MacBook SKU.
    if not _same_sku_family(candidates):
        return set(candidates)

    # A case-only duplicate points to the same catalogue key.  Keep one
    # spelling for validation/output, while preserving case for every SKU that
    # does not have such a duplicate.
    groups = {}
    for sku in candidates:
        groups.setdefault(sku.casefold(), set()).add(sku)
    representatives = {
        _preferred_case_spelling(values) for values in groups.values()
    }

    checks = {
        sku: validator.validate_mapping(
            name, sku, _aliases_for_candidate(mapping, sku)
        )
        for sku in representatives
    }
    ok = {
        sku for sku, result in checks.items()
        if result.status == ValidationStatus.OK
    }
    if len(ok) == 1:
        return ok
    if len(ok) > 1:
        return ok

    review = {
        sku for sku, result in checks.items()
        if result.status == ValidationStatus.REVIEW
    }
    if len(review) == 1:
        return review
    if len(review) > 1:
        return review

    # With no catalogue-approved candidate, retain the original conflict so a
    # bad mapping cannot become a match merely because one error looked closer.
    return set(candidates)


def match_from_mapping(name, mapping, validator=None, with_validation=False, country=""):
    """Match exact aliases first, then safe normalized aliases with validation."""
    candidates = mapping.get(normalize_text(name), set())
    if validator is None:
        # Registry-free mode accepts only an explicitly saved, unambiguous
        # supplier name. Preserve SKU case: it may identify a distinct item.
        identities = set(candidates)
        # A US supplier marker can distinguish a base/eSIM pair, but a
        # brandless title gives no proof which variant was meant. Keep it for
        # manual review instead of silently choosing the base alias.
        if country.upper() == "US" and not _has_explicit_esim(name):
            variants = {sku.casefold() for sku in _region_lookup_candidates(name, mapping)}
            if len(variants) > 1:
                result = ("", "MAPPING_CONFLICT", None)
                return result if with_validation else result[:2]
        if not identities:
            result = ("", "NOT_FOUND", None)
        elif len(identities) > 1:
            result = ("", "MAPPING_CONFLICT", None)
        else:
            result = (_preferred_case_spelling(candidates), "OK", None)
        return result if with_validation else result[:2]
    candidates = _filter_candidates_by_region(candidates, name, country)
    # A flexible key is never enough to return a SKU: it only becomes useful
    # when the catalog validator is available for the mandatory second check.
    if not candidates and validator is not None:
        candidates = _filter_candidates_by_region(
            _flexible_candidates(name, mapping), name, country
        )

    # Supplier prices often omit SIM text entirely.  In that case use the
    # dedicated region index, which ignores only SIM markers and preserves
    # model / memory / colour.  Region filtering then selects the correct
    # variant: for example EU -> 1Sim+eSim SKU, US iPhone 14+ -> eSIM SKU.
    if not candidates and validator is not None:
        candidates = _filter_candidates_by_region(
            _region_lookup_candidates(name, mapping), name, country
        )

    if not candidates and validator is not None:
        candidates = _filter_candidates_by_region(
            _model_code_candidates(name, mapping), name, country
        )
    candidates = _resolve_candidates_by_catalog(
        candidates, name, mapping, validator
    )
    if not candidates:
        return ("", "NOT_FOUND", None) if with_validation else ("", "NOT_FOUND")
    if len(candidates) > 1:
        return ("", "MAPPING_CONFLICT", None) if with_validation else ("", "MAPPING_CONFLICT")
    sku = next(iter(candidates))
    if validator is None:
        return (sku, "OK", None) if with_validation else (sku, "OK")
    aliases = tuple(alias for alias, values in mapping.items() if sku in values)
    validation = validator.validate_mapping(name, sku, aliases)
    if validation.status == ValidationStatus.OK:
        return (sku, "OK", validation) if with_validation else (sku, "OK")
    if validation.status == ValidationStatus.REVIEW:
        return ("", "REVIEW", validation) if with_validation else ("", "REVIEW")
    if validation.status == ValidationStatus.BLOCKED:
        return ("", "MAPPING_BLOCKED", validation) if with_validation else ("", "MAPPING_BLOCKED")
    return ("", "SKU_NOT_FOUND", validation) if with_validation else ("", "SKU_NOT_FOUND")


def prepare_price(name, price, country, mapping=None, validator=None):
    """Apply the same name/SIM rules to explicit table fields and text input."""
    country = flag_map.get(country, country.upper())
    for emoji, code in flag_map.items():
        if emoji in name:
            if not country:
                country = code
            name = name.replace(emoji, "")
    name = " ".join(clean_text(name).split())
    has_sim = re.search(r"(?<!\w)(?:e\s*sim|[12]\s*sim)(?!\w)", name, re.I)
    # US iPhones 14 and newer are eSIM-only in this workflow.  A US marker
    # must never turn a Samsung, iPad, Mac or an older iPhone into an eSIM
    # variant.  CN/HK's existing dual-SIM handling remains unchanged.
    add_region_sim = country in {"CN", "HK"} or (
        country == "US" and _is_iphone_14_or_newer(name, mapping, validator)
    )
    if add_region_sim and not has_sim:
        name += " " + region_map[country]
    return name, price, country


def parse_price_line(line, mapping=None, validator=None):
    line = line.strip()
    if not line:
        return None

    country = ""
    for emoji, code in flag_map.items():
        if emoji in line:
            country = code
            line = line.replace(emoji, "")
            break

    line = clean_text(line)
    numbers = re.findall(r"\d[\d.,]*", line)
    parsed_numbers = []
    for num in numbers:
        clean = num.replace(".", "").replace(",", "")
        try:
            parsed_numbers.append(int(clean))
        except ValueError:
            continue

    if not parsed_numbers:
        return None

    price = max(parsed_numbers)
    name = line
    for num in numbers:
        clean = num.replace(".", "").replace(",", "")
        try:
            if int(clean) == price:
                name = name.replace(num, "")
        except ValueError:
            continue

    return prepare_price(name, price, country, mapping, validator)


def parse_prices(lines, mapping, validator=None, return_mapping_errors=False):
    rows = []
    not_found = set()
    best_prices = {}
    mapping_errors = []

    for line in lines:
        if isinstance(line, dict):
            parsed = prepare_price(
                line["name"], line["price"], line["country"], mapping, validator
            )
        else:
            parsed = parse_price_line(line, mapping, validator)
        if parsed is None:
            continue

        name, price, country = parsed
        sku, status, validation = match_from_mapping(name, mapping, validator, with_validation=True, country=country)
        report_sku = sku or (validation.sku if validation else "")
        score = 100 if status == "OK" else 0
        row = [name, price, country, report_sku, score, status]

        if status == "OK":
            # Сохраняем существующее правило выбора цены для одного SKU.
            if sku not in best_prices or price > best_prices[sku][1]:
                best_prices[sku] = row
        elif status in {"NOT_FOUND", "MAPPING_CONFLICT"}:
            not_found.add(name)
            rows.append(row)
        else:
            rows.append(row)
            if status in {"MAPPING_BLOCKED", "SKU_NOT_FOUND"} and validation:
                mapping_errors.append({
                    "Товар поставщика": name,
                    "SKU из mapping": validation.sku,
                    "Эталон": validation.catalog_name or "",
                    "Ошибка": validation.reason,
                })

    rows.extend(best_prices.values())
    return (rows, not_found, mapping_errors) if return_mapping_errors else (rows, not_found)


def main(input_json=None):
    mapping = load_mapping_from_github()
    validator = None
    print(f"Загружено названий из mapping GitHub: {len(mapping)}")
    conflicts = sum(len(skus) > 1 for skus in mapping.values())
    if conflicts:
        print(f"⚠️ Названий с разными SKU в mapping: {conflicts}")

    if input_json:
        with open(input_json, "r", encoding="utf-8") as f:
            rows, not_found, mapping_errors = parse_prices(json.load(f), mapping, validator, return_mapping_errors=True)
    else:
        with open("prices_utf8.txt", "r", encoding="utf-8-sig") as f:
            rows, not_found, mapping_errors = parse_prices(f, mapping, validator, return_mapping_errors=True)

    with open("prices_parsed.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Название", "Цена", "Страна", "SKU", "Score", "Status"])
        writer.writerows(rows)

    with open("not_found.txt", "w", encoding="utf-8") as f:
        for item in sorted(not_found):
            f.write(item + "\n")

    with open("mapping_errors.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer = csv.DictWriter(f, fieldnames=["Товар поставщика", "SKU из mapping", "Эталон", "Ошибка"])
        writer.writeheader()
        writer.writerows(mapping_errors)

    print("✅ Готово!")
    print(f"! Не найдено товаров: {len(not_found)}")


if __name__ == "__main__":
    import argparse
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--input-json", help="Очередь текстовых строк и товаров из таблиц")
    main(arguments.parse_args().input_json)
