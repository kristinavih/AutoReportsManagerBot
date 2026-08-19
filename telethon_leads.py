from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import timezone, timedelta
from typing import Optional

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telethon import TelegramClient, events
from telethon.sessions import StringSession


load_dotenv()


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
TELETHON_SESSION_STRING = os.getenv(
    "TELETHON_SESSION_STRING",
    "",
).strip()

REPORT_SHEET_ID = os.getenv("REPORT_SHEET_ID", "").strip()
USERS_SHEET_NAME = os.getenv(
    "USERS_SHEET_NAME",
    "Пользователи",
).strip()

LEADS_SHEET_NAME = os.getenv(
    "LEADS_SHEET_NAME",
    "Лиды",
).strip()

GOOGLE_CREDENTIALS_B64 = os.getenv(
    "GOOGLE_CREDENTIALS_B64",
    "",
).strip()

_DEFAULT_GOOGLE_JSON = (
    "/app/credentials.json"
    if os.path.isdir("/app")
    else "credentials.json"
)

GOOGLE_JSON = (
    os.getenv("GOOGLE_JSON", "").strip()
    or _DEFAULT_GOOGLE_JSON
)

# Лучше использовать ID группы.
# Если ID не указан, сборщик попробует найти группу по названию.
LEADS_SOURCE_CHAT_ID_RAW = os.getenv(
    "LEADS_SOURCE_CHAT_ID",
    "",
).strip()

LEADS_SOURCE_CHAT_TITLE = os.getenv(
    "LEADS_SOURCE_CHAT_TITLE",
    "Leads Source",
).strip()

USERS_MAX_ROWS = int(
    os.getenv("USERS_MAX_ROWS", "1000")
)

USERS_CACHE_TTL = int(
    os.getenv("LEADS_USERS_CACHE_TTL", "60")
)

MOSCOW_TZ = timezone(
    timedelta(hours=3),
    name="MSK",
)

ACTIVE_STATUS = "активен"

ROLE_BOSS = "boss"
ROLE_MANAGER = "manager"
ROLE_TEAMLEAD = "teamlead"
ROLE_BUYER = "buyer"

VALID_ROLES = {
    ROLE_BOSS,
    ROLE_MANAGER,
    ROLE_TEAMLEAD,
    ROLE_BUYER,
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

LEADS_HEADERS = [
    "Время",
    "Дата",
    "Message ID",
    "AffiliateID",
    "Buyer ID",
    "Telegram ID",
    "Teamlead ID",
    "Status",
    "Country",
    "ClickID",
    "Name",
]


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not API_ID_RAW:
    raise RuntimeError(
        "API_ID не указан в переменных окружения"
    )

try:
    API_ID = int(API_ID_RAW)
except ValueError as exc:
    raise RuntimeError(
        "API_ID должен состоять только из цифр"
    ) from exc

if not API_HASH:
    raise RuntimeError(
        "API_HASH не указан в переменных окружения"
    )

if not TELETHON_SESSION_STRING:
    raise RuntimeError(
        "TELETHON_SESSION_STRING не указан "
        "в переменных окружения"
    )

if not REPORT_SHEET_ID:
    raise RuntimeError(
        "REPORT_SHEET_ID не указан "
        "в переменных окружения"
    )


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | %(message)s"
    ),
)

logger = logging.getLogger("telethon_leads")


# ============================================================
# МОДЕЛИ
# ============================================================

@dataclass(slots=True)
class TeamUser:
    internal_id: int
    name: str
    telegram_id: str
    role: str
    manager_id: Optional[int]
    status: str
    affiliate_id: Optional[int]

    @property
    def is_active(self) -> bool:
        return normalize_status(
            self.status
        ) == ACTIVE_STATUS


@dataclass(slots=True)
class ParsedLead:
    affiliate_id: int
    status: str
    country: str
    click_id: str
    name: str


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_status(value: str) -> str:
    return " ".join(
        str(value or "").strip().lower().split()
    )


def parse_int(value) -> Optional[int]:
    text = str(value or "").strip()

    if not text:
        return None

    try:
        return int(float(text.replace(",", ".")))
    except (TypeError, ValueError):
        return None


