from __future__ import annotations

import asyncio
import html
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials


load_dotenv()

router = Router(name="buyer_leads")

REPORT_SHEET_ID = os.getenv("REPORT_SHEET_ID", "").strip()
USERS_SHEET_NAME = os.getenv("USERS_SHEET_NAME", "Пользователи").strip()
LEADS_SHEET_NAME = os.getenv("LEADS_SHEET_NAME", "Лиды").strip()
GOOGLE_JSON = os.getenv("GOOGLE_JSON", "credentials.json").strip()

MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")

USERS_MAX_ROWS = 1000
LEADS_MAX_ROWS = 20000

ACTIVE_STATUS = "активен"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


if not REPORT_SHEET_ID:
    raise RuntimeError("REPORT_SHEET_ID не указан в переменных окружения")


# ============================================================
# МОДЕЛИ
# ============================================================

@dataclass(slots=True)
class BuyerUser:
    internal_id: int
    name: str
    telegram_id: str
    role: str
    status: str

    @property
    def is_active(self) -> bool:
        return normalize_status(self.status) == ACTIVE_STATUS


@dataclass(slots=True)
class LeadRow:
    time: str
    date: str
    message_id: str
    affiliate_id: str
    buyer_id: int
    telegram_id: str
    teamlead_id: Optional[int]
    status: str
    country: str
    click_id: str
    name: str


# ============================================================
# GOOGLE SHEETS
# ============================================================

def open_sheets():
    credentials = Credentials.from_service_account_file(
        GOOGLE_JSON,
        scopes=SCOPES,
    )

    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(REPORT_SHEET_ID)

    users_sheet = spreadsheet.worksheet(USERS_SHEET_NAME)
    leads_sheet = spreadsheet.worksheet(LEADS_SHEET_NAME)

    return users_sheet, leads_sheet


_users_sheet = None
_leads_sheet = None
_sheets_lock = asyncio.Lock()


async def get_sheets():
    global _users_sheet, _leads_sheet

    if _users_sheet is not None and _leads_sheet is not None:
        return _users_sheet, _leads_sheet

    async with _sheets_lock:
        if _users_sheet is None or _leads_sheet is None:
            _users_sheet, _leads_sheet = await asyncio.to_thread(open_sheets)

    return _users_sheet, _leads_sheet


async def google_call(func, *args):
    return await asyncio.to_thread(func, *args)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

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


def safe_html(value) -> str:
    return html.escape(str(value or "").strip())


def today_moscow_string() -> str:
    return datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y")


def normalize_lead_status(value: str) -> str:
    status = str(value or "").strip().upper()

    if status == "GOOD":
        return "GOOD"

    if status == "BAD":
        return "BAD"

    return status


def lead_status_icon(status: str) -> str:
    if status == "GOOD":
        return "✅"

    if status == "BAD":
        return "❌"

    return "➖"


# ============================================================
# ЧТЕНИЕ ПОЛЬЗОВАТЕЛЯ
# ============================================================

async def find_active_user_by_telegram_id(
    telegram_id: int,
) -> Optional[BuyerUser]:
    users_sheet, _ = await get_sheets()

    rows = await google_call(
        users_sheet.get,
        f"A2:G{USERS_MAX_ROWS}",
    )

    target_telegram_id = str(telegram_id)

    for row in rows:
        internal_id = parse_int(row[0] if len(row) > 0 else None)
        name = str(row[1]).strip() if len(row) > 1 else ""
        current_telegram_id = str(row[2]).strip() if len(row) > 2 else ""
        role = str(row[4]).strip().lower() if len(row) > 4 else ""
        status = str(row[6]).strip() if len(row) > 6 else ""

        if internal_id is None or current_telegram_id != target_telegram_id:
            continue

        user = BuyerUser(
            internal_id=internal_id,
            name=name,
            telegram_id=current_telegram_id,
            role=role,
            status=status,
        )

        if not user.is_active:
            return None

        return user

    return None


# ============================================================
# ЧТЕНИЕ ЛИДОВ
# ============================================================

