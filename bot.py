ALLOWED_USERS = [800906903, 686105512, 5652216103, 7434891167]

user_store = {}
import asyncio
import csv
import json
import logging
from io import StringIO
import os
import subprocess
import sys
import requests
import base64
import secrets
import wb_handlers
from mapping_validation import ValidationStatus, get_runtime_validator
from price_files import MAX_ROWS, PriceFileError, check_file, read_price_file
from telegram import ReplyKeyboardMarkup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    CommandHandler,
    TypeHandler
)

ADMIN_USERS = {
    int(item) for item in os.getenv("ADMIN_USERS", str(ALLOWED_USERS[0])).split(",")
    if item.strip().isdigit()
}

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [["📦 Не найдено"], [wb_handlers.BUTTON]],
        resize_keyboard=True
    )

TOKEN = os.getenv("TOKEN")
logger = logging.getLogger("price_bot")


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        secrets = [TOKEN, os.getenv("GITHUB_TOKEN")]
        secrets.extend(value for key, value in os.environ.items() if key.startswith("WB_TOKEN_"))
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # HTTP-запросы содержат токен в URL и не нужны в обычном журнале.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def log_telegram_initialized(application):
    logger.info(
        "Подключение к Telegram установлено: @%s (bot_id=%s)",
        application.bot.username, application.bot.id,
    )


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.callback_query:
        kind = "нажатие кнопки"
    elif update.effective_message and update.effective_message.document:
        kind = "документ"
    else:
        kind = "сообщение"
    logger.info(
        "Получено обновление %s: %s; user_id=%s; доступ=%s",
        update.update_id, kind, user.id if user else None,
        bool(user and user.id in ALLOWED_USERS),
    )