def normalize_message_id(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ============================================================
# ПАРСИНГ СООБЩЕНИЯ
# ============================================================

AFFILIATE_PATTERN = re.compile(
    r"AffiliateID\s*:\s*(\d+)",
    re.IGNORECASE,
)

STATUS_PATTERN = re.compile(
    r"Status\s*:\s*(GOOD|BAD)",
    re.IGNORECASE,
)

COUNTRY_PATTERN = re.compile(
    r"Country\s*:\s*([A-Za-z]{2,5})",
    re.IGNORECASE,
)

CLICK_ID_PATTERN = re.compile(
    r"ClickID\s*:\s*([^\s\r\n]+)",
    re.IGNORECASE,
)

NAME_PATTERN = re.compile(
    r"(?:^|\n)\s*Name\s*:\s*(.+?)(?=\r?\n|$)",
    re.IGNORECASE,
)


def parse_lead_message(
    message_text: str,
) -> Optional[ParsedLead]:
    text = str(message_text or "").strip()

    if not text:
        return None

    affiliate_match = AFFILIATE_PATTERN.search(text)

    if not affiliate_match:
        return None

    affiliate_id = int(
        affiliate_match.group(1)
    )

    status_match = STATUS_PATTERN.search(text)
    country_match = COUNTRY_PATTERN.search(text)
    click_id_match = CLICK_ID_PATTERN.search(text)
    name_match = NAME_PATTERN.search(text)

    status = (
        status_match.group(1).upper()
        if status_match
        else ""
    )

    country = (
        country_match.group(1).upper()
        if country_match
        else ""
    )

    click_id = (
        click_id_match.group(1).strip()
        if click_id_match
        else ""
    )

    name = (
        name_match.group(1).strip()
        if name_match
        else ""
    )

    return ParsedLead(
        affiliate_id=affiliate_id,
        status=status,
        country=country,
        click_id=click_id,
        name=name,
    )


# ============================================================
# GOOGLE CREDENTIALS
# ============================================================

def ensure_google_credentials() -> str:
    """
    Возвращает путь к Google service-account JSON.

    Локально использует GOOGLE_JSON или credentials.json.
    В Docker по умолчанию использует /app/credentials.json.

    Если файла нет, создаёт его из GOOGLE_CREDENTIALS_B64.
    Переменная может содержать:
    - service-account JSON в Base64;
    - либо сам JSON-текст.
    """
    credentials_path = os.path.abspath(GOOGLE_JSON)

    if os.path.isfile(credentials_path):
        logger.info(
            "Использую Google credentials: %s",
            credentials_path,
        )
        return credentials_path

    if not GOOGLE_CREDENTIALS_B64:
        raise RuntimeError(
            "Файл Google credentials не найден: "
            f"{credentials_path}. Также не задана переменная "
            "GOOGLE_CREDENTIALS_B64."
        )

    raw_value = GOOGLE_CREDENTIALS_B64.strip()

    try:
        if raw_value.startswith("{"):
            decoded_text = raw_value
        else:
            try:
                decoded_bytes = base64.b64decode(
                    raw_value,
                    validate=True,
                )
            except binascii.Error:
                # На случай Base64 без padding или с переносами строк.
                normalized = "".join(raw_value.split())
                normalized += "=" * (-len(normalized) % 4)
                decoded_bytes = base64.b64decode(normalized)

            decoded_text = decoded_bytes.decode("utf-8")

        credentials_data = json.loads(decoded_text)

    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_B64 не содержит корректный "
            "service-account JSON."
        ) from exc

    required_fields = {
        "type",
        "project_id",
        "private_key",
        "client_email",
        "token_uri",
    }
    missing_fields = sorted(
        required_fields.difference(credentials_data)
    )

    if credentials_data.get("type") != "service_account":
        raise RuntimeError(
            "Google credentials имеют неверный type: "
            f"{credentials_data.get('type')!r}"
        )

    if missing_fields:
        raise RuntimeError(
            "В Google credentials отсутствуют поля: "
            + ", ".join(missing_fields)
        )

    parent = os.path.dirname(credentials_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    temporary_path = credentials_path + ".tmp"

    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(
            credentials_data,
            file,
            ensure_ascii=False,
        )

    os.replace(temporary_path, credentials_path)

    try:
        os.chmod(credentials_path, 0o600)
    except OSError:
        # На Windows chmod работает иначе; это не мешает запуску.
        pass

    logger.info(
        "Google credentials созданы: %s",
        credentials_path,
    )

    return credentials_path


# ============================================================
# GOOGLE SHEETS
# ============================================================

def open_google_sheets():
    credentials_path = ensure_google_credentials()

    credentials = (
        Credentials.from_service_account_file(
            credentials_path,
            scopes=SCOPES,
        )
    )

    google_client = gspread.authorize(
        credentials
    )

    spreadsheet = google_client.open_by_key(
        REPORT_SHEET_ID
    )

    users_sheet = spreadsheet.worksheet(
        USERS_SHEET_NAME
    )

    try:
        leads_sheet = spreadsheet.worksheet(
            LEADS_SHEET_NAME
        )
    except gspread.WorksheetNotFound:
        logger.warning(
            "Лист %r не найден. Создаю новый лист.",
            LEADS_SHEET_NAME,
        )

        leads_sheet = spreadsheet.add_worksheet(
            title=LEADS_SHEET_NAME,
            rows=5000,
            cols=len(LEADS_HEADERS),
        )

    return users_sheet, leads_sheet


users_sheet = None
leads_sheet = None


async def initialize_google_sheets():
    global users_sheet
    global leads_sheet

    users_sheet, leads_sheet = await asyncio.to_thread(
        open_google_sheets
    )


def require_google_sheets():
    if users_sheet is None or leads_sheet is None:
        raise RuntimeError(
            "Google Sheets ещё не инициализированы."
        )


async def run_google(func, *args):
    return await asyncio.to_thread(
        func,
        *args,
    )


async def prepare_leads_sheet():
    first_row = await run_google(
        leads_sheet.get,
        "A1:K1",
    )

    current_headers = (
        first_row[0]
        if first_row
        else []
    )

    if current_headers == LEADS_HEADERS:
        return

    await run_google(
        lambda: leads_sheet.update(
            values=[LEADS_HEADERS],
            range_name="A1:K1",
        )
    )

    logger.info(
        "Заголовки листа %r проверены.",
        LEADS_SHEET_NAME,
    )


# ============================================================
# ЗАГРУЗКА ПОЛЬЗОВАТЕЛЕЙ
# ============================================================

users_cache: dict[int, TeamUser] = {}
users_cache_updated_at = 0.0
users_cache_lock = asyncio.Lock()


async def load_users_from_sheet() -> dict[int, TeamUser]:
    """
    Лист «Пользователи»:

    A — внутренний ID
    B — имя
    C — Telegram ID
    D — Username
    E — роль
    F — внутренний ID тимлида
    G — статус
    H — AffiliateID / ID для лидов
    """

    rows = await run_google(
        users_sheet.get,
        f"A2:H{USERS_MAX_ROWS}",
    )

    result: dict[int, TeamUser] = {}

    for row_number, row in enumerate(
        rows,
        start=2,
    ):
        internal_id = parse_int(
            row[0] if len(row) > 0 else None
        )

        name = (
            str(row[1]).strip()
            if len(row) > 1
            else ""
        )

        telegram_id = (
            str(row[2]).strip()
            if len(row) > 2
            else ""
        )

        role = (
            str(row[4]).strip().lower()
            if len(row) > 4
            else ""
        )

        manager_id = parse_int(
            row[5] if len(row) > 5 else None
        )

        status = (
            str(row[6]).strip()
            if len(row) > 6
            else ""
        )

        affiliate_id = parse_int(
            row[7] if len(row) > 7 else None
        )

        if internal_id is None or not name:
            continue

        if affiliate_id is None:
            continue

        if role not in VALID_ROLES:
            logger.warning(
                "Строка %s: неизвестная роль %r",
                row_number,
                role,
            )

        user = TeamUser(
            internal_id=internal_id,
            name=name,
            telegram_id=telegram_id,
            role=role,
            manager_id=manager_id,
            status=status,
            affiliate_id=affiliate_id,
        )

        if affiliate_id in result:
            previous = result[affiliate_id]

            logger.warning(
                "Дублирующийся AffiliateID %s: "
                "%s и %s. Используется строка %s.",
                affiliate_id,
                previous.name,
                name,
                row_number,
            )

        result[affiliate_id] = user

    logger.info(
        "Загружено соответствий "
        "AffiliateID → пользователь: %s",
        len(result),
    )

    return result


async def get_users_map(
    force: bool = False,
) -> dict[int, TeamUser]:
    global users_cache
    global users_cache_updated_at

    now = time.time()

    if (
        not force
        and users_cache
        and now - users_cache_updated_at
        < USERS_CACHE_TTL
    ):
        return users_cache

    async with users_cache_lock:
        now = time.time()

        if (
            not force
            and users_cache
            and now - users_cache_updated_at
            < USERS_CACHE_TTL
        ):
            return users_cache

        users_cache = await load_users_from_sheet()
        users_cache_updated_at = time.time()

        return users_cache


# ============================================================
# ЗАЩИТА ОТ ДУБЛЕЙ
# ============================================================

processed_message_ids: set[int] = set()
processed_ids_lock = asyncio.Lock()


async def load_existing_message_ids():
    values = await run_google(
        leads_sheet.col_values,
        3,
    )

    loaded = 0

    for value in values[1:]:
        message_id = normalize_message_id(value)

        if message_id is None:
            continue

        processed_message_ids.add(message_id)
        loaded += 1

    logger.info(
        "Загружено сохранённых Message ID: %s",
        loaded,
    )


async def is_message_processed(
    message_id: int,
) -> bool:
    async with processed_ids_lock:
        return message_id in processed_message_ids


async def mark_message_processed(
    message_id: int,
):
    async with processed_ids_lock:
        processed_message_ids.add(message_id)


# ============================================================
# СОХРАНЕНИЕ ЛИДА
# ============================================================

def get_teamlead_id(user: TeamUser) -> str:
    """
    Для обычного баера записываем ID его тимлида.

    Для лида самого тимлида записываем внутренний ID
    этого тимлида.

    Для boss без руководителя поле остаётся пустым.
    """

    if user.role == ROLE_TEAMLEAD:
        return str(user.internal_id)

    if user.manager_id is not None:
        return str(user.manager_id)

    return ""


async def save_lead(
    *,
    message_id: int,
    message_date,
    lead: ParsedLead,
    user: TeamUser,
):
    local_datetime = message_date.astimezone(
        MOSCOW_TZ
    )

    row = [
        local_datetime.strftime("%H:%M:%S"),
        local_datetime.strftime("%d.%m.%Y"),
        message_id,
        lead.affiliate_id,
        user.internal_id,
        user.telegram_id,
        get_teamlead_id(user),
        lead.status,
        lead.country,
        lead.click_id,
        lead.name,
    ]

    await run_google(
        leads_sheet.append_row,
        row,
        value_input_option="USER_ENTERED",
    )

    await mark_message_processed(
        message_id
    )

    logger.info(
        "Лид сохранён: Message ID=%s, "
        "AffiliateID=%s, Buyer=%s, "
        "Status=%s, Country=%s",
        message_id,
        lead.affiliate_id,
        user.name,
        lead.status or "не указан",
        lead.country or "не указана",
    )


# ============================================================
# TELETHON
# ============================================================

client = TelegramClient(
    StringSession(
        TELETHON_SESSION_STRING
    ),
    API_ID,
    API_HASH,
)


async def find_source_chat():
    if LEADS_SOURCE_CHAT_ID_RAW:
        try:
            chat_id = int(
                LEADS_SOURCE_CHAT_ID_RAW
            )
        except ValueError as exc:
            raise RuntimeError(
                "LEADS_SOURCE_CHAT_ID должен "
                "состоять только из цифр "
                "с возможным минусом"
            ) from exc

        entity = await client.get_entity(
            chat_id
        )

        logger.info(
            "Группа найдена по ID: %s",
            chat_id,
        )

        return entity

    logger.info(
        "Ищу группу по названию: %s",
        LEADS_SOURCE_CHAT_TITLE,
    )

    async for dialog in client.iter_dialogs():
        if (
            str(dialog.name or "").strip()
            == LEADS_SOURCE_CHAT_TITLE
        ):
            logger.info(
                "Группа найдена: %s, ID: %s",
                dialog.name,
                dialog.id,
            )

            return dialog.entity

    raise RuntimeError(
        "Группа не найдена: "
        f"{LEADS_SOURCE_CHAT_TITLE}"
    )


async def handle_new_message(event):
    try:
        message = event.message

        if not message:
            return

        message_id = int(message.id)

        if await is_message_processed(
            message_id
        ):
            logger.info(
                "Message ID %s уже сохранён. Пропускаю.",
                message_id,
            )
            return

        text = str(
            message.raw_text or ""
        ).strip()

        lead = parse_lead_message(text)

        if lead is None:
            logger.info(
                "Message ID %s: AffiliateID не найден. "
                "Сообщение пропущено.",
                message_id,
            )
            return

        users_map = await get_users_map()

        user = users_map.get(
            lead.affiliate_id
        )

        if user is None:
            # Повторно читаем таблицу, потому что
            # пользователя могли добавить недавно.
            users_map = await get_users_map(
                force=True
            )

            user = users_map.get(
                lead.affiliate_id
            )

        if user is None:
            logger.warning(
                "Message ID %s: пользователь "
                "с AffiliateID %s не найден. "
                "Лид не сохранён.",
                message_id,
                lead.affiliate_id,
            )
            return

        await save_lead(
            message_id=message_id,
            message_date=message.date,
            lead=lead,
            user=user,
        )

    except Exception:
        logger.exception(
            "Ошибка обработки нового сообщения"
        )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    await initialize_google_sheets()
    require_google_sheets()

    await prepare_leads_sheet()
    await load_existing_message_ids()
    await get_users_map(force=True)

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telethon-сессия не авторизована. "
            "Проверь TELETHON_SESSION_STRING."
        )

    logger.info(
        "Telethon успешно подключен"
    )

    source_chat = await find_source_chat()

    client.add_event_handler(
        handle_new_message,
        events.NewMessage(
            chats=source_chat
        ),
    )

    logger.info(
        "Слушаю только новые сообщения. "
        "История группы не обрабатывается."
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())