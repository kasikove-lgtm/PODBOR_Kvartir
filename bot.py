import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone, timedelta as td

import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avito_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Объявления")
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def now_msk():
    msk = timezone(td(hours=3))
    return datetime.now(timezone.utc).astimezone(msk).replace(tzinfo=None)


def today_msk_str():
    return now_msk().strftime("%d.%m.%Y")


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet(SHEET_NAME)


# Колонка B = "дата занесения в таблицу" — по ней определяем первую свободную строку.
DATE_COL = "B"
LINK_COL = "G"

# Соответствие полей бота реальным буквам колонок таблицы "Объявления".
# Колонки C, D, E, F, O бот не трогает — туда Евгений вписывает даты звонков/осмотров
# и параметры дома вручную.
COLUMN_MAP = {
    "address": "H",
    "condition": "I",
    "agent_phone": "J",
    "year": "K",
    "material": "L",
    "elevator": "M",
    "comment": "N",
    "rooms": "P",
    "floor": "Q",
    "price": "R",
    "area_total": "S",
    "area_living": "T",
    "area_kitchen": "U",
    "balcony": "V",
    "stove": "W",
}


def find_first_empty_row(ws) -> int:
    """Первая строка, где колонка B (дата занесения) пустая, начиная со строки 3
    (строки 1-2 — заголовки/легенда)."""
    col_values = ws.col_values(gspread.utils.a1_to_rowcol(f"{DATE_COL}1")[1])
    row = 3
    while row <= len(col_values) and col_values[row - 1].strip():
        row += 1
    return row


FIELDS = [
    ("address", "Адрес"),
    ("condition", "Состояние квартиры"),
    ("agent_phone", "Телефон/агент"),
    ("year", "Год постройки"),
    ("material", "Материал дома"),
    ("elevator", "Лифт"),
    ("comment", "Комментарии"),
    ("rooms", "К-во комнат"),
    ("floor", "Этаж/этажей"),
    ("price", "Цена"),
    ("area_total", "Площадь общая"),
    ("area_living", "Площадь жилая"),
    ("area_kitchen", "Кухня"),
    ("balcony", "Балкон"),
    ("stove", "Плита"),
]

EXTRACTION_PROMPT = """Ты помогаешь распознавать объявления о квартирах с Avito по скриншоту.
Внимательно посмотри на изображение и извлеки следующие поля. Если поля на скрине нет — оставь пустую строку "".

Верни ТОЛЬКО валидный JSON без markdown-разметки, без ```json, без пояснений. Строго такой формат:

{
  "address": "адрес квартиры",
  "condition": "состояние квартиры (отделка, окна, состояние - как написано в объявлении)",
  "agent_phone": "",
  "year": "год постройки дома, если указан",
  "material": "материал дома (панель/кирпич/монолит и т.п.), если указан",
  "elevator": "количество лифтов, если указано",
  "comment": "краткие важные детали из описания, которые не попали в другие поля (1-2 предложения)",
  "rooms": "количество комнат, например 2",
  "floor": "этаж/этажность, например 4/9",
  "price": "цена квартиры, только число без пробелов и валюты",
  "area_total": "общая площадь в м2, только число",
  "area_living": "жилая площадь в м2, только число, если указана",
  "area_kitchen": "площадь кухни в м2, только число, если указана",
  "balcony": "площадь балкона в м2, только число, если указана",
  "stove": "газ или электро, если указано"
}

Поле agent_phone оставляй пустым всегда — телефон на скринах Avito обычно скрыт.
Если что-то не удаётся прочитать однозначно — оставляй пустую строку, не придумывай.
"""


async def extract_from_image(image_bytes: bytes) -> dict:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": EXTRACTION_PROMPT},
                        ],
                    }
                ],
            },
        )
    data = resp.json()
    if "content" not in data:
        log.error(f"Anthropic API error: {data}")
        raise RuntimeError(f"Ошибка Claude API: {data}")
    text_parts = [b["text"] for b in data["content"] if b.get("type") == "text"]
    raw = "".join(text_parts).strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def format_session(session: dict) -> str:
    lines = ["<b>Накоплено по квартире:</b>\n"]
    for key, label in FIELDS:
        val = session["fields"].get(key, "") or "—"
        lines.append(f"<b>{label}:</b> {val}")
    link = session.get("link") or "—"
    lines.append(f"<b>Ссылка:</b> {link}")
    lines.append("\n<i>Шли ещё скрины/ссылку или жми «Готово»</i>")
    return "\n".join(lines)


