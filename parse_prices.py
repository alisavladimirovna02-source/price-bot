import re
import csv

# =========================
# 📦 ЗАГРУЗКА КАТАЛОГА
# =========================
catalog = []

with open("catalog.txt", "r", encoding="utf-8") as f:
    for line in f:
        item = line.strip()
        if item:
            catalog.append(item)

print(f"Загружено SKU: {len(catalog)}")

not_found = set()

# =========================
# 📌 ЗАГРУЗКА MAPPING
# =========================
mapping = {}

with open("mapping.txt", "r", encoding="utf-8") as f:
    for line in f:
        if "=" in line:
            left, right = line.split("=")
            mapping[left.strip()] = right.strip()

# =========================
# 🧹 НОРМАЛИЗАЦИЯ ТЕКСТА
# =========================
def normalize_text(text):
    return " ".join(text.lower().split())

# =========================
# 🔍 МАТЧИНГ ПО MAPPING
# =========================
def match_from_mapping(name):
    normalized = normalize_text(name)

    for key in mapping:
        key_norm = normalize_text(key)

        if key_norm in normalized:
            return mapping[key], "OK"

    return "", "NOT_FOUND"

# =========================
# 🌍 ФЛАГИ
# =========================
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

# =========================
# 📂 ФАЙЛЫ
# =========================
input_file = "prices_utf8.txt"
output_file = "prices_parsed.csv"

# =========================
# 📖 ЧТЕНИЕ
# =========================
with open(input_file, "r", encoding="utf-8") as f:
    lines = f.readlines()

# =========================
# 💾 ЗАПИСЬ CSV
# =========================
with open(output_file, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Название", "Цена", "Страна", "SKU", "Score", "Status"])

    best_prices = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 🌍 страна
        country = ""
        for emoji, code in flag_map.items():
            if emoji in line:
                country = code
                line = line.replace(emoji, "")
                break

        # 🧹 чистка
        line = re.sub(r'[^\w\s/.,+]', '', line)

        # 🔢 числа
        numbers = re.findall(r'\d{2,6}[.,]?\d*', line)

        if not numbers:
            continue

        parsed_numbers = []

        for num in numbers:
            clean = num.replace(".", "").replace(",", "")
            try:
                value = int(clean)
                parsed_numbers.append(value)
                if value > 2000:
                    parsed_numbers.append(value)
            except:
                continue

        if not parsed_numbers:
            continue

        price = max(parsed_numbers)

        name = line

        # удаляем именно цену
        for num in numbers:
            clean = num.replace(".", "").replace(",", "")
            try:
                if int(clean) == price:
                    name = name.replace(num, "")
            except:
                continue

        name = re.sub(r'\s+', ' ', name).strip()

        if country in region_map:
            name += " " + region_map[country]

        # 🔍 mapping
        sku, status = match_from_mapping(name)

        if sku:
            score = 100
        else:
            score = 0
            not_found.add(name)

        # =========================
        # 📊 ЛОГИКА ЛУЧШЕЙ ЦЕНЫ
        # =========================
        if sku:
            if sku in best_prices:
                if price > best_prices[sku]["price"]:
                    best_prices[sku] = {
                        "name": name,
                        "price": price,
                        "country": country,
                        "score": score,
                        "status": status
                    }
            else:
                best_prices[sku] = {
                    "name": name,
                    "price": price,
                    "country": country,
                    "score": score,
                    "status": status
                }
        else:
            writer.writerow([name, price, country, sku, score, status])

    # =========================
    # 💾 ЗАПИСЬ ЛУЧШИХ SKU
    # =========================
    for sku, data in best_prices.items():
        writer.writerow([
            data["name"],
            data["price"],
            data["country"],
            sku,
            data["score"],
            data["status"]
        ])

print("✅ Готово!")

with open("not_found.txt", "w", encoding="utf-8") as f:
    for item in sorted(not_found):
        f.write(item + "\n")

print(f"! Не найдено товаров: {len(not_found)}")
