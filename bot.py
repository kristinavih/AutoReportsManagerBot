from __future__ import annotations

import asyncio
import html
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

from buyer_statistics import start_buyer_statistics_scheduler
from buyer_leads import (
    router as buyer_leads_router,
    build_summary_text,
    buyer_leads_summary_keyboard,
    read_today_leads_for_buyer,
)

load_dotenv()

# =========================
# ENV / SETTINGS
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
REPORT_SHEET_ID = os.getenv("REPORT_SHEET_ID", "").strip()
REPORT_SHEET_NAME = os.getenv("REPORT_SHEET_NAME", "Отчеты_баеров").strip()
USERS_SHEET_NAME = os.getenv("USERS_SHEET_NAME", "Пользователи").strip()
BOT_SHEET_NAME = os.getenv("BOT_SHEET_NAME", "bot").strip()
GOOGLE_JSON = os.getenv("GOOGLE_JSON", "credentials.json").strip()

# Резервные ID. Основные роли бот читает из листа «Пользователи».
BOSS_CHAT_ID = int(os.getenv("BOSS_CHAT_ID", "0"))
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0"))

FORM_URL = os.getenv(
    "FORM_URL",
    FORM_URL,
).strip()

MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")
REPORT_REFRESH_TIMEOUT_SECONDS = int(os.getenv("REPORT_REFRESH_TIMEOUT_SECONDS", "300"))
REPORT_RECHECK_DELAY_SECONDS = int(os.getenv("REPORT_RECHECK_DELAY_SECONDS", "30"))

GOOGLE_RETRIES = int(os.getenv("GOOGLE_RETRIES", "5"))
GOOGLE_RETRY_BASE_DELAY = float(os.getenv("GOOGLE_RETRY_BASE_DELAY", "1.5"))
GOOGLE_RETRY_MAX_DELAY = float(os.getenv("GOOGLE_RETRY_MAX_DELAY", "12"))

REMINDER_FIRST_TIME = os.getenv("REMINDER_FIRST_TIME", "10:48")
REMINDER_SECOND_TIME = os.getenv("REMINDER_SECOND_TIME", "10:58")
REMINDER_URGENT_TIME = os.getenv("REMINDER_URGENT_TIME", "11:03")
REMINDER_FINAL_TIME = os.getenv("REMINDER_FINAL_TIME", "11:08")

REPORT_START_ROW = 5
REPORT_MAX_ROWS = 300
USERS_MAX_ROWS = 1000
CACHE_TTL = 60