async def read_today_leads_for_buyer(
    buyer_id: int,
) -> list[LeadRow]:
    _, leads_sheet = await get_sheets()

    rows = await google_call(
        leads_sheet.get,
        f"A2:K{LEADS_MAX_ROWS}",
    )

    today = today_moscow_string()
    result: list[LeadRow] = []

    for row in rows:
        lead_date = str(row[1]).strip() if len(row) > 1 else ""
        row_buyer_id = parse_int(row[4] if len(row) > 4 else None)

        if lead_date != today:
            continue

        if row_buyer_id != buyer_id:
            continue

        result.append(
            LeadRow(
                time=str(row[0]).strip() if len(row) > 0 else "",
                date=lead_date,
                message_id=str(row[2]).strip() if len(row) > 2 else "",
                affiliate_id=str(row[3]).strip() if len(row) > 3 else "",
                buyer_id=row_buyer_id,
                telegram_id=str(row[5]).strip() if len(row) > 5 else "",
                teamlead_id=parse_int(row[6] if len(row) > 6 else None),
                status=normalize_lead_status(
                    row[7] if len(row) > 7 else ""
                ),
                country=(
                    str(row[8]).strip().upper()
                    if len(row) > 8
                    else ""
                ),
                click_id=str(row[9]).strip() if len(row) > 9 else "",
                name=str(row[10]).strip() if len(row) > 10 else "",
            )
        )

    result.sort(key=lambda lead: lead.time)

    return result


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def buyer_leads_summary_keyboard():
    keyboard = InlineKeyboardBuilder()

    keyboard.button(
        text="📋 Посмотреть все",
        callback_data="buyer_leads:all",
    )

    keyboard.button(
        text="🌍 По странам",
        callback_data="buyer_leads:countries",
    )

    keyboard.button(
        text="🔄 Обновить",
        callback_data="buyer_leads:today",
    )

    keyboard.adjust(1)

    return keyboard.as_markup()


def buyer_leads_details_keyboard():
    keyboard = InlineKeyboardBuilder()

    keyboard.button(
        text="🌍 По странам",
        callback_data="buyer_leads:countries",
    )

    keyboard.button(
        text="↩️ К сводке",
        callback_data="buyer_leads:today",
    )

    keyboard.adjust(1)

    return keyboard.as_markup()


def buyer_leads_countries_keyboard():
    keyboard = InlineKeyboardBuilder()

    keyboard.button(
        text="📋 Посмотреть все",
        callback_data="buyer_leads:all",
    )

    keyboard.button(
        text="↩️ К сводке",
        callback_data="buyer_leads:today",
    )

    keyboard.adjust(1)

    return keyboard.as_markup()


# ============================================================
# ФОРМАТИРОВАНИЕ
# ============================================================

def build_summary_text(
    buyer: BuyerUser,
    leads: list[LeadRow],
) -> str:
    good_count = sum(lead.status == "GOOD" for lead in leads)
    bad_count = sum(lead.status == "BAD" for lead in leads)

    return (
        "🎯 <b>Мои лиды за сегодня</b>\n\n"
        f"Баер: <b>{safe_html(buyer.name)}</b>\n"
        f"Дата: <b>{today_moscow_string()}</b>\n\n"
        f"Всего: <b>{len(leads)}</b>\n\n"
        f"✅ GOOD — <b>{good_count}</b>\n"
        f"❌ BAD — <b>{bad_count}</b>"
    )


def build_all_leads_blocks(
    leads: list[LeadRow],
) -> list[str]:
    if not leads:
        return [
            "📋 <b>Все мои лиды за сегодня</b>\n\n"
            "За сегодня лидов пока нет."
        ]

    header = (
        "📋 <b>Все мои лиды за сегодня</b>\n\n"
        f"Дата: <b>{today_moscow_string()}</b>\n"
        f"Всего: <b>{len(leads)}</b>"
    )

    lead_blocks: list[str] = []

    for lead in leads:
        icon = lead_status_icon(lead.status)
        country = safe_html(lead.country or "—")
        name = safe_html(lead.name or "Имя не указано")
        lead_time = safe_html(lead.time or "—")

        lead_blocks.append(
            f"<b>{lead_time} {icon} {country}</b>\n"
            f"{name}"
        )

    return split_html_blocks(
        header=header,
        blocks=lead_blocks,
    )