async def log_error(update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    logger.error(
        "Ошибка Telegram или обработчика: %s", type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )


async def check_access(update: Update):
    user = update.effective_user
    if user and user.id in ALLOWED_USERS:
        return True
    logger.warning("Отказ в доступе: user_id=%s", user.id if user else None)
    if user and update.effective_message:
        await update.effective_message.reply_text(
            "⛔ Ваш аккаунт не добавлен в список доступа.\n"
            f"Ваш Telegram ID: {user.id}\n"
            "Передайте этот ID владельцу бота."
        )
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.effective_message.reply_text(
        "✅ Бот на связи.\nПришли прайс текстом или файлом CSV, Excel (.xlsx), TXT до 10 МБ, "
        "затем нажми «Обработать».\n"
        "Можно объединять несколько файлов и сообщений.\n"
        "Для остатков Wildberries нажми «Обнулить остатки WB» или /wb.",
        reply_markup=get_main_keyboard(),
    )


def delete_mapping_github(item):
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    path = "mapping.txt"

    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    headers = {
        "Authorization": f"token {token}"
    }

    response = requests.get(url, headers=headers)
    data = response.json()

    if "content" not in data:
        return "ERROR"

    content = base64.b64decode(data["content"]).decode("utf-8")
    lines = content.splitlines()

    new_lines = []
    removed = False

    for line in lines:
        if line.startswith(item + " ="):
            removed = True
            continue
        new_lines.append(line)

    if not removed:
        return "NOT_FOUND"

    updated_content = "\n".join(new_lines)
    encoded_content = base64.b64encode(updated_content.encode()).decode()

    requests.put(
        url,
        headers=headers,
        json={
            "message": f"delete mapping: {item}",
            "content": encoded_content,
            "sha": data["sha"]
        }
    )

    return "DELETED"


def load_mvc_from_google():
    url = "https://docs.google.com/spreadsheets/d/1FPMu1Q7cmtQu8KxXNJqhqI97L-KXTR43V8DcDnDfTZs/export?format=csv&gid=0"

    response = requests.get(url, timeout=20)
    response.raise_for_status()

    data = list(csv.reader(StringIO(response.text)))

    mvc = {}

    for row in data[1:]:
        try:
            sku = row[0].strip()
            price = float(row[10].replace(" ", "").replace(",", "."))

            if sku:
                mvc[sku] = price
        except:
            continue

    return mvc



def compare_mvc():
    today = load_mvc_from_google()
    if not today:
        raise Exception("МВЦ не загрузилась или в колонке M не нашлись цены")

    old = {}
    if os.path.exists("mvc_yesterday.csv"):
        with open("mvc_yesterday.csv", "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                try:
                    old[row[0]] = float(row[1])
                except:
                    continue

    results = []
    check_results = []

    for sku in today:
        new_price = today[sku]
        old_price = old.get(sku)

        if old_price is None:
            status = "NEW"
            diff = ""
            ratio = ""
            need_check = True
        elif new_price == 0:
            status = "CHECK"
            diff = round(new_price - old_price, 2)
            ratio = ""
            need_check = True
        else:
            diff = round(new_price - old_price, 2)
            ratio = old_price / new_price

            if ratio < 0.95 or ratio > 1.05:
                status = "CHECK"
                need_check = True
            else:
                status = "OK"
                need_check = False

        row = [
            sku,
            old_price,
            new_price,
            diff,
            "" if ratio == "" else round(ratio, 4),
            status
        ]

        results.append(row)

        if need_check:
            check_results.append(row)

    with open("mvc_diff.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Вчера", "Сегодня", "Разница", "Вчера/Сегодня", "Статус"])
        writer.writerows(results)

    with open("mvc_check.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Вчера", "Сегодня", "Разница", "Вчера/Сегодня", "Статус"])
        writer.writerows(check_results)

    with open("mvc_yesterday.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for sku, price in today.items():
            writer.writerow([sku, price])

    return len(check_results)


async def delete_mapping_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item = query.data.replace("delmap:", "")

    result = delete_mapping_github(item)

    if result == "DELETED":
        if "not_found_list" in context.user_data:
            context.user_data["not_found_list"] = [
                x for x in context.user_data["not_found_list"] if x != item
            ]

        await send_not_found_page(query, context)

    elif result == "NOT_FOUND":
        await query.message.reply_text("❌ Не найдено")
    else:
        await query.message.reply_text("❌ Ошибка")


def update_mapping_github(new_entry):
    """Compatibility wrapper. All writes now pass through save_validated_mapping()."""
    if "=" not in new_entry:
        return "ERROR"
    name, sku = new_entry.rsplit("=", 1)
    if not name.strip() or not sku.strip():
        return "ERROR"
    _, status = save_validated_mapping(name.strip(), sku.strip(), "legacy")
    return status


def _github_file(path, allow_missing=False):
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    if not token or not repo:
        raise RuntimeError("Не заданы GITHUB_TOKEN или GITHUB_REPO")
    response = requests.get(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers={"Authorization": f"token {token}"}, timeout=20,
    )
    if allow_missing and response.status_code == 404:
        return "", None
    response.raise_for_status()
    data = response.json()
    return base64.b64decode(data["content"]).decode("utf-8-sig"), data["sha"]


def _write_github_file(path, content, message, sha=None):
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    response = requests.put(
        f"https://api.github.com/repos/{repo}/contents/{path}",
        headers={"Authorization": f"token {token}"}, json=body, timeout=20,
    )
    response.raise_for_status()


def _mapping_aliases(content, sku):
    aliases = []
    for line in content.splitlines():
        if "=" not in line:
            continue
        name, mapped_sku = line.rsplit("=", 1)
        if mapped_sku.strip().casefold() == sku.casefold() and name.strip():
            aliases.append(name.strip())
    return tuple(aliases)


def validate_mapping_entry(name, sku):
    content, _ = _github_file("mapping.txt")
    return get_runtime_validator().validate_mapping(name, sku, _mapping_aliases(content, sku))


def record_mapping_history(result, user_id, action):
    """History is append-only. Failed validation attempts are retained too."""
    try:
        content, sha = _github_file("mapping_history.csv", allow_missing=True)
        line = StringIO()
        writer = csv.writer(line)
        if not content:
            writer.writerow(["date", "telegram_user_id", "action", "source_name", "sku", "catalog_name", "status", "conflicts"])
        writer.writerow([
            __import__("datetime").datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            user_id, action, result.source_name, result.sku, result.catalog_name or "",
            result.status, result.reason,
        ])
        separator = "" if not content or content.endswith("\n") else "\n"
        _write_github_file("mapping_history.csv", content + separator + line.getvalue(), f"Record mapping {action}", sha)
    except Exception:
        logger.exception("Не удалось записать mapping_history.csv")


def save_validated_mapping(name, sku, user_id, override=False):
    """The only mapping.txt write path: revalidates immediately before a GitHub PUT."""
    result = validate_mapping_entry(name, sku)
    if result.status == ValidationStatus.SKU_NOT_FOUND:
        record_mapping_history(result, user_id, "SKU_NOT_FOUND")
        return result, "SKU_NOT_FOUND"
    can_override = str(user_id).isdigit() and int(user_id) in ADMIN_USERS
    if result.status == ValidationStatus.BLOCKED and (not override or not can_override):
        record_mapping_history(result, user_id, "BLOCKED")
        return result, "BLOCKED"
    content, sha = _github_file("mapping.txt")
    entry = f"{name.strip()} = {result.sku}"
    if entry in content.splitlines():
        record_mapping_history(result, user_id, "DUPLICATE")
        return result, "EXISTS"
    separator = "" if not content or content.endswith("\n") else "\n"
    _write_github_file("mapping.txt", content + separator + entry + "\n", "Add validated mapping", sha)
    record_mapping_history(result, user_id, "OVERRIDE" if override else "ADD")
    return result, "ADDED"


def format_validation(result):
    headings = {
        ValidationStatus.OK: "✅ Сопоставление проверено",
        ValidationStatus.REVIEW: "⚠️ Сопоставление требует проверки",
        ValidationStatus.BLOCKED: "⛔ Возможно неправильное сопоставление",
        ValidationStatus.SKU_NOT_FOUND: "❓ SKU отсутствует в эталонной базе",
    }
    text = (
        f"{headings[result.status]}\n\nТовар из прайса:\n{result.source_name}\n\n"
        f"SKU:\n{result.sku}\n\nЭталон SKU:\n{result.catalog_name or '—'}"
    )
    if result.conflicts:
        text += "\n\nКритические расхождения:\n" + "\n".join(f"❌ {item.display()}" for item in result.conflicts)
    if result.reviews:
        text += "\n\nТребует уточнения:\n" + "\n".join(f"• {item.display()}" for item in result.reviews)
    if result.mapping_signals:
        text += "\n\nСигналы mapping:\n" + "\n".join(f"• {item}" for item in result.mapping_signals)
    return text


async def not_found_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_not_found(update, context)


async def process_and_reply(update: Update, input_json=None):
    try:
        msg = await update.message.reply_text("⏳ Обрабатываю прайс...")

        command = [sys.executable, "parse_prices.py"]
        if input_json:
            command += ["--input-json", input_json]
        subprocess.run(command, check=True)

        with open("prices_parsed.csv", "r", encoding="utf-8", newline="") as f:
            processed_rows = list(csv.DictReader(f))
        matched_count = sum(row["Status"] == "OK" for row in processed_rows)
        review_count = sum(row["Status"] == "REVIEW" for row in processed_rows)
        mapping_error_count = sum(
            row["Status"] in {"MAPPING_BLOCKED", "SKU_NOT_FOUND"}
            for row in processed_rows
        )

        not_found_count = 0
        if os.path.exists("not_found.txt"):
            with open("not_found.txt", "r", encoding="utf-8") as f:
                not_found_count = sum(1 for _ in f)

        await msg.edit_text(
            f"📊 Прайс обработан\n\n"
            f"✅ Сопоставлено: {matched_count}\n"
            f"📦 Не найдено: {not_found_count}\n"
            f"⚠️ Требует проверки: {review_count}\n"
            f"⛔ Ошибок mapping: {mapping_error_count}"
        )

        try:
            with open("prices_parsed.csv", "rb") as f:
                await update.message.reply_document(
                    f,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30
                )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Не удалось отправить prices_parsed.csv: {e}")

        if not_found_count > 0:
            try:
                with open("not_found.txt", "rb") as f:
                    await update.message.reply_document(
                        f,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30
                    )
            except Exception as e:
                await update.message.reply_text(f"⚠️ Не удалось отправить not_found.txt: {e}")

        if mapping_error_count > 0:
            try:
                with open("mapping_errors.csv", "rb") as f:
                    await update.message.reply_document(
                        f,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
            except Exception as e:
                await update.message.reply_text(f"⚠️ Не удалось отправить mapping_errors.csv: {e}")

        await update.message.reply_text("🔎 Запускаю проверку МВЦ...")

        try:
            check_count = compare_mvc()

            if check_count > 0:
                await update.message.reply_text(
                    f"⚠️ Проверка МВЦ: найдено позиций для проверки: {check_count}"
                )

                with open("mvc_check.csv", "rb") as f:
                    await update.message.reply_document(
                        f,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30
                    )
            else:
                await update.message.reply_text("✅ Проверка МВЦ: подозрительных изменений нет")

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка проверки МВЦ: {e}")

        return True
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка обработки прайса: {e}")
        return False



async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    document = update.message.document
    try:
        check_file(document.file_name, document.file_size or 0)
        file = await document.get_file(read_timeout=30, connect_timeout=30)
        content = await file.download_as_bytearray(read_timeout=120, connect_timeout=30)
        entries = await asyncio.to_thread(read_price_file, document.file_name, content)
    except PriceFileError as error:
        await update.message.reply_text(f"❌ {error}\nФайл не добавлен. Уже принятые прайсы сохранены.")
        return
    except Exception:
        logger.exception("Не удалось загрузить прайс из Telegram")
        await update.message.reply_text("❌ Не удалось загрузить файл. Пришли его ещё раз. Уже принятые прайсы сохранены.")
        return
    await add_price_entries(update, context, entries, from_file=True)


async def handle_mapping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    text = update.message.text.strip()

    # SKU for an item selected from «Не найдено» uses the same confirmation flow.
    if "mapping_item" in context.user_data:
        item = context.user_data["mapping_item"]

        if "=" not in text:
            await update.message.reply_text("❌ Напиши в формате: товар = sku")
            return

        _, right = text.split("=", 1)
        right = right.strip()

        context.user_data.pop("mapping_item")
        await offer_mapping_confirmation(update, context, item, right)
        return

    lines = text.split("\n")
    candidates = []
    for line in lines:
        if "=" not in line:
            continue
        left, right = line.rsplit("=", 1)
        if left.strip() and right.strip():
            candidates.append((left.strip(), right.strip()))
    if len(candidates) == 1:
        await offer_mapping_confirmation(update, context, *candidates[0])
        return

    added = 0
    exists = 0
    review = 0
    blocked = 0
    sku_not_found = 0
    errors = len([line for line in lines if line.strip()]) - len(candidates)
    for left, right in candidates:
        try:
            validation = await asyncio.to_thread(validate_mapping_entry, left, right)
            record_mapping_history(validation, update.effective_user.id, "MASS_VALIDATE")
            if validation.status == ValidationStatus.OK:
                _, result = await asyncio.to_thread(save_validated_mapping, left, right, update.effective_user.id)
                if result == "ADDED":
                    added += 1
                elif result == "EXISTS":
                    exists += 1
                else:
                    errors += 1
            elif validation.status == ValidationStatus.REVIEW:
                review += 1
            elif validation.status == ValidationStatus.BLOCKED:
                blocked += 1
            else:
                sku_not_found += 1
        except Exception:
            logger.exception("Не удалось проверить массовое сопоставление")
            errors += 1

    if added == 0 and exists == 0 and review == 0 and blocked == 0 and sku_not_found == 0 and errors == 0:
        return

    await update.message.reply_text(
        f"📊 Результат:\n\n"
        f"✅ Добавлено: {added}\n"
        f"⚠️ Уже было: {exists}\n"
        f"⚠️ Требует подтверждения: {review}\n"
        f"⛔ Заблокировано: {blocked}\n"
        f"❓ SKU не найден: {sku_not_found}\n"
        f"❌ Ошибок: {errors}"
    )


async def offer_mapping_confirmation(update, context, name, sku):
    try:
        result = await asyncio.to_thread(validate_mapping_entry, name, sku)
        record_mapping_history(result, update.effective_user.id, "VALIDATE")
    except Exception:
        logger.exception("Не удалось проверить сопоставление")
        await update.effective_message.reply_text("❌ Не удалось проверить SKU по эталонной базе. Сопоставление не добавлено.")
        return
    token = secrets.token_urlsafe(8)
    context.user_data["pending_mapping"] = {"token": token, "name": name, "sku": sku}
    keyboard = []
    if result.status in {ValidationStatus.OK, ValidationStatus.REVIEW}:
        keyboard.append([InlineKeyboardButton("✅ Добавить", callback_data=f"map:{token}:add")])
    elif result.status == ValidationStatus.BLOCKED and update.effective_user.id in ADMIN_USERS:
        keyboard.append([InlineKeyboardButton("⚠️ Всё равно добавить", callback_data=f"map:{token}:override")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"map:{token}:cancel")])
    await update.effective_message.reply_text(format_validation(result), reply_markup=InlineKeyboardMarkup(keyboard))


async def mapping_validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, token, action = query.data.split(":", 2)
    pending = context.user_data.get("pending_mapping")
    if not pending or pending["token"] != token:
        await query.message.reply_text("❌ Подтверждение устарело. Проверь сопоставление заново.")
        return
    if action == "cancel":
        context.user_data.pop("pending_mapping", None)
        await query.message.edit_reply_markup(reply_markup=None)
        return
    if action == "override" and update.effective_user.id not in ADMIN_USERS:
        await query.message.reply_text("⛔ Принудительное добавление доступно только администратору.")
        return
    try:
        result, status = await asyncio.to_thread(
            save_validated_mapping, pending["name"], pending["sku"], update.effective_user.id, action == "override"
        )
    except Exception:
        logger.exception("Не удалось записать mapping")
        await query.message.reply_text("❌ Не удалось записать сопоставление в GitHub.")
        return
    context.user_data.pop("pending_mapping", None)
    if status == "ADDED":
        prefix = "⚠️ Добавлено с OVERRIDE." if action == "override" else "✅ Сопоставление добавлено."
    elif status == "EXISTS":
        prefix = "⚠️ Такое сопоставление уже есть."
    else:
        prefix = "⛔ Повторная проверка не разрешила добавить сопоставление."
    await query.message.edit_text(f"{prefix}\n\n{format_validation(result)}")


async def show_not_found(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists("not_found.txt"):
        await update.message.reply_text("❌ Файл not_found.txt не найден")
        return

    with open("not_found.txt", "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        await update.message.reply_text("✅ Все товары сопоставлены")
        return

    context.user_data["not_found_list"] = lines
    context.user_data["page"] = 0

    await send_not_found_page(update, context)


async def not_found_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if "page" not in context.user_data:
        context.user_data["page"] = 0
    if query.data == "nf_next":
        context.user_data["page"] += 1
    elif query.data == "nf_prev":
        context.user_data["page"] -= 1

    await send_not_found_page(query, context)


async def send_not_found_page(source, context):
    if "not_found_list" not in context.user_data:
        await source.message.reply_text("❌ Список устарел, открой заново")
        return

    lines = context.user_data["not_found_list"]
    page = context.user_data["page"]

    per_page = 5
    total_pages = (len(lines) - 1) // per_page

    if page > total_pages:
        page = total_pages
        context.user_data["page"] = page

    if page < 0:
        page = 0
        context.user_data["page"] = page

    start = page * per_page
    end = start + per_page

    chunk = lines[start:end]

    text = f"📄 Страница {page + 1}\n\n❌ Не найдено:\n\n"
    keyboard = []

    for item in chunk:
        text += f"• {item}\n"
        keyboard.append([
            InlineKeyboardButton("➕ Добавить", callback_data=f"addmap:{item}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delmap:{item}")
        ])

    nav_buttons = []

    if start > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="nf_prev"))

    if end < len(lines):
        nav_buttons.append(InlineKeyboardButton("➡️ Дальше", callback_data="nf_next"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    from telegram import CallbackQuery
    if isinstance(source, CallbackQuery):
        try:
            await source.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except:
            await source.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    else:
        await source.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def add_mapping_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item = query.data.replace("addmap:", "")
    context.user_data["mapping_item"] = item

    await query.message.reply_text(
        f"✏️ Введи SKU для:\n{item}\n\nПример:\n{item} = sku123"
    )


async def check_mapping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    if update.effective_user.id not in ADMIN_USERS:
        await update.effective_message.reply_text("⛔ Аудит mapping доступен только администраторам.")
        return
    try:
        content, _ = await asyncio.to_thread(_github_file, "mapping.txt")
        validator = get_runtime_validator()
        rows = []
        for line in content.splitlines():
            if "=" not in line:
                continue
            name, sku = line.rsplit("=", 1)
            if not name.strip() or not sku.strip():
                continue
            result = validator.validate_mapping(name.strip(), sku.strip(), _mapping_aliases(content, sku.strip()))
            rows.append([name.strip(), sku.strip(), result.catalog_name or "", result.status, result.reason])
        audit = StringIO()
        writer = csv.writer(audit)
        writer.writerow(["Название mapping", "SKU", "Эталон", "Статус", "Причина"])
        writer.writerows(rows)
        audit_content = audit.getvalue()
        await asyncio.to_thread(_write_github_file, "mapping_audit.csv", audit_content, "Create mapping audit")
        with open("mapping_audit.csv", "w", encoding="utf-8-sig", newline="") as file:
            file.write(audit_content)
        counts = {status: sum(row[3] == status for row in rows) for status in ("OK", "REVIEW", "BLOCKED", "SKU_NOT_FOUND")}
        await update.effective_message.reply_text(
            f"Аудит завершён. OK: {counts['OK']}; REVIEW: {counts['REVIEW']}; "
            f"BLOCKED: {counts['BLOCKED']}; SKU_NOT_FOUND: {counts['SKU_NOT_FOUND']}."
        )
        with open("mapping_audit.csv", "rb") as file:
            await update.effective_message.reply_document(file)
    except Exception:
        logger.exception("Не удалось выполнить аудит mapping")
        await update.effective_message.reply_text("❌ Не удалось выполнить аудит mapping.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    entries = [line.strip() for line in update.message.text.splitlines() if line.strip()]
    await add_price_entries(update, context, entries)


async def add_price_entries(update, context, entries, from_file=False):
    chat_id = update.effective_chat.id
    data = user_store.setdefault(chat_id, [])
    if len(data) + len(entries) > MAX_ROWS:
        await update.message.reply_text(f"❌ В одном прайсе может быть не больше {MAX_ROWS} позиций. Сначала обработай уже добавленные. Новые данные не добавлены.")
        return
    data.extend(entries)
    context.user_data["data"] = data
    count = len(data)

    keyboard = [[InlineKeyboardButton("🚀 Обработать", callback_data="done")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = f"📦 Добавлено позиций: {count}\n\nОтправь ещё или нажми кнопку 👇"
    if from_file:
        message_text = (f"📄 Файл принят: {len(entries)} позиций.\n"
                        f"📦 Всего в прайсе: {count}\n\nОтправь ещё файл или сообщение либо нажми кнопку 👇")

    # A fresh acknowledgement stays next to the incoming price. Editing an old
    # counter can happen far above /start and look like the bot ignored input.
    await update.message.reply_text(
        message_text,
        reply_markup=reply_markup
    )


async def done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return

    chat_id = query.message.chat.id
    data = user_store.get(chat_id, [])

    if not data:
        await query.message.reply_text("❌ Нет данных для обработки")
        return

    # JSON keeps table columns separate and preserves text messages as text.
    with open("prices_input.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    await query.message.edit_text("⏳ Обрабатываю...")

    class FakeUpdate:
        def __init__(self, message):
            self.message = message

    if await process_and_reply(FakeUpdate(query.message), input_json="prices_input.json"):
        user_store[chat_id] = []
        context.user_data.pop("data", None)
        context.user_data.pop("last_msg_id", None)
    else:
        await query.message.reply_text(
            "Прайс сохранён. Можно повторить обработку.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Обработать", callback_data="done")]]),
        )
    



def build_application():
    if not TOKEN or not TOKEN.strip():
        raise RuntimeError("На сервере не задана переменная TOKEN")
    app = (ApplicationBuilder().token(TOKEN)
           .post_init(log_telegram_initialized).build())
    app.add_handler(TypeHandler(Update, log_update), group=-2)
    wb_handlers.register_handlers(app, check_access)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("check_mapping", check_mapping_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📦 Не найдено"), not_found_button))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".+=.+"), handle_mapping))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CallbackQueryHandler(delete_mapping_button, pattern="delmap:"))
    app.add_handler(CallbackQueryHandler(add_mapping_button, pattern="addmap:"))
    app.add_handler(CallbackQueryHandler(not_found_nav, pattern="nf_"))
    app.add_handler(CallbackQueryHandler(mapping_validation_callback, pattern="^map:"))
    app.add_handler(CallbackQueryHandler(done_button, pattern="done"))
    app.add_error_handler(log_error)
    return app


def main():
    configure_logging()
    logger.info("Запускаю бота. Подключаюсь к Telegram...")
    try:
        app = build_application()
        # Явно запрашиваем сообщения и кнопки независимо от прежних настроек API.
        app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=0)
    except Exception:
        logger.exception("Не удалось запустить бота")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
