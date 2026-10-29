"""
AutoSMM Plugin for FunPayCardinal
Автор: @klaymov
Репозиторий: https://github.com/klaymov/FPC-AutoSmm
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional, List, Dict, Any, Tuple

import requests
import telebot
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
from FunPayAPI.types import MessageTypes
from locales.localizer import Localizer
from tg_bot.utils import load_authorized_users, escape
from tg_bot import CBT

# ---------------------------------------------------------------------------
# Метаданные плагина
# ---------------------------------------------------------------------------
NAME = "AUTOSMM"
VERSION = "1.0.0"
DESCRIPTION = "Автоматическое создание и отслеживание заказов через SMM API v2 (by @klaymov)"
CREDITS = "@klaymov"
UUID = "7aa412ab-0840-455d-9513-6f51bf83d43b"
SETTINGS_PAGE = True
BIND_TO_DELETE = None

LOGGER_PREFIX = "AutoSmm"
logger = logging.getLogger(f"FPC.{LOGGER_PREFIX}")
localizer = Localizer()
_ = localizer.translate

README_URL = "https://github.com/klaymov/FPC-AutoSmm/blob/main/README.md"

# ---------------------------------------------------------------------------
# Пути к файлам хранилища
# ---------------------------------------------------------------------------
PLUGIN_STORAGE_DIR = f"storage/plugins/{UUID}"
ORDERS_FILE = os.path.join(PLUGIN_STORAGE_DIR, "orders.json")
PAYORDERS_FILE = os.path.join(PLUGIN_STORAGE_DIR, "payorders.json")
SETTINGS_FILE = os.path.join(PLUGIN_STORAGE_DIR, "settings.json")
CASHLIST_FILE = os.path.join(PLUGIN_STORAGE_DIR, "cashlist.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "api_url": "",
    "api_key": "",
    "set_alert_neworder": True,
    "set_alert_errororder": True,
    "set_alert_smmbalance": True,
    "set_refund_smm": True,
    "set_start_mess": True,
    "set_tg_private": False,
    "set_recreated_order": False,
}

# ---------------------------------------------------------------------------
# Потокобезопасное хранилище (StorageManager)
# ---------------------------------------------------------------------------
class StorageManager:
    _lock = threading.Lock()

    @classmethod
    def _ensure_dir(cls) -> None:
        os.makedirs(PLUGIN_STORAGE_DIR, exist_ok=True)

    @classmethod
    def load_settings(cls) -> Dict[str, Any]:
        with cls._lock:
            cls._ensure_dir()
            if not os.path.exists(SETTINGS_FILE):
                settings = DEFAULT_SETTINGS.copy()
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=4, ensure_ascii=False)
                return settings
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in DEFAULT_SETTINGS.items():
                        if k not in data:
                            data[k] = v
                    return data
            except Exception as e:
                logger.error(f"Ошибка при чтении {SETTINGS_FILE}: {e}")
                return DEFAULT_SETTINGS.copy()

    @classmethod
    def save_settings(cls, settings: Dict[str, Any]) -> None:
        with cls._lock:
            cls._ensure_dir()
            try:
                with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Ошибка при сохранении {SETTINGS_FILE}: {e}")

    @classmethod
    def load_orders(cls) -> Dict[str, Dict[str, Any]]:
        with cls._lock:
            cls._ensure_dir()
            if not os.path.exists(ORDERS_FILE):
                return {}
            try:
                with open(ORDERS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка при чтении {ORDERS_FILE}: {e}")
                return {}

    @classmethod
    def save_orders(cls, orders: Dict[str, Dict[str, Any]]) -> None:
        with cls._lock:
            cls._ensure_dir()
            try:
                with open(ORDERS_FILE, "w", encoding="utf-8") as f:
                    json.dump(orders, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Ошибка при сохранении {ORDERS_FILE}: {e}")

    @classmethod
    def load_payorders(cls) -> List[Dict[str, Any]]:
        with cls._lock:
            cls._ensure_dir()
            if not os.path.exists(PAYORDERS_FILE):
                return []
            try:
                with open(PAYORDERS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка при чтении {PAYORDERS_FILE}: {e}")
                return []

    @classmethod
    def save_payorders(cls, payorders: List[Dict[str, Any]]) -> None:
        with cls._lock:
            cls._ensure_dir()
            try:
                with open(PAYORDERS_FILE, "w", encoding="utf-8") as f:
                    json.dump(payorders, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Ошибка при сохранении {PAYORDERS_FILE}: {e}")

    @classmethod
    def load_cashlist(cls) -> Dict[str, Dict[str, Any]]:
        with cls._lock:
            cls._ensure_dir()
            if not os.path.exists(CASHLIST_FILE):
                return {}
            try:
                with open(CASHLIST_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка при чтении {CASHLIST_FILE}: {e}")
                return {}

    @classmethod
    def save_cashlist(cls, cashlist: Dict[str, Dict[str, Any]]) -> None:
        with cls._lock:
            cls._ensure_dir()
            try:
                with open(CASHLIST_FILE, "w", encoding="utf-8") as f:
                    json.dump(cashlist, f, indent=4, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Ошибка при сохранении {CASHLIST_FILE}: {e}")


# ---------------------------------------------------------------------------
# API Клиент SMM V2 (универсальный для любых SMM-панелей)
# ---------------------------------------------------------------------------
class SmmApiClient:
    def __init__(self, api_url: str, api_key: str):
        url = (api_url or "").strip()
        if url and not url.endswith("/") and not url.endswith(".php"):
            url += "/"
        self.api_url = url
        self.api_key = (api_key or "").strip()
        self.timeout = 15

    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def _request(self, params: Dict[str, Any]) -> Tuple[bool, Any]:
        """
        Отправляет запрос к API панели SMM.
        Поддерживает POST и GET с таймаутом и валидацией JSON.
        """
        if not self.is_configured():
            return False, "API URL или API KEY не настроены."

        req_params = {"key": self.api_key}
        req_params.update(params)

        # 1. Пробуем POST
        try:
            response = requests.post(self.api_url, data=req_params, timeout=self.timeout)
            if response.status_code == 405:  # Method Not Allowed -> fallback GET
                response = requests.get(self.api_url, params=req_params, timeout=self.timeout)
            response.raise_for_status()
            return True, response.json()
        except requests.exceptions.RequestException as e:
            # 2. Пробуем GET при сбое POST
            try:
                response = requests.get(self.api_url, params=req_params, timeout=self.timeout)
                response.raise_for_status()
                return True, response.json()
            except Exception as ex:
                logger.error(f"Ошибка подключения к SMM API ({self.api_url}): {ex}")
                return False, f"Сетевая ошибка API: {ex}"
        except json.JSONDecodeError:
            logger.error(f"SMM API вернул некорректный JSON: {response.text[:200]}")
            return False, "Некорректный формат ответа от сервера SMM."
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при запросе к SMM API: {e}")
            return False, str(e)

    def get_balance(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Возвращает (баланс, валюта).
        """
        success, data = self._request({"action": "balance"})
        if not success or not isinstance(data, dict):
            return None, None

        if "balance" in data:
            try:
                raw_bal = str(data["balance"])
                match = re.search(r"[-+]?\d*\.?\d+", raw_bal)
                balance_val = float(match.group()) if match else float(raw_bal)
                currency = str(data.get("currency", "RUB")).upper()
                return balance_val, currency
            except Exception as e:
                logger.error(f"Ошибка парсинга баланса ({data}): {e}")
                return None, None
        return None, None

    def create_order(self, service_id: int, link: str, quantity: int) -> Tuple[bool, Any]:
        """
        Создает заказ в SMM.
        Возвращает (True, order_id) или (False, error_str).
        """
        params = {
            "action": "add",
            "service": int(service_id),
            "link": str(link).strip(),
            "quantity": int(quantity),
        }
        success, data = self._request(params)
        if not success:
            return False, data

        if isinstance(data, dict):
            if "order" in data:
                return True, str(data["order"])
            if "error" in data:
                return False, str(data["error"])
        return False, "Неизвестный ответ от SMM сервиса."

    def get_order_status(self, order_id: int | str) -> Optional[Dict[str, Any]]:
        """
        Возвращает статус одного заказа.
        """
        success, data = self._request({"action": "status", "order": str(order_id)})
        if success and isinstance(data, dict) and "status" in data:
            return data
        return None

    def get_multiple_orders_status(self, order_ids: List[str]) -> Optional[Dict[str, Any]]:
        """
        Возвращает статусы пачки заказов (до 100 шт).
        """
        if not order_ids:
            return {}
        orders_str = ",".join(order_ids[:100])
        success, data = self._request({"action": "status", "orders": orders_str})
        if success and isinstance(data, dict):
            return data
        return None

    def refill_order(self, order_id: int | str) -> Tuple[bool, Any]:
        """
        Запрашивает докрутку заказа (refill).
        """
        success, data = self._request({"action": "refill", "order": str(order_id)})
        if success and isinstance(data, dict):
            if "refill" in data:
                return True, data["refill"]
            if "error" in data:
                return False, data["error"]
        return False, "Ошибка при выполнении рефилла."

    def cancel_order(self, order_id: int | str) -> Tuple[bool, Any]:
        """
        Запрашивает отмену заказа (cancel).
        """
        success, data = self._request({"action": "cancel", "order": str(order_id)})
        if success and isinstance(data, dict):
            if "cancel" in data:
                return True, data["cancel"]
            if "error" in data:
                return False, data["error"]
        return False, "Отмена не поддерживается или произошла ошибка."


