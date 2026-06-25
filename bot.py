import asyncio
import base64
import json
import logging
import os
import shelve
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

# Папка для persistent-данных — на bothost туда монтируется постоянный volume,
# чтобы текущие сессии (незавершённые квартиры) переживали перезапуск бота.
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
SESSIONS_DB_PATH = os.path.join(DATA_DIR, "avito_bot_sessions")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Когда пользователь шлёт несколько фото почти одновременно (альбом/пачка), Telegram
# доставляет их как отдельные сообщения, и aiogram обрабатывает их параллельно — каждое
# фото распознаётся через Claude API независимо. Без блокировки два параллельных
# обработчика могут прочитать сессию, оба подождать ответ от API, и затем один из них
# перезапишет результат другого при сохранении — часть распознанных полей потеряется.
# Лок применяется ТОЛЬКО на короткий участок (слияние+сохранение), а не на сам запрос
# к Claude API — так скрины всё равно распознаются параллельно и быстро, а гонки нет.
_session_locks: dict[int, asyncio.Lock] = {}


def get_session_lock(uid: int) -> asyncio.Lock:
    if uid not in _session_locks:
        _session_locks[uid] = asyncio.Lock()
    return _session_locks[uid]


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
# Колонки C, D, E, F бот не трогает — туда Евгений вписывает даты звонков/осмотров вручную.
# Колонка O собирается отдельно через build_o_column_text() из нескольких полей
# (metro, entrance, building_look, garbage_chute, lobby, window_view, noise_nearby, room_areas) —
# поэтому здесь её нет, см. запись в apt_done.
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
    "ceiling_height": "AB",
    "bathroom": "AD",
    "hallway": "AE",
}
O_COL = "O"


def find_first_empty_row(ws) -> int:
    """Первая строка, где ВСЕ ячейки в диапазоне A:W пустые, начиная со строки 3
    (строки 1-2 — заголовки/легенда). Раньше проверяли только колонку B (дата) — это
    давало сбой, если строка была частично заполнена вручную без даты: бот считал её
    свободной и затирал данные. Теперь строка считается занятой, если хоть одна
    ячейка в A:W непустая."""
    rows = ws.get("A3:W")
    row = 3
    for r in rows:
        if any(cell.strip() for cell in r):
            row += 1
        else:
            break
    return row


FIELDS = [
    ("address", "Адрес"),
    ("condition", "Состояние квартиры"),
    ("agent_phone", "Телефон/агент"),
    ("year", "Год постройки"),
    ("material", "Материал дома"),
    ("elevator", "Лифт"),
    ("comment", "Комментарии"),
    ("metro", "Метро"),
    ("entrance", "Парадная"),
    ("building_look", "Внешний вид дома"),
    ("garbage_chute", "Мусоропровод"),
    ("lobby", "Тамбур"),
    ("window_view", "Окна куда"),
    ("noise_nearby", "Шумное соседство"),
    ("room_areas", "Метраж помещений"),
    ("rooms", "К-во комнат"),
    ("floor", "Этаж/этажей"),
    ("price", "Цена"),
    ("area_total", "Площадь общая"),
    ("area_living", "Площадь жилая"),
    ("area_kitchen", "Кухня"),
    ("balcony", "Балкон"),
    ("stove", "Плита"),
    ("ceiling_height", "Высота потолков"),
    ("bathroom", "С/у"),
    ("hallway", "Прихожая"),
]

# Поля, из которых при записи в таблицу собирается единая строка для колонки O.
# Порядок в этом списке — порядок частей в итоговой строке.
O_COLUMN_FIELDS = [
    "metro",
    "entrance",
    "building_look",
    "garbage_chute",
    "lobby",
    "window_view",
    "noise_nearby",
    "room_areas",
]


def build_o_column_text(fields: dict) -> str:
    """Склеивает все под-поля колонки O в одну строку через '; '."""
    parts = [fields.get(key, "").strip() for key in O_COLUMN_FIELDS]
    parts = [p for p in parts if p]
    return "; ".join(parts)

