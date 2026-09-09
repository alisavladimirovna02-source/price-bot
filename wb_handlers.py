"""Private Telegram workflow for WB stock zeroing, isolated from price input."""

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
import logging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, TypeHandler

from wb_api import WBError, execute_plan, list_warehouses, load_accounts, parse_articles, prepare_plan


BUTTON = "🟠 Обнулить остатки WB"
KEY = "wb_zero"
logger = logging.getLogger("price_bot.wb")


@dataclass
class Workflow:
    chat_id: int
    accounts: list
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))
    phase: str = "account"
    account: object = None
    warehouses: list = field(default_factory=list)
    selected: set = field(default_factory=set)
    articles: list = field(default_factory=list)
    plan: object = None
    page: int = 0
    touched_at: float = field(default_factory=time.monotonic)


def button(state, label, action):
    return InlineKeyboardButton(label, callback_data=f"wb:{state.nonce}:{action}")


def keyboard(state, rows=()):
    return InlineKeyboardMarkup([*rows, [button(state, "Отмена", "cancel")]])


def warehouse_view(state):
    start = state.page * 8
    rows = []
    for item in state.warehouses[start:start + 8]:
        label = ("✅ " if item["id"] in state.selected else "⬜️ ") + item["name"]
        rows.append([button(state, label[:60], f"wh:{item['id']}")])
    nav = []
    if state.page:
        nav.append(button(state, "← Назад", "prev"))
    if start + 8 < len(state.warehouses):
        nav.append(button(state, "Дальше →", "next"))
    if nav:
        rows.append(nav)
    rows += [[button(state, "Выбрать все склады", "all")],
             [button(state, "Продолжить", "warehouses_done")]]
    return (f"Кабинет: {state.account.name}\nВыбери склады продавца.\n"
            f"Выбрано: {len(state.selected)}. Остатки на складах WB не меняются.",
            keyboard(state, rows))


def report_file(plan, results=None):
    # Plain text preserves seller codes verbatim without spreadsheet formulas.
    lines = [f"Кабинет: {plan.account.name}",
             "Результат обнуления" if results is not None else "Предпросмотр обнуления",
             "Одна строка на размер товара и склад.", ""]
    result_map = {(item.row.warehouse_id, item.row.chrt_id): item for item in results or []}
    for row in plan.rows:
        before = "нет данных" if row.before is None else str(row.before)
        text = (f"{row.article} | WB {row.nm_id} | размер {row.size} | ID размера {row.chrt_id} | "
                f"склад {row.warehouse_name} (ID {row.warehouse_id}) | было {before}")
        if results is None:
            text += " → задать 0"
        else:
            item = result_map[(row.warehouse_id, row.chrt_id)]
            after = "нет данных" if item.after is None else str(item.after)
            text += f" | {item.status} | сейчас {after}"
            if item.detail:
                text += f" | {item.detail}"
        lines.append(text)
    if plan.issues:
        lines += ["", "Пропущены (остатки не меняются):"]
        lines.extend(f"{article}: {reason}" for article, reason in plan.issues.items())
    return BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


async def send_report(message, plan, results=None):
    await message.reply_document(
        report_file(plan, results),
        filename="wb_zero_result.txt" if results is not None else "wb_zero_preview.txt",
    )


async def show_input(message, state):
    await message.reply_text(
        f"Кабинет: {state.account.name}\nСкладов выбрано: {len(state.selected)}.\n"
        "Пришли артикулы продавца: один артикул в строке, без цен.\n"
        "Можно отправить несколько сообщений. У выбранных товаров будут обнулены все размеры.\n"
        f"В списке: {len(state.articles)}.",
        reply_markup=keyboard(state, [[button(state, "Проверить товары и остатки", "prepare")]]),
    )


