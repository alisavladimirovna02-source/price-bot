import base64
import csv
import json
import os
import re

import requests


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


def clean_text(text):
    """Одинаковая очистка названия в прайсе и слева от '=' в mapping."""
    return re.sub(r"[^\w\s/.,+]", "", text)


def normalize_text(text):
    return " ".join(clean_text(text).lower().split())


def parse_mapping(content):
    """Сохраняем все SKU одного названия, чтобы обнаруживать конфликты."""
    mapping = {}
    for line in content.splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        name = normalize_text(left)
        sku = right.strip()
        if name and sku:
            mapping.setdefault(name, set()).add(sku)
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


def match_from_mapping(name, mapping):
    """Только полное совпадение. Дополнительные слова не отбрасываются."""
    candidates = mapping.get(normalize_text(name), set())
    if not candidates:
        return "", "NOT_FOUND"
    if len(candidates) > 1:
        return "", "MAPPING_CONFLICT"
    return next(iter(candidates)), "OK"


def prepare_price(name, price, country):
    """Apply the same name/SIM rules to explicit table fields and text input."""
    country = flag_map.get(country, country.upper())
    for emoji, code in flag_map.items():
        if emoji in name:
            if not country:
                country = code
            name = name.replace(emoji, "")
    name = " ".join(clean_text(name).split())
    has_sim = re.search(r"(?<!\w)(?:e\s*sim|[12]\s*sim)(?!\w)", name, re.I)
    if country in region_map and not has_sim:
        name += " " + region_map[country]
    return name, price, country


def parse_price_line(line):
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

    return prepare_price(name, price, country)


def parse_prices(lines, mapping):
    rows = []
    not_found = set()
    best_prices = {}

    for line in lines:
        if isinstance(line, dict):
            parsed = prepare_price(line["name"], line["price"], line["country"])
        else:
            parsed = parse_price_line(line)
        if parsed is None:
            continue

        name, price, country = parsed
        sku, status = match_from_mapping(name, mapping)
        score = 100 if sku else 0
        row = [name, price, country, sku, score, status]

        if sku:
            # Сохраняем существующее правило выбора цены для одного SKU.
            if sku not in best_prices or price > best_prices[sku][1]:
                best_prices[sku] = row
        else:
            not_found.add(name)
            rows.append(row)

    rows.extend(best_prices.values())
    return rows, not_found


def main(input_json=None):
    mapping = load_mapping_from_github()
    print(f"Загружено названий из mapping GitHub: {len(mapping)}")
    conflicts = sum(len(skus) > 1 for skus in mapping.values())
    if conflicts:
        print(f"⚠️ Названий с разными SKU в mapping: {conflicts}")

    if input_json:
        with open(input_json, "r", encoding="utf-8") as f:
            rows, not_found = parse_prices(json.load(f), mapping)
    else:
        with open("prices_utf8.txt", "r", encoding="utf-8-sig") as f:
            rows, not_found = parse_prices(f, mapping)

    with open("prices_parsed.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Название", "Цена", "Страна", "SKU", "Score", "Status"])
        writer.writerows(rows)

    with open("not_found.txt", "w", encoding="utf-8") as f:
        for item in sorted(not_found):
            f.write(item + "\n")

    print("✅ Готово!")
    print(f"! Не найдено товаров: {len(not_found)}")


if __name__ == "__main__":
    import argparse
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--input-json", help="Очередь текстовых строк и товаров из таблиц")
    main(arguments.parse_args().input_json)
