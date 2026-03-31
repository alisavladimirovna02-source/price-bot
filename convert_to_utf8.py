# convert_to_utf8.py

# Укажи путь к своему исходному файлу
source_file = "prices.txt"

# Новый файл в UTF-8
target_file = "prices_utf8.txt"

# Читаем старый файл в latin1 (читает любые символы)
with open(source_file, "r", encoding="latin1") as f:
    text = f.read()

# Записываем в новый файл в UTF-8
with open(target_file, "w", encoding="utf-8") as f:
    f.write(text)

print(f"Файл перекодирован! Новый файл: {target_file}")
