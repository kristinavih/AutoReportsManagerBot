"""
Статистика команды для Telegram-бота.

Вт/Чт: L:O - G:J.
Сб: L:O - G:J и L:O - B:E.

Распределение:
- boss получает статистику всех активных teamlead + buyer;
- teamlead получает статистику только своих активных buyer;
- manager и buyer статистику не получают.
"""

from __future__ import annotations

import asyncio
import html
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from zoneinfo import ZoneInfo

import gspread
from aiogram import Bot
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

BOSS_CHAT_ID = int(os.getenv("BOSS_CHAT_ID", "0"))
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "0"))
BUYER_STATS_SHEET_ID = os.getenv("REPORT_SHEET_ID", "").strip()
BUYER_STATS_SHEET_NAME = os.getenv("BUYER_STATS_SHEET_NAME", "Статистика_баеров").strip()
USERS_SHEET_NAME = os.getenv("USERS_SHEET_NAME", "Пользователи").strip()
GOOGLE_JSON = os.getenv("GOOGLE_JSON", "credentials.json").strip()
BUYER_STATS_TIMEZONE = os.getenv("BUYER_STATS_TIMEZONE", "Europe/Moscow")
BUYER_STATS_SEND_TIME = os.getenv("BUYER_STATS_SEND_TIME", "10:00")
REPORT_DISPLAY_TIMEZONE = os.getenv("REPORT_DISPLAY_TIMEZONE", "Asia/Vladivostok")

NEUTRAL_PROFIT_THRESHOLD = Decimal(os.getenv("NEUTRAL_PROFIT_THRESHOLD", "100"))

GOOGLE_RETRIES = int(os.getenv("GOOGLE_RETRIES", "5"))
GOOGLE_RETRY_BASE_DELAY = float(os.getenv("GOOGLE_RETRY_BASE_DELAY", "1.5"))
GOOGLE_RETRY_MAX_DELAY = float(os.getenv("GOOGLE_RETRY_MAX_DELAY", "12"))

START_ROW = 3
MAX_ROWS = 300
LAST_ROW = START_ROW + MAX_ROWS - 1
WEEKLY_RANGE = f"B{START_ROW}:E{LAST_ROW}"
PREVIOUS_RANGE = f"G{START_ROW}:J{LAST_ROW}"
CURRENT_RANGE = f"L{START_ROW}:O{LAST_ROW}"
CURRENT_DATE_CELL = f"K{START_ROW}"
USERS_MAX_ROWS = 1000

ROLE_BOSS = "boss"
ROLE_MANAGER = "manager"
ROLE_TEAMLEAD = "teamlead"
ROLE_BUYER = "buyer"
ACTIVE_STATUS = "активен"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_statistics_sheet = None
_users_sheet = None
_sheet_lock = asyncio.Lock()
_send_lock = asyncio.Lock()


@dataclass(slots=True)
class TeamUser:
    internal_id: int
    name: str
    telegram_id: str
    role: str
    manager_id: Optional[int]
    status: str

    @property
    def is_active(self) -> bool:
        return normalize_status(self.status) == ACTIVE_STATUS


def google_call_sync(label: str, func, *args):
    last_error = None
    for attempt in range(1, GOOGLE_RETRIES + 1):
        try:
            return func(*args)
        except Exception as exc:
            last_error = exc
            if attempt >= GOOGLE_RETRIES:
                print(f"[BUYER STATS GOOGLE ERROR] {label}: {exc!r}", flush=True)
                raise
            delay = min(GOOGLE_RETRY_MAX_DELAY, GOOGLE_RETRY_BASE_DELAY * attempt)
            delay += random.uniform(0, 0.5)
            print(
                f"[BUYER STATS GOOGLE RETRY] {label}: повтор через {delay:.1f} сек.",
                flush=True,
            )
            time.sleep(delay)
    raise last_error


async def google_call(label: str, func, *args):
    return await asyncio.to_thread(google_call_sync, label, func, *args)