EXTRACTION_PROMPT = """Ты помогаешь собирать данные по квартире для подбора недвижимости.
Тебе присылают изображение — это один из следующих типов:
1) скриншот объявления о квартире (Avito, ЦИАН, Яндекс.Недвижимость или любая похожая площадка),
2) план квартиры с указанием площадей комнат (поэтажный план, экспликация),
3) скриншот карты местности с отметкой дома (для оценки соседства — ж/д пути, трамвайные линии,
   шумные дороги).

Внимательно определи, какой это тип, и извлеки то, что есть на изображении. Если поля нет на
изображении — оставь пустую строку "". НИКОГДА не придумывай значение, которого не видишь явно —
это особенно важно для числовых полей (площади, высота потолков, год): если цифра не написана
в тексте или не видна на плане/скрине — оставляй поле пустым, а не подставляй "типичное" значение.

ЕСЛИ ЭТО СКРИНШОТ ОБЪЯВЛЕНИЯ — читай ВЕСЬ текст описания внимательно, целиком, а не только
заголовок/шапку с ценой. Очень часто важные детали (адрес, состояние ремонта, юридические
моменты) написаны в первом или последнем абзаце описания, а не вынесены отдельно.

Извлекай из текста объявления:
- address — адрес. Часто это первая фраза описания ("улица Такая-то, дом X"), а не только то,
  что показано крупным шрифтом в шапке. Ищи по всему тексту.
- condition — это ОДНА ФРАЗА, собранная из всего, что нашлось про состояние квартиры:
  общее состояние ремонта (например "требует ремонта", "хорошее состояние", "косметический
  ремонт", "после капремонта") + состояние окон (пластиковые новые / деревянные старые, если
  упомянуто) + состояние батарей (поменяны / старые, если упомянуто) + состояние входной
  двери (поменяна / старая, если упомянуто). Собери всё найденное в одну фразу через запятую,
  например: "Требует ремонта, окна деревянные старые, батареи старые". Если что-то из этого
  не упомянуто в тексте — просто не включай эту часть, не выдумывай. Ищи слова "ремонт",
  "состояние", "окна", "батареи", "радиаторы", "дверь входная" по всему тексту описания.
- comment — юридически значимые детали сделки. Это ВАЖНО и часто пропускается, будь внимателен:
  ипотека/обременение (например "квартира в ипотеке банка X", "обременение снимается при сделке"),
  встречная покупка или альтернативная сделка, сделка через опеку (несовершеннолетние собственники),
  использование/неиспользование материнского капитала, действие по доверенности, давность
  вступления в наследство, наличие прописанных лиц, которые должны выписаться, и любые похожие
  по смыслу юридические оговорки. Если что-то из этого есть в тексте — ОБЯЗАТЕЛЬНО включи в
  comment, не пропускай даже короткие формальные фразы об этом. Если в тексте таких моментов
  нет — просто опиши другие важные детали из описания (1-2 предложения), не выдумывай юридических
  обременений, которых нет в тексте.
- window_view — куда выходят окна, ТОЛЬКО если это прямо написано в тексте объявления
  (например "окна выходят во двор", "окна на улицу и во двор"). Если не написано — оставь пустым,
  это будет уточнено отдельным вопросом позже.
- room_areas — если в описании подробно расписан метраж КАЖДОГО отдельного помещения квартиры
  (так бывает на ЦИАН и иногда на Avito: "комната изолированная 7.3 кв.м., комната 15.5 кв.м.,
  прихожая 3.2 кв.м." и т.п.) — перечисли их в формате "Прихожая 3.2 м2, комната 1 7.3 м2,
  комната 2 5.8 м2, комната 3 15.5 м2" (используй порядковые номера для комнат без названия,
  отдельно указывай прихожую/кухню/санузел если они расписаны отдельно от общих area_kitchen/
  hallway/bathroom). Если такой подробной построчной разбивки в тексте нет — оставь пустым,
  не пытайся угадать метраж по общей площади.

ЕСЛИ ЭТО ПЛАН КВАРТИРЫ — на нём обычно подписаны площади каждой комнаты, кухни, балкона/лоджии,
санузла, прихожей, и итоговая общая/жилая площадь, иногда высота потолков. Возьми из плана то,
что относится к полям area_total, area_living, area_kitchen, balcony, bathroom, hallway,
ceiling_height. Остальные поля (адрес, цена, состояние и т.д.) на плане обычно отсутствуют —
оставляй их пустыми.

Как понять назначение комнаты на плане без текстовой подписи:
- Кухня — узнаётся по значкам плиты и раковины. Площадь — в area_kitchen.
- Санузел/ванная — узнаётся по значкам унитаза, ванны или душевой кабины. Площадь — в bathroom.
- Прихожая/коридор — узкое помещение у входной двери, без окон и сантехники. Площадь — в hallway.
- Балкон/лоджия — отдельный узкий выступ за пределами основного контура. Площадь — в balcony.
- Оставшиеся жилые комнаты — их площади суммируй в area_living.
Если на плане есть прямые текстовые подписи (например "кухня 9.4") — используй их, это надёжнее значков.

ЕСЛИ ЭТО СКРИНШОТ КАРТЫ с отметкой дома — внимательно посмотри, что есть в окрестности отметки.
Эти три типа объектов выглядят по-разному, не путай их между собой:
- Железнодорожные пути — на Яндекс.Картах и похожих сервисах рисуются как ТОНКАЯ серая или
  чёрная линия со штриховкой/насечками поперёк (похоже на пунктир из коротких чёрточек вдоль
  линии). Она часто визуально менее заметна, чем обычные дороги — присматривайся внимательно
  по всей видимой площади карты, не только к самым ярким линиям.
- Трамвайные пути — обычно показаны как линия посередине проезжей части дороги, иногда с
  пунктиром другого цвета, либо отдельным значком трамвая на остановке.
- Крупная дорога/шоссе/проспект — это широкая, обычно жёлтая или оранжевая полоса с названием
  типа "проспект", "шоссе", обычной заливкой без штриховки (визуально отличается от ж/д пути).
Это РАЗНЫЕ объекты — не считай дорогу железной дорогой и наоборот, даже если они расположены
рядом друг с другом на карте.

Если рядом с домом (визуально близко на самом скрине, без попытки точно посчитать метры — ты
не можешь надёжно оценить расстояние по карте, не выдумывай конкретные цифры типа "150м")
есть что-то из вышеперечисленного — запиши в noise_nearby простым описанием, например
"Рядом железнодорожные пути" или "Рядом крупный проспект" или "Рядом трамвайные пути". Можно
использовать оценку "близко"/"в отдалении", но НЕ конкретные метры, которые ты не можешь
измерить точно. Если ничего из этого не видно рядом — оставь noise_nearby пустым (не пиши
"шума нет", просто пустая строка). Остальные поля на карте обычно отсутствуют.

Особое поле metro: если на скрине указаны станции метро со временем (значок человечка — пешком,
значок машинки — на машине), перечисли ВСЕ станции через запятую: "Название — X-Y мин пеш".

Особое поле bathroom (санузел): "раздельный 3+2" (метраж каждой части) или "совмещённый 4.5"
(общий метраж) или просто тип словом, если метража нет.

Верни ТОЛЬКО валидный JSON без markdown-разметки, без ```json, без пояснений. Строго такой формат:

{
  "address": "адрес квартиры, ищи по всему тексту описания",
  "condition": "состояние одной фразой: ремонт + окна + батареи + дверь, см. инструкцию выше",
  "agent_phone": "",
  "year": "год постройки дома, если указан",
  "material": "материал дома (панель/кирпич/монолит и т.п.), если указан",
  "elevator": "количество лифтов, если указано",
  "comment": "юридические моменты сделки (см. инструкцию выше) + другие важные детали",
  "metro": "все станции метро со временем и способом",
  "entrance": "",
  "building_look": "",
  "garbage_chute": "",
  "lobby": "",
  "window_view": "куда выходят окна, ТОЛЬКО если прямо написано в тексте объявления",
  "noise_nearby": "находки со скрина карты, см. инструкцию выше",
  "room_areas": "метраж отдельных помещений, см. инструкцию выше",
  "rooms": "количество комнат, например 2",
  "floor": "этаж/этажность, например 4/9",
  "price": "цена квартиры, только число без пробелов и валюты",
  "area_total": "общая площадь в м2, только число",
  "area_living": "жилая площадь в м2, только число, если указана",
  "area_kitchen": "площадь кухни в м2, только число, если указана",
  "balcony": "площадь балкона/лоджии в м2, только число, если указана",
  "stove": "газ или электро, если указано",
  "ceiling_height": "высота потолков в метрах, ТОЛЬКО если явно написана цифра — иначе пусто",
  "bathroom": "санузел, см. формат в инструкции выше",
  "hallway": "площадь прихожей/коридора в м2, если указана"
}

Поля entrance, building_look, garbage_chute, lobby обычно НЕЛЬЗЯ определить со скрина объявления
или плана — оставляй их пустыми всегда в этом случае, они заполняются позже отдельными вопросами.
Поле agent_phone оставляй пустым всегда.
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


def format_session_plain(session: dict) -> str:
    """Версия без HTML-разметки — для копирования и правки руками.
    Формат строки 'Подпись: значение' должен совпадать с тем, что разбирает parse_plain_session."""
    lines = []
    for key, label in FIELDS:
        val = session["fields"].get(key, "") or ""
        lines.append(f"{label}: {val}")
    lines.append(f"Ссылка: {session.get('link') or ''}")
    return "\n".join(lines)


LABEL_TO_KEY = {label: key for key, label in FIELDS}


# Блок уточняющих вопросов, который идёт после накопления карточки и перед финальным
# подтверждением. Каждый вопрос задаётся только если соответствующее поле ещё не заполнено
# (не извлеклось автоматически из объявления/плана/карты). Любой вопрос можно пропустить.
QUESTIONS = [
    {
        "field": "window_view",
        "text": "Куда выходят окна?",
        "options": [("Во двор", "Во двор"), ("На улицу", "На улицу"), ("Обе стороны", "Окна во двор и на улицу")],
    },
    {
        "field": "entrance",
        "text": "Какая парадная?",
        "options": [("✨ Чистая", "Парадная чистая"), ("😐 Обычная", "Парадная обычная"), ("🚮 Грязная", "Парадная грязная")],
    },
    {
        "field": "building_look",
        "text": "Внешний вид дома?",
        "options": [("✨ Хороший", "Внешний вид дома хороший"), ("😐 Обычный", "Внешний вид дома обычный"), ("🏗 Убитый", "Внешний вид дома убитый, требует ремонта")],
    },
    {
        "field": "garbage_chute",
        "text": "Мусоропровод?",
        "options": [("✅ Рабочий", "Мусоропровод рабочий"), ("❌ Не работает", "Мусоропровод не работает"), ("🚫 Отсутствует", "Мусоропровод отсутствует")],
    },
    {
        "field": "lobby",
        "text": "Тамбур на этаже?",
        "options": [("🧹 Чистый", "Тамбур чистый"), ("📦 Завален вещами", "Тамбур завален вещами")],
    },
    {
        "field": "noise_nearby",
        "text": "Рядом шумное соседство (ж/д, трамвай, трасса)?",
        "options": [("🚂 Ж/д рядом", "Рядом ж/д пути"), ("🚊 Трамвай рядом", "Рядом трамвайные пути"), ("🛣 Шумная дорога", "Рядом шумная дорога"), ("✅ Нет шума", "")],
    },
]


def next_question(session: dict):
    """Возвращает первый вопрос из QUESTIONS, на который ещё нет ответа в session['fields'],
    или None, если все вопросы закрыты (отвечены или пропущены)."""
    answered = session.get("answered_questions", [])
    for q in QUESTIONS:
        if q["field"] in answered:
            continue
        if session["fields"].get(q["field"]):
            continue
        return q
    return None


def question_kb(uid: int, field: str):
    q = next((q for q in QUESTIONS if q["field"] == field), None)
    rows = []
    for idx, (label, _) in enumerate(q["options"]):
        rows.append([InlineKeyboardButton(text=label, callback_data=f"q_{field}_{idx}_{uid}")])
    rows.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"qskip_{field}_{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_next_step(target, uid: int, session: dict):
    """После накопления данных (новый скрин/ссылка) или ответа на вопрос — решает,
    что показать дальше: следующий неотвеченный вопрос, либо финальную карточку с кнопками.
    target — объект с методом .answer() (Message или CallbackQuery.message)."""
    q = next_question(session)
    if q:
        await target.answer(q["text"], reply_markup=question_kb(uid, q["field"]))
    else:
        await target.answer(format_session(session), reply_markup=confirm_kb(uid))


def parse_plain_session(text: str) -> dict:
    """Разбирает текст вида 'Подпись: значение' обратно в {"fields": {...}, "link": ...}.
    Строки без узнаваемой подписи или с пустым значением игнорируются."""
    new_fields: dict[str, str] = {}
    new_link = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip()
        value = value.strip()
        if not value:
            continue
        if label == "Ссылка":
            new_link = value
        elif label in LABEL_TO_KEY:
            new_fields[LABEL_TO_KEY[label]] = value
    return {"fields": new_fields, "link": new_link}


def confirm_kb(uid: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Готово, записать", callback_data=f"apt_done_{uid}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Исправить", callback_data=f"apt_edit_{uid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"apt_drop_{uid}"),
            ],
        ]
    )


# Все незавершённые сессии (квартиры в процессе сбора данных) хранятся в shelve-файле
# на диске, а не просто в памяти процесса — иначе перезапуск бота на bothost обрушивает
# текущую работу (показанная карточка остаётся, а данные за ней пропадают).
# Ключи в shelve — строки (shelve этого требует), поэтому uid переводим в str.


def _load_session(uid: int) -> dict:
    with shelve.open(SESSIONS_DB_PATH) as db:
        key = str(uid)
        if key not in db:
            db[key] = {"fields": {}, "link": None, "awaiting_edit": False}
        return db[key]


def _save_session(uid: int, session: dict) -> None:
    with shelve.open(SESSIONS_DB_PATH) as db:
        db[str(uid)] = session


def _drop_session(uid: int) -> None:
    with shelve.open(SESSIONS_DB_PATH) as db:
        db.pop(str(uid), None)


def get_session(uid: int) -> dict:
    return _load_session(uid)


def set_awaiting_edit(uid: int, value: bool) -> None:
    session = _load_session(uid)
    session["awaiting_edit"] = value
    _save_session(uid, session)


def is_awaiting_edit(uid: int) -> bool:
    return _load_session(uid).get("awaiting_edit", False)


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
        "Пришли один или несколько скринов объявления, план квартиры с метражами "
        "(и/или ссылку на объявление) — в любом порядке. "
        "Я распознаю данные и буду показывать, что уже накопилось.\n\n"
        "Когда всё отправлено — жми «✅ Готово, записать» под последним сообщением. "
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

    # Сам запрос к Claude API (выше) идёт без блокировки — параллельные скрины
    # распознаются одновременно. Лок берём только на короткое слияние+сохранение,
    # чтобы при пачке из нескольких фото результаты не перетирали друг друга.
    async with get_session_lock(uid):
        session = get_session(uid)
        merge_fields(session, new_fields)
        _save_session(uid, session)

    await status.delete()
    await message.answer(format_session(session), reply_markup=confirm_kb(uid))


@dp.callback_query(F.data.startswith("apt_drop_"))
async def apt_drop(cb: CallbackQuery):
    uid = int(cb.data[len("apt_drop_"):])
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return
    async with get_session_lock(uid):
        _drop_session(uid)
    await cb.message.edit_text("Отменено. Можешь начать новую квартиру — пришли скрин.")
    await cb.answer()


@dp.callback_query(F.data.startswith("apt_edit_"))
async def apt_edit(cb: CallbackQuery):
    uid = int(cb.data[len("apt_edit_"):])
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return

    session = get_session(uid)
    if not session["fields"] and not session.get("link"):
        await cb.answer("Нет данных для правки, пришли скрин заново", show_alert=True)
        return

    set_awaiting_edit(uid, True)
    await cb.answer()
    await cb.message.answer(
        "Скопируй сообщение ниже целиком (долгий тап → Копировать), "
        "поправь нужные строки и пришли обратно."
    )
    await cb.message.answer(format_session_plain(session))


@dp.callback_query(F.data.startswith("q_"))
async def question_answer(cb: CallbackQuery):
    # формат: q_{field}_{idx}_{uid}
    rest = cb.data[len("q_"):]
    field, idx_s, uid_s = rest.rsplit("_", 2)
    idx = int(idx_s)
    uid = int(uid_s)
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return

    q = next((q for q in QUESTIONS if q["field"] == field), None)
    if not q or idx >= len(q["options"]):
        await cb.answer()
        return

    _, value = q["options"][idx]
    async with get_session_lock(uid):
        session = get_session(uid)
        if value:
            session["fields"][field] = value
        session.setdefault("answered_questions", []).append(field)
        _save_session(uid, session)

    await cb.answer()
    await show_next_step(cb.message, uid, session)


@dp.callback_query(F.data.startswith("qskip_"))
async def question_skip(cb: CallbackQuery):
    # формат: qskip_{field}_{uid}
    rest = cb.data[len("qskip_"):]
    field, uid_s = rest.rsplit("_", 1)
    uid = int(uid_s)
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return

    async with get_session_lock(uid):
        session = get_session(uid)
        session.setdefault("answered_questions", []).append(field)
        _save_session(uid, session)

    await cb.answer()
    await show_next_step(cb.message, uid, session)


@dp.callback_query(F.data.startswith("apt_done_"))
async def apt_done(cb: CallbackQuery):
    uid = int(cb.data[len("apt_done_"):])
    if cb.from_user.id != uid:
        await cb.answer("Это не твоя карточка", show_alert=True)
        return

    async with get_session_lock(uid):
        session = get_session(uid)
        if not session["fields"]:
            await cb.answer("Нет данных для записи, пришли скрин заново", show_alert=True)
            return

        q = next_question(session)
        if q:
            await cb.answer()
            await cb.message.answer(q["text"], reply_markup=question_kb(uid, q["field"]))
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

            o_text = build_o_column_text(session["fields"])
            if o_text:
                updates.append({"range": f"{O_COL}{row_idx}", "values": [[o_text]]})

            if session.get("link"):
                updates.append({"range": f"{LINK_COL}{row_idx}", "values": [[session["link"]]]})

            ws.batch_update(updates, value_input_option="USER_ENTERED")
        except Exception as e:
            log.exception("Ошибка записи в таблицу")
            await cb.message.edit_text(f"Не получилось записать в таблицу: {e}")
            return

        _drop_session(uid)

    await cb.message.edit_text(
        cb.message.html_text + "\n\n✅ <b>Квартира записана в таблицу одной строкой.</b>"
    )


@dp.message(F.text)
async def handle_text(message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if is_awaiting_edit(uid):
        set_awaiting_edit(uid, False)
        parsed = parse_plain_session(text)
        async with get_session_lock(uid):
            session = get_session(uid)
            session["fields"].update(parsed["fields"])
            if parsed["link"] is not None:
                session["link"] = parsed["link"]
            _save_session(uid, session)
        await message.answer(format_session(session), reply_markup=confirm_kb(uid))
        return

    if "avito.ru" not in text and "http" not in text:
        await message.answer(
            "Если это ссылка на объявление — пришли её как есть (начинается с http).\n"
            "Если хочешь добавить квартиру — пришли скрин с Avito."
        )
        return

    async with get_session_lock(uid):
        session = get_session(uid)
        session["link"] = text
        _save_session(uid, session)
    await message.answer(format_session(session), reply_markup=confirm_kb(uid))


async def main():
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
