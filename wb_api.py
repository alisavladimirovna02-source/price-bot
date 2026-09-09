"""WB stock operations. Preparation only reads; execution requires a prepared plan."""

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import threading
import time

import requests


MARKETPLACE = "https://marketplace-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"
MAX_ARTICLES = 1000
_gates = {}
_gates_lock = threading.Lock()


class WBError(Exception):
    """Safe to display: never includes headers, tokens or raw response bodies."""


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    token: str = field(repr=False)


def load_accounts():
    raw = os.getenv("WB_ACCOUNTS", "[]")
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        raise WBError("WB_ACCOUNTS: нужен корректный JSON со списком кабинетов.") from None
    if not isinstance(items, list) or len(items) > 20:
        raise WBError("WB_ACCOUNTS: укажите список, не более 20 кабинетов.")
    accounts = []
    for item in items:
        if not isinstance(item, dict):
            raise WBError("WB_ACCOUNTS: каждый кабинет должен быть объектом.")
        ident, name, token_env = (item.get(key) for key in ("id", "name", "token_env"))
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", ident):
            raise WBError("WB_ACCOUNTS: id — 1–24 латинских буквы, цифры, _ или -.")
        if ident in {account.id for account in accounts}:
            raise WBError("WB_ACCOUNTS: id кабинетов должны отличаться.")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise WBError("WB_ACCOUNTS: задайте название кабинета до 80 символов.")
        if not isinstance(token_env, str) or not re.fullmatch(r"WB_TOKEN_[A-Z0-9_]+", token_env):
            raise WBError("WB_ACCOUNTS: token_env должен иметь вид WB_TOKEN_MAIN.")
        token = os.getenv(token_env, "").strip()
        if not token:
            raise WBError(f"Для кабинета «{name}» не задана переменная {token_env}.")
        accounts.append(Account(ident, name.strip(), token))
    return accounts


def parse_articles(text, existing=()):
    # One complete vendorCode per line: spaces, commas, leading zeros are significant.
    articles = list(dict.fromkeys([*existing, *(line.strip() for line in text.splitlines()
                                             if line.strip())]))
    if not articles:
        raise WBError("Пришли хотя бы один артикул продавца, каждый с новой строки.")
    if len(articles) > MAX_ARTICLES:
        raise WBError(f"За одну операцию можно передать до {MAX_ARTICLES} артикулов.")
    if any(len(article) > 200 or any(ord(char) < 32 for char in article) for article in articles):
        raise WBError("Артикул должен занимать одну строку до 200 символов без табуляции.")
    return articles


def chunks(items, size=1000):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def positive_id(value):
    return type(value) is int and value > 0