async def prepare_worker(message, context, state):
    try:
        warehouses = [item for item in state.warehouses if item["id"] in state.selected]
        plan = await asyncio.to_thread(prepare_plan, state.account, warehouses, tuple(state.articles))
        if context.user_data.get(KEY) is not state:
            return
        state.plan = plan
        await send_report(message, plan)
        if context.user_data.get(KEY) is not state:
            return
        if not plan.rows:
            state.phase = "input"
            await message.reply_text("Не найдено товаров для обнуления. Причины — в файле.")
            await show_input(message, state)
            return
        known = sum(row.before for row in plan.rows if row.before is not None)
        unknown = sum(row.before is None for row in plan.rows)
        articles = list(dict.fromkeys(row.article for row in plan.rows))
        names = list(dict.fromkeys(row.warehouse_name for row in plan.rows))
        sample = "\n".join(f"• {article}" for article in articles[:8])
        if len(articles) > 8:
            sample += f"\n… ещё {len(articles) - 8}, полный список в файле."
        warehouse_text = ", ".join(names)
        if len(warehouse_text) > 500:
            warehouse_text = warehouse_text[:500] + "… (полный список в файле)"
        await message.reply_text(
            f"Обнуление в кабинете «{state.account.name}»\nСклады: {warehouse_text}\n\n"
            f"{sample}\n\nТоваров: {len(articles)}. Записей по размерам и складам: {len(plan.rows)}.\n"
            f"Известный остаток: {known} шт. Без данных об остатке: {unknown} записей.\n"
            f"Пропущено артикулов: {len(plan.issues)} — причины в файле.\n\n"
            "Проверь файл выше. Кнопка ниже выставит 0 только для перечисленных в нём товаров и складов.\n"
            "Подтверждение действует 15 минут.",
            reply_markup=keyboard(state, [[button(state, "Подтвердить обнуление", "confirm")],
                                          [button(state, "Изменить список", "edit")]]),
        )
        state.phase = "preview"
        state.touched_at = time.monotonic()
    except WBError as error:
        if context.user_data.get(KEY) is state:
            state.phase = "input"
            await message.reply_text(f"Не удалось подготовить обнуление: {error}")
            await show_input(message, state)
    except Exception:
        logger.exception("Ошибка подготовки обнуления WB")
        if context.user_data.get(KEY) is state:
            state.phase = "input"
            await message.reply_text("Не удалось подготовить или отправить предпросмотр. Остатки не менялись. /cancel — выйти.")


async def execute_worker(message, context, state):
    try:
        results = await asyncio.to_thread(execute_plan, state.plan)
        # Retain the report for /wb_result if Telegram delivery fails.
        context.user_data["wb_last_result"] = (state.plan, results)
        await send_report(message, state.plan, results)
        confirmed = sum(item.status == "Ноль подтверждён" for item in results)
        await message.reply_text(
            f"Кабинет: {state.account.name}\n"
            f"Ноль подтверждён: {confirmed} из {len(results)} записей по размерам и складам.\n"
            f"Требуют проверки: {len(results) - confirmed}. "
            f"Пропущено артикулов: {len(state.plan.issues)}.\nПодробности — в файле."
        )
        logger.info("Обнуление WB завершено: кабинет=%s, подтверждено=%s, всего=%s",
                    state.account.id, confirmed, len(results))
    except WBError as error:
        await message.reply_text(f"Обнуление не выполнено: {error}")
    except Exception:
        logger.exception("Ошибка выполнения или отправки результата обнуления WB")
        await message.reply_text(
            "Не удалось завершить операцию или отправить отчёт. Часть остатков могла измениться.\n"
            "Отчёт, если сохранён, доступен через /wb_result. Проверь остатки в кабинете WB."
        )
    finally:
        if context.user_data.get(KEY) is state:
            context.user_data.pop(KEY, None)