# ---------------------------------------------------------------------------
# Маскирование и фильтрация ошибок для покупателя на FunPay
# ---------------------------------------------------------------------------
class ErrorSanitizer:
    """
    Безопасный транслятор ошибок от SMM API для покупателей на FunPay.
    Скрывает ключи, URL, балансы и технические подробности.
    """
    PUBLIC_MAP = {
        "account is private": "Ваш профиль или канал закрыт. Пожалуйста, откройте его в настройках приватности и пришлите ссылку снова.",
        "link incorrect": "Указана некорректная ссылка. Проверьте адрес и пришлите правильную ссылку.",
        "is not link to the post": "Указана ссылка не на публикацию (пост/видео). Пожалуйста, отправьте прямую ссылку на нужный пост.",
        "is not full link": "Указана неполная ссылка. Пожалуйста, отправьте полную ссылку целиком.",
        "is not login": "Указан некорректный логин.",
        "limit min": "Заказанное количество меньше минимально допустимого лимита для данной услуги.",
        "limit max": "Заказанное количество превышает максимально допустимый лимит для данной услуги.",
        "is already running": "Заказ по этой ссылке уже выполняется сервисом. Дождитесь завершения или отправьте другую ссылку.",
        "thematics forbidden": "Тематика данной страницы запрещена правилами сервиса продвижения.",
        "black list": "Данная ссылка находится в черном списке сервиса.",
        "invalid character": "Ссылка содержит недопустимые спецсимволы.",
        "qnt is not a multiple": "Количество не кратно требованиям данной услуги.",
        "not found": "Заказ не найден в базе сервиса.",
        "order completed": "Заказ уже завершен в сервисе.",
        "cancel not available": "Отмена данной услуги недоступна сервисом.",
        "order in progress": "Заказ еще выполняется, докрутка пока недоступна.",
        "date expired": "Срок гарантии на данную услугу истек.",
    }

    @classmethod
    def sanitize_for_buyer(cls, raw_error: str) -> str:
        if not raw_error:
            return "Временная техническая задержка на стороне сервиса. Продавец уже оповещен и запустит заказ вручную."

        err_lower = str(raw_error).lower()

        # 1. Проверяем понятные пользовательские ошибки
        for key, text in cls.PUBLIC_MAP.items():
            if key in err_lower:
                return text

        # 2. Если ошибка системная (auth error, no money, connection error, token, key, url)
        # Никогда не показываем покупателю внутреннюю кухню продавца!
        return "Временная техническая задержка на стороне сервиса. Продавец уже оповещен и запустит заказ вручную в ближайшее время."


# ---------------------------------------------------------------------------
# Кэш курсов валют
# ---------------------------------------------------------------------------
class CurrencyConverter:
    _rate_cache: Dict[str, Tuple[float, float]] = {}  # pair: (rate, timestamp)

    @classmethod
    def get_rate(cls, from_curr: str = "USD", to_curr: str = "RUB") -> float:
        pair = f"{from_curr}_{to_curr}".upper()
        now = time.time()
        if pair in cls._rate_cache:
            rate, ts = cls._rate_cache[pair]
            if now - ts < 3600:  # кэш на 1 час
                return rate

        # Запрос к CoinGate
        try:
            url = f"https://api.coingate.com/v2/rates/merchant/{from_curr}/{to_curr}"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                rate = float(resp.text)
                cls._rate_cache[pair] = (rate, now)
                return rate
        except Exception:
            pass

        # Дефолтные запасные коэффициенты
        fallback_rates = {
            "USD_RUB": 95.0,
            "RUB_USD": 0.0105,
            "EUR_RUB": 102.0,
            "RUB_EUR": 0.0098,
        }
        return fallback_rates.get(pair, 1.0)