class WBClient:
    def __init__(self, account):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": account.token})
        key = hashlib.sha256(account.token.encode()).digest()
        with _gates_lock:
            self.gate = _gates.setdefault(key, {"lock": threading.Lock(), "next": 0.0})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()

    def request(self, method, base, path, payload=None):
        # Shared pacing across users and operations in this process. 650 ms also
        # respects the stricter Content limit (100/minute).
        with self.gate["lock"]:
            for attempt in range(3):
                time.sleep(max(0, self.gate["next"] - time.monotonic()))
                self.gate["next"] = time.monotonic() + 0.65
                try:
                    response = self.session.request(
                        method, base + path, json=payload, timeout=(10, 30),
                        allow_redirects=False,
                    )
                except requests.RequestException:
                    # A timed-out PUT may have taken effect. Verify it; never
                    # automatically replay it after a network failure.
                    if method != "PUT" and attempt < 2:
                        self.gate["next"] = time.monotonic() + 2 ** (attempt + 1)
                        continue
                    raise WBError("Нет надёжного ответа WB: ошибка соединения или тайм-аут.") from None
                status = response.status_code
                if status == 429 or (status >= 500 and method != "PUT"):
                    if attempt < 2:
                        try:
                            delay = float(response.headers.get(
                                "X-Ratelimit-Retry", response.headers.get("Retry-After", "5")
                            ))
                        except (ValueError, TypeError):
                            delay = 5
                        if not 0 <= delay <= 60:
                            raise WBError("WB просит подождать больше минуты. Повторите позже.")
                        self.gate["next"] = time.monotonic() + max(delay, 2 ** (attempt + 1))
                        continue
                expected = 204 if method == "PUT" else 200
                if status != expected:
                    reasons = {
                        401: "Токен WB недействителен или истёк.",
                        403: "Нет прав WB: нужны «Контент» и «Маркетплейс» с записью остатков.",
                        406: "WB временно заблокировал обновление остатков этого склада.",
                        409: "WB отклонил обновление: проверьте товары и тип склада.",
                        429: "Превышен лимит запросов WB. Повторите позже.",
                    }
                    if status == 409:
                        self.gate["next"] = time.monotonic() + 6.5
                    raise WBError(reasons.get(status, f"WB вернул ошибку HTTP {status}."))
                if status == 204:
                    return None
                try:
                    return response.json()
                except ValueError:
                    raise WBError("WB вернул ответ в неизвестном формате.") from None

    def warehouses(self):
        data = self.request("GET", MARKETPLACE, "/api/v3/warehouses")
        if not isinstance(data, list) or any(
            not isinstance(item, dict) or not positive_id(item.get("id")) for item in data
        ):
            raise WBError("WB вернул некорректный список складов.")
        return [item for item in data if not item.get("isDeleting") and not item.get("isProcessing")]

    def find_cards(self, articles):
        wanted = set(articles)
        found = defaultdict(dict)
        # Trashed cards can still have stock. Include them with the same exact
        # vendorCode matching and flag duplicates across both sources.
        for endpoint, cursor_field in (("list", "updatedAt"), ("trash", "trashedAt")):
            cursor = {"limit": 100}
            seen_cursors = set()
            for _ in range(10000):
                settings = {"sort": {"ascending": True}, "cursor": cursor}
                if endpoint == "list":
                    settings["filter"] = {"withPhoto": -1}
                data = self.request("POST", CONTENT, f"/content/v2/get/cards/{endpoint}",
                                    {"settings": settings})
                if not isinstance(data, dict) or not isinstance(data.get("cards"), list):
                    raise WBError("WB вернул некорректный список карточек.")
                for card in data["cards"]:
                    if not isinstance(card, dict):
                        raise WBError("WB вернул некорректную карточку.")
                    code = card.get("vendorCode")
                    if isinstance(code, str) and code in wanted:
                        if not positive_id(card.get("nmID")):
                            raise WBError("WB не вернул артикул WB для найденной карточки.")
                        found[code][card["nmID"]] = card
                next_cursor = data.get("cursor")
                if not isinstance(next_cursor, dict) or type(next_cursor.get("total")) is not int:
                    raise WBError("WB не вернул курсор списка карточек. Поиск не завершён.")
                if not 0 <= next_cursor["total"] <= 100:
                    raise WBError("WB вернул некорректное количество карточек на странице.")
                if next_cursor["total"] < 100:
                    break
                point = (next_cursor.get(cursor_field), next_cursor.get("nmID"))
                if not isinstance(point[0], str) or not point[0] or not positive_id(point[1]) or point in seen_cursors:
                    raise WBError("Не удалось перейти к следующей странице карточек WB.")
                seen_cursors.add(point)
                cursor = {"limit": 100, cursor_field: point[0], "nmID": point[1]}
            else:
                raise WBError("Слишком большой каталог WB. Поиск не завершён.")
        return found

    def stocks(self, warehouse_id, ids):
        result = {}
        wanted = set(ids)
        for batch in chunks(ids):
            data = self.request("POST", MARKETPLACE, f"/api/v3/stocks/{warehouse_id}",
                                {"chrtIds": batch})
            if not isinstance(data, dict) or not isinstance(data.get("stocks"), list):
                raise WBError("WB вернул некорректный список остатков.")
            for item in data["stocks"]:
                if not isinstance(item, dict) or not positive_id(item.get("chrtId")):
                    raise WBError("WB вернул некорректный ID размера в остатках.")
                ident, amount = item["chrtId"], item.get("amount")
                if type(amount) is not int or amount < 0 or ident not in wanted or ident in result:
                    raise WBError("WB вернул некорректное количество или состав остатков.")
                result[ident] = amount
        # Missing records remain unknown; absence is never reported as zero.
        return result

    def zero(self, warehouse_id, ids):
        self.request("PUT", MARKETPLACE, f"/api/v3/stocks/{warehouse_id}",
                     {"stocks": [{"chrtId": ident, "amount": 0} for ident in ids]})