ROLE_BOSS = "boss"
ROLE_MANAGER = "manager"
ROLE_TEAMLEAD = "teamlead"
ROLE_BUYER = "buyer"
REPORTER_ROLES = {ROLE_TEAMLEAD, ROLE_BUYER}
VALID_ROLES = {ROLE_BOSS, ROLE_MANAGER, ROLE_TEAMLEAD, ROLE_BUYER}
ACTIVE_STATUS = "активен"
INACTIVE_STATUS = "неактивен"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")
if not REPORT_SHEET_ID:
    raise RuntimeError("REPORT_SHEET_ID не указан в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(buyer_leads_router)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# =========================
# GOOGLE SHEETS
# =========================
def google_call_sync(label: str, func, *args):
    last_error = None
    for attempt in range(1, GOOGLE_RETRIES + 1):
        try:
            return func(*args)
        except Exception as exc:
            last_error = exc
            if attempt >= GOOGLE_RETRIES:
                print(f"[GOOGLE ERROR] {label}: {exc!r}", flush=True)
                raise
            delay = min(GOOGLE_RETRY_MAX_DELAY, GOOGLE_RETRY_BASE_DELAY * attempt)
            delay += random.uniform(0, 0.5)
            print(
                f"[GOOGLE RETRY] {label}: {attempt}/{GOOGLE_RETRIES}, "
                f"повтор через {delay:.1f} сек.",
                flush=True,
            )
            time.sleep(delay)
    raise last_error


async def google_call(label: str, func, *args):
    return await asyncio.to_thread(google_call_sync, label, func, *args)


def open_worksheets():
    creds = Credentials.from_service_account_file(GOOGLE_JSON, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = google_call_sync("open spreadsheet", client.open_by_key, REPORT_SHEET_ID)
    report_ws = google_call_sync(
        "open report worksheet", spreadsheet.worksheet, REPORT_SHEET_NAME
    )
    users_ws = google_call_sync(
        "open users worksheet", spreadsheet.worksheet, USERS_SHEET_NAME
    )
    bot_ws = google_call_sync(
        "open bot worksheet", spreadsheet.worksheet, BOT_SHEET_NAME
    )
    return report_ws, users_ws, bot_ws


report_sheet, users_sheet, bot_sheet = open_worksheets()


async def report_get(range_name: str):
    return await google_call(f"report.get {range_name}", report_sheet.get, range_name)


async def report_col_values(col: int):
    return await google_call(f"report.col_values {col}", report_sheet.col_values, col)


async def users_get(range_name: str):
    return await google_call(f"users.get {range_name}", users_sheet.get, range_name)


async def users_update(range_name: str, values: list[list]):
    return await google_call(
        f"users.update {range_name}",
        lambda: users_sheet.update(values=values, range_name=range_name),
    )


async def bot_get(range_name: str):
    return await google_call(f"bot.get {range_name}", bot_sheet.get, range_name)


async def bot_update(range_name: str, values: list[list]):
    return await google_call(
        f"bot.update {range_name}",
        lambda: bot_sheet.update(values=values, range_name=range_name),
    )


# =========================
# DATA MODELS / CACHE
# =========================
@dataclass(slots=True)
class TeamUser:
    row: int
    internal_id: int
    name: str
    telegram_id: str
    username: str
    role: str
    manager_id: Optional[int]
    status: str

    @property
    def is_active(self) -> bool:
        return normalize_status(self.status) == ACTIVE_STATUS


@dataclass(slots=True)
class ReportState:
    name: str
    report: str


users_cache: list[TeamUser] = []
users_cache_time = 0.0
users_cache_lock = asyncio.Lock()
write_lock = asyncio.Lock()
report_update_lock = asyncio.Lock()

# Простые состояния диалогов.
# user Telegram ID -> контекст
pending_add: dict[int, dict] = {}
pending_feedback: set[int] = set()
pending_broadcast: set[int] = set()
pending_access_requests: dict[int, dict] = {}


def safe_html(value) -> str:
    return html.escape(str(value or "").strip())


def normalize_name(value: str) -> str:
    return "".join(str(value or "").strip().lower().replace("ё", "е").split())


def normalize_status(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def parse_int(value) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text.replace(",", ".")))
    except (TypeError, ValueError):
        return None


async def load_users_from_sheet() -> list[TeamUser]:
    rows = await users_get(f"A2:G{USERS_MAX_ROWS}")
    result: list[TeamUser] = []

    for row_number, row in enumerate(rows, start=2):
        internal_id = parse_int(row[0] if len(row) > 0 else None)
        name = str(row[1]).strip() if len(row) > 1 else ""
        telegram_id = str(row[2]).strip() if len(row) > 2 else ""
        username = str(row[3]).strip() if len(row) > 3 else ""
        role = str(row[4]).strip().lower() if len(row) > 4 else ""
        manager_id = parse_int(row[5] if len(row) > 5 else None)
        status = str(row[6]).strip() if len(row) > 6 else ""

        if internal_id is None or not name:
            continue
        if role not in VALID_ROLES:
            print(
                f"[USERS WARNING] Строка {row_number}: неизвестная роль {role!r}",
                flush=True,
            )

        result.append(
            TeamUser(
                row=row_number,
                internal_id=internal_id,
                name=name,
                telegram_id=telegram_id,
                username=username,
                role=role,
                manager_id=manager_id,
                status=status,
            )
        )

    return result


async def get_users(force: bool = False) -> list[TeamUser]:
    global users_cache, users_cache_time
    now = time.time()
    if not force and users_cache and now - users_cache_time < CACHE_TTL:
        return users_cache

    async with users_cache_lock:
        now = time.time()
        if not force and users_cache and now - users_cache_time < CACHE_TTL:
            return users_cache
        users_cache = await load_users_from_sheet()
        users_cache_time = time.time()
        return users_cache


def find_user_by_tg(users: list[TeamUser], telegram_id: int | str) -> Optional[TeamUser]:
    target = str(telegram_id).strip()
    return next((u for u in users if u.telegram_id == target), None)


def find_user_by_internal_id(users: list[TeamUser], internal_id: int) -> Optional[TeamUser]:
    return next((u for u in users if u.internal_id == internal_id), None)


async def get_current_user(telegram_id: int, force: bool = False) -> Optional[TeamUser]:
    users = await get_users(force=force)
    user = find_user_by_tg(users, telegram_id)
    return user if user and user.is_active else None


async def get_role(telegram_id: int) -> Optional[str]:
    user = await get_current_user(telegram_id)
    return user.role if user else None


async def get_boss_user(users: Optional[list[TeamUser]] = None) -> Optional[TeamUser]:
    users = users or await get_users()
    return next(
        (u for u in users if u.is_active and u.role == ROLE_BOSS and u.telegram_id),
        None,
    )


async def get_manager_user(users: Optional[list[TeamUser]] = None) -> Optional[TeamUser]:
    users = users or await get_users()
    return next(
        (u for u in users if u.is_active and u.role == ROLE_MANAGER and u.telegram_id),
        None,
    )


def get_teamlead_buyers(teamlead: TeamUser, users: list[TeamUser]) -> list[TeamUser]:
    return [
        u
        for u in users
        if u.is_active
        and u.role == ROLE_BUYER
        and u.manager_id == teamlead.internal_id
    ]


def get_active_reporters(users: list[TeamUser]) -> list[TeamUser]:
    return [
        u
        for u in users
        if u.is_active and u.role in REPORTER_ROLES and u.telegram_id
    ]


# =========================
# REPORT READING / FILTERING
# =========================
def extract_report_owner(line: str) -> str:
    text = str(line or "").strip()
    if " - " in text:
        return text.split(" - ", 1)[0].strip()
    if "-" in text:
        return text.split("-", 1)[0].strip()
    return text


def is_no_report(value: str) -> bool:
    text = " ".join(str(value or "").lower().replace("ё", "е").split())
    return "нет отчета" in text


async def read_report_lines() -> list[str]:
    values = await report_col_values(2)
    return [str(value).strip() for value in values[:400] if str(value).strip()]


async def load_report_states() -> dict[str, ReportState]:
    end_row = REPORT_START_ROW + REPORT_MAX_ROWS - 1
    rows = await report_get(f"A{REPORT_START_ROW}:B{end_row}")
    report_by_name: dict[str, str] = {}
    names: list[str] = []

    for row in rows:
        name = str(row[0]).strip() if len(row) > 0 else ""
        line = str(row[1]).strip() if len(row) > 1 else ""
        if name:
            names.append(name)
        if line:
            key = normalize_name(extract_report_owner(line))
            if key:
                report_by_name[key] = line

    return {
        normalize_name(name): ReportState(
            name=name,
            report=report_by_name.get(normalize_name(name), ""),
        )
        for name in names
    }


def filter_report_for_users(report_lines: list[str], allowed_users: list[TeamUser]) -> str:
    allowed = {normalize_name(user.name) for user in allowed_users}
    all_known_names = {
        normalize_name(extract_report_owner(line))
        for line in report_lines
        if "-" in line
    }

    headers: list[str] = []
    selected: list[str] = []
    for line in report_lines:
        owner_key = normalize_name(extract_report_owner(line))
        if owner_key in allowed:
            selected.append(line)
        elif owner_key not in all_known_names:
            headers.append(line)

    # В текущей таблице первые строки — дата/запуски/гео. Сохраняем их тимлиду.
    if not headers:
        headers = report_lines[:4]
    return "\n".join(headers[:4] + selected).strip()


async def send_long_message(chat_id: int, text: str, parse_mode: Optional[str] = None):
    if not text:
        await bot.send_message(chat_id, "Отчёт пустой.")
        return

    max_len = 3900
    chunk = ""
    for block in text.split("\n"):
        candidate = block if not chunk else f"{chunk}\n{block}"
        if len(candidate) <= max_len:
            chunk = candidate
            continue
        if chunk:
            await bot.send_message(chat_id, chunk, parse_mode=parse_mode)
        chunk = block
    if chunk:
        await bot.send_message(chat_id, chunk, parse_mode=parse_mode)


async def distribute_final_report(report_lines: Optional[list[str]] = None):
    users = await get_users(force=True)
    report_lines = report_lines or await read_report_lines()

    boss_user = await get_boss_user(users)
    boss_id = int(boss_user.telegram_id) if boss_user else BOSS_CHAT_ID
    if boss_id:
        await bot.send_message(
            boss_id,
            "📌 Отчёт обновлён. Отправляю итоговую сводку.",
            reply_markup=boss_menu(),
        )
        await send_long_message(boss_id, "\n".join(report_lines))

    teamleads = [
        u for u in users if u.is_active and u.role == ROLE_TEAMLEAD and u.telegram_id
    ]
    for teamlead in teamleads:
        buyers = get_teamlead_buyers(teamlead, users)
        team_report = filter_report_for_users(report_lines, buyers)
        title = f"📋 Отчёт команды: {teamlead.name}\n\n"
        await send_long_message(int(teamlead.telegram_id), title + (team_report or "Нет данных."))
        await asyncio.sleep(0.25)


# =========================
# UPDATE VIA bot SHEET
# =========================
def get_first_cell(values: list[list], index: int) -> str:
    if index >= len(values) or not values[index]:
        return ""
    return str(values[index][0] or "").strip()


def parse_bot_updated_at(value) -> Optional[datetime]:
    raw = str(value or "").strip().replace("\xa0", " ")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=MOSCOW_TZ)
        except ValueError:
            continue
    return None


