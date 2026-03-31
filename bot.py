ALLOWED_USERS = [800906903, 686105512, 5652216103]

user_store = {}

import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

TOKEN = os.getenv("TOKEN")


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


# 📩 текст
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if chat_id not in user_store:
        user_store[chat_id] = []

    context.user_data["data"] = user_store[chat_id]

    # 🔥 разбиваем на строки
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

    # 👉 редактируем одно сообщение
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

    # очистка
    user_store[chat_id] = []
    context.user_data.pop("last_msg_id", None)

    await query.message.edit_text("⏳ Обрабатываю...")

    # создаём "фейковый" update
    class FakeUpdate:
        def __init__(self, message):
            self.message = message

    fake_update = FakeUpdate(query.message)

    await process_and_reply(fake_update)


# 🚀 запуск
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
app.add_handler(MessageHandler(filters.TEXT, handle_text))
app.add_handler(CallbackQueryHandler(done_button, pattern="done"))

print("🤖 Бот запущен...")

app.run_polling()