@dataclass(frozen=True)
class StockRow:
    article: str
    nm_id: int
    chrt_id: int
    size: str
    warehouse_id: int
    warehouse_name: str
    before: object


@dataclass
class Plan:
    account: Account
    rows: list
    issues: dict
    created_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class Result:
    row: StockRow
    status: str
    after: object
    detail: str = ""


def list_warehouses(account):
    with WBClient(account) as client:
        return client.warehouses()


def prepare_plan(account, warehouses, articles):
    with WBClient(account) as client:
        cards = client.find_cards(articles)
        issues, sizes = {}, []
        for article in articles:
            matches = list(cards.get(article, {}).values())
            if not matches:
                issues[article] = "Не найден точный артикул продавца в этом кабинете"
                continue
            if len(matches) != 1:
                issues[article] = "Найдено несколько карточек: неоднозначный артикул"
                continue
            card = matches[0]
            card_sizes = card.get("sizes")
            if not isinstance(card_sizes, list) or not card_sizes or any(
                not isinstance(size, dict) or not positive_id(size.get("chrtID")) for size in card_sizes
            ):
                issues[article] = "Нет корректных ID размеров для обнуления"
                continue
            for size in card_sizes:
                sizes.append((article, card["nmID"], size["chrtID"], str(size.get("techSize", ""))))
        ids = [size[2] for size in sizes]
        if len(ids) != len(set(ids)):
            raise WBError("WB вернул повторяющиеся ID размеров. Обнуление отменено.")
        rows = []
        for warehouse in warehouses:
            stock = client.stocks(warehouse["id"], ids) if ids else {}
            for article, nm_id, chrt_id, size in sizes:
                rows.append(StockRow(article, nm_id, chrt_id, size, warehouse["id"],
                                     warehouse["name"], stock.get(chrt_id)))
    return Plan(account, rows, issues)


def execute_plan(plan):
    if time.monotonic() - plan.created_at > 900:
        raise WBError("Предпросмотр старше 15 минут. Подготовьте список заново.")
    results = []
    by_warehouse = defaultdict(list)
    for row in plan.rows:
        by_warehouse[row.warehouse_id].append(row)
    with WBClient(plan.account) as client:
        available = {warehouse["id"] for warehouse in client.warehouses()}
        for warehouse_id, rows in by_warehouse.items():
            if warehouse_id not in available:
                results.extend(Result(row, "Не отправлено", None,
                                      "Склад недоступен, удаляется или обновляется") for row in rows)
                continue
            for batch in chunks(rows):
                ids = [row.chrt_id for row in batch]
                write_error = ""
                try:
                    client.zero(warehouse_id, ids)
                except WBError as error:
                    write_error = str(error)
                after, verify_error = {}, ""
                # WB may accept writes before reads reflect them. Retry reads,
                # including after ambiguous PUT failures, never repeat the PUT.
                for attempt in range(3):
                    if attempt:
                        time.sleep(1 + attempt)
                    try:
                        after = client.stocks(warehouse_id, ids)
                        verify_error = ""
                    except WBError as error:
                        verify_error = str(error)
                        after = {}
                    if all(after.get(ident) == 0 for ident in ids):
                        break
                for row in batch:
                    amount = after.get(row.chrt_id)
                    if amount == 0:
                        status, detail = "Ноль подтверждён", write_error
                    else:
                        status = "Ноль не подтверждён"
                        detail = "; ".join(filter(None, [write_error, verify_error])) or (
                            "WB не вернул запись об остатке" if amount is None else "WB вернул ненулевой остаток"
                        )
                    results.append(Result(row, status, amount, detail))
    return results