def confirm_kb(uid: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Готово, записать", callback_data=f"apt_done_{uid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"apt_drop_{uid}"),
            ]
        ]
    )


# sessions[user_id] = {"fields": {...накопленные поля...}, "link": "..." или None}
# Одна сессия = одна квартира. Все скрины и ссылка, присланные подряд этим пользователем,
# копятся в одну сессию, пока он не нажмёт "Готово" или "Отмена".
sessions: dict[int, dict] = {}


def get_session(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = {"fields": {}, "link": None}
    return sessions[uid]


def merge_fields(session: dict, new_fields: dict) -> None:
    """Докладывает новые распознанные поля в сессию.
    Не перетирает уже заполненное непустое значение — новое значение
    добавляется только в пустые поля."""
    for key, _ in FIELDS:
        new_val = (new_fields.get(key) or "").strip()
        if not new_val:
            continue
        if not session["fields"].get(key):
            session["fields"][key] = new_val


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Подбираем квартиру по объявлению с Avito.\n\n"
        "Пришли один или несколько скринов объявления (и/или ссылку на него) — в любом порядке. "
        "Я распознаю данные и буду показывать, что уже накопилось.\n\n"
        "Когда все скрины и ссылка отправлены — жми «✅ Готово, записать» под последним сообщением. "
        "Это запишет одну строку в таблицу."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    uid = message.from_user.id
    status = await message.answer("Распознаю скрин...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        new_fields = await extract_from_image(file_bytes.read())
    except Exception as e:
        log.exception("Ошибка распознавания")
        await status.edit_text(f"Не получилось распознать скрин: {e}")
        return

    session = get_session(uid)
    merge_fields(session, new_fields)
    await status.delete()
    await message.answer(format_session(session), reply_markup=confirm_kb(uid))


@dp.callback_query(F.data.startswith("apt_drop_"))
async def apt_drop(cb: CallbackQuery):
    uid = int(cb.data[len("apt_drop_"):])
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return
    sessions.pop(uid, None)
    await cb.message.edit_text("Отменено. Можешь начать новую квартиру — пришли скрин.")
    await cb.answer()


@dp.callback_query(F.data.startswith("apt_done_"))
async def apt_done(cb: CallbackQuery):
    uid = int(cb.data[len("apt_done_"):])
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return

    session = sessions.get(uid)
    if not session or not session["fields"]:
        await cb.answer("Нет данных для записи, пришли скрин заново", show_alert=True)
        return

    await cb.answer("Записываю...")
    try:
        ws = get_sheet()
        row_idx = find_first_empty_row(ws)

        updates = [{"range": f"{DATE_COL}{row_idx}", "values": [[today_msk_str()]]}]
        for key, col in COLUMN_MAP.items():
            val = session["fields"].get(key, "")
            if val:
                updates.append({"range": f"{col}{row_idx}", "values": [[val]]})
        if session.get("link"):
            updates.append({"range": f"{LINK_COL}{row_idx}", "values": [[session["link"]]]})

        ws.batch_update(updates, value_input_option="USER_ENTERED")
    except Exception as e:
        log.exception("Ошибка записи в таблицу")
        await cb.message.edit_text(f"Не получилось записать в таблицу: {e}")
        return

    sessions.pop(uid, None)
    await cb.message.edit_text(
        cb.message.html_text + "\n\n✅ <b>Квартира записана в таблицу одной строкой.</b>"
    )


@dp.message(F.text)
async def handle_text(message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if "avito.ru" not in text and "http" not in text:
        await message.answer(
            "Если это ссылка на объявление — пришли её как есть (начинается с http).\n"
            "Если хочешь добавить квартиру — пришли скрин с Avito."
        )
        return

    session = get_session(uid)
    session["link"] = text
    await message.answer(format_session(session), reply_markup=confirm_kb(uid))


async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