def open_sheets():
    if not BUYER_STATS_SHEET_ID:
        raise RuntimeError("REPORT_SHEET_ID не указан в .env")
    creds = Credentials.from_service_account_file(GOOGLE_JSON, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = google_call_sync(
        "open statistics spreadsheet", client.open_by_key, BUYER_STATS_SHEET_ID
    )
    statistics = google_call_sync(
        "open statistics worksheet", spreadsheet.worksheet, BUYER_STATS_SHEET_NAME
    )
    users = google_call_sync(
        "open users worksheet", spreadsheet.worksheet, USERS_SHEET_NAME
    )
    return statistics, users


async def get_sheets():
    global _statistics_sheet, _users_sheet
    if _statistics_sheet is not None and _users_sheet is not None:
        return _statistics_sheet, _users_sheet
    async with _sheet_lock:
        if _statistics_sheet is None or _users_sheet is None:
            _statistics_sheet, _users_sheet = await asyncio.to_thread(open_sheets)
    return _statistics_sheet, _users_sheet


async def read_statistics_range(range_name: str):
    statistics, _ = await get_sheets()
    return await google_call(
        f"statistics.get {range_name}", statistics.get, range_name
    )


async def read_users_range(range_name: str):
    _, users = await get_sheets()
    return await google_call(f"users.get {range_name}", users.get, range_name)


def normalize_buyer_name(value: str) -> str:
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


async def load_team_users() -> list[TeamUser]:
    rows = await read_users_range(f"A2:G{USERS_MAX_ROWS}")
    result: list[TeamUser] = []
    for row in rows:
        internal_id = parse_int(row[0] if len(row) > 0 else None)
        name = str(row[1]).strip() if len(row) > 1 else ""
        telegram_id = str(row[2]).strip() if len(row) > 2 else ""
        role = str(row[4]).strip().lower() if len(row) > 4 else ""
        manager_id = parse_int(row[5] if len(row) > 5 else None)
        status = str(row[6]).strip() if len(row) > 6 else ""
        if internal_id is None or not name:
            continue
        result.append(
            TeamUser(
                internal_id=internal_id,
                name=name,
                telegram_id=telegram_id,
                role=role,
                manager_id=manager_id,
                status=status,
            )
        )
    return result


def parse_number(value) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    upper = text.upper()
    if "DIV/0" in upper or upper in {"#N/A", "#VALUE!", "#REF!", "#ERROR!", "#NUM!"}:
        return None
    text = text.replace("$", "").replace("%", "").replace("−", "-")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def rows_to_map(rows: list[list]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        buyer = str(row[0]).strip() if len(row) > 0 else ""
        if not buyer:
            continue
        key = normalize_buyer_name(buyer)
        if not key:
            continue
        result[key] = {
            "buyer": buyer,
            "revenue": parse_number(row[1] if len(row) > 1 else None),
            "profit": parse_number(row[2] if len(row) > 2 else None),
            "roi": parse_number(row[3] if len(row) > 3 else None),
        }
    return result


async def read_all_statistics_blocks():
    weekly_rows, previous_rows, current_rows = await asyncio.gather(
        read_statistics_range(WEEKLY_RANGE),
        read_statistics_range(PREVIOUS_RANGE),
        read_statistics_range(CURRENT_RANGE),
    )
    return rows_to_map(weekly_rows), rows_to_map(previous_rows), rows_to_map(current_rows)


def calculate_delta(current: Optional[Decimal], previous: Optional[Decimal]) -> Optional[Decimal]:
    if current is None or previous is None:
        return None
    return current - previous


def get_buyer_status(profit_delta: Optional[Decimal]) -> tuple[int, str]:
    if profit_delta is None:
        return 1, "🟨"
    if profit_delta < -NEUTRAL_PROFIT_THRESHOLD:
        return 0, "🟥"
    if profit_delta > NEUTRAL_PROFIT_THRESHOLD:
        return 2, "🟩"
    return 1, "🟨"


def format_compact_money(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    rounded = value.quantize(Decimal("0.01"))
    sign = "+" if rounded > 0 else "−" if rounded < 0 else ""
    rendered = f"{abs(rounded):,.2f}".replace(",", " ").replace(".", ",")
    return f"{sign}{rendered} $"


def format_compact_percent(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    rounded = value.quantize(Decimal("0.01"))
    sign = "+" if rounded > 0 else "−" if rounded < 0 else ""
    rendered = f"{abs(rounded):.2f}".replace(".", ",")
    if rendered.endswith(",00"):
        rendered = rendered[:-3]
    return f"{sign}{rendered}%"


WEEKDAY_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}


def parse_snapshot_datetime(value) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for date_format in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%y %H:%M:%S",
        "%d.%m.%y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    return None


def format_snapshot_datetime(value) -> str:
    parsed = parse_snapshot_datetime(value)
    if parsed is None:
        return "Дата фиксации не определена"
    return f"{WEEKDAY_RU[parsed.weekday()]}, {parsed.strftime('%d.%m.%y %H:%M')}"


async def read_current_snapshot_time() -> str:
    statistics, _ = await get_sheets()
    cell = await google_call(
        f"statistics.acell {CURRENT_DATE_CELL}", statistics.acell, CURRENT_DATE_CELL
    )
    return format_snapshot_datetime(getattr(cell, "value", None))


def get_report_update_time() -> str:
    now = datetime.now(ZoneInfo(REPORT_DISPLAY_TIMEZONE))
    return f"{WEEKDAY_RU[now.weekday()]}, {now.strftime('%d.%m.%y %H:%M')}"


def filter_statistics_map(data: dict[str, dict], allowed_names: set[str]) -> dict[str, dict]:
    return {key: value for key, value in data.items() if key in allowed_names}


def build_report(
    title: str,
    base: dict[str, dict],
    current: dict[str, dict],
    update_time: Optional[str] = None,
) -> str:
    if not current:
        return f"<b>{html.escape(title)}</b>\n\n⚠️ Нет данных для выбранной команды."

    prepared = []
    for key, current_item in current.items():
        previous_item = base.get(key)
        revenue_delta = calculate_delta(
            current_item["revenue"], previous_item["revenue"] if previous_item else None
        )
        profit_delta = calculate_delta(
            current_item["profit"], previous_item["profit"] if previous_item else None
        )
        roi_delta = calculate_delta(
            current_item["roi"], previous_item["roi"] if previous_item else None
        )

        status_order, status_icon = get_buyer_status(profit_delta)
        if status_order == 0:
            sort_value = profit_delta if profit_delta is not None else Decimal("0")
        elif status_order == 1:
            sort_value = abs(profit_delta) if profit_delta is not None else Decimal("0")
        else:
            sort_value = profit_delta if profit_delta is not None else Decimal("0")

        prepared.append(
            {
                "buyer": current_item["buyer"],
                "status_order": status_order,
                "status_icon": status_icon,
                "sort_value": sort_value,
                "revenue_delta": revenue_delta,
                "profit_delta": profit_delta,
                "roi_delta": roi_delta,
            }
        )

    prepared.sort(
        key=lambda item: (
            item["status_order"],
            item["sort_value"],
            normalize_buyer_name(item["buyer"]),
        )
    )

    red_count = sum(item["status_order"] == 0 for item in prepared)
    yellow_count = sum(item["status_order"] == 1 for item in prepared)
    green_count = sum(item["status_order"] == 2 for item in prepared)

    if not update_time or update_time == "Дата фиксации не определена":
        update_time = get_report_update_time()

    blocks = [
        f"<b>{html.escape(title)}</b>\n"
        f"🕒 <b>{html.escape(update_time)}</b>\n\n"
        f"🟥 Просадка: <b>{red_count}</b>\n"
        f"🟨 Почти без изменений: <b>{yellow_count}</b>\n"
        f"🟩 Рост: <b>{green_count}</b>"
    ]

    for item in prepared:
        blocks.append(
            f"<b>{item['status_icon']} {html.escape(item['buyer'])}</b>\n"
            f"<code>Rev  {format_compact_money(item['revenue_delta'])}\n"
            f"Prf  {format_compact_money(item['profit_delta'])}\n"
            f"ROI  {format_compact_percent(item['roi_delta'])}</code>"
        )
    return "\n\n".join(blocks)


async def send_long_html_message(bot: Bot, chat_id: int, text: str):
    max_len = 3900
    chunk = ""
    for block in text.split("\n\n"):
        candidate = block if not chunk else chunk + "\n\n" + block
        if len(candidate) <= max_len:
            chunk = candidate
            continue
        if chunk:
            await bot.send_message(chat_id, chunk, parse_mode="HTML")
        chunk = block
    if chunk:
        await bot.send_message(chat_id, chunk, parse_mode="HTML")


def find_boss(users: list[TeamUser]) -> Optional[TeamUser]:
    return next(
        (u for u in users if u.is_active and u.role == ROLE_BOSS and u.telegram_id),
        None,
    )


def find_manager(users: list[TeamUser]) -> Optional[TeamUser]:
    return next(
        (u for u in users if u.is_active and u.role == ROLE_MANAGER and u.telegram_id),
        None,
    )


def teamlead_buyers(teamlead: TeamUser, users: list[TeamUser]) -> list[TeamUser]:
    return [
        u
        for u in users
        if u.is_active
        and u.role == ROLE_BUYER
        and u.manager_id == teamlead.internal_id
    ]


def allowed_keys(users: list[TeamUser]) -> set[str]:
    return {normalize_buyer_name(u.name) for u in users}


async def send_reports_to_recipients(bot: Bot, *, include_weekly: bool):
    users = await load_team_users()
    weekly, previous, current = await read_all_statistics_blocks()
    update_time = await read_current_snapshot_time()

    boss = find_boss(users)
    boss_id = int(boss.telegram_id) if boss else BOSS_CHAT_ID
    boss_reporters = [
        u for u in users if u.is_active and u.role in {ROLE_TEAMLEAD, ROLE_BUYER}
    ]
    boss_keys = allowed_keys(boss_reporters)

    if boss_id:
        regular = build_report(
            "📊 Изменение с последней фиксации",
            filter_statistics_map(previous, boss_keys),
            filter_statistics_map(current, boss_keys),
            update_time,
        )
        await send_long_html_message(bot, boss_id, regular)
        if include_weekly:
            await asyncio.sleep(1)
            weekly_report = build_report(
                "📅 Итоги недели",
                filter_statistics_map(weekly, boss_keys),
                filter_statistics_map(current, boss_keys),
                update_time,
            )
            await send_long_html_message(bot, boss_id, weekly_report)

    teamleads = [
        u for u in users if u.is_active and u.role == ROLE_TEAMLEAD and u.telegram_id
    ]
    for teamlead in teamleads:
        buyers = teamlead_buyers(teamlead, users)
        keys = allowed_keys(buyers)
        regular = build_report(
            f"📊 Статистика команды {teamlead.name}",
            filter_statistics_map(previous, keys),
            filter_statistics_map(current, keys),
            update_time,
        )
        await send_long_html_message(bot, int(teamlead.telegram_id), regular)
        if include_weekly:
            await asyncio.sleep(0.5)
            weekly_report = build_report(
                f"📅 Итоги недели команды {teamlead.name}",
                filter_statistics_map(weekly, keys),
                filter_statistics_map(current, keys),
                update_time,
            )
            await send_long_html_message(bot, int(teamlead.telegram_id), weekly_report)
        await asyncio.sleep(0.25)

    print(
        f"[BUYER STATS] Рассылка завершена. Тимлидов: {len(teamleads)}, "
        f"weekly={include_weekly}",
        flush=True,
    )


async def send_regular_statistics_report(bot: Bot):
    async with _send_lock:
        await send_reports_to_recipients(bot, include_weekly=False)


async def send_saturday_statistics_reports(bot: Bot):
    async with _send_lock:
        await send_reports_to_recipients(bot, include_weekly=True)


async def send_statistics_to_one_teamlead(bot: Bot, telegram_id: int):
    """Ручная кнопка «Статистика моей команды» из bot.py."""
    async with _send_lock:
        users = await load_team_users()
        teamlead = next(
            (
                u
                for u in users
                if u.is_active
                and u.role == ROLE_TEAMLEAD
                and u.telegram_id == str(telegram_id)
            ),
            None,
        )
        if not teamlead:
            await bot.send_message(telegram_id, "⚠️ Тимлид не найден в листе Пользователи.")
            return

        weekly, previous, current = await read_all_statistics_blocks()
        update_time = await read_current_snapshot_time()
        keys = allowed_keys(teamlead_buyers(teamlead, users))
        regular = build_report(
            f"📊 Статистика команды {teamlead.name}",
            filter_statistics_map(previous, keys),
            filter_statistics_map(current, keys),
            update_time,
        )
        await send_long_html_message(bot, telegram_id, regular)

        now = datetime.now(ZoneInfo(BUYER_STATS_TIMEZONE))
        if now.weekday() == 5:
            weekly_report = build_report(
                f"📅 Итоги недели команды {teamlead.name}",
                filter_statistics_map(weekly, keys),
                filter_statistics_map(current, keys),
                update_time,
            )
            await asyncio.sleep(0.5)
            await send_long_html_message(bot, telegram_id, weekly_report)


async def send_buyer_statistics_report(bot: Bot):
    await send_regular_statistics_report(bot)


async def send_statistics_for_today(bot: Bot):
    now = datetime.now(ZoneInfo(BUYER_STATS_TIMEZONE))
    if now.weekday() == 5:
        await send_saturday_statistics_reports(bot)
    else:
        await send_regular_statistics_report(bot)


def is_statistics_day(dt: datetime) -> bool:
    return dt.weekday() in {1, 3, 5}


def parse_send_time() -> tuple[int, int]:
    hour, minute = map(int, BUYER_STATS_SEND_TIME.split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuntimeError("BUYER_STATS_SEND_TIME содержит некорректное время")
    return hour, minute


def next_statistics_run() -> datetime:
    tz = ZoneInfo(BUYER_STATS_TIMEZONE)
    hour, minute = parse_send_time()
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while not is_statistics_day(target):
        target += timedelta(days=1)
    return target


async def send_error(bot: Bot, exc: Exception):
    users = await load_team_users()
    manager = find_manager(users)
    recipient = int(manager.telegram_id) if manager else MANAGER_CHAT_ID
    if not recipient:
        recipient = BOSS_CHAT_ID
    if not recipient:
        return
    try:
        await bot.send_message(
            recipient,
            "⚠️ Не удалось сформировать статистику команды.\n\n"
            f"Ошибка: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def buyer_statistics_scheduler(bot: Bot):
    while True:
        target = next_statistics_run()
        seconds = max(1, (target - datetime.now(target.tzinfo)).total_seconds())
        print(f"[BUYER STATS SCHEDULER] Следующая отправка: {target.isoformat()}", flush=True)
        await asyncio.sleep(seconds)
        try:
            await send_statistics_for_today(bot)
        except Exception as exc:
            print(f"[BUYER STATS ERROR] {exc!r}", flush=True)
            await send_error(bot, exc)
        await asyncio.sleep(2)


def start_buyer_statistics_scheduler(bot: Bot) -> asyncio.Task:
    return asyncio.create_task(buyer_statistics_scheduler(bot))


async def test_regular_report(bot: Bot):
    await send_regular_statistics_report(bot)


async def test_saturday_reports(bot: Bot):
    await send_saturday_statistics_reports(bot)