def build_countries_text(
    leads: list[LeadRow],
) -> str:
    if not leads:
        return (
            "🌍 <b>Мои лиды по странам за сегодня</b>\n\n"
            "За сегодня лидов пока нет."
        )

    country_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "GOOD": 0,
            "BAD": 0,
            "TOTAL": 0,
        }
    )

    for lead in leads:
        country = lead.country or "Не указана"

        country_stats[country]["TOTAL"] += 1

        if lead.status == "GOOD":
            country_stats[country]["GOOD"] += 1
        elif lead.status == "BAD":
            country_stats[country]["BAD"] += 1

    sorted_countries = sorted(
        country_stats.items(),
        key=lambda item: (
            -item[1]["TOTAL"],
            item[0],
        ),
    )

    blocks = [
        "🌍 <b>Мои лиды по странам за сегодня</b>\n\n"
        f"Дата: <b>{today_moscow_string()}</b>\n"
        f"Всего: <b>{len(leads)}</b>"
    ]

    for country, stats in sorted_countries:
        blocks.append(
            f"<b>{safe_html(country)}</b>\n"
            f"✅ GOOD — <b>{stats['GOOD']}</b>\n"
            f"❌ BAD — <b>{stats['BAD']}</b>"
        )

    return "\n\n".join(blocks)


def split_html_blocks(
    *,
    header: str,
    blocks: list[str],
    max_length: int = 3900,
) -> list[str]:
    messages: list[str] = []
    current = header

    for block in blocks:
        candidate = f"{current}\n\n{block}"

        if len(candidate) <= max_length:
            current = candidate
            continue

        messages.append(current)
        current = block

    if current:
        messages.append(current)

    return messages


# ============================================================
# ПРОВЕРКА ДОСТУПА
# ============================================================

async def get_buyer_or_answer(
    callback: CallbackQuery,
) -> Optional[BuyerUser]:
    """
    Возвращает активного пользователя, которому разрешено
    смотреть собственные лиды.

    Личные лиды доступны:
    - buyer;
    - teamlead;
    - boss.

    Manager доступа не имеет.
    """
    user = await find_active_user_by_telegram_id(
        callback.from_user.id
    )

    if user is None:
        await callback.answer(
            "Пользователь не найден или отключён.",
            show_alert=True,
        )
        return None

    if user.role not in {"buyer", "teamlead", "boss"}:
        await callback.answer(
            "Эта функция недоступна для вашей роли.",
            show_alert=True,
        )
        return None

    return user


# ============================================================
# CALLBACK: СВОДКА
# ============================================================

@router.callback_query(F.data == "buyer_leads:today")
async def show_buyer_leads_summary(
    callback: CallbackQuery,
):
    await callback.answer("Обновляю…")

    user = await get_buyer_or_answer(callback)

    if user is None:
        return

    leads = await read_today_leads_for_buyer(
        user.internal_id
    )

    text = build_summary_text(
        user,
        leads,
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=buyer_leads_summary_keyboard(),
    )


# ============================================================
# CALLBACK: ВСЕ ЛИДЫ
# ============================================================

@router.callback_query(F.data == "buyer_leads:all")
async def show_all_buyer_leads(
    callback: CallbackQuery,
):
    await callback.answer("Загружаю лиды…")

    user = await get_buyer_or_answer(callback)

    if user is None:
        return

    leads = await read_today_leads_for_buyer(
        user.internal_id
    )

    messages = build_all_leads_blocks(leads)

    # Первое сообщение заменяет текущую сводку.
    await callback.message.edit_text(
        messages[0],
        parse_mode="HTML",
        reply_markup=(
            buyer_leads_details_keyboard()
            if len(messages) == 1
            else None
        ),
    )

    # Остальные части отправляются отдельными сообщениями.
    for index, text in enumerate(messages[1:], start=1):
        is_last = index == len(messages) - 1

        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=(
                buyer_leads_details_keyboard()
                if is_last
                else None
            ),
        )

        await asyncio.sleep(0.15)


# ============================================================
# CALLBACK: СТРАНЫ
# ============================================================

@router.callback_query(F.data == "buyer_leads:countries")
async def show_buyer_leads_by_country(
    callback: CallbackQuery,
):
    await callback.answer("Считаю по странам…")

    user = await get_buyer_or_answer(callback)

    if user is None:
        return

    leads = await read_today_leads_for_buyer(
        user.internal_id
    )

    text = build_countries_text(leads)

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=buyer_leads_countries_keyboard(),
    )