async def warehouse_worker(message, context, state):
    try:
        warehouses = await asyncio.to_thread(list_warehouses, state.account)
        if context.user_data.get(KEY) is not state:
            return
        state.warehouses = [dict(item, name=str(item.get("name") or item["id"])) for item in warehouses]
        if not warehouses:
            context.user_data.pop(KEY, None)
            await message.reply_text("В кабинете нет доступных складов продавца. Склады в процессе удаления или обновления исключены.")
            return
        state.phase = "warehouses"
        text, markup = warehouse_view(state)
        await message.reply_text(text, reply_markup=markup)
    except WBError as error:
        if context.user_data.get(KEY) is state:
            context.user_data.pop(KEY, None)
            await message.reply_text(f"Не удалось загрузить склады: {error}\nПосле исправления открой /wb заново.")
    except Exception:
        logger.exception("Ошибка загрузки складов WB")
        if context.user_data.get(KEY) is state:
            context.user_data.pop(KEY, None)
            await message.reply_text("Не удалось загрузить или показать склады. Открой /wb заново.")


async def start(message, context):
    # Invalidate a previous read-only session before a potentially failing load.
    context.user_data.pop(KEY, None)
    try:
        accounts = load_accounts()
    except WBError as error:
        await message.reply_text(str(error))
        return
    if not accounts:
        await message.reply_text(
            "Кабинеты WB пока не подключены. На сервере нужно настроить WB_ACCOUNTS и токен каждого кабинета.\n"
            "Токену нужны категории «Контент» и «Маркетплейс» с правом изменять остатки."
        )
        return
    state = Workflow(message.chat_id, accounts)
    context.user_data[KEY] = state
    rows = [[button(state, account.name, f"account:{index}")] for index, account in enumerate(accounts)]
    await message.reply_text("Выбери кабинет WB для обнуления остатков:", reply_markup=keyboard(state, rows))


