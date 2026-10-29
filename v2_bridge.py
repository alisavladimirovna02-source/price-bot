"""Telegram mapping controls. Supplier prices stay in the legacy collector."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

_SOURCE_ROOT = Path(__file__).with_name("src")
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from mapping_only import check_entries, github_repository, parse_entries, save_entries
from parse_prices import normalize_text
from price_bot.repositories import parse_mapping_text


ROOT = Path(os.environ.get("PRICE_BOT_ROOT", Path.cwd()))
MAPPING_PATH = Path(os.environ.get("MAPPING_PATH", ROOT / "mapping.txt"))
ADMIN_USERS = frozenset(
    int(value) for value in os.environ.get("ADMIN_USERS", "800906903").split(",")
    if value.strip().isdigit()
)
BUTTON_GUIDE = "ℹ️ Инструкция"
BUTTON_AUDIT_MAPPING = "🗂 Проверить mapping"
BUTTON_ADD_MAPPING = "➕ Пополнить mapping"
MENU_BUTTONS = (BUTTON_GUIDE, BUTTON_AUDIT_MAPPING, BUTTON_ADD_MAPPING)
DISABLED_BUTTONS = (
    "🧾 Аудит SKU", "🔎 Найти SKU", "➕ Добавить SKU",
    "📤 Выгрузить SKU", "📥 Загрузить SKU",
)
logger = logging.getLogger("price_bot.mapping")
_access_check = None


@dataclass(frozen=True)
class PendingMappings:
    user_id: int
    chat_id: int
    entries: tuple[tuple[str, str], ...]
    created_at: float


def install(application: Application, access_check) -> None:
    global _access_check
    _access_check = access_check
    # This group runs before the generic price and mapping handlers.
    group = -3
    application.add_handler(CommandHandler(["cancel", "start"], reset_mapping_mode), group=group)
    application.add_handler(CommandHandler("mapping_add", stop_after(mapping_add)), group=group)
    application.add_handler(CommandHandler("audit_mapping_v2", stop_after(audit_mapping)), group=group)
    application.add_handler(CommandHandler(
        ["audit_sku", "sku_add", "sku_find", "sku_export", "sku_import"],
        stop_after(registry_disabled),
    ), group=group)
    application.add_handler(CallbackQueryHandler(mapping_batch_callback, pattern=r"^v2bulkmap:"), group=group)
    application.add_handler(CallbackQueryHandler(registry_disabled_callback, pattern=r"^v2(?:reg|map):"), group=group)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(_menu_pattern()), menu_button), group=group)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(r"^📦 Не найдено$"), text), group=group)


def stop_after(handler):
    async def wrapped(update, context):
        await handler(update, context)
        raise ApplicationHandlerStop
    return wrapped


async def _allowed(update: Update) -> bool:
    return bool(_access_check and await _access_check(update))


async def _admin(update: Update) -> bool:
    if not await _allowed(update):
        return False
    if not update.effective_user or update.effective_user.id not in ADMIN_USERS:
        await update.effective_message.reply_text("Команда доступна только администратору.")
        return False
    return True


async def reset_mapping_mode(update, context):
    if not await _allowed(update):
        raise ApplicationHandlerStop
    active = bool(context.user_data.pop("v2_menu_action", None))
    active = bool(context.user_data.pop("pending_mappings", None)) or active
    if active and (update.effective_message.text or "").split()[0].split("@")[0] == "/cancel":
        await update.effective_message.reply_text("Ввод отменён. Можно отправлять прайс.")
        raise ApplicationHandlerStop


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update) or not update.message:
        return
    body = update.message.text or ""
    if context.user_data.get("v2_menu_action") == "add_mapping":
        try:
            entries = parse_entries(body)
            await offer_mapping_batch(update, context, entries)
            context.user_data.pop("v2_menu_action", None)
        except ValueError as error:
            await update.effective_message.reply_text(str(error))
        raise ApplicationHandlerStop
    if "wb_zero" in context.user_data:
        return
    if context.user_data.get("mapping_item"):
        sku = body.rsplit("=", 1)[-1].strip()
        name = context.user_data.pop("mapping_item")
        try:
            await offer_mapping_batch(update, context, parse_entries(f"{name} = {sku}"))
        except ValueError as error:
            await update.effective_message.reply_text(str(error))
        raise ApplicationHandlerStop
    nonempty = [line for line in body.splitlines() if line.strip()]
    if nonempty and all(line.count("=") == 1 for line in nonempty):
        try:
            await offer_mapping_batch(update, context, parse_entries(body))
        except ValueError as error:
            await update.effective_message.reply_text(str(error))
        raise ApplicationHandlerStop
    # Plain text and files are collected by the existing price handlers.


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update) or not update.message:
        return
    button = (update.message.text or "").strip()
    if button == BUTTON_GUIDE:
        await update.effective_message.reply_text(
            "Пришли прайс текстом, CSV, XLSX или TXT и нажми «Обработать». "
            "Бот сопоставляет товары по однозначным названиям из mapping.txt. "
            "Неизвестные товары попадут в «Не найдено».\n\n"
            "Чтобы добавить соответствия, нажми «➕ Пополнить mapping» и пришли "
            "строки «название поставщика = SKU». После предпросмотра подтверди запись. "
            "«🗂 Проверить mapping» покажет повторяющиеся названия с разными SKU."
        )
    elif button == BUTTON_AUDIT_MAPPING:
        await audit_mapping(update, context)
    elif button == BUTTON_ADD_MAPPING:
        await mapping_add(update, context)
    elif button in DISABLED_BUTTONS:
        await registry_disabled(update, context)
    raise ApplicationHandlerStop


async def registry_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if await _allowed(update):
        await update.effective_message.reply_text(
            "Registry отключён. Для нового сопоставления используй «➕ Пополнить mapping»."
        )


async def registry_disabled_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _allowed(update) or not update.callback_query:
        raise ApplicationHandlerStop
    await update.callback_query.answer("Registry отключён", show_alert=True)
    raise ApplicationHandlerStop


async def mapping_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update):
        return
    body = (update.effective_message.text or "").split(maxsplit=1)
    if len(body) == 1 or not (update.effective_message.text or "").startswith("/mapping_add "):
        context.user_data["v2_menu_action"] = "add_mapping"
        await update.effective_message.reply_text(
            "Пришли список: название поставщика = SKU, по одной записи в строке. "
            "Сначала покажу предпросмотр. /cancel — выйти."
        )
    else:
        try:
            await offer_mapping_batch(update, context, parse_entries(body[1]))
        except ValueError as error:
            await update.effective_message.reply_text(str(error))


async def offer_mapping_batch(update, context, entries):
    # Preview the current GitHub state. Commit checks it again to catch races.
    try:
        repository = github_repository()
        content, _ = await asyncio.to_thread(repository._get_file, repository.mapping_path, missing_ok=True)
    except Exception:
        logger.exception("Не удалось прочитать mapping из GitHub")
        await update.effective_message.reply_text("❌ Не удалось прочитать mapping.txt из GitHub. Повтори попытку позже.")
        return
    checks = check_entries(entries, content)
    token = uuid4().hex[:20]
    context.user_data["pending_mappings"] = (
        token, PendingMappings(update.effective_user.id, update.effective_chat.id, tuple(entries), time.monotonic())
    )
    icons = {"ADD": "✅", "EXISTS": "↩️", "CONFLICT": "⛔"}
    full = "\n".join(f"{icons[item.status]} {item.name} = {item.sku}" for item in checks)
    preview = full
    if len(full) > 2400:
        await update.effective_message.reply_document(io.BytesIO(full.encode("utf-8")), filename="mapping_preview.txt")
        preview = "Полный список — в файле выше."
    conflicts = sum(item.status == "CONFLICT" for item in checks)
    message = (
        f"Пополнение mapping: {len(checks)} записей\n"
        f"Новых: {sum(item.status == 'ADD' for item in checks)}; уже есть: "
        f"{sum(item.status == 'EXISTS' for item in checks)}; конфликтов: {conflicts}\n\n"
        f"{preview}\n\n"
        + ("Исправь конфликтующие названия и отправь список заново. " if conflicts else "После подтверждения запишу список в GitHub. ")
        + "Подтверждение действует 15 минут."
    )
    buttons = [] if conflicts else [[InlineKeyboardButton("✅ Подтвердить mapping", callback_data=f"v2bulkmap:{token}:save")]]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"v2bulkmap:{token}:cancel")])
    await update.effective_message.reply_text(message, reply_markup=InlineKeyboardMarkup(buttons))


async def mapping_batch_callback(update, context):
    if not await _allowed(update) or not update.callback_query:
        raise ApplicationHandlerStop
    query = update.callback_query
    await query.answer()
    _, token, action = query.data.split(":", 2)
    saved = context.user_data.get("pending_mappings")
    if not saved or saved[0] != token:
        await query.message.reply_text("Подтверждение устарело. Открой /mapping_add заново.")
        raise ApplicationHandlerStop
    pending = saved[1]
    if pending.user_id != update.effective_user.id or pending.chat_id != update.effective_chat.id:
        raise ApplicationHandlerStop
    if action == "cancel":
        context.user_data.pop("pending_mappings", None)
        await query.edit_message_text("Пополнение mapping отменено.")
        raise ApplicationHandlerStop
    if action != "save":
        raise ApplicationHandlerStop
    context.user_data.pop("pending_mappings", None)
    if time.monotonic() - pending.created_at > 900:
        await query.edit_message_text("Предпросмотр устарел. Отправь список заново через /mapping_add.")
        raise ApplicationHandlerStop
    try:
        checks = await asyncio.to_thread(save_entries, list(pending.entries), MAPPING_PATH)
    except Exception:
        logger.exception("Не удалось сохранить mapping в GitHub")
        await query.edit_message_text("❌ Не удалось сохранить mapping в GitHub. Повтори попытку через /mapping_add.")
        raise ApplicationHandlerStop
    if any(item.status == "CONFLICT" for item in checks):
        await query.edit_message_text("⛔ Mapping изменился после предпросмотра. Есть конфликт названия и SKU; открой /mapping_add заново.")
    else:
        await query.edit_message_text(
            "Mapping сохранён в GitHub и обновлён локально.\n"
            f"✅ Добавлено: {sum(item.status == 'ADD' for item in checks)}\n"
            f"↩️ Уже было: {sum(item.status == 'EXISTS' for item in checks)}"
        )
    raise ApplicationHandlerStop


async def audit_mapping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not await _admin(update):
        return
    try:
        repository = github_repository()
        content, _ = await asyncio.to_thread(repository._get_file, repository.mapping_path, missing_ok=True)
    except Exception:
        logger.exception("Не удалось прочитать mapping для аудита")
        await update.effective_message.reply_text("❌ Не удалось прочитать mapping.txt из GitHub.")
        return
    entries = parse_mapping_text(content)
    by_name: dict[str, set[str]] = {}
    for item in entries:
        by_name.setdefault(normalize_text(item.name), set()).add(item.sku)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Название mapping", "SKU", "Статус"])
    for item in entries:
        writer.writerow([item.name, item.sku, "CONFLICT" if len(by_name[normalize_text(item.name)]) > 1 else "OK"])
    payload = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    await update.effective_message.reply_text(
        f"Проверено записей: {len(entries)}. Конфликтующих названий: {sum(len(values) > 1 for values in by_name.values())}."
    )
    await update.effective_message.reply_document(payload, filename="mapping_audit.csv")


def _menu_pattern() -> str:
    import re
    return r"^(?:" + "|".join(re.escape(value) for value in MENU_BUTTONS + DISABLED_BUTTONS) + r")$"
