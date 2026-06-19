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


def format_preview(fields: dict) -> str:
    lines = ["<b>Распознал со скрина:</b>\n"]
    for key, label in FIELDS:
        val = fields.get(key, "") or "—"
        lines.append(f"<b>{label}:</b> {val}")
    lines.append("\n<i>Проверь и подтверди</i>")
    return "\n".join(lines)


def confirm_kb(card_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Записать", callback_data=f"apt_confirm_{card_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"apt_cancel_{card_id}"),
            ]
        ]
    )


# pending[card_id] = dict с полями, ожидающими подтверждения
# card_id уникален для каждого скрина — так параллельные скрины не перетирают друг друга
pending: dict[int, dict] = {}
_card_counter = 0


def next_card_id() -> int:
    global _card_counter
    _card_counter += 1
    return _card_counter


# rows_waiting_for_link[user_id] = список номеров строк, записанных в таблицу,
# но ещё без ссылки (в порядке добавления — старые первыми)
rows_waiting_for_link: dict[int, list[int]] = {}


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Пришли скрин объявления с Avito — распознаю данные и покажу для проверки.\n\n"
        "После подтверждения и записи в таблицу пришли ссылку на объявление отдельным сообщением — "
        "впишу её в ту же строку."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    status = await message.answer("Распознаю скрин...")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        fields = await extract_from_image(file_bytes.read())
    except Exception as e:
        log.exception("Ошибка распознавания")
        await status.edit_text(f"Не получилось распознать скрин: {e}")
        return

    card_id = next_card_id()
    pending[card_id] = fields
    await status.delete()
    await message.answer(format_preview(fields), reply_markup=confirm_kb(card_id))


@dp.callback_query(F.data.startswith("apt_cancel_"))
async def apt_cancel(cb: CallbackQuery):
    card_id = int(cb.data[len("apt_cancel_"):])
    pending.pop(card_id, None)
    await cb.message.edit_text("Отменено. Можешь прислать новый скрин.")
    await cb.answer()


@dp.callback_query(F.data.startswith("apt_confirm_"))
async def apt_confirm(cb: CallbackQuery):
    card_id = int(cb.data[len("apt_confirm_"):])
    uid = cb.from_user.id
    fields = pending.get(card_id)
    if not fields:
        await cb.answer("Данные устарели, пришли скрин заново", show_alert=True)
        return

    await cb.answer("Записываю...")
    try:
        ws = get_sheet()
        row = [
            today_msk_str(),  # дата
            "",  # ссылка (заполнится отдельным сообщением)
            fields.get("address", ""),
            fields.get("condition", ""),
            fields.get("agent_phone", ""),
            fields.get("year", ""),
            fields.get("material", ""),
            fields.get("elevator", ""),
            fields.get("comment", ""),
            fields.get("rooms", ""),
            fields.get("floor", ""),
            fields.get("price", ""),
            fields.get("area_total", ""),
            fields.get("area_living", ""),
            fields.get("area_kitchen", ""),
            fields.get("balcony", ""),
            fields.get("stove", ""),
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        row_count = len(ws.get_all_values())
        rows_waiting_for_link.setdefault(uid, []).append(row_count)
    except Exception as e:
        log.exception("Ошибка записи в таблицу")
        await cb.message.edit_text(f"Не получилось записать в таблицу: {e}")
        return

    pending.pop(card_id, None)
    await cb.message.edit_text(
        cb.message.html_text + "\n\n✅ <b>Записано в таблицу.</b>\nПришли ссылку на объявление отдельным сообщением."
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

    queue = rows_waiting_for_link.get(uid) or []
    if not queue:
        await message.answer("Не вижу записанных строк без ссылки. Сначала пришли скрин и подтверди запись.")
        return

    row_idx = queue.pop(0)  # самая старая ожидающая строка

    try:
        ws = get_sheet()
        ws.update_cell(row_idx, 2, text)  # колонка 2 = ссылка
    except Exception as e:
        log.exception("Ошибка записи ссылки")
        await message.answer(f"Не получилось вписать ссылку: {e}")
        return

    await message.answer("Ссылка добавлена в таблицу ✅")


async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
