ALLOWED_USERS = [800906903, 686105512, 5652216103, 7434891167]

user_store = {}
import asyncio
import csv
import json
import logging
from io import BytesIO, StringIO
import os
import sys
import threading
import time
from pathlib import Path
import requests
import base64
import secrets
import wb_handlers
from v2_bridge import install as install_safe_pipeline
from mapping_validation import ValidationStatus
from mapping_only import validation_result as validate_mapping_without_registry
from price_files import MAX_ROWS, PriceFileError, check_file, read_price_file
from price_jobs import (
    UserUpdateProcessor, create_job, latest_report_directory, publish_job, run_parser,
)
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
        [
            ["📦 Не найдено", "ℹ️ Инструкция"],
            [wb_handlers.BUTTON],
            ["🗂 Проверить mapping", "➕ Пополнить mapping"],
        ],
        resize_keyboard=True,
    )

TOKEN = os.getenv("TOKEN")
logger = logging.getLogger("price_bot")
_mvc_snapshot_lock = threading.RLock()


def reports_root():
    return Path(os.environ.get("REPORTS_DIR", Path.cwd())).resolve()


def price_owner_key(update):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    return chat_id if update.effective_chat.type == "private" else (chat_id, user_id)


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
    context.user_data.pop("process_prompt", None)
    await update.effective_message.reply_text(
        "✅ Бот на связи.\nПришли прайс текстом или файлом CSV, Excel (.xlsx), TXT до 10 МБ, "
        "затем нажми «Обработать».\n"
        "Можно объединять несколько файлов и сообщений.\n"
        "Для массового пополнения mapping нажми «➕ Пополнить mapping».\n"
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

    response = requests.get(url, headers=headers, timeout=20)
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
        },
        timeout=20,
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



def compare_mvc(output_dir=None):
    today = load_mvc_from_google()
    if not today:
        raise Exception("МВЦ не загрузилась или в колонке M не нашлись цены")

    # Network requests can overlap. Only the shared baseline's read/update is
    # serialized; each price receives its own diff and check files.
    with _mvc_snapshot_lock:
        return _compare_mvc_snapshot(today, Path(output_dir or Path.cwd()))


