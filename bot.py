ALLOWED_USERS = [800906903, 686105512, 5652216103, 7434891167]

user_store = {}

import os
import requests
import base64
from telegram import ReplyKeyboardMarkup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [["📦 Не найдено"]],
        resize_keyboard=True
    )

TOKEN = os.getenv("TOKEN")


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
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    path = "mapping.txt"

    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    headers = {
        "Authorization": f"token {token}"
    }

    response = requests.get(url, headers=headers)
    data = response.json()

    print("STATUS:", response.status_code)
    print("DATA:", data)

    if "content" not in data:
        return "ERROR"

    content = base64.b64decode(data["content"]).decode("utf-8")

    if new_entry in content:
        return "EXISTS"

    updated_content = content + "\n" + new_entry
    encoded_content = base64.b64encode(updated_content.encode()).decode()

    requests.put(
        url,
        headers=headers,
        json={
            "message": f"add mapping: {new_entry}",
            "content": encoded_content,
            "sha": data["sha"]
        }
    )

    return "ADDED"


async def not_found_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_not_found(update, context)


async def process_and_reply(update: Update):
    try:
        msg = await update.message.reply_text("⏳ Обрабатываю прайс...")

        os.system("python3 parse_prices.py")

        total = 0
        with open("prices_parsed.csv", "r", encoding="utf-8") as f:
            total = sum(1 for _ in f) - 1

        not_found_count = 0
        if os.path.exists("not_found.txt"):
            with open("not_found.txt", "r", encoding="utf-8") as f:
                not_found_count = sum(1 for _ in f)

        await msg.edit_text(
            f"✅ Готово!\n\n"
            f"📦 Обработано: {total}\n"
            f"❌ Не найдено: {not_found_count}"
        )

        with open("prices_parsed.csv", "rb") as f:
            await update.message.reply_document(f)

        if not_found_count > 0:
            with open("not_found.txt", "rb") as f:
                await update.message.reply_document(f)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    await file.download_to_drive("prices_utf8.txt")

    await process_and_reply(update)


async def handle_mapping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USERS:
        return

    text = update.message.text.strip()

    if "mapping_item" in context.user_data:
        item = context.user_data["mapping_item"]

        if "=" not in text:
            await update.message.reply_text("❌ Напиши в формате: товар = sku")
            return

        _, right = text.split("=", 1)
        right = right.strip()

        entry = f"{item} = {right}"

        update_mapping_github(entry)

        context.user_data.pop("mapping_item")

        await update.message.reply_text(f"✅ Добавлено:\n{entry}")
        return

    if "=" not in text:
        return

    try:
        left, right = text.split("=", 1)

        entry = f"{left.strip()} = {right.strip()}"

        result = update_mapping_github(entry)

        if result == "ERROR":
            await update.message.reply_text("❌ Ошибка GitHub")
        elif result == "EXISTS":
            await update.message.reply_text("⚠️ Уже есть в mapping")
        else:
            await update.message.reply_text(f"✅ Добавлено:\n{entry}")

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")


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

    if hasattr(source, "data"):
        await source.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await source.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def add_mapping_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    item = query.data.replace("addmap:", "")
    context.user_data["mapping_item"] = item

    await query.message.reply_text(
        f"✏️ Введи SKU для:\n{item}\n\nПример:\n{item} = sku123"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USERS:
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    if chat_id not in user_store:
        user_store[chat_id] = []

    context.user_data["data"] = user_store[chat_id]

    for line in text.split("\n"):
        line = line.strip()
        if line:
            context.user_data["data"].append(line)

    count = len(context.user_data["data"])

    keyboard = [[InlineKeyboardButton("🚀 Обработать", callback_data="done")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = f"📦 Добавлено позиций: {count}\n\nОтправь ещё или нажми кнопку 👇"

    if "last_msg_id" in context.user_data:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=context.user_data["last_msg_id"],
                text=message_text,
                reply_markup=reply_markup
            )
            return
        except:
            pass

    msg = await update.message.reply_text(
        message_text,
        reply_markup=get_main_keyboard()
    )

    context.user_data["last_msg_id"] = msg.message_id


async def done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    data = user_store.get(chat_id, [])

    if not data:
        await query.message.reply_text("❌ Нет данных для обработки")
        return

    with open("prices_utf8.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(data))

    user_store[chat_id] = []
    context.user_data.pop("last_msg_id", None)

    await query.message.edit_text("⏳ Обрабатываю...")

    class FakeUpdate:
        def __init__(self, message):
            self.message = message

    await process_and_reply(FakeUpdate(query.message))


app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.TEXT & filters.Regex("📦 Не найдено"), not_found_button))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".+=.+"), handle_mapping))
app.add_handler(MessageHandler(filters.TEXT, handle_text))

app.add_handler(CallbackQueryHandler(delete_mapping_button, pattern="delmap:"))
app.add_handler(CallbackQueryHandler(add_mapping_button, pattern="addmap:"))
app.add_handler(CallbackQueryHandler(not_found_nav, pattern="nf_"))
app.add_handler(CallbackQueryHandler(done_button, pattern="done"))

print("🤖 Бот запущен...")
app.run_polling()
