ALLOWED_USERS = [800906903, 686105512, 5652216103, 7434891167]

user_store = {}

import os
import requests
import base64

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

TOKEN = os.getenv("TOKEN")


# 🔥 GitHub обновление mapping
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


# 🚀 универсальная функция обработки
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


# 📥 файл
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    await file.download_to_drive("prices_utf8.txt")

    await process_and_reply(update)


# 🧠 обучение mapping
async def handle_mapping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USERS:
        return

    text = update.message.text.strip()

    if "=" not in text:
        return

    try:
        left, right = text.split("=", 1)

        left = left.strip()
        right = right.strip()

        entry = f"{left} = {right}"

        result = update_mapping_github(entry)

        if result == "EXISTS":
            await update.message.reply_text("⚠️ Уже есть в mapping")
        else:
            await update.message.reply_text(
                f"✅ Добавлено в GitHub:\n{left} → {right}"
            )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")


# 📩 текст
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USERS:
        return

    text = update.message.text
    chat_id = update.effective_chat.id

    if chat_id not in user_store:
        user_store[chat_id] = []

    context.user_data["data"] = user_store[chat_id]

    lines = text.split("\n")

    for line in lines:
        line = line.strip()
        if line:
            context.user_data["data"].append(line)

    count = len(context.user_data["data"])

    keyboard = [
        [InlineKeyboardButton("🚀 Обработать", callback_data="done")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (
        f"📦 Добавлено позиций: {count}\n\n"
        f"Отправь ещё или нажми кнопку 👇"
    )

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
        reply_markup=reply_markup
    )

    context.user_data["last_msg_id"] = msg.message_id


# ✅ кнопка
async def done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    data = user_store.get(chat_id, [])

    print("STORE:", user_store)
    print("CHAT:", chat_id)

    if not data:
        await query.message.reply_text("❌ Нет данных для обработки")
        return

    full_text = "\n".join(data)

    with open("prices_utf8.txt", "w", encoding="utf-8") as f:
        f.write(full_text)

    user_store[chat_id] = []
    context.user_data.pop("last_msg_id", None)

    await query.message.edit_text("⏳ Обрабатываю...")

    class FakeUpdate:
        def __init__(self, message):
            self.message = message

    fake_update = FakeUpdate(query.message)

    await process_and_reply(fake_update)


# 🚀 запуск
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r".+=.+"), handle_mapping))
app.add_handler(MessageHandler(filters.TEXT, handle_text))
app.add_handler(CallbackQueryHandler(done_button, pattern="done"))

print("🤖 Бот запущен...")

app.run_polling()