async def callback(query, context, state):
    parts = query.data.split(":", 2)
    if not state or len(parts) != 3 or parts[1] != state.nonce:
        await query.answer("Кнопка устарела. Открой /wb заново.", show_alert=True)
        return
    action = parts[2]
    if state.phase == "executing":
        await query.answer("Обнуление уже выполняется. Дождись отчёта.", show_alert=True)
        return
    if action == "cancel":
        context.user_data.pop(KEY, None)
        await query.answer()
        await query.edit_message_text("Обнуление отменено. Можно отправлять прайс.")
        return
    await query.answer()
    message = query.message
    if action.startswith("account:") and state.phase == "account":
        index = action.split(":", 1)[1]
        if not index.isdigit() or int(index) >= len(state.accounts):
            return
        state.account = state.accounts[int(index)]
        state.phase = "loading"
        await query.edit_message_text(f"Загружаю склады кабинета «{state.account.name}»… /cancel — выйти.")
        context.application.create_task(warehouse_worker(message, context, state))
    elif state.phase == "warehouses":
        if action.startswith("wh:"):
            ident = action.split(":", 1)[1]
            if not ident.isdigit() or int(ident) not in {item["id"] for item in state.warehouses}:
                return
            state.selected.symmetric_difference_update({int(ident)})
        elif action == "all":
            state.selected = {item["id"] for item in state.warehouses}
        elif action == "next":
            state.page = min(state.page + 1, (len(state.warehouses) - 1) // 8)
        elif action == "prev":
            state.page = max(0, state.page - 1)
        elif action == "warehouses_done":
            if not state.selected:
                await message.reply_text("Выбери хотя бы один склад.")
                return
            state.phase = "input"
            await query.edit_message_text(f"Выбрано складов: {len(state.selected)}.")
            await show_input(message, state)
            return
        else:
            return
        text, markup = warehouse_view(state)
        # Repeated presses (e.g. 'all') may produce identical markup.
        if query.message.text != text or query.message.reply_markup != markup:
            await query.edit_message_text(text, reply_markup=markup)
    elif action == "prepare" and state.phase == "input":
        if not state.articles:
            await message.reply_text("Сначала пришли список артикулов продавца.")
            return
        state.phase = "preparing"
        await query.edit_message_text("Ищу товары и проверяю остатки… /cancel — отменить проверку.")
        context.application.create_task(prepare_worker(message, context, state))
    elif action == "edit" and state.phase == "preview":
        state.plan = None
        state.phase = "input"
        state.nonce = secrets.token_hex(4)
        state.articles = []
        await query.edit_message_text("Предыдущий список сброшен. Пришли новый список целиком.")
        await show_input(message, state)
    elif action == "confirm" and state.phase == "preview":
        if not state.plan or time.monotonic() - state.plan.created_at > 900:
            context.user_data.pop(KEY, None)
            await query.edit_message_text("Предпросмотр устарел. Открой /wb и проверь список заново.")
            return
        # Mark consumed before the first await: duplicate callbacks cannot write twice.
        state.phase = "executing"
        context.application.create_task(execute_worker(message, context, state))
        try:
            await query.edit_message_text("Обнуляю остатки и проверяю результат. Дождись отчёта…")
        except TelegramError:
            logger.warning("Не удалось обновить сообщение подтверждения WB; операция запущена")
    else:
        await message.reply_text("Эта кнопка сейчас недоступна. Используй последний шаг или /cancel.")


def register_handlers(app, check_access):
    async def route(update, context):
        message, query = update.effective_message, update.callback_query
        if not message:
            return
        text = (message.text or "").strip() if not query else ""
        command = text.split(maxsplit=1)[0].split("@")[0].lower() if text.startswith("/") else ""
        state = context.user_data.get(KEY)
        wb_callback = bool(query and query.data and query.data.startswith("wb:"))
        explicit = wb_callback or text == BUTTON or command in ("/wb", "/cancel", "/wb_result")
        same_chat = state and state.chat_id == update.effective_chat.id
        if not explicit and not same_chat:
            return
        if not await check_access(update):
            if query:
                await query.answer()
            raise ApplicationHandlerStop
        if update.effective_chat.type != "private":
            if query:
                await query.answer()
            await message.reply_text("Обнуление доступно в личном чате с ботом.")
            raise ApplicationHandlerStop
        if state and state.phase not in ("executing", "preparing", "loading") and time.monotonic() - state.touched_at > 900:
            context.user_data.pop(KEY, None)
            state = None
            if not (command == "/wb" or text == BUTTON):
                if query:
                    await query.answer()
                await message.reply_text("Сессия обнуления устарела. Открой /wb заново.")
                raise ApplicationHandlerStop
        if state:
            state.touched_at = time.monotonic()
        if wb_callback:
            await callback(query, context, state)
        elif state and state.phase == "executing":
            if query:
                await query.answer()
            await message.reply_text("Обнуление уже выполняется. Дождись отчёта.")
        elif command in ("/cancel", "/start"):
            context.user_data.pop(KEY, None)
            if command == "/start":
                return  # The existing /start handler displays the main keyboard.
            await message.reply_text("Режим обнуления закрыт. Можно отправлять прайс.")
        elif command == "/wb" or text == BUTTON:
            await start(message, context)
        elif command == "/wb_result":
            saved = context.user_data.get("wb_last_result")
            if saved:
                await send_report(message, *saved)
            else:
                await message.reply_text("Сохранённого отчёта нет. Отчёты в памяти доступны до перезапуска бота.")
        elif state and state.phase == "input" and text and not command:
            try:
                state.articles = parse_articles(text, state.articles)
                await show_input(message, state)
            except WBError as error:
                await message.reply_text(str(error))
        else:
            if query:
                await query.answer()
            await message.reply_text("Сейчас открыт режим обнуления WB. Используй его кнопки или /cancel для выхода.")
        raise ApplicationHandlerStop

    # Run after logging and before all price/mapping handlers.
    app.add_handler(TypeHandler(Update, route), group=-1)