async def clear_bot_sheet_status():
    await bot_update("B1:B4", [[""], [""], [""], [""]])


async def request_report_update() -> datetime:
    started_at = datetime.now(MOSCOW_TZ)
    await clear_bot_sheet_status()
    await asyncio.sleep(1)
    await bot_update(
        "B1:B4",
        [["UPDATE"], ["WAIT"], [""], ["Команда на обновление отправлена"]],
    )
    return started_at


async def wait_report_done(started_at: datetime) -> tuple[bool, str]:
    started = time.time()
    while time.time() - started < REPORT_REFRESH_TIMEOUT_SECONDS:
        values = await bot_get("B2:B4")
        status = get_first_cell(values, 0).upper()
        updated_at_raw = get_first_cell(values, 1)
        message = get_first_cell(values, 2)

        if status == "DONE":
            updated_at = parse_bot_updated_at(updated_at_raw)
            if not updated_at_raw or updated_at is None or updated_at >= started_at - timedelta(seconds=90):
                return True, "✅ Отчёт обновлён."
        if status == "ERROR":
            return False, f"❌ Ошибка обновления отчёта:\n{message or 'Нет текста ошибки.'}"
        await asyncio.sleep(3)

    return False, "⏳ Не дождался статуса DONE. Проверь Apps Script и лист bot."


async def refresh_report_via_bot_sheet() -> tuple[bool, str]:
    async with report_update_lock:
        started_at = await request_report_update()
        return await wait_report_done(started_at)


# =========================
# NOTIFICATIONS / REMINDERS
# =========================
async def send_error_to_manager(text: str):
    users = await get_users()
    manager = await get_manager_user(users)
    manager_id = int(manager.telegram_id) if manager else MANAGER_CHAT_ID
    if not manager_id:
        print(f"[MANAGER ERROR NOT SENT] {text}", flush=True)
        return
    try:
        await bot.send_message(manager_id, text, parse_mode="HTML")
    except Exception as exc:
        print(f"[MANAGER SEND ERROR] {exc!r}", flush=True)