# ---------------------------------------------------------------------------
# Telegram Уведомления и Сервис форматирования
# ---------------------------------------------------------------------------
class TelegramNotifier:
    @staticmethod
    def send_to_all_authorized(c: Cardinal, text: str, reply_markup=None) -> None:
        if not c.telegram or not c.telegram.bot:
            return
        users = load_authorized_users()
        if not users:
            return
        for uid in users:
            try:
                c.telegram.bot.send_message(
                    uid,
                    text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Не удалось отправить TG-сообщение пользователю {uid}: {e}")

    @classmethod
    def send_order_created(
        cls,
        c: Cardinal,
        order: Dict[str, Any],
        smm_order_id: str | int,
        api_client: SmmApiClient,
    ) -> None:
        """
        Красивое уведомление о новом созданном заказе в SMM.
        """
        # Сведения о заказе в SMM
        smm_status = api_client.get_order_status(smm_order_id) or {}
        smm_charge_raw = smm_status.get("charge", 0)
        smm_currency = smm_status.get("currency", "RUB")
        try:
            smm_charge = float(smm_charge_raw)
        except Exception:
            smm_charge = 0.0

        smm_balance, bal_curr = api_client.get_balance()
        smm_balance_str = f"{smm_balance:.2f} {bal_curr}" if smm_balance is not None else "Неизвестно"

        # Баланс FunPay
        try:
            fp_bal = c.get_balance()
            fp_bal_str = f"{fp_bal.total_rub}₽ | {fp_bal.available_usd}$ | {fp_bal.total_eur}€"
        except Exception:
            fp_bal_str = "Не удалось получить"

        fp_currency = order.get("OrderCurrency", "₽")
        fp_price = float(order.get("OrderPrice", 0.0))

        # Пересчет валют для расчета прибыли
        smm_cost_in_fp = smm_charge
        if fp_currency == "₽" and smm_currency == "USD":
            smm_cost_in_fp = smm_charge * CurrencyConverter.get_rate("USD", "RUB")
        elif fp_currency == "$" and smm_currency == "RUB":
            smm_cost_in_fp = smm_charge * CurrencyConverter.get_rate("RUB", "USD")

        profit = fp_price - smm_cost_in_fp
        profit_6com = profit * 0.94
        profit_3com = profit * 0.97

        clean_link = str(order.get("url", "")).replace("https://", "").replace("http://", "")

        text = (
            f"🚀 <b>Создан заказ <code>{NAME}</code>!</b>\n\n"
            f"🛒 <b>Лот:</b> <code>{escape(str(order.get('title', 'Без названия')))}</code>\n"
            f"👤 <b>Покупатель:</b> <code>{escape(str(order.get('buyer', '')))}</code>\n"
            f"📇 <b>ID FunPay:</b> <code>#{order.get('OrderID')}</code>\n"
            f"🆔 <b>ID SMM:</b> <code>#{smm_order_id}</code> (Услуга: <code>{order.get('service_id')}</code>)\n"
            f"🔢 <b>Количество:</b> <code>{order.get('Amount')} шт.</code>\n"
            f"🔗 <b>Ссылка:</b> <code>{escape(clean_link)}</code>\n\n"
            f"💵 <b>Сумма FunPay:</b> <code>{fp_price:.2f} {fp_currency}</code>\n"
            f"📉 <b>Списано в SMM:</b> <code>{smm_charge:.2f} {smm_currency}</code>\n"
            f"📈 <b>Чистая прибыль:</b> <code>{profit:.2f} {fp_currency}</code>\n"
            f"📊 <b>С учетом комиссии FunPay:</b> <code>{profit_6com:.2f} (6%)</code> | <code>{profit_3com:.2f} (3%)</code>\n\n"
            f"💰 <b>Остаток в SMM:</b> <code>{smm_balance_str}</code>\n"
            f"💳 <b>Баланс FunPay:</b> <code>{fp_bal_str}</code>\n\n"
            f"👨‍💻 <i>Автор плагина: @klaymov</i>"
        )

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🌐 Открыть заказ на FunPay", url=f"https://funpay.com/orders/{order.get('OrderID')}/"))

        cls.send_to_all_authorized(c, text, reply_markup=kb)

    @classmethod
    def send_order_error(cls, c: Cardinal, order: Dict[str, Any], raw_error: Any, refunded: bool) -> None:
        """
        Уведомление об ошибке создания заказа в SMM (со всеми техническими данными).
        """
        refund_text = "🟢 Автовозврат покупателю выполнен" if refunded else "🔴 Автовозврат выключен (требуется ручная проверка)"
        clean_link = str(order.get("url", "")).replace("https://", "").replace("http://", "")

        text = (
            f"⚠️ <b>Ошибка при создании заказа <code>{NAME}</code>!</b>\n\n"
            f"📇 <b>ID FunPay:</b> <code>#{order.get('OrderID')}</code>\n"
            f"👤 <b>Покупатель:</b> <code>{escape(str(order.get('buyer', '')))}</code>\n"
            f"🛒 <b>Лот:</b> <code>{escape(str(order.get('title', '')))}</code>\n"
            f"🆔 <b>Услуга:</b> <code>{order.get('service_id')}</code> | <b>Кол-во:</b> <code>{order.get('Amount')}</code>\n"
            f"🔗 <b>Ссылка:</b> <code>{escape(clean_link)}</code>\n\n"
            f"❌ <b>Причина ошибки от API SMM:</b>\n"
            f"<code>{escape(str(raw_error))}</code>\n\n"
            f"⚡ <b>Статус возврата:</b> {refund_text}\n\n"
            f"👨‍💻 <i>Плагин: @klaymov</i>"
        )

        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🌐 Открыть заказ на FunPay", url=f"https://funpay.com/orders/{order.get('OrderID')}/"))

        cls.send_to_all_authorized(c, text, reply_markup=kb)

    @classmethod
    def send_order_completed(cls, c: Cardinal, order_info: Dict[str, Any], smm_id: str) -> None:
        """
        Уведомление о завершении заказа в SMM.
        """
        text = (
            f"🎉 <b>Заказ в SMM успешно выполнен!</b>\n\n"
            f"📇 <b>ID FunPay:</b> <code>#{order_info.get('order_id')}</code>\n"
            f"🆔 <b>ID SMM:</b> <code>#{smm_id}</code>\n"
            f"👤 <b>Покупатель:</b> <code>{escape(str(order_info.get('buyer', '')))}</code>\n"
            f"🔢 <b>Количество:</b> <code>{order_info.get('order_amount')} шт.</code>\n\n"
            f"<i>Покупателю отправлено сообщение с просьбой подтвердить заказ на FunPay.</i>"
        )
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🌐 Открыть заказ на FunPay", url=f"https://funpay.com/orders/{order_info.get('order_id')}/"))
        cls.send_to_all_authorized(c, text, reply_markup=kb)

    @classmethod
    def send_smm_start_info(cls, c: Cardinal) -> None:
        """
        Приветственное сообщение в Telegram при старте Cardinal.
        """
        text = (
            f"╔══════════════════════════╗\n"
            f"  🤖 <b>AutoSMM Инициализирован!</b>\n"
            f"╚══════════════════════════╝\n\n"
            f"<b><i>ℹ️ Версия:</i></b> <code>{VERSION}</code>\n"
            f"<b><i>⚙️ Управление:</i></b> /autosmm\n"
            f"<b><i>💰 Баланс:</i></b> /check_balance\n\n"
            f"👨‍💻 <b>Автор плагина:</b> @klaymov"
        )
        cls.send_to_all_authorized(c, text)


# ---------------------------------------------------------------------------
# Извлечение ссылок (с очисткой пунктуации)
# ---------------------------------------------------------------------------
def extract_links(text: str) -> List[str]:
    pattern = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"
    raw_links = re.findall(pattern, text)
    cleaned: List[str] = []
    for l in raw_links:
        clean = l.rstrip(".,;:!?)'\"")
        if clean:
            cleaned.append(clean)
    return cleaned


# ---------------------------------------------------------------------------
# Логика работы с заказами на FunPay (FunPay Workflow)
# ---------------------------------------------------------------------------
pending_confirmations: Dict[str | int, Dict[str, Any]] = {}


def bind_to_new_order(c: Cardinal, e: NewOrderEvent) -> None:
    """
    Хэндлер события появления нового заказа на FunPay.
    """
    try:
        order_shortcut = e.order
        order_id = order_shortcut.id

        # Получаем полные данные о заказе
        order_full = c.account.get_order(order_id)
        if not order_full:
            logger.warning(f"Не удалось получить детали заказа #{order_id}")
            return

        desc = order_full.full_description or ""
        short_desc = order_full.short_description or ""
        combined_desc = f"{desc}\n{short_desc}"

        # Проверяем, автонакрутка ли это
        match_id = re.search(r"ID:\s*(\d+)", combined_desc, re.IGNORECASE)
        if not match_id:
            logger.debug(f"Заказ #{order_id} не содержит маркера ID:, пропускаем.")
            return

        service_id = int(match_id.group(1))
        match_quan = re.search(r"#Quan:\s*(\d+)", combined_desc, re.IGNORECASE)
        multiplier = int(match_quan.group(1)) if match_quan else 1

        total_amount = order_full.amount * multiplier
        buyer_username = order_full.buyer_username or ""
        chat_id = order_full.chat_id
        lot_title = order_full.title or f"Заказ #{order_id}"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        current_order_data: Dict[str, Any] = {
            "OrderID": str(order_id),
            "Amount": int(total_amount),
            "OrderPrice": float(order_full.sum),
            "OrderCurrency": str(order_full.currency),
            "title": str(lot_title),
            "service_id": int(service_id),
            "buyer": str(buyer_username),
            "url": "",
            "chat_id": chat_id,
            "OrderDateTime": now_str,
        }

        # Сохраняем в список ожидающих ссылку
        payorders = StorageManager.load_payorders()
        payorders = [o for o in payorders if o.get("OrderID") != str(order_id)]
        payorders.append(current_order_data)
        StorageManager.save_payorders(payorders)

        logger.info(f"Заказ #{order_id} опознан как AutoSMM (Услуга: {service_id}, Кол-во: {total_amount}). Отправляем запрос ссылки.")

        # Мгновенно отправляем покупателю в чат FunPay сообщение с просьбой отправить ссылку!
        greeting_text = (
            f"👋 Здравствуйте, {buyer_username}! Спасибо за ваш заказ #{order_id}!\n\n"
            f"📦 Лот: {lot_title}\n"
            f"🔢 Количество: {total_amount} шт.\n\n"
            f"🔗 Пожалуйста, отправьте в этот чат ссылку на ваш профиль / публикацию / канал для запуска накрутки."
        )
        try:
            c.send_message(chat_id, greeting_text, buyer_username)
        except Exception as ex:
            logger.error(f"Не удалось отправить приветственное сообщение в чат #{chat_id}: {ex}")

    except Exception as ex:
        logger.error(f"Ошибка в bind_to_new_order: {ex}", exc_info=True)


def msg_hook(c: Cardinal, e: NewMessageEvent) -> None:
    """
    Хэндлер сообщений в чатах FunPay.
    """
    msg = e.message
    msg_chat_name = msg.chat_name
    msg_text = (msg.text or "").strip()

    # Игнорируем не-пользовательские и собственные сообщения
    if msg.type != MessageTypes.NON_SYSTEM or msg.author_id == c.account.id:
        return

    payorders = StorageManager.load_payorders()
    order = next((o for o in payorders if o.get("buyer") == msg_chat_name or str(o.get("chat_id")) == str(msg.chat_id)), None)

    # 1. Если возврат средств произошел в чате
    if "вернул деньги покупателю" in msg_text.lower():
        if order:
            payorders = [o for o in payorders if o.get("OrderID") != order.get("OrderID")]
            StorageManager.save_payorders(payorders)
            pending_confirmations.pop(msg.chat_id, None)
            logger.info(f"Заказ #{order.get('OrderID')} удален из ожидания из-за возврата.")
        return

    settings = StorageManager.load_settings()
    api_client = SmmApiClient(settings.get("api_url", ""), settings.get("api_key", ""))

    # 2. Обработка команд покупателя
    command_parts = msg_text.split()
    if len(command_parts) >= 2 and command_parts[0].lower() in ["#статус", "/status"]:
        smm_order_id = command_parts[1].strip()
        status_data = api_client.get_order_status(smm_order_id)
        if status_data:
            start_count = status_data.get("start_count", "*")
            remains = status_data.get("remains", "*")
            status_title = status_data.get("status", "Неизвестно")
            reply = (
                f"📈 Статус заказа #{smm_order_id}:\n"
                f"⠀∟ 📊 Статус: {status_title}\n"
                f"⠀∟ 🔢 Начальное количество: {start_count}\n"
                f"⠀∟ 👀 Осталось накрутить: {remains}"
            )
            try:
                c.send_message(msg.chat_id, reply, msg_chat_name)
            except Exception as ex:
                logger.error(f"Ошибка при отправке статуса: {ex}")
        else:
            try:
                c.send_message(msg.chat_id, f"🔴 Не удалось получить статус заказа #{smm_order_id}. Проверьте номер заказа.", msg_chat_name)
            except Exception:
                pass
        return

    if len(command_parts) >= 2 and command_parts[0].lower() in ["#рефилл", "/refill"]:
        smm_order_id = command_parts[1].strip()
        success, res = api_client.refill_order(smm_order_id)
        if success:
            try:
                c.send_message(msg.chat_id, f"✅ Запрос на докрутку (рефилл) для заказа #{smm_order_id} успешно отправлен!", msg_chat_name)
            except Exception as ex:
                logger.error(f"Ошибка при отправке подтверждения рефилла: {ex}")
        else:
            safe_err = ErrorSanitizer.sanitize_for_buyer(str(res))
            try:
                c.send_message(msg.chat_id, f"🔴 Ошибка при запросе рефилла: {safe_err}", msg_chat_name)
            except Exception:
                pass
        return

    # 3. Если этот покупатель находится на этапе подтверждения (+ или -)
    if msg.chat_id in pending_confirmations:
        active_order = pending_confirmations[msg.chat_id]

        if msg_text == "+":
            # Запуск заказа в SMM!
            pending_confirmations.pop(msg.chat_id, None)
            execute_smm_order(c, active_order, api_client, settings)
            return

        elif msg_text == "-":
            # Отмена заказа покупателем
            pending_confirmations.pop(msg.chat_id, None)
            payorders = [o for o in payorders if o.get("OrderID") != active_order.get("OrderID")]
            StorageManager.save_payorders(payorders)
            try:
                c.send_message(msg.chat_id, f"❌ Заказ #{active_order.get('OrderID')} отменен. Выполняем возврат средств...", msg_chat_name)
                c.account.refund(active_order.get("OrderID"))
            except Exception as e:
                logger.error(f"Ошибка при возврате заказа #{active_order.get('OrderID')}: {e}")
            return

        elif "http" in msg_text:
            # Пользователь прислал другую ссылку вместо подтверждения
            links = extract_links(msg_text)
            if links:
                process_new_link(c, active_order, links[0], settings, msg.chat_id, msg_chat_name)
                return

        else:
            try:
                c.send_message(
                    msg.chat_id,
                    "⚪ Пожалуйста, отправьте «+» для подтверждения запуска накрутки, «-» для отмены и возврата или пришлите новую ссылку.",
                    msg_chat_name,
                )
            except Exception:
                pass
            return

    # 4. Если заказ в очереди ожидания ссылки
    if order:
        order["chat_id"] = msg.chat_id
        links = extract_links(msg_text)
        if links:
            process_new_link(c, order, links[0], settings, msg.chat_id, msg_chat_name)
        else:
            try:
                c.send_message(
                    msg.chat_id,
                    f"🔗 Пожалуйста, отправьте прямую ссылку для накрутки по заказу #{order.get('OrderID')}.",
                    msg_chat_name,
                )
            except Exception:
                pass


def process_new_link(
    c: Cardinal,
    order: Dict[str, Any],
    link: str,
    settings: Dict[str, Any],
    chat_id: str | int,
    buyer_name: str,
) -> None:
    """
    Валидирует ссылку и запрашивает подтверждение у покупателя (+/-).
    """
    # Проверка на приватность Telegram
    if not settings.get("set_tg_private", False):
        if "t.me" in link and ("/c/" in link or "+" in link):
            try:
                c.send_message(
                    chat_id,
                    "❌ Данный тип ссылки ведет в закрытый канал/группу. Для накрутки ресурс должен быть открытым (публичным). Пожалуйста, пришлите публичную ссылку!",
                    buyer_name,
                )
            except Exception:
                pass
            return

    order["url"] = link
    order["chat_id"] = chat_id

    # Обновляем в payorders
    payorders = StorageManager.load_payorders()
    for idx, o in enumerate(payorders):
        if o.get("OrderID") == order.get("OrderID"):
            payorders[idx] = order
            break
    StorageManager.save_payorders(payorders)

    pending_confirmations[chat_id] = order

    clean_link = link.replace("https://", "").replace("http://", "")
    confirm_text = (
        f"📋 Пожалуйста, проверьте параметры заказа перед запуском:\n\n"
        f"🛒 Заказ: #{order.get('OrderID')} ({order.get('title')})\n"
        f"🔢 Количество: {order.get('Amount')} шт.\n"
        f"🔗 Ссылка: {clean_link}\n\n"
        f"✅ Отправьте «+», если всё верно и можно запускать\n"
        f"❌ Отправьте «-» для отмены заказа и возврата средств\n"
        f"🔄 Или отправьте новую ссылку, если хотите её изменить."
    )
    try:
        c.send_message(chat_id, confirm_text, buyer_name)
    except Exception as ex:
        logger.error(f"Не удалось отправить запрос подтверждения заказа #{order.get('OrderID')}: {ex}")


def execute_smm_order(
    c: Cardinal,
    order: Dict[str, Any],
    api_client: SmmApiClient,
    settings: Dict[str, Any],
) -> None:
    """
    Запускает заказ через SMM API после подтверждения покупателя (+).
    """
    order_id = order.get("OrderID")
    chat_id = order.get("chat_id")
    buyer = order.get("buyer", "")
    service_id = order.get("service_id")
    link = order.get("url")
    amount = order.get("Amount")

    success, result = api_client.create_order(service_id, link, amount)

    if success and (isinstance(result, (int, str)) and str(result).isdigit()):
        smm_order_id = str(result)
        logger.info(f"Заказ #{order_id} успешно создан в SMM: ID #{smm_order_id}")

        # 1. Сохраняем в активные заказы
        orders = StorageManager.load_orders()
        orders[smm_order_id] = {
            "order_id": order_id,
            "service_id": service_id,
            "chat_id": chat_id,
            "buyer": buyer,
            "order_url": link,
            "order_amount": amount,
            "order_price": order.get("OrderPrice", 0.0),
            "order_currency": order.get("OrderCurrency", "₽"),
            "title": order.get("title", ""),
            "partial_amount": 0,
            "orderdatetime": order.get("OrderDateTime"),
            "status": "pending",
        }
        StorageManager.save_orders(orders)

        # 2. Удаляем из очереди ожидания
        payorders = StorageManager.load_payorders()
        payorders = [o for o in payorders if o.get("OrderID") != order_id]
        StorageManager.save_payorders(payorders)

        # 3. Отправляем подтверждение покупателю на FunPay
        buyer_msg = (
            f"🚀 Ваш заказ успешно запущен в работу!\n"
            f"🆔 ID заказа в SMM: {smm_order_id}\n\n"
            f"📋 Доступные команды для проверки:\n"
            f"⠀∟ 📊 #статус {smm_order_id} — узнать актуальный прогресс\n"
            f"⠀∟ 🔄 #рефилл {smm_order_id} — запросить докрутку (если поддерживается)\n\n"
            f"⏳ Время выполнения: от нескольких минут до 24-48 часов."
        )
        try:
            c.send_message(chat_id, buyer_msg, buyer)
        except Exception as ex:
            logger.error(f"Не удалось отправить сообщение о создании заказа #{order_id}: {ex}")

        # Проверяем, есть ли еще заказы от этого покупателя
        remaining_orders = [o for o in payorders if o.get("buyer") == buyer or str(o.get("chat_id")) == str(chat_id)]
        if remaining_orders:
            next_order = remaining_orders[0]
            next_msg = (
                f"ℹ️ У вас есть еще один оплаченный заказ #{next_order.get('OrderID')} ({next_order.get('title')}, {next_order.get('Amount')} шт.)!\n"
                f"🔗 Пожалуйста, отправьте ссылку для его выполнения."
            )
            try:
                c.send_message(chat_id, next_msg, buyer)
            except Exception:
                pass

        # 4. Telegram-уведомление продавцу
        if settings.get("set_alert_neworder", True):
            TelegramNotifier.send_order_created(c, order, smm_order_id, api_client)

    else:
        # Ошибка создания в SMM
        raw_error = str(result)
        logger.error(f"Ошибка создания заказа #{order_id} в SMM API: {raw_error}")

        safe_error = ErrorSanitizer.sanitize_for_buyer(raw_error)
        buyer_err_msg = (
            f"❌ Не удалось запустить накрутку:\n{safe_error}\n\n"
            f"Вы можете прислать другую ссылку для повторной попытки или отправить «-» для отмены заказа."
        )
        try:
            c.send_message(chat_id, buyer_err_msg, buyer)
        except Exception:
            pass

        refunded = False
        if settings.get("set_refund_smm", True):
            try:
                c.account.refund(order_id)
                refunded = True
                logger.info(f"Автовозврат средств по заказу #{order_id} выполнен.")
                c.send_message(chat_id, f"💳 Средства за заказ #{order_id} возвращены на ваш баланс FunPay.", buyer)
                # Удаляем заказ из очереди
                payorders = StorageManager.load_payorders()
                payorders = [o for o in payorders if o.get("OrderID") != order_id]
                StorageManager.save_payorders(payorders)
            except Exception as ex:
                logger.error(f"Не удалось выполнить автовозврат по заказу #{order_id}: {ex}")

        # Уведомление продавцу с полными техническими данными
        if settings.get("set_alert_errororder", True):
            TelegramNotifier.send_order_error(c, order, raw_error, refunded)


# ---------------------------------------------------------------------------
# Фоновый поток проверки статусов заказов (Order Checker Thread)
# ---------------------------------------------------------------------------
def start_checker_thread(cardinal: Cardinal) -> None:
    threading.Thread(target=process_orders_loop, args=[cardinal], daemon=True).start()


def process_orders_loop(c: Cardinal) -> None:
    """
    Фоновый цикл опроса статусов активных заказов раз в 60 секунд.
    """
    logger.info(f"$MAGENTA[{LOGGER_PREFIX}]$RESET Фоновый чекер заказов запущен (интервал 60с).")
    while True:
        try:
            time.sleep(60)
            settings = StorageManager.load_settings()
            api_client = SmmApiClient(settings.get("api_url", ""), settings.get("api_key", ""))
            if not api_client.is_configured():
                continue

            orders = StorageManager.load_orders()
            if not orders:
                continue

            order_ids = list(orders.keys())
            batch_status = api_client.get_multiple_orders_status(order_ids)

            orders_to_delete = []

            for smm_id, order_info in list(orders.items()):
                status_info = None
                if batch_status and smm_id in batch_status and isinstance(batch_status[smm_id], dict):
                    status_info = batch_status[smm_id]
                else:
                    status_info = api_client.get_order_status(smm_id)

                if not status_info or "status" not in status_info:
                    continue

                raw_status = str(status_info.get("status", "")).strip()
                status_lower = raw_status.lower()
                order_info["status"] = raw_status
                order_info["partial_amount"] = int(status_info.get("remains", 0))

                fp_order_id = order_info.get("order_id")
                chat_id = order_info.get("chat_id")
                buyer = order_info.get("buyer", "")

                # 1. Заказ выполнен
                if "completed" in status_lower:
                    logger.info(f"Заказ #{fp_order_id} (SMM #{smm_id}) выполнен!")
                    completion_msg = (
                        f"✅ Заказ #{fp_order_id} успешно выполнен!\n\n"
                        f"Пожалуйста, подтвердите выполнение на странице заказа:\n"
                        f"https://funpay.com/orders/{fp_order_id}/\n\n"
                        f"Будем очень благодарны за ваш отзыв! ⭐"
                    )
                    try:
                        c.send_message(chat_id, completion_msg, buyer)
                    except Exception as e:
                        logger.error(f"Не удалось отправить сообщение о завершении #{fp_order_id}: {e}")

                    TelegramNotifier.send_order_completed(c, order_info, smm_id)
                    orders_to_delete.append(smm_id)

                # 2. Заказ отменен сервисом
                elif "cancel" in status_lower:
                    logger.warning(f"Заказ #{fp_order_id} (SMM #{smm_id}) отменен сервисом SMM!")
                    cancel_msg = f"❌ Заказ #{fp_order_id} был отменен сервисом накрутки."
                    try:
                        c.send_message(chat_id, cancel_msg, buyer)
                        if settings.get("set_refund_smm", True):
                            c.account.refund(fp_order_id)
                            c.send_message(chat_id, f"💳 Средства за заказ #{fp_order_id} возвращены на ваш баланс FunPay.", buyer)
                    except Exception as e:
                        logger.error(f"Ошибка при обработке отмены #{fp_order_id}: {e}")

                    orders_to_delete.append(smm_id)

                # 3. Частичное выполнение (Partial)
                elif "partial" in status_lower:
                    remains = int(status_info.get("remains", 0))
                    logger.warning(f"Заказ #{fp_order_id} (SMM #{smm_id}) выполнен частично. Остаток: {remains}")

                    if settings.get("set_recreated_order", False) and remains > 0:
                        # Пробуем перезапустить на остаток
                        success, new_smm_id = api_client.create_order(
                            order_info.get("service_id"), order_info.get("order_url"), remains
                        )
                        if success:
                            orders[str(new_smm_id)] = {
                                "order_id": fp_order_id,
                                "service_id": order_info.get("service_id"),
                                "chat_id": chat_id,
                                "buyer": buyer,
                                "order_url": order_info.get("order_url"),
                                "order_amount": remains,
                                "order_price": order_info.get("order_price"),
                                "order_currency": order_info.get("order_currency"),
                                "title": order_info.get("title"),
                                "partial_amount": 0,
                                "orderdatetime": order_info.get("orderdatetime"),
                                "status": "pending",
                            }
                            recreated_msg = (
                                f"📈 Заказ #{fp_order_id} был частично выполнен и автоматически пересоздан на остаток!\n"
                                f"🆔 Новый номер в SMM: #{new_smm_id}\n"
                                f"⏳ Остаток выполнения: {remains} шт."
                            )
                            try:
                                c.send_message(chat_id, recreated_msg, buyer)
                            except Exception:
                                pass
                        else:
                            try:
                                c.send_message(chat_id, f"⚠️ Заказ #{fp_order_id} выполнен частично. Остаток: {remains} шт.", buyer)
                            except Exception:
                                pass
                    else:
                        try:
                            c.send_message(chat_id, f"⚠️ Заказ #{fp_order_id} выполнен частично. Остаток: {remains} шт.", buyer)
                        except Exception:
                            pass

                    orders_to_delete.append(smm_id)

            # Удаляем завершенные / отмененные
            for oid in orders_to_delete:
                orders.pop(oid, None)

            StorageManager.save_orders(orders)

        except Exception as ex:
            logger.error(f"Ошибка в цикле process_orders_loop: {ex}", exc_info=True)


# ---------------------------------------------------------------------------
# Telegram UI: Главная панель управления (/autosmm)
# ---------------------------------------------------------------------------
def build_main_menu(c: Cardinal) -> Tuple[str, InlineKeyboardMarkup]:
    """
    Генерирует текст и инлайн-клавиатуру главной панели управления плагина.
    """
    settings = StorageManager.load_settings()
    api_url = settings.get("api_url", "").strip()
    api_key = settings.get("api_key", "").strip()

    # 1. API URL
    if api_url:
        url_display = f"<code>{escape(api_url)}</code>"
    else:
        url_display = "⚠️ <b>Не установлен! Требуется настройка</b>"

    # 2. API Token (скрыт под спойлер)
    if api_key:
        token_display = f'<span class="tg-spoiler">{escape(api_key)}</span>'
    else:
        token_display = "⚠️ <b>Не установлен! Требуется настройка</b>"

    # 3. Баланс SMM
    api_client = SmmApiClient(api_url, api_key)
    if api_client.is_configured():
        bal, curr = api_client.get_balance()
        if bal is not None:
            smm_bal_display = f"<code>{bal:.2f} {curr}</code>"
        else:
            smm_bal_display = "⚠️ <i>Ошибка подключения к API</i>"
    else:
        smm_bal_display = "<i>Требуется настройка API URL и Token</i>"

    # 4. Баланс FunPay
    try:
        fp_bal = c.get_balance()
        fp_bal_display = f"<code>{fp_bal.total_rub}₽ | {fp_bal.available_usd}$ | {fp_bal.total_eur}€</code>"
    except Exception:
        fp_bal_display = "<i>Недоступен</i>"

    # 5. Счетчики очереди
    payorders = StorageManager.load_payorders()
    orders = StorageManager.load_orders()
    pending_count = len(payorders)
    active_count = len(orders)

    text = (
        f"╔══════════════════════════╗\n"
        f"       🤖 <b>AutoSMM Control Panel</b>\n"
        f"╚══════════════════════════╝\n\n"
        f"🔗 <b>API URL:</b> {url_display}\n"
        f"🔑 <b>API Token:</b> {token_display}\n"
        f"💰 <b>Баланс SMM:</b> {smm_bal_display}\n"
        f"💳 <b>Баланс FunPay:</b> {fp_bal_display}\n\n"
        f"📊 <b>Очередь заказов:</b>\n"
        f"  • ⏳ Ожидают ссылку: <b>{pending_count}</b>\n"
        f"  • ⚙️ В работе в SMM: <b>{active_count}</b>\n\n"
        f"ℹ️ <i>В описании лота укажите:</i> <code>ID: &lt;номер_услуги&gt;</code>\n"
        f"<i>(опционально множитель:</i> <code>#Quan: 1000</code><i>)</i>\n\n"
        f"👨‍💻 <b>Автор плагина:</b> @klaymov"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    btn_url = InlineKeyboardButton("🔗 API URL", callback_data="autosmm_set_url")
    btn_key = InlineKeyboardButton("🔑 API Token", callback_data="autosmm_set_key")
    btn_balance = InlineKeyboardButton("💰 Баланс", callback_data="autosmm_check_bal")
    btn_refresh = InlineKeyboardButton("🔄 Обновить", callback_data="autosmm_refresh")
    btn_pending = InlineKeyboardButton(f"⏳ Ожидают ({pending_count})", callback_data="autosmm_pending_orders")
    btn_active = InlineKeyboardButton(f"📋 В работе ({active_count})", callback_data="autosmm_active_orders")
    btn_settings = InlineKeyboardButton("⚙️ Настройки и уведомления", callback_data="autosmm_settings_menu")
    btn_readme = InlineKeyboardButton("📖 Инструкция", url=README_URL)

    kb.row(btn_url, btn_key)
    kb.row(btn_balance, btn_refresh)
    kb.row(btn_pending, btn_active)
    kb.row(btn_settings)
    kb.row(btn_readme)

    return text, kb


def build_settings_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    """
    Генерирует инлайн-клавиатуру для тумблеров настроек.
    """
    kb = InlineKeyboardMarkup(row_width=1)

    def icon(flag: bool) -> str:
        return "🟢" if flag else "🔴"

    kb.add(
        InlineKeyboardButton(
            f"{icon(settings.get('set_alert_neworder', True))} Уведомление о новом заказе",
            callback_data="autosmm_toggle:set_alert_neworder",
        ),
        InlineKeyboardButton(
            f"{icon(settings.get('set_alert_errororder', True))} Уведомление об ошибке заказа",
            callback_data="autosmm_toggle:set_alert_errororder",
        ),
        InlineKeyboardButton(
            f"{icon(settings.get('set_alert_smmbalance', True))} Уведомление о балансе SMM",
            callback_data="autosmm_toggle:set_alert_smmbalance",
        ),
        InlineKeyboardButton(
            f"{icon(settings.get('set_refund_smm', True))} Автовозврат на FunPay при ошибке",
            callback_data="autosmm_toggle:set_refund_smm",
        ),
        InlineKeyboardButton(
            f"{icon(settings.get('set_start_mess', True))} Сообщение при старте Cardinal",
            callback_data="autosmm_toggle:set_start_mess",
        ),
        InlineKeyboardButton(
            f"{icon(settings.get('set_tg_private', False))} Разрешить закрытые TG ссылки",
            callback_data="autosmm_toggle:set_tg_private",
        ),
        InlineKeyboardButton(
            f"{icon(settings.get('set_recreated_order', False))} Авто-пересоздание остатка (Partial)",
            callback_data="autosmm_toggle:set_recreated_order",
        ),
        InlineKeyboardButton("⬅️ Назад в меню", callback_data="autosmm_main_menu"),
    )
    return kb


# ---------------------------------------------------------------------------
# Регистрация команд и Telegram хэндлеров
# ---------------------------------------------------------------------------
def init_telegram_ui(cardinal: Cardinal, *args) -> None:
    # 1. Стартовое приветствие
    settings = StorageManager.load_settings()
    if settings.get("set_start_mess", True):
        TelegramNotifier.send_smm_start_info(cardinal)

    if not cardinal.telegram or not cardinal.telegram.bot:
        return

    tg = cardinal.telegram
    bot = tg.bot

    # Лог успешной инициализации
    logger.info(f"$MAGENTA[{LOGGER_PREFIX}]$RESET Плагин автонакрутки от @klaymov успешно инициализирован! (v{VERSION})")

    # Хэндлер команды /autosmm
    def cmd_autosmm(m: types.Message):
        text, kb = build_main_menu(cardinal)
        bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

    # Хэндлер команды /check_balance
    def cmd_check_balance(m: types.Message):
        settings_data = StorageManager.load_settings()
        api_client = SmmApiClient(settings_data.get("api_url", ""), settings_data.get("api_key", ""))
        bal, curr = api_client.get_balance()
        smm_text = f"<code>{bal:.2f} {curr}</code>" if bal is not None else "⚠️ Ошибка подключения к API"

        try:
            fp_bal = cardinal.get_balance()
            fp_text = f"<code>{fp_bal.total_rub}₽ | {fp_bal.available_usd}$ | {fp_bal.total_eur}€</code>"
        except Exception:
            fp_text = "Недоступен"

        reply = (
            f"💰 <b>Баланс SMM:</b> {smm_text}\n"
            f"💳 <b>Баланс FunPay:</b> {fp_text}\n\n"
            f"👨‍💻 <i>Плагин: @klaymov</i>"
        )
        bot.reply_to(m, reply, parse_mode="HTML")

    # Обработка Callback Query
    def handle_callbacks(call: types.CallbackQuery):
        data = call.data
        chat_id = call.message.chat.id
        msg_id = call.message.message_id

        # Главное меню / Обновить
        if data in ["autosmm_main_menu", "autosmm_refresh"]:
            text, kb = build_main_menu(cardinal)
            try:
                bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            except Exception:
                bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            bot.answer_callback_query(call.id, "Данные обновлены!")

        # Быстрая проверка баланса из меню
        elif data == "autosmm_check_bal":
            s_data = StorageManager.load_settings()
            client = SmmApiClient(s_data.get("api_url", ""), s_data.get("api_key", ""))
            bal, curr = client.get_balance()
            if bal is not None:
                alert_text = f"Баланс SMM: {bal:.2f} {curr}"
            else:
                alert_text = "Ошибка подключения к SMM API! Проверьте URL и ключ."
            bot.answer_callback_query(call.id, alert_text, show_alert=True)

        # Переход в настройки тумблеров
        elif data == "autosmm_settings_menu":
            current_s = StorageManager.load_settings()
            kb = build_settings_keyboard(current_s)
            bot.edit_message_text(
                "⚙️ <b>Настройки и уведомления плагина AutoSMM:</b>\n\n<i>Нажмите на опцию для переключения:</i>",
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
            bot.answer_callback_query(call.id)

        # Переключение тумблеров
        elif data.startswith("autosmm_toggle:"):
            setting_name = data.split(":")[1]
            current_s = StorageManager.load_settings()
            if setting_name in current_s:
                current_s[setting_name] = not current_s[setting_name]
                StorageManager.save_settings(current_s)
            kb = build_settings_keyboard(current_s)
            try:
                bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=kb)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Настройка сохранена!")

        # Список заказов, ожидающих ссылку
        elif data == "autosmm_pending_orders":
            payorders = StorageManager.load_payorders()
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("⬅️ Назад в меню", callback_data="autosmm_main_menu"))

            if not payorders:
                orders_text = "⏳ <b>Очередь ожидания пуста.</b>\nВсе покупатели предоставили ссылки."
            else:
                orders_text = f"⏳ <b>Заказы, ожидающие ссылку ({len(payorders)}):</b>\n\n"
                for o in payorders:
                    clean_l = escape(str(o.get("url") or "Еще не прислал"))
                    orders_text += (
                        f"🆔 <b>FunPay:</b> #{o.get('OrderID')} | 👤 <code>{escape(str(o.get('buyer')))}</code>\n"
                        f"⠀∟ 🛒 <b>Лот:</b> {escape(str(o.get('title')))}\n"
                        f"⠀∟ 🔢 <b>Кол-во:</b> {o.get('Amount')} шт.\n"
                        f"⠀∟ 📅 <b>Дата:</b> {o.get('OrderDateTime')}\n"
                        f"⠀∟ 🔗 <b>Ссылка:</b> {clean_l}\n\n"
                    )

            bot.edit_message_text(orders_text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            bot.answer_callback_query(call.id)

        # Список активных заказов в SMM
        elif data == "autosmm_active_orders":
            orders = StorageManager.load_orders()
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("⬅️ Назад в меню", callback_data="autosmm_main_menu"))

            if not orders:
                orders_text = "📋 <b>Нет активных заказов в SMM.</b>"
            else:
                orders_text = f"📋 <b>Активные заказы в SMM ({len(orders)}):</b>\n\n"
                for smm_id, o in orders.items():
                    clean_l = escape(str(o.get("order_url", "")).replace("https://", "").replace("http://", ""))
                    orders_text += (
                        f"🆔 <b>SMM:</b> #{smm_id} (FunPay #{o.get('order_id')})\n"
                        f"⠀∟ 👤 <b>Покупатель:</b> <code>{escape(str(o.get('buyer')))}</code>\n"
                        f"⠀∟ 📊 <b>Статус:</b> <code>{o.get('status')}</code>\n"
                        f"⠀∟ 🔢 <b>Кол-во:</b> {o.get('order_amount')} шт. (Остаток: {o.get('partial_amount', 0)})\n"
                        f"⠀∟ 📅 <b>Дата:</b> {o.get('orderdatetime')}\n"
                        f"⠀∟ 🔗 <b>Ссылка:</b> <code>{clean_l}</code>\n\n"
                    )

            bot.edit_message_text(orders_text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            bot.answer_callback_query(call.id)

        # Ввод API URL
        elif data == "autosmm_set_url":
            cancel_kb = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Отмена", callback_data="autosmm_cancel_input"))
            cur_s = StorageManager.load_settings()
            cur_url = cur_s.get("api_url", "")
            cur_display = f"<code>{escape(cur_url)}</code>" if cur_url else "<i>Не установлен</i>"

            res = bot.send_message(
                chat_id,
                f"🔗 <b>Настройка API URL SMM-сервиса</b>\n\n"
                f"<b>Текущий URL:</b> {cur_display}\n\n"
                f"<i>Пример: <code>https://soc-rocket.ru/api/v2/</code></i>\n\n"
                f"⬇️ <b>Отправьте новый URL ответным сообщением:</b>",
                parse_mode="HTML",
                reply_markup=cancel_kb,
            )
            tg.set_state(chat_id, res.message_id, call.from_user.id, "autosmm_state_url")
            bot.answer_callback_query(call.id)

        # Ввод API Token
        elif data == "autosmm_set_key":
            cancel_kb = InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Отмена", callback_data="autosmm_cancel_input"))
            cur_s = StorageManager.load_settings()
            cur_k = cur_s.get("api_key", "")
            cur_display = f'<span class="tg-spoiler">{escape(cur_k)}</span>' if cur_k else "<i>Не установлен</i>"

            res = bot.send_message(
                chat_id,
                f"🔑 <b>Настройка API Token SMM-сервиса</b>\n\n"
                f"<b>Текущий токен:</b> {cur_display}\n\n"
                f"⬇️ <b>Отправьте ваш новый API-токен ответным сообщением:</b>",
                parse_mode="HTML",
                reply_markup=cancel_kb,
            )
            tg.set_state(chat_id, res.message_id, call.from_user.id, "autosmm_state_key")
            bot.answer_callback_query(call.id)

        # Отмена ввода
        elif data == "autosmm_cancel_input":
            tg.clear_state(chat_id, call.from_user.id, del_msg=True)
            bot.answer_callback_query(call.id, "Ввод отменен.")

        # Интеграция со страницей настроек плагинов Cardinal (SETTINGS_PAGE = True)
        elif data.startswith(f"47:{UUID}:") or data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"):
            text, kb = build_main_menu(cardinal)
            try:
                bot.edit_message_text(text, chat_id, msg_id, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            except Exception:
                bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            bot.answer_callback_query(call.id)

    # Обработка текстового ввода для состояний
    def handle_text_states(message: types.Message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        state_data = tg.get_state(chat_id, user_id)

        if not state_data or "state" not in state_data:
            return

        current_state = state_data["state"]

        if current_state == "autosmm_state_url":
            new_url = message.text.strip()
            # Нормализация протокола и слэша
            if not new_url.startswith("http://") and not new_url.startswith("https://"):
                new_url = f"https://{new_url}"
            if not new_url.endswith("/") and not new_url.endswith(".php"):
                new_url += "/"

            settings_data = StorageManager.load_settings()
            settings_data["api_url"] = new_url
            StorageManager.save_settings(settings_data)

            tg.clear_state(chat_id, user_id, del_msg=True)
            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass

            bot.send_message(
                chat_id,
                f"✅ <b>API URL успешно обновлен!</b>\nНовое значение: <code>{escape(new_url)}</code>",
                parse_mode="HTML",
            )
            # Отправляем обновленное главное меню
            text, kb = build_main_menu(cardinal)
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

        elif current_state == "autosmm_state_key":
            new_key = message.text.strip()
            settings_data = StorageManager.load_settings()
            settings_data["api_key"] = new_key
            StorageManager.save_settings(settings_data)

            tg.clear_state(chat_id, user_id, del_msg=True)
            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass

            # Проверяем связь с API сразу при установке ключа!
            api_client = SmmApiClient(settings_data.get("api_url", ""), new_key)
            bal, curr = api_client.get_balance()
            if bal is not None:
                bal_report = f"🟢 <b>Связь с API успешно установлена!</b>\n💰 Текущий баланс: <code>{bal:.2f} {curr}</code>"
            else:
                bal_report = "⚠️ <b>Внимание:</b> Токен сохранен, но не удалось получить баланс. Проверьте правильность URL и токена."

            bot.send_message(
                chat_id,
                f"✅ <b>API Token успешно обновлен!</b>\n\n{bal_report}",
                parse_mode="HTML",
            )
            # Отправляем обновленное главное меню
            text, kb = build_main_menu(cardinal)
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

    # Регистрация хэндлеров в Telegram
    tg.cbq_handler(
        handle_callbacks,
        lambda c: c.data.startswith("autosmm_")
        or c.data.startswith(f"47:{UUID}:")
        or c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"),
    )

    tg.msg_handler(
        handle_text_states,
        func=lambda m: tg.check_state(m.chat.id, m.from_user.id, "autosmm_state_url")
        or tg.check_state(m.chat.id, m.from_user.id, "autosmm_state_key"),
    )

    tg.msg_handler(cmd_autosmm, commands=["autosmm"])
    tg.msg_handler(cmd_check_balance, commands=["check_balance"])

    cardinal.add_telegram_commands(
        UUID,
        [
            ("autosmm", f"панель управления {NAME}", True),
            ("check_balance", f"баланс {NAME}", True),
        ],
    )


# ---------------------------------------------------------------------------
# Точки связывания с Cardinal (Хуки)
# ---------------------------------------------------------------------------
BIND_TO_PRE_INIT = [init_telegram_ui]
BIND_TO_POST_INIT = [start_checker_thread]
BIND_TO_NEW_ORDER = [bind_to_new_order]
BIND_TO_NEW_MESSAGE = [msg_hook]
