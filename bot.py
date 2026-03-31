ALLOWED_USERS = [800906903, 686105512, 5652216103]

import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler
)

TOKEN = os.getenv("TOKEN")
user_data_store = {}

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
    user_id = update.message.from_user.id
    text = update.message.text

    if user_id not in user_data_store:
        user_data_store[user_id] = []

    user_data_store[user_id].append(text)

    keyboard = [
        [InlineKeyboardButton("✅ Готово", callback_data="done")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "📩 Добавлено. Нажми кнопку, когда закончишь",
        reply_markup=reply_markup
    )


# команда (оставляем)
async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in user_data_store or not user_data_store[user_id]:
        await update.message.reply_text("❌ Нет данных для обработки")
        return

    full_text = "\n".join(user_data_store[user_id])

    with open("prices_utf8.txt", "w", encoding="utf-8") as f:
        f.write(full_text)

    user_data_store[user_id] = []

    await process_and_reply(update)


# ✅ кнопка
async def done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    if user_id not in user_data_store or not user_data_store[user_id]:
        await query.message.reply_text("❌ Нет данных для обработки")
        return

    full_text = "\n".join(user_data_store[user_id])

    with open("prices_utf8.txt", "w", encoding="utf-8") as f:
        f.write(full_text)

    user_data_store[user_id] = []

    # 🔥 важно: передаём message в process
    fake_update = update
    fake_update.message = query.message

    await query.message.reply_text("⏳ Обрабатываю...")
    await process_and_reply(fake_update)


# 🚀 запуск
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
app.add_handler(MessageHandler(filters.TEXT, handle_text))
app.add_handler(CommandHandler("done", done))
app.add_handler(CallbackQueryHandler(done_button, pattern="done"))

print("🤖 Бот запущен...")

app.run_polling()
