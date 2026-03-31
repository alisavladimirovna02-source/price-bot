ALLOWED_USERS = [800906903,686105512]  # сюда свой ID
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

import os
TOKEN = os.getenv("TOKEN")

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

    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ У вас нет доступа")
        return

    text = update.message.text

    with open("prices_utf8.txt", "w", encoding="utf-8") as f:
        f.write(text)

    await process_and_reply(update)


# 🚀 запуск
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
app.add_handler(MessageHandler(filters.TEXT, handle_text))

print("🤖 Бот запущен...")

app.run_polling()
