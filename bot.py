ALLOWED_USERS = [800906903,686105512,5652216103]  # сюда свой ID
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

import os
TOKEN = os.getenv("TOKEN")
user_data_store = {}

# 🚀 универсальная функция обработки
async def process_and_reply(update: Update):
    try:
        # сообщение "обрабатываю"
        msg = await update.message.reply_text("⏳ Обрабатываю прайс...")

        # запускаем парсер
        os.system("python3 parse_prices.py")

        # считаем строки
        total = 0
        with open("prices_parsed.csv", "r", encoding="utf-8") as f:
            total = sum(1 for _ in f) - 1  # без заголовка

        # считаем not_found
        not_found_count = 0
        if os.path.exists("not_found.txt"):
            with open("not_found.txt", "r", encoding="utf-8") as f:
                not_found_count = sum(1 for _ in f)

        # обновляем сообщение
        await msg.edit_text(
            f"✅ Готово!\n\n"
            f"📦 Обработано: {total}\n"
            f"❌ Не найдено: {not_found_count}"
        )

        # отправляем CSV
        with open("prices_parsed.csv", "rb") as f:
            await update.message.reply_document(f)

        # отправляем not_found если есть
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

    await update.message.reply_text("📩 Добавлено. Когда закончишь — напиши /done")
    async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in user_data_store or not user_data_store[user_id]:
        await update.message.reply_text("❌ Нет данных для обработки")
        return

    # объединяем все сообщения
    full_text = "\n".join(user_data_store[user_id])

    with open("prices_utf8.txt", "w", encoding="utf-8") as f:
        f.write(full_text)

    # очищаем память
    user_data_store[user_id] = []

    # запускаем парсер
    await process_and_reply(update)


# 🚀 запуск
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
app.add_handler(MessageHandler(filters.TEXT, handle_text))
from telegram.ext import CommandHandler
app.add_handler(CommandHandler("done", done))


print("🤖 Бот запущен...")

app.run_polling()