async def safe_send(tg_id: str, text: str, reply_markup=None, parse_mode="HTML") -> bool:
    try:
        await bot.send_message(
            int(tg_id), text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return True
    except Exception as exc:
        await send_error_to_manager(
            "⚠️ <b>Не удалось отправить сообщение</b>\n\n"
            f"Telegram ID: <code>{safe_html(tg_id)}</code>\n"
            f"Ошибка: <code>{safe_html(exc)}</code>"
        )
        return False


def form_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заполнить отчёт", url=FORM_URL)
    return kb.as_markup()


def reporter_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Открыть форму отчёта", url=FORM_URL)
    kb.button(text="🎯 Мои лиды за сегодня", callback_data="buyer_leads:today")
    kb.button(text="💬 Ошибка / идея", callback_data="feedback:start")
    kb.adjust(1)
    return kb.as_markup()


def late_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Заполнить отчёт", url=FORM_URL)
    kb.button(text="✅ Я заполнил отчёт", callback_data="late:done")
    kb.adjust(1)
    return kb.as_markup()


def first_reminder_text() -> str:
    return "📝 Напоминаю: сегодня нужно заполнить отчёт. Если уже отправил — проигнорируй сообщение."


def second_reminder_text() -> str:
    return "⚠️ Я проверил таблицу — твоего отчёта пока нет. Пожалуйста, заполни форму."


def urgent_reminder_text() -> str:
    return "🚨 Отчёта всё ещё нет. Пожалуйста, заполни форму прямо сейчас."


def late_report_text() -> str:
    return (
        "⚠️ Итоговая сводка уже отправлена, а твоего отчёта в ней не было.\n\n"
        "Заполни форму и нажми «✅ Я заполнил отчёт»."
    )


async def get_missing_reporters() -> list[TeamUser]:
    users = await get_users(force=True)
    reporters = get_active_reporters(users)
    states = await load_report_states()
    missing: list[TeamUser] = []

    for reporter in reporters:
        state = states.get(normalize_name(reporter.name))
        if state is None or not state.report or is_no_report(state.report):
            missing.append(reporter)
    return missing


async def get_confirmed_missing_reporters() -> list[TeamUser]:
    missing = await get_missing_reporters()
    if not missing:
        return []
    await asyncio.sleep(REPORT_RECHECK_DELAY_SECONDS)
    return await get_missing_reporters()


async def refresh_or_report_error() -> bool:
    ok, text = await refresh_report_via_bot_sheet()
    if not ok:
        await send_error_to_manager(
            "⚠️ <b>Ошибка обновления отчёта</b>\n\n" + safe_html(text)
        )
        await clear_bot_sheet_status()
        return False
    return True


async def run_first_reminder():
    if not await refresh_or_report_error():
        return
    users = await get_users(force=True)
    reporters = get_active_reporters(users)
    for user in reporters:
        await safe_send(user.telegram_id, first_reminder_text(), form_keyboard())
        await asyncio.sleep(0.2)
    await clear_bot_sheet_status()


async def run_second_reminder():
    if not await refresh_or_report_error():
        return
    for user in await get_confirmed_missing_reporters():
        await safe_send(user.telegram_id, second_reminder_text(), form_keyboard())
        await asyncio.sleep(0.2)
    await clear_bot_sheet_status()


async def run_urgent_reminder():
    if not await refresh_or_report_error():
        return
    for user in await get_confirmed_missing_reporters():
        await safe_send(user.telegram_id, urgent_reminder_text(), form_keyboard())
        await asyncio.sleep(0.2)
    await clear_bot_sheet_status()


async def run_final_report_and_late_notice():
    if not await refresh_or_report_error():
        return
    missing = await get_confirmed_missing_reporters()
    await distribute_final_report()
    for user in missing:
        await safe_send(user.telegram_id, late_report_text(), late_keyboard())
        await asyncio.sleep(0.2)
    await clear_bot_sheet_status()


def is_weekday_moscow(dt: datetime) -> bool:
    return dt.weekday() < 5


def next_moscow_datetime(hh_mm: str) -> datetime:
    hour, minute = map(int, hh_mm.split(":"))
    now = datetime.now(MOSCOW_TZ)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while not is_weekday_moscow(target):
        target += timedelta(days=1)
    return target


async def daily_reminder_scheduler():
    schedule = [
        (REMINDER_FIRST_TIME, run_first_reminder),
        (REMINDER_SECOND_TIME, run_second_reminder),
        (REMINDER_URGENT_TIME, run_urgent_reminder),
        (REMINDER_FINAL_TIME, run_final_report_and_late_notice),
    ]
    while True:
        candidates = [(next_moscow_datetime(t), t, fn) for t, fn in schedule]
        target, label, func = min(candidates, key=lambda item: item[0])
        seconds = max(1, (target - datetime.now(MOSCOW_TZ)).total_seconds())
        print(f"[SCHEDULER] Следующий запуск {label}: {target.isoformat()}", flush=True)
        await asyncio.sleep(seconds)
        try:
            await func()
        except Exception as exc:
            print(f"[SCHEDULER ERROR] {exc!r}", flush=True)
            await send_error_to_manager(
                f"⚠️ <b>Ошибка планировщика {label}</b>\n\n<code>{safe_html(exc)}</code>"
            )
        await asyncio.sleep(2)


# =========================
# MENUS
# =========================
def boss_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔄 Обновить общий отчёт")
    kb.button(text="🎯 Мои лиды за сегодня")
    kb.button(text="👥 Вся команда")
    kb.button(text="➕ Добавить баера")
    kb.button(text="⭐ Сделать тимлидом")
    kb.button(text="👤 Сделать баером")
    kb.button(text="🔄 Перевести баера")
    kb.button(text="🚫 Отключить пользователя")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def teamlead_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📝 Мой отчёт")
    kb.button(text="🎯 Мои лиды за сегодня")
    kb.button(text="📋 Отчёт моей команды")
    kb.button(text="📊 Статистика моей команды")
    kb.button(text="👥 Моя команда")
    kb.button(text="➕ Добавить баера")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def manager_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔄 Обновить общий отчёт")
    kb.button(text="📊 Получить общий отчёт")
    kb.button(text="👥 Вся команда")
    kb.button(text="📢 Сделать рассылку")
    kb.button(text="🧹 Очистить bot")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def menu_for(user: TeamUser):
    if user.role == ROLE_BOSS:
        return boss_menu()
    if user.role == ROLE_MANAGER:
        return manager_menu()
    if user.role == ROLE_TEAMLEAD:
        return teamlead_menu()
    return None


# =========================
# USER WRITES
# =========================
async def add_user(
    *,
    name: str,
    telegram_id: str,
    username: str,
    role: str,
    manager_id: Optional[int],
) -> tuple[bool, str, Optional[TeamUser]]:
    name = name.strip()
    telegram_id = telegram_id.strip()
    username = username.strip()

    if len(name) < 2:
        return False, "Имя слишком короткое.", None
    if not telegram_id.isdigit():
        return False, "Telegram ID должен состоять только из цифр.", None
    if role not in VALID_ROLES:
        return False, "Некорректная роль.", None
    if role == ROLE_BUYER and manager_id is None:
        return False, "Для баера обязательно выбрать тимлида.", None

    async with write_lock:
        users = await get_users(force=True)
        if find_user_by_tg(users, telegram_id):
            return False, "Пользователь с таким Telegram ID уже существует.", None
        if any(normalize_name(u.name) == normalize_name(name) for u in users):
            return False, "Пользователь с таким именем уже существует.", None

        new_id = max((u.internal_id for u in users), default=0) + 1
        occupied_rows = {u.row for u in users}
        target_row = next(
            (row for row in range(2, USERS_MAX_ROWS + 1) if row not in occupied_rows),
            None,
        )
        if target_row is None:
            return False, "В листе Пользователи нет свободной строки.", None

        await users_update(
            f"A{target_row}:G{target_row}",
            [[
                new_id,
                name,
                telegram_id,
                username,
                role,
                manager_id or "",
                "Активен",
            ]],
        )
        await get_users(force=True)
        new_user = find_user_by_tg(users_cache, telegram_id)
        return True, f"✅ {safe_html(name)} добавлен. Внутренний ID: {new_id}.", new_user


async def set_user_fields(user: TeamUser, *, role=None, manager_id="KEEP", status=None):
    values = await users_get(f"A{user.row}:G{user.row}")
    row = list(values[0]) if values else []
    row += [""] * (7 - len(row))
    if role is not None:
        row[4] = role
    if manager_id != "KEEP":
        row[5] = manager_id or ""
    if status is not None:
        row[6] = status
    await users_update(f"A{user.row}:G{user.row}", [row[:7]])
    await get_users(force=True)


def parse_new_user_line(text: str) -> tuple[Optional[str], Optional[str], str, str]:
    parts = [part.strip() for part in str(text or "").split("|")]
    if len(parts) < 2:
        return None, None, "", "Формат: Имя | Telegram ID | Username"
    name, tg_id = parts[0], parts[1]
    username = parts[2] if len(parts) > 2 else ""
    return name, tg_id, username, ""


# =========================
# START / ACCESS
# =========================
@dp.message(CommandStart())
async def start(message: Message):
    user = await get_current_user(message.from_user.id, force=True)
    if not user:
        kb = InlineKeyboardBuilder()
        kb.button(text="📨 Отправить заявку", callback_data="access:request")
        await message.answer(
            "⛔ <b>У вас нет доступа к этому боту.</b>\n\n"
            "Доступ выдаёт руководитель.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    if user.role == ROLE_BOSS:
        await message.answer(
            f"👑 Вы вошли как <b>{safe_html(user.name)}</b>.",
            parse_mode="HTML",
            reply_markup=boss_menu(),
        )
    elif user.role == ROLE_MANAGER:
        await message.answer(
            f"🛠 Вы вошли как менеджер: <b>{safe_html(user.name)}</b>.",
            parse_mode="HTML",
            reply_markup=manager_menu(),
        )
    elif user.role == ROLE_TEAMLEAD:
        await message.answer(
            f"👥 Вы вошли как тимлид: <b>{safe_html(user.name)}</b>.",
            parse_mode="HTML",
            reply_markup=teamlead_menu(),
        )
        await message.answer("Форма собственного отчёта:", reply_markup=reporter_keyboard())
    elif user.role == ROLE_BUYER:
        await message.answer(
            f"👋 Вы подключены как <b>{safe_html(user.name)}</b>.",
            parse_mode="HTML",
            reply_markup=reporter_keyboard(),
        )
    else:
        await message.answer("⚠️ В таблице указана неизвестная роль. Обратитесь к менеджеру.")


@dp.callback_query(F.data == "access:request")
async def access_request(callback: CallbackQuery):
    await callback.answer("Отправляю заявку…")
    user = callback.from_user
    users = await get_users(force=True)
    boss_user = await get_boss_user(users)
    boss_id = int(boss_user.telegram_id) if boss_user else BOSS_CHAT_ID
    if not boss_id:
        await callback.message.edit_text("Не удалось найти руководителя.")
        return

    pending_access_requests[user.id] = {
        "telegram_id": str(user.id),
        "username": f"@{user.username}" if user.username else "",
        "full_name": user.full_name or "",
    }
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить как баера", callback_data=f"request:add:{user.id}")
    kb.button(text="❌ Отклонить", callback_data=f"request:deny:{user.id}")
    kb.adjust(1)
    await bot.send_message(
        boss_id,
        "📨 <b>Новая заявка на доступ</b>\n\n"
        f"Имя: {safe_html(user.full_name)}\n"
        f"Username: {safe_html('@' + user.username if user.username else 'нет')}\n"
        f"Telegram ID: <code>{user.id}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.message.edit_text("📨 Заявка отправлена руководителю.")


# =========================
# GENERAL ACTIONS
# =========================
@dp.message(F.text == "📝 Мой отчёт")
async def my_report_button(message: Message):
    user = await get_current_user(message.from_user.id)
    if not user or user.role not in REPORTER_ROLES:
        return
    await message.answer("Откройте форму:", reply_markup=form_keyboard())


@dp.message(F.text == "🎯 Мои лиды за сегодня")
async def my_today_leads_button(message: Message):
    user = await get_current_user(message.from_user.id, force=True)

    if not user:
        return

    if user.role not in {ROLE_BUYER, ROLE_TEAMLEAD, ROLE_BOSS}:
        await message.answer("Эта функция недоступна для вашей роли.")
        return

    leads = await read_today_leads_for_buyer(user.internal_id)

    await message.answer(
        build_summary_text(user, leads),
        parse_mode="HTML",
        reply_markup=buyer_leads_summary_keyboard(),
    )


@dp.message(F.text == "🔄 Обновить общий отчёт")
async def update_full_report(message: Message):
    user = await get_current_user(message.from_user.id)
    if not user or user.role not in {ROLE_BOSS, ROLE_MANAGER}:
        return
    await message.answer("🔄 Обновляю отчёт…")
    ok, text = await refresh_report_via_bot_sheet()
    await message.answer(text, reply_markup=menu_for(user))
    if ok:
        await send_long_message(message.chat.id, "\n".join(await read_report_lines()))
    await clear_bot_sheet_status()


@dp.message(F.text == "📊 Получить общий отчёт")
async def get_full_report(message: Message):
    user = await get_current_user(message.from_user.id)
    if not user or user.role != ROLE_MANAGER:
        return
    await send_long_message(message.chat.id, "\n".join(await read_report_lines()))


@dp.message(F.text == "📋 Отчёт моей команды")
async def get_team_report(message: Message):
    user = await get_current_user(message.from_user.id, force=True)
    if not user or user.role != ROLE_TEAMLEAD:
        return
    users = await get_users()
    buyers = get_teamlead_buyers(user, users)
    report = filter_report_for_users(await read_report_lines(), buyers)
    await send_long_message(message.chat.id, report or "По вашей команде пока нет данных.")


@dp.message(F.text == "📊 Статистика моей команды")
async def get_team_stats(message: Message):
    user = await get_current_user(message.from_user.id)
    if not user or user.role != ROLE_TEAMLEAD:
        return
    # Импорт внутри обработчика убирает циклическую зависимость.
    from buyer_statistics import send_statistics_to_one_teamlead

    await message.answer("📊 Формирую статистику вашей команды…")
    await send_statistics_to_one_teamlead(bot, message.from_user.id)


@dp.message(F.text.in_({"👥 Вся команда", "👥 Моя команда"}))
async def show_team(message: Message):
    current = await get_current_user(message.from_user.id, force=True)
    if not current:
        return
    users = await get_users()

    if message.text == "👥 Моя команда":
        if current.role != ROLE_TEAMLEAD:
            return
        team = get_teamlead_buyers(current, users)
        lines = [f"👥 <b>Команда {safe_html(current.name)}: {len(team)}</b>"]
        for user in team:
            lines.append(f"• {safe_html(user.name)} — {safe_html(user.status)}")
    else:
        if current.role not in {ROLE_BOSS, ROLE_MANAGER}:
            return
        lines = ["👥 <b>Структура команды</b>"]
        teamleads = [u for u in users if u.is_active and u.role == ROLE_TEAMLEAD]
        for tl in teamleads:
            buyers = get_teamlead_buyers(tl, users)
            lines.append(f"\n<b>{safe_html(tl.name)}</b> — {len(buyers)} баеров")
            for buyer in buyers:
                lines.append(f"  • {safe_html(buyer.name)}")
        unassigned = [u for u in users if u.is_active and u.role == ROLE_BUYER and u.manager_id is None]
        if unassigned:
            lines.append("\n⚠️ <b>Без тимлида</b>")
            lines.extend(f"  • {safe_html(u.name)}" for u in unassigned)

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=menu_for(current))


# =========================
# ADD BUYER
# =========================
@dp.message(F.text == "➕ Добавить баера")
async def add_buyer_start(message: Message):
    current = await get_current_user(message.from_user.id, force=True)
    if not current or current.role not in {ROLE_BOSS, ROLE_TEAMLEAD}:
        return

    if current.role == ROLE_TEAMLEAD:
        pending_add[message.from_user.id] = {
            "manager_id": current.internal_id,
            "role": ROLE_BUYER,
        }
        await message.answer(
            "Введите данные одной строкой:\n\n"
            "<code>Имя | Telegram ID | Username</code>\n\n"
            "Username можно оставить пустым.",
            parse_mode="HTML",
        )
        return

    users = await get_users()
    teamleads = [u for u in users if u.is_active and u.role == ROLE_TEAMLEAD]
    if not teamleads:
        await message.answer("Сначала нужно назначить хотя бы одного тимлида.")
        return
    kb = InlineKeyboardBuilder()
    for tl in teamleads:
        kb.button(text=tl.name, callback_data=f"add:tl:{tl.internal_id}")
    kb.adjust(1)
    await message.answer("К какому тимлиду добавить баера?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("add:tl:"))
async def add_buyer_choose_teamlead(callback: CallbackQuery):
    current = await get_current_user(callback.from_user.id)
    if not current or current.role != ROLE_BOSS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    manager_id = int(callback.data.rsplit(":", 1)[1])
    pending_add[callback.from_user.id] = {"manager_id": manager_id, "role": ROLE_BUYER}
    await callback.answer()
    await callback.message.answer(
        "Введите данные одной строкой:\n\n"
        "<code>Имя | Telegram ID | Username</code>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("request:add:"))
async def add_from_access_request(callback: CallbackQuery):
    current = await get_current_user(callback.from_user.id)
    if not current or current.role != ROLE_BOSS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_tg_id = int(callback.data.rsplit(":", 1)[1])
    request = pending_access_requests.get(request_tg_id, {})
    users = await get_users(force=True)
    teamleads = [u for u in users if u.is_active and u.role == ROLE_TEAMLEAD]
    kb = InlineKeyboardBuilder()
    for tl in teamleads:
        kb.button(
            text=tl.name,
            callback_data=f"request:tl:{request_tg_id}:{tl.internal_id}",
        )
    kb.adjust(1)
    await callback.answer()
    await callback.message.answer(
        f"Выберите тимлида для {safe_html(request.get('full_name', str(request_tg_id)))}:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@dp.callback_query(F.data.startswith("request:tl:"))
async def request_choose_teamlead(callback: CallbackQuery):
    current = await get_current_user(callback.from_user.id)
    if not current or current.role != ROLE_BOSS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, tg_raw, tl_raw = callback.data.split(":")
    request = pending_access_requests.get(int(tg_raw))
    if not request:
        await callback.answer("Заявка устарела", show_alert=True)
        return
    pending_add[callback.from_user.id] = {
        "manager_id": int(tl_raw),
        "role": ROLE_BUYER,
        "telegram_id": request["telegram_id"],
        "username": request.get("username", ""),
    }
    await callback.answer()
    await callback.message.answer(
        "Введите имя для таблицы, например:\n<code>PavelK (333)</code>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("request:deny:"))
async def deny_access_request(callback: CallbackQuery):
    current = await get_current_user(callback.from_user.id)
    if not current or current.role != ROLE_BOSS:
        return
    tg_id = int(callback.data.rsplit(":", 1)[1])
    pending_access_requests.pop(tg_id, None)
    await callback.answer("Отклонено")
    await callback.message.edit_reply_markup(reply_markup=None)
    try:
        await bot.send_message(tg_id, "⛔ Заявка на доступ отклонена руководителем.")
    except Exception:
        pass


# =========================
# BOSS ROLE / STRUCTURE MANAGEMENT
# =========================
async def require_boss(user_id: int) -> Optional[TeamUser]:
    user = await get_current_user(user_id, force=True)
    return user if user and user.role == ROLE_BOSS else None


@dp.message(F.text == "⭐ Сделать тимлидом")
async def choose_make_teamlead(message: Message):
    if not await require_boss(message.from_user.id):
        return
    users = await get_users()
    candidates = [u for u in users if u.is_active and u.role == ROLE_BUYER]
    kb = InlineKeyboardBuilder()
    for user in candidates:
        kb.button(text=user.name, callback_data=f"role:teamlead:{user.internal_id}")
    kb.adjust(1)
    await message.answer("Кого сделать тимлидом?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("role:teamlead:"))
async def make_teamlead(callback: CallbackQuery):
    if not await require_boss(callback.from_user.id):
        return
    internal_id = int(callback.data.rsplit(":", 1)[1])
    users = await get_users(force=True)
    target = find_user_by_internal_id(users, internal_id)
    if not target:
        await callback.answer("Не найден", show_alert=True)
        return
    await set_user_fields(target, role=ROLE_TEAMLEAD, manager_id=None)
    await callback.answer("Готово")
    await callback.message.edit_text(f"✅ {safe_html(target.name)} теперь тимлид.", parse_mode="HTML")


@dp.message(F.text == "👤 Сделать баером")
async def choose_make_buyer(message: Message):
    if not await require_boss(message.from_user.id):
        return
    users = await get_users()
    candidates = [u for u in users if u.is_active and u.role == ROLE_TEAMLEAD]
    kb = InlineKeyboardBuilder()
    for user in candidates:
        kb.button(text=user.name, callback_data=f"role:buyerpick:{user.internal_id}")
    kb.adjust(1)
    await message.answer("Кого сделать баером?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("role:buyerpick:"))
async def make_buyer_pick_manager(callback: CallbackQuery):
    if not await require_boss(callback.from_user.id):
        return
    target_id = int(callback.data.rsplit(":", 1)[1])
    users = await get_users(force=True)
    target = find_user_by_internal_id(users, target_id)
    teamleads = [u for u in users if u.is_active and u.role == ROLE_TEAMLEAD and u.internal_id != target_id]
    if not target or not teamleads:
        await callback.answer("Нет доступного тимлида", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for tl in teamleads:
        kb.button(text=tl.name, callback_data=f"role:buyer:{target_id}:{tl.internal_id}")
    kb.adjust(1)
    await callback.answer()
    await callback.message.edit_text("Выберите нового тимлида:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("role:buyer:"))
async def make_buyer(callback: CallbackQuery):
    if not await require_boss(callback.from_user.id):
        return
    _, _, target_raw, manager_raw = callback.data.split(":")
    users = await get_users(force=True)
    target = find_user_by_internal_id(users, int(target_raw))
    if not target:
        return
    await set_user_fields(target, role=ROLE_BUYER, manager_id=int(manager_raw))
    await callback.answer("Готово")
    await callback.message.edit_text(f"✅ {safe_html(target.name)} теперь баер.", parse_mode="HTML")


@dp.message(F.text == "🔄 Перевести баера")
async def transfer_buyer_start(message: Message):
    if not await require_boss(message.from_user.id):
        return
    users = await get_users()
    buyers = [u for u in users if u.is_active and u.role == ROLE_BUYER]
    kb = InlineKeyboardBuilder()
    for user in buyers:
        kb.button(text=user.name, callback_data=f"transfer:buyer:{user.internal_id}")
    kb.adjust(1)
    await message.answer("Кого перевести?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("transfer:buyer:"))
async def transfer_choose_teamlead(callback: CallbackQuery):
    if not await require_boss(callback.from_user.id):
        return
    buyer_id = int(callback.data.rsplit(":", 1)[1])
    users = await get_users(force=True)
    teamleads = [u for u in users if u.is_active and u.role == ROLE_TEAMLEAD]
    kb = InlineKeyboardBuilder()
    for tl in teamleads:
        kb.button(text=tl.name, callback_data=f"transfer:set:{buyer_id}:{tl.internal_id}")
    kb.adjust(1)
    await callback.answer()
    await callback.message.edit_text("К какому тимлиду перевести?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("transfer:set:"))
async def transfer_set(callback: CallbackQuery):
    if not await require_boss(callback.from_user.id):
        return
    _, _, buyer_raw, tl_raw = callback.data.split(":")
    users = await get_users(force=True)
    buyer_user = find_user_by_internal_id(users, int(buyer_raw))
    if not buyer_user:
        return
    await set_user_fields(buyer_user, manager_id=int(tl_raw))
    await callback.answer("Переведён")
    await callback.message.edit_text(f"✅ {safe_html(buyer_user.name)} переведён.", parse_mode="HTML")


@dp.message(F.text == "🚫 Отключить пользователя")
async def deactivate_start(message: Message):
    if not await require_boss(message.from_user.id):
        return
    users = await get_users()
    candidates = [u for u in users if u.is_active and u.role not in {ROLE_BOSS, ROLE_MANAGER}]
    kb = InlineKeyboardBuilder()
    for user in candidates:
        kb.button(text=user.name, callback_data=f"deactivate:{user.internal_id}")
    kb.adjust(1)
    await message.answer("Кого отключить?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("deactivate:"))
async def deactivate_user(callback: CallbackQuery):
    if not await require_boss(callback.from_user.id):
        return
    internal_id = int(callback.data.rsplit(":", 1)[1])
    users = await get_users(force=True)
    target = find_user_by_internal_id(users, internal_id)
    if not target:
        return
    await set_user_fields(target, status="Неактивен")
    await callback.answer("Отключён")
    await callback.message.edit_text(f"🚫 {safe_html(target.name)} отключён.", parse_mode="HTML")


# =========================
# FEEDBACK / MANAGER
# =========================
@dp.callback_query(F.data == "feedback:start")
async def feedback_start(callback: CallbackQuery):
    current = await get_current_user(callback.from_user.id)
    if not current or current.role not in REPORTER_ROLES:
        return
    pending_feedback.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer("Напишите одним сообщением ошибку или предложение.")


@dp.message(F.text == "📢 Сделать рассылку")
async def broadcast_start(message: Message):
    current = await get_current_user(message.from_user.id)
    if not current or current.role != ROLE_MANAGER:
        return
    pending_broadcast.add(message.from_user.id)
    await message.answer("Напишите текст рассылки. Для отмены: отмена")


@dp.message(F.text == "🧹 Очистить bot")
async def clear_bot_action(message: Message):
    current = await get_current_user(message.from_user.id)
    if not current or current.role != ROLE_MANAGER:
        return
    await clear_bot_sheet_status()
    await message.answer("✅ B1:B4 очищены.", reply_markup=manager_menu())


@dp.callback_query(F.data == "late:done")
async def late_done(callback: CallbackQuery):
    current = await get_current_user(callback.from_user.id, force=True)
    if not current or current.role not in REPORTER_ROLES:
        return
    await callback.answer("Проверяю…")
    if not await refresh_or_report_error():
        return
    states = await load_report_states()
    state = states.get(normalize_name(current.name))
    if state is None or not state.report or is_no_report(state.report):
        await callback.message.answer("Пока не вижу отчёт. Попробуйте ещё раз через 30 секунд.", reply_markup=late_keyboard())
        return
    boss_user = await get_boss_user(await get_users())
    boss_id = int(boss_user.telegram_id) if boss_user else BOSS_CHAT_ID
    if boss_id:
        await bot.send_message(
            boss_id,
            f"✅ <b>{safe_html(current.name)}</b> отправил отчёт после дедлайна.",
            parse_mode="HTML",
        )
    await callback.message.answer("✅ Отчёт найден. Руководитель уведомлён.")
    await clear_bot_sheet_status()


# =========================
# TEXT STATE HANDLER
# =========================
@dp.message()
async def text_state_handler(message: Message):
    user_id = message.from_user.id
    text = str(message.text or "").strip()

    if user_id in pending_feedback:
        pending_feedback.discard(user_id)
        current = await get_current_user(user_id, force=True)
        if not current:
            return
        await send_error_to_manager(
            "💬 <b>Сообщение от сотрудника</b>\n\n"
            f"Пользователь: <b>{safe_html(current.name)}</b>\n"
            f"Роль: <code>{safe_html(current.role)}</code>\n"
            f"Telegram ID: <code>{user_id}</code>\n\n"
            f"{safe_html(text)}"
        )
        await message.answer("✅ Сообщение передано менеджеру.", reply_markup=reporter_keyboard())
        return

    if user_id in pending_broadcast:
        current = await get_current_user(user_id)
        if not current or current.role != ROLE_MANAGER:
            pending_broadcast.discard(user_id)
            return
        if text.lower() in {"отмена", "cancel", "стоп"}:
            pending_broadcast.discard(user_id)
            await message.answer("Рассылка отменена.", reply_markup=manager_menu())
            return
        pending_broadcast.discard(user_id)
        reporters = get_active_reporters(await get_users(force=True))
        sent = 0
        failed = 0
        for reporter in reporters:
            if await safe_send(reporter.telegram_id, safe_html(text), form_keyboard()):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.2)
        await message.answer(
            f"✅ Рассылка завершена. Отправлено: {sent}, ошибок: {failed}.",
            reply_markup=manager_menu(),
        )
        return

    context = pending_add.get(user_id)
    if context:
        current = await get_current_user(user_id, force=True)
        if not current or current.role not in {ROLE_BOSS, ROLE_TEAMLEAD}:
            pending_add.pop(user_id, None)
            return

        if context.get("telegram_id"):
            name = text
            telegram_id = context["telegram_id"]
            username = context.get("username", "")
            error = ""
        else:
            name, telegram_id, username, error = parse_new_user_line(text)
            if error:
                await message.answer(error)
                return

        pending_add.pop(user_id, None)
        ok, result, new_user = await add_user(
            name=name or "",
            telegram_id=telegram_id or "",
            username=username,
            role=ROLE_BUYER,
            manager_id=context["manager_id"],
        )
        await message.answer(result, parse_mode="HTML", reply_markup=menu_for(current))
        if ok and new_user:
            try:
                await bot.send_message(
                    int(new_user.telegram_id),
                    f"✅ Вас добавили в команду как <b>{safe_html(new_user.name)}</b>.\n"
                    "Нажмите /start.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return


# =========================
# STARTUP
# =========================
async def warm_cache():
    users = await get_users(force=True)
    print(f"[STARTUP] Загружено пользователей: {len(users)}", flush=True)


async def main():
    await warm_cache()
    asyncio.create_task(daily_reminder_scheduler())
    start_buyer_statistics_scheduler(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