def _compare_mvc_snapshot(today, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = reports_root() / "mvc_yesterday.csv"

    old = {}
    if baseline.exists():
        with baseline.open("r", encoding="utf-8") as f:
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

    with (output_dir / "mvc_diff.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Вчера", "Сегодня", "Разница", "Вчера/Сегодня", "Статус"])
        writer.writerows(results)

    with (output_dir / "mvc_check.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["SKU", "Вчера", "Сегодня", "Разница", "Вчера/Сегодня", "Статус"])
        writer.writerows(check_results)

    with baseline.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for sku, price in today.items():
            writer.writerow([sku, price])

    return len(check_results)


async def delete_mapping_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item = query.data.replace("delmap:", "")

    result = await asyncio.to_thread(delete_mapping_github, item)

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
    return validate_mapping_without_registry(name, sku, content)


def validate_mapping_entries(entries, mapping_content=None):
    """Validate a group against one current mapping.txt snapshot.

    The catalog remains the source of truth.  Reading mapping.txt once keeps a
    large REVIEW list responsive while still supplying its aliases as an
    additional signal to every validation.
    """
    if mapping_content is None:
        mapping_content, _ = _github_file("mapping.txt")
    return [
        validate_mapping_without_registry(name, sku, mapping_content)
        for name, sku in entries
    ]


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


def record_mapping_history_batch(events, user_id):
    """Append several history events with one GitHub update.

    ``events`` contains ``(ValidationResult, action)`` pairs.  This is used
    for explicit confirmation of a REVIEW list, so a large price does not
    cause one GitHub commit per line.
    """
    if not events:
        return
    try:
        content, sha = _github_file("mapping_history.csv", allow_missing=True)
        line = StringIO()
        writer = csv.writer(line)
        if not content:
            writer.writerow(["date", "telegram_user_id", "action", "source_name", "sku", "catalog_name", "status", "conflicts"])
        timestamp = __import__("datetime").datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        for result, action in events:
            writer.writerow([
                timestamp, user_id, action, result.source_name, result.sku,
                result.catalog_name or "", result.status, result.reason,
            ])
        separator = "" if not content or content.endswith("\n") else "\n"
        _write_github_file(
            "mapping_history.csv", content + separator + line.getvalue(),
            "Record batch mapping history", sha,
        )
    except Exception:
        logger.exception("Не удалось пакетно записать mapping_history.csv")


def save_validated_mapping(name, sku, user_id, override=False):
    """The only mapping.txt write path: revalidates immediately before a GitHub PUT."""
    result = validate_mapping_entry(name, sku)
    if result.status == ValidationStatus.SKU_NOT_FOUND:
        record_mapping_history(result, user_id, "SKU_NOT_FOUND")
        return result, "SKU_NOT_FOUND"
    if result.status == ValidationStatus.BLOCKED:
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


def save_validated_mappings_batch(entries, user_id, action="PRICE_REVIEW_BATCH"):
    """Revalidate and save many REVIEW aliases in one mapping.txt update.

    Each row is checked against the current catalog immediately before saving.
    Only ``OK`` and ``REVIEW`` results are eligible; a newly discovered
    conflict or missing SKU is therefore never written merely because it was
    present in an earlier REVIEW report.
    """
    content, sha = _github_file("mapping.txt")
    mapping_lines = set(content.splitlines())
    additions = []
    outcomes = []

    for name, sku in entries:
        result = validate_mapping_without_registry(name, sku, content)
        if result.status not in {ValidationStatus.OK, ValidationStatus.REVIEW}:
            outcomes.append((name, sku, result, result.status))
            continue
        entry = f"{name.strip()} = {result.sku}"
        if entry in mapping_lines:
            outcomes.append((name, sku, result, "EXISTS"))
            continue
        additions.append(entry)
        mapping_lines.add(entry)
        # Let subsequent rows see aliases accepted in this very batch too.
        content += ("" if not content or content.endswith("\n") else "\n") + entry + "\n"
        outcomes.append((name, sku, result, "ADDED"))

    if additions:
        _write_github_file("mapping.txt", content, "Add validated mapping batch", sha)

    history_events = []
    for _, _, result, status in outcomes:
        history_action = f"{action}_{status}"
        history_events.append((result, history_action))
    record_mapping_history_batch(history_events, user_id)
    return outcomes


def format_validation(result):
    headings = {
        ValidationStatus.OK: "✅ Сопоставление готово к добавлению",
        ValidationStatus.REVIEW: "⚠️ Сопоставление требует проверки",
        ValidationStatus.BLOCKED: "⛔ Название уже связано с другим SKU",
        ValidationStatus.SKU_NOT_FOUND: "⛔ SKU не найден",
    }
    text = (
        f"{headings[result.status]}\n\nТовар из прайса:\n{result.source_name}\n\n"
        f"SKU:\n{result.sku}"
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


async def process_and_reply(update: Update, input_json=None, report_dir=None, job=None):
    directory = Path(report_dir or Path.cwd()).resolve()
    try:
        msg = update.message if job else await update.message.reply_text("⏳ Обрабатываю прайс...")

        await run_parser(
            Path(__file__).with_name("parse_prices.py"), input_json, directory,
        )

        with (directory / "prices_parsed.csv").open("r", encoding="utf-8", newline="") as f:
            processed_rows = list(csv.DictReader(f))
        matched_count = sum(row["Status"] == "OK" for row in processed_rows)
        review_count = sum(row["Status"] == "REVIEW" for row in processed_rows)
        mapping_error_count = sum(
            row["Status"] in {"MAPPING_BLOCKED", "SKU_NOT_FOUND"}
            for row in processed_rows
        )

        not_found_count = 0
        if (directory / "not_found.txt").exists():
            with (directory / "not_found.txt").open("r", encoding="utf-8") as f:
                not_found_count = sum(1 for _ in f)

        if job:
            await asyncio.to_thread(publish_job, job)

        report = (
            f"📊 Прайс обработан\n\n"
            f"✅ Сопоставлено: {matched_count}\n"
            f"📦 Не найдено: {not_found_count}\n"
            f"⚠️ Требует проверки: {review_count}\n"
            f"⛔ Ошибок mapping: {mapping_error_count}"
        )
        review_entries = [
            {"name": row["Название"], "sku": row["SKU"]}
            for row in processed_rows
            if row["Status"] == "REVIEW" and row["SKU"]
        ]
        reply_markup = None
        context = getattr(update, "context", None)
        if review_entries and context is not None:
            token = secrets.token_urlsafe(8)
            context.user_data["price_review_queue"] = {
                "token": token,
                "entries": review_entries,
                "rows": processed_rows,
                "position": 0,
                "confirmed": 0,
                "skipped": 0,
                "not_confirmed": 0,
            }
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"⚠️ Проверить REVIEW списком ({len(review_entries)})",
                    callback_data=f"pricerev:{token}:start",
                )
            ]])
        await msg.edit_text(report, reply_markup=reply_markup)

        try:
            with (directory / "prices_parsed.csv").open("rb") as f:
                await update.message.reply_document(
                    f,
                    filename="prices_parsed.csv",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30
                )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Не удалось отправить prices_parsed.csv: {e}")

        if not_found_count > 0:
            try:
                with (directory / "not_found.txt").open("rb") as f:
                    await update.message.reply_document(
                        f,
                        filename="not_found.txt",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30
                    )
            except Exception as e:
                await update.message.reply_text(f"⚠️ Не удалось отправить not_found.txt: {e}")

        if mapping_error_count > 0:
            try:
                with (directory / "mapping_errors.csv").open("rb") as f:
                    await update.message.reply_document(
                        f,
                        filename="mapping_errors.csv",
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
            except Exception as e:
                await update.message.reply_text(f"⚠️ Не удалось отправить mapping_errors.csv: {e}")

        await update.message.reply_text("🔎 Запускаю проверку МВЦ...")

        try:
            check_count = await asyncio.to_thread(compare_mvc, directory)

            if check_count > 0:
                await update.message.reply_text(
                    f"⚠️ Проверка МВЦ: найдено позиций для проверки: {check_count}"
                )

                with (directory / "mvc_check.csv").open("rb") as f:
                    await update.message.reply_document(
                        f,
                        filename="mvc_check.csv",
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
        logger.exception("Ошибка обработки прайса; job=%s", job.job_id if job else "legacy")
        await update.message.reply_text(f"❌ Ошибка обработки прайса: {e}")
        return False


def _review_report_document(results):
    """Build the human-reviewable REVIEW report without a shared temp file."""
    report = StringIO(newline="")
    writer = csv.writer(report)
    writer.writerow(["Товар из прайса", "SKU", "Эталон SKU", "Статус проверки", "Причина"])
    for result in results:
        writer.writerow([
            result.source_name, result.sku, result.catalog_name or "",
            result.status, result.reason,
        ])
    document = BytesIO(report.getvalue().encode("utf-8-sig"))
    document.name = "review_mapping.csv"
    return document


def _confirmed_price_document(rows):
    output = StringIO(newline="")
    columns = ["Название", "Цена", "Страна", "SKU", "Score", "Status"]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    document = BytesIO(output.getvalue().encode("utf-8-sig"))
    document.name = "prices_parsed_confirmed.csv"
    return document


async def price_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Review all uncertain matches in a price as one report and one action."""
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return
    _, token, action = query.data.split(":", 2)
    queue = context.user_data.get("price_review_queue")
    if not queue or queue.get("token") != token:
        await query.message.reply_text("❌ Этот список REVIEW устарел. Обработай прайс заново.")
        return
    if action == "cancel":
        context.user_data.pop("price_review_queue", None)
        await query.message.edit_text("⏭ REVIEW не добавлены в mapping. Исходный прайс остался без изменений.")
        return
    if action == "start":
        if queue.get("prepared"):
            await query.message.reply_text("⚠️ Список REVIEW уже отправлен. Используй кнопку под ним.")
            return
        entries = [(entry["name"], entry["sku"]) for entry in queue["entries"]]
        try:
            # One mapping.txt snapshot, then feature validation for every row.
            # A second full validation is deliberately performed after the user
            # presses the confirmation button.
            results = await asyncio.to_thread(validate_mapping_entries, entries)
        except Exception:
            logger.exception("Не удалось подготовить список REVIEW из прайса")
            await query.message.reply_text("❌ Не удалось проверить REVIEW. Обработай прайс заново.")
            return

        queue["prepared"] = True
        queue["preflight_results"] = results
        addable = sum(result.status in {ValidationStatus.OK, ValidationStatus.REVIEW} for result in results)
        blocked = sum(result.status == ValidationStatus.BLOCKED for result in results)
        missing = sum(result.status == ValidationStatus.SKU_NOT_FOUND for result in results)
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_document(
            _review_report_document(results), filename="review_mapping.csv",
            read_timeout=120, write_timeout=120, connect_timeout=30, pool_timeout=30,
        )
        keyboard = []
        if addable:
            keyboard.append([InlineKeyboardButton(
                f"✅ Подтвердить и добавить все ({addable})",
                callback_data=f"pricerev:{token}:confirmall",
            )])
        keyboard.append([InlineKeyboardButton(
            "❌ Не добавлять REVIEW", callback_data=f"pricerev:{token}:cancel",
        )])
        await query.message.reply_text(
            f"📋 REVIEW проверены списком: {len(results)}\n\n"
            f"✅ Можно подтвердить: {addable}\n"
            f"⛔ Заблокировано: {blocked}\n"
            f"❓ SKU не найден: {missing}\n\n"
            "В файле review_mapping.csv указана причина для каждой позиции. "
            "После подтверждения бот ещё раз проверит все строки и добавит "
            "разрешённые сопоставления одним обновлением mapping.txt.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    if action != "confirmall" or not queue.get("prepared"):
        return

    entries = [(entry["name"], entry["sku"]) for entry in queue["entries"]]
    try:
        outcomes = await asyncio.to_thread(
            save_validated_mappings_batch, entries, update.effective_user.id,
        )
    except Exception:
        logger.exception("Не удалось пакетно добавить REVIEW в mapping")
        await query.message.reply_text("❌ Не удалось добавить REVIEW в mapping. Ничего не подтверждено.")
        return

    added = sum(status == "ADDED" for _, _, _, status in outcomes)
    exists = sum(status == "EXISTS" for _, _, _, status in outcomes)
    blocked = sum(status == ValidationStatus.BLOCKED for _, _, _, status in outcomes)
    missing = sum(status == ValidationStatus.SKU_NOT_FOUND for _, _, _, status in outcomes)
    confirmed = {
        (name, sku) for name, sku, _, status in outcomes
        if status in {"ADDED", "EXISTS"}
    }
    for row in queue["rows"]:
        if (row["Status"] == "REVIEW" and
                (row["Название"], row["SKU"]) in confirmed):
            row["Status"] = "OK_CONFIRMED"
            row["Score"] = "100"

    context.user_data.pop("price_review_queue", None)
    await query.message.edit_text(
        f"📊 REVIEW добавлены списком:\n\n"
        f"✅ Добавлено: {added}\n"
        f"⚠️ Уже было в mapping: {exists}\n"
        f"⛔ Заблокировано при повторной проверке: {blocked}\n"
        f"❓ SKU не найден: {missing}"
    )
    if confirmed:
        await query.message.reply_document(
            _confirmed_price_document(queue["rows"]),
            filename="prices_parsed_confirmed.csv",
            read_timeout=120, write_timeout=120, connect_timeout=30, pool_timeout=30,
        )


async def show_next_price_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show one uncertain price match and let the user accept it into mapping."""
    queue = context.user_data.get("price_review_queue")
    if not queue:
        return
    if queue["position"] >= len(queue["entries"]):
        context.user_data.pop("price_review_queue", None)
        await update.effective_message.reply_text(
            f"📊 REVIEW в прайсе разобраны:\n\n"
            f"✅ Подтверждено: {queue['confirmed']}\n"
            f"⏭ Пропущено: {queue['skipped']}\n"
            f"⛔ Не подтверждено после повторной проверки: {queue['not_confirmed']}"
        )
        if queue["confirmed"]:
            await update.effective_message.reply_document(
                _confirmed_price_document(queue["rows"]),
                filename="prices_parsed_confirmed.csv",
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )
        return

    entry = queue["entries"][queue["position"]]
    queue["position"] += 1
    try:
        result = await asyncio.to_thread(validate_mapping_entry, entry["name"], entry["sku"])
        await asyncio.to_thread(record_mapping_history, result, update.effective_user.id, "PRICE_REVIEW")
    except Exception:
        logger.exception("Не удалось открыть REVIEW из обработанного прайса")
        queue["not_confirmed"] += 1
        await show_next_price_review(update, context)
        return

    queue["current"] = {"entry": entry, "result": result}
    token = queue["token"]
    keyboard = []
    if result.status in {ValidationStatus.OK, ValidationStatus.REVIEW}:
        keyboard.append([InlineKeyboardButton(
            "✅ Подтвердить и добавить в mapping",
            callback_data=f"pricerevitem:{token}:confirm",
        )])
    keyboard.append([InlineKeyboardButton(
        "⏭ Пропустить",
        callback_data=f"pricerevitem:{token}:skip",
    )])
    await update.effective_message.reply_text(
        f"⚠️ REVIEW {queue['position']}/{len(queue['entries'])}\n\n"
        f"{format_validation(result)}\n\n"
        "Подтверждение добавит сопоставление в mapping.txt и включит позицию "
        "в итоговый файл этого прайса.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def price_review_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return
    _, token, action = query.data.split(":", 2)
    queue = context.user_data.get("price_review_queue")
    if not queue or queue.get("token") != token or not queue.get("current"):
        await query.message.reply_text("❌ Подтверждение устарело. Обработай прайс заново.")
        return

    current = queue.pop("current")
    result = current["result"]
    entry = current["entry"]
    if action == "confirm" and result.status in {ValidationStatus.OK, ValidationStatus.REVIEW}:
        try:
            saved_result, status = await asyncio.to_thread(
                save_validated_mapping, entry["name"], entry["sku"], update.effective_user.id
            )
        except Exception:
            logger.exception("Не удалось добавить подтверждённый REVIEW в mapping")
            queue["not_confirmed"] += 1
            await query.message.edit_text("❌ Не удалось добавить позицию в mapping.")
        else:
            if status in {"ADDED", "EXISTS"}:
                for row in queue["rows"]:
                    if (row["Status"] == "REVIEW" and row["Название"] == entry["name"]
                            and row["SKU"] == entry["sku"]):
                        row["Status"] = "OK_CONFIRMED"
                        row["Score"] = "100"
                queue["confirmed"] += 1
                prefix = "✅ Добавлено в mapping." if status == "ADDED" else "⚠️ Уже есть в mapping."
                await query.message.edit_text(f"{prefix} Позиция включена в этот прайс.")
            else:
                queue["not_confirmed"] += 1
                await query.message.edit_text(
                    f"⛔ Повторная проверка не разрешила добавить позицию.\n\n{format_validation(saved_result)}"
                )
    else:
        queue["skipped"] += 1
        await query.message.edit_text("⏭ Позиция пропущена.")
    await show_next_price_review(update, context)



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

    ready_to_add = []
    review_entries = []
    blocked = 0
    sku_not_found = 0
    errors = len([line for line in lines if line.strip()]) - len(candidates)
    for left, right in candidates:
        try:
            validation = await asyncio.to_thread(validate_mapping_entry, left, right)
            record_mapping_history(validation, update.effective_user.id, "MASS_VALIDATE")
            if validation.status == ValidationStatus.OK:
                # Do not write the whole list immediately.  The employee sees
                # the validation report first and explicitly confirms the safe
                # rows with one button.
                ready_to_add.append((left, right))
            elif validation.status == ValidationStatus.REVIEW:
                review_entries.append((left, right))
            elif validation.status == ValidationStatus.BLOCKED:
                blocked += 1
            else:
                sku_not_found += 1
        except Exception:
            logger.exception("Не удалось проверить массовое сопоставление")
            errors += 1

    if not ready_to_add and not review_entries and blocked == 0 and sku_not_found == 0 and errors == 0:
        return

    report = (
        f"📊 Проверка списка завершена:\n\n"
        f"✅ Готово к добавлению: {len(ready_to_add)}\n"
        f"⚠️ Требует ручной проверки: {len(review_entries)}\n"
        f"⛔ Заблокировано: {blocked}\n"
        f"❓ SKU не найден: {sku_not_found}\n"
        f"❌ Ошибок формата: {errors}"
    )
    token = secrets.token_urlsafe(8)
    context.user_data["pending_bulk_mapping"] = {
        "token": token,
        "entries": ready_to_add,
        "review_entries": review_entries,
    }
    keyboard_rows = []
    if ready_to_add:
        keyboard_rows.append([InlineKeyboardButton(
            f"✅ Добавить проверенные ({len(ready_to_add)})",
            callback_data=f"bulkmap:{token}:add",
        )])
    if review_entries:
        keyboard_rows.append([InlineKeyboardButton(
            f"⚠️ Разобрать REVIEW ({len(review_entries)})",
            callback_data=f"bulkmap:{token}:review",
        )])
    keyboard_rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"bulkmap:{token}:cancel")])
    await update.message.reply_text(
        report + "\n\nСтроки OK можно добавить одной кнопкой. "
        "Для REVIEW откройте позиции и подтвердите или пропустите каждую.",
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def bulk_mapping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commit only the OK rows from a previously validated mapping list."""
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return

    _, token, action = query.data.split(":", 2)
    pending = context.user_data.get("pending_bulk_mapping")
    if not pending or pending.get("token") != token:
        await query.message.reply_text("❌ Подтверждение устарело. Пришли список заново.")
        return
    if action == "cancel":
        context.user_data.pop("pending_bulk_mapping", None)
        context.user_data.pop("bulk_review_queue", None)
        await query.message.edit_reply_markup(reply_markup=None)
        return
    if action == "review":
        if not pending.get("review_entries"):
            await query.message.reply_text("✅ В этом списке не осталось позиций REVIEW.")
            return
        if context.user_data.get("bulk_review_queue"):
            await query.message.reply_text("⚠️ REVIEW уже открыт. Закончи текущую позицию.")
            return
        context.user_data["bulk_review_queue"] = {
            "entries": pending["review_entries"],
            "position": 0,
            "added": 0,
            "exists": 0,
            "skipped": 0,
            "not_added": 0,
        }
        await show_next_bulk_review(update, context)
        return
    if action != "add":
        return

    added = 0
    exists = 0
    not_added = 0
    for name, sku in pending["entries"]:
        try:
            # Revalidation inside save_validated_mapping is the second safety
            # line: a changed catalog or mapping cannot bypass the check.
            _, status = await asyncio.to_thread(
                save_validated_mapping, name, sku, update.effective_user.id
            )
            if status == "ADDED":
                added += 1
            elif status == "EXISTS":
                exists += 1
            else:
                not_added += 1
        except Exception:
            logger.exception("Не удалось добавить строку из массового сопоставления")
            not_added += 1

    pending["entries"] = []
    summary = (
        f"📊 Добавление проверенных завершено:\n\n"
        f"✅ Добавлено: {added}\n"
        f"⚠️ Уже было: {exists}\n"
        f"⛔ Не добавлено после повторной проверки: {not_added}"
    )
    if pending.get("review_entries"):
        await query.message.edit_text(
            summary + f"\n\nОсталось REVIEW: {len(pending['review_entries'])}.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"⚠️ Разобрать REVIEW ({len(pending['review_entries'])})",
                    callback_data=f"bulkmap:{token}:review",
                )],
                [InlineKeyboardButton("❌ Отмена", callback_data=f"bulkmap:{token}:cancel")],
            ]),
        )
    else:
        context.user_data.pop("pending_bulk_mapping", None)
        await query.message.edit_text(summary)


async def show_next_bulk_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show REVIEW rows one at a time so each can be consciously confirmed."""
    queue = context.user_data.get("bulk_review_queue")
    if not queue:
        return
    if queue["position"] >= len(queue["entries"]):
        context.user_data.pop("bulk_review_queue", None)
        pending = context.user_data.get("pending_bulk_mapping")
        if pending:
            pending["review_entries"] = []
            if not pending.get("entries"):
                context.user_data.pop("pending_bulk_mapping", None)
        await update.effective_message.reply_text(
            f"📊 REVIEW разобраны:\n\n"
            f"✅ Добавлено: {queue['added']}\n"
            f"⚠️ Уже было: {queue['exists']}\n"
            f"⏭ Пропущено: {queue['skipped']}\n"
            f"⛔ Не добавлено: {queue['not_added']}"
        )
        return

    name, sku = queue["entries"][queue["position"]]
    queue["position"] += 1
    try:
        result = await asyncio.to_thread(validate_mapping_entry, name, sku)
        record_mapping_history(result, update.effective_user.id, "MASS_REVIEW")
    except Exception:
        logger.exception("Не удалось открыть REVIEW из массового сопоставления")
        queue["not_added"] += 1
        await show_next_bulk_review(update, context)
        return

    token = secrets.token_urlsafe(8)
    context.user_data["pending_mapping"] = {
        "token": token,
        "name": name,
        "sku": sku,
        "bulk_review": True,
    }
    keyboard = []
    if result.status in {ValidationStatus.OK, ValidationStatus.REVIEW}:
        keyboard.append([InlineKeyboardButton("✅ Добавить", callback_data=f"map:{token}:add")])
    keyboard.append([InlineKeyboardButton("⏭ Пропустить", callback_data=f"map:{token}:cancel")])
    await update.effective_message.reply_text(
        f"⚠️ REVIEW {queue['position']}/{len(queue['entries'])}\n\n{format_validation(result)}",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
    bulk_review = pending.get("bulk_review", False)
    if action == "cancel":
        context.user_data.pop("pending_mapping", None)
        await query.message.edit_reply_markup(reply_markup=None)
        if bulk_review:
            queue = context.user_data.get("bulk_review_queue")
            if queue:
                queue["skipped"] += 1
            await show_next_bulk_review(update, context)
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
    if bulk_review:
        queue = context.user_data.get("bulk_review_queue")
        if queue:
            if status == "ADDED":
                queue["added"] += 1
            elif status == "EXISTS":
                queue["exists"] += 1
            else:
                queue["not_added"] += 1
        await show_next_bulk_review(update, context)


async def show_not_found(update: Update, context: ContextTypes.DEFAULT_TYPE):
    directory = latest_report_directory(
        reports_root(), update.effective_user.id, update.effective_chat.id,
    )
    if directory is None or not (directory / "not_found.txt").exists():
        await update.message.reply_text("📦 Сначала обработай свой прайс — здесь появятся его ненайденные товары.")
        return

    with (directory / "not_found.txt").open("r", encoding="utf-8") as f:
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
    from v2_bridge import audit_mapping
    await audit_mapping(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return

    entries = [line.strip() for line in update.message.text.splitlines() if line.strip()]
    await add_price_entries(update, context, entries)


async def add_price_entries(update, context, entries, from_file=False):
    chat_id = update.effective_chat.id
    data = user_store.setdefault(price_owner_key(update), [])
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

    # A supplier price is often sent as several messages/files.  Keep one live
    # processing prompt rather than filling the chat with identical buttons.
    prompt = context.user_data.get("process_prompt")
    if prompt and prompt.get("chat_id") == chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=prompt["message_id"],
                text=message_text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            # The user may have deleted the old bot message.  A new prompt is
            # safer than leaving a collected price without an action button.
            logger.info("Не удалось обновить сообщение обработки; создаю новое")
            context.user_data.pop("process_prompt", None)

    message = await update.message.reply_text(
        message_text,
        reply_markup=reply_markup
    )
    context.user_data["process_prompt"] = {
        "chat_id": chat_id,
        "message_id": message.message_id,
    }


async def done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await check_access(update):
        return

    active = context.user_data.get("active_price_job")
    if active:
        await query.message.reply_text(
            "⏳ Твой прайс уже обрабатывается. Результат придёт сюда. "
            "Новые позиции сохранены для следующей обработки."
        )
        return

    owner_key = price_owner_key(update)
    data = list(user_store.get(owner_key, []))

    if not data:
        await query.message.reply_text("❌ Нет данных для обработки")
        return

    job = await asyncio.to_thread(
        create_job, reports_root(), update.effective_user.id, update.effective_chat.id, data,
    )
    await query.message.edit_text("⏳ Обрабатываю прайс. Можно отправлять позиции для следующего прайса.")
    user_store[owner_key] = []
    context.user_data.pop("data", None)
    context.user_data.pop("process_prompt", None)
    context.user_data["active_price_job"] = job.job_id
    context.application.create_task(
        process_price_job(update, context, job, owner_key, data), update=update,
    )


async def process_price_job(update, context, job, owner_key, entries):
    from types import SimpleNamespace

    started = time.monotonic()
    logger.info("Начата обработка прайса: job=%s user_id=%s rows=%s",
                job.job_id, job.owner_id, len(entries))
    succeeded = False
    try:
        succeeded = await process_and_reply(
            SimpleNamespace(message=update.callback_query.message, context=context),
            input_json=job.directory / "prices_input.json", report_dir=job.directory, job=job,
        )
    finally:
        if not succeeded:
            user_store[owner_key] = entries + user_store.get(owner_key, [])
        if context.user_data.get("active_price_job") == job.job_id:
            context.user_data.pop("active_price_job", None)
        logger.info("Завершена обработка прайса: job=%s user_id=%s success=%s seconds=%.2f",
                    job.job_id, job.owner_id, succeeded, time.monotonic() - started)
    if not succeeded:
        await update.callback_query.message.reply_text(
            "Прайс сохранён. Можно повторить обработку.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Обработать", callback_data="done")]]),
        )
    



def build_application():
    if not TOKEN or not TOKEN.strip():
        raise RuntimeError("На сервере не задана переменная TOKEN")
    app = (ApplicationBuilder().token(TOKEN)
           .concurrent_updates(UserUpdateProcessor())
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
    app.add_handler(CallbackQueryHandler(price_review_item_callback, pattern="^pricerevitem:"))
    app.add_handler(CallbackQueryHandler(price_review_callback, pattern="^pricerev:"))
    app.add_handler(CallbackQueryHandler(bulk_mapping_callback, pattern="^bulkmap:"))
    app.add_handler(CallbackQueryHandler(mapping_validation_callback, pattern="^map:"))
    app.add_handler(CallbackQueryHandler(done_button, pattern="done"))
    app.add_error_handler(log_error)
    return app


def main():
    configure_logging()
    logger.info("Запускаю бота. Подключаюсь к Telegram...")
    try:
        app = build_application()
        install_safe_pipeline(app, check_access)
        # Явно запрашиваем сообщения и кнопки независимо от прежних настроек API.
        app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=0)
    except Exception:
        logger.exception("Не удалось запустить бота")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
