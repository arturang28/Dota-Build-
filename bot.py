from __future__ import annotations

import io
import os
import re
import sys
import time
import logging
import asyncio
import urllib.request
import warnings

from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# Подавляем назойливые системные предупреждения (Python 3.9 EOL, thought_signature, deprecations)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Глушим технические INFO-логи Google SDK (AFC, gRPC)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("google.genai").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("google_genai.models").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Логирование самого бота
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 1. Исправляем сетевой стек Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 2. Очищаем переменные от некорректных socks-прокси
for key in ["ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"]:
    if key in os.environ and "socks4" in os.environ[key]:
        del os.environ[key]

_orig_getproxies = urllib.request.getproxies


def _patched_getproxies():
    proxies = _orig_getproxies()
    return {k: v for k, v in proxies.items() if not v.startswith("socks4")}


urllib.request.getproxies = _patched_getproxies

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai
from google.genai import types as g_types
from google.genai.errors import APIError

# ================= НАСТРОЙКИ (ИЗ .ENV) =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN не найден в .env файле!")
if not GEMINI_API_KEY:
    logging.error("❌ GEMINI_API_KEY не найден в .env файле!")

bot: Bot | None = None
dp = Dispatcher()
ai = genai.Client(api_key=GEMINI_API_KEY)

# Список моделей для отказоустойчивости при перегрузках
MODELS_TO_TRY = ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-flash-latest"]

# ================= КЭШИРОВАНИЕ ЗАПРОСОВ (ЭКОНОМИЯ API) =================
# Кэш хранит: normalize_key -> (result_text, timestamp)
# Срок жизни кэша: 2 часа (7200 сек)
BUILD_CACHE: dict[str, tuple[str, float]] = {}
CACHE_TTL = 7200


def get_from_cache(key: str) -> str | None:
    norm_key = re.sub(r"\s+", " ", key.lower().strip())
    if norm_key in BUILD_CACHE:
        val, ts = BUILD_CACHE[norm_key]
        if time.time() - ts < CACHE_TTL:
            return val
        else:
            del BUILD_CACHE[norm_key]
    return None


def save_to_cache(key: str, val: str) -> None:
    norm_key = re.sub(r"\s+", " ", key.lower().strip())
    # Ограничиваем размер кэша до 200 записей
    if len(BUILD_CACHE) > 200:
        BUILD_CACHE.pop(next(iter(BUILD_CACHE)))
    BUILD_CACHE[norm_key] = (val, time.time())


# ================= FSM СОСТОЯНИЯ =================
class BuildDialog(StatesGroup):
    waiting_for_hero = State()
    waiting_for_position = State()
    waiting_for_lane_enemies = State()
    waiting_for_team_enemies = State()
    build_ready = State()


# ================= СИСТЕМНЫЙ ПРОМПТ =================
SYSTEM_PROMPT = """
Ты — профессиональный аналитик и драфт-тренер по Dota 2 с 10 000 MMR.
Твоя задача — составить четкий, практичный билд под конкретный матч, опираясь на актуальные патчи Dota 2.

ЖЕСТКОЕ ПРАВИЛО СТАРТОВОГО ЗАКУПА (МАТЕМАТИКА 600 ЗОЛОТА):
У игрока на старте ровно 600 золота!
Сумма всех предметов в разделе 'СТАРТОВЫЙ ЗАКУП' ОБЯЗАНА быть МЕНЬШЕ ИЛИ РАВНА 600!
Актуальные базовые цены:
• Tango: 90
• Healing Salve: 100
• Magic Stick: 200
• Faerie Fire: 65
• Iron Branch: 50
• Blood Grenade: 50
• Wind Lace: 250
• Circlet: 155
• Gauntlets / Slippers / Mantle: 140
• Ring of Protection: 175
• Quelling Blade: 100
• Blight Stone: 300

В стартовом закупе ОБЯЗАТЕЛЬНО указывай цену каждого предмета и выведи:
'💰 <b>Итого: [Сумма]/600 золота</b>'.
Если стики, сапог или виндлейс нужны, но превышают 600 золота — переноси их в раздел ранней игры.

ВАЖНО:
- ОТВЕЧАЙ СТРОГО НА РУССКОМ ЯЗЫКЕ! Названия предметов и способностей можно писать на английском или русском, но весь анализ, пояснения, роли и заголовки пиши ТОЛЬКО на русском языке.
- НЕ упоминай аспекты (facets) и нейтральные предметы.
- Для предметов раздела CORE ОБЯЗАТЕЛЬНО указывай ОРИЕНТИРОВОЧНЫЙ ЦЕЛЕВОЙ ТАЙМИНГ (например: 13–15 мин) и краткую подсказку, что делать при отставании.

СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ ДЛЯ TELEGRAM:
1. Запрещено использовать Markdown заголовки ('###', '##', '#') и разделители ('---').
2. Запрещено использовать двойные звездочки '**'.
3. Для жирного шрифта используй ТОЛЬКО HTML-теги: <b>жирный текст</b>. Для курсива: <i>курсив</i>.
4. Обильно используй тематические эмодзи.

Структура ответа:
🛡️ <b>СТАРТОВЫЙ ЗАКУП (ПОД ЛАЙН)</b>
• 🌿 <b>Название</b> (цена золота) — зачем берем против оппонентов на линии.
💰 <b>Итого: ХХХ/600 золота</b>

⚡ <b>РАННЯЯ ИГРА (ДОНОС КУРЬЕРОМ И ЛАЙНИНГ)</b>
• 🪄 <b>Название</b> — что доносим первым курьером (стик, сапог, капли, грейды).

⚔️ <b>ОСНОВНЫЕ ПРЕДМЕТЫ (CORE) С ТАЙМИНГАМИ</b>
• ⚔️ <b>Название</b> (ориентир: ХХ–ХХ мин) — роль предмета в этом матче и план при отставании.

👑 <b>ЛЕЙТ-ГЕЙМ (ФИНАЛЬНЫЙ СЛОТ-БИЛД)</b>
• 👑 <b>Название</b> — лейт-артефакты и грейды под эту игру.

🎯 <b>СИТУАТИВНЫЕ ПРЕДМЕТЫ И КОНТРПИКИ</b>
• 🎯 <b>Название</b> — ПРОТИВ КОГО конкретно из вражеского пика собирать и почему.

💡 <b>ПРОКАЧКА СПОСОБНОСТЕЙ (1-6 УРОВЕНЬ)</b>
• Порядок прокачки способностей под эту линию (1-й, 2-й, 3-й, 4-й, 5-й, 6-й лвл).
"""


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
VALID_HTML_TAGS = re.compile(r"(</?(?:b|i|u|s|code|pre|a)(?:\s+[^>]+)?>)", re.IGNORECASE)


def sanitize_telegram_html(text: str) -> str:
    """Заменяет опасные символы, не ломая легитимные теги <b>, <i>, <code>, <pre>."""
    # Преобразуем маркдаун **жирный** в <b>
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
    # Удаляем маркдаун заголовки #
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    # Разбиваем текст по тегам и экранируем только содержимое
    tokens = VALID_HTML_TAGS.split(text)
    sanitized_chunks = []
    for token in tokens:
        if VALID_HTML_TAGS.fullmatch(token):
            sanitized_chunks.append(token)
        else:
            escaped = (
                token.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            # Исправляем случайно задвоенные спецсимволы
            escaped = re.sub(r"&amp;(amp|lt|gt|quot);", r"&\1;", escaped)
            sanitized_chunks.append(escaped)

    return "".join(sanitized_chunks)


def extract_clean_text(response) -> str:
    """Извлекает текст ответа напрямую из parts, убирая предупреждения о thought_signature."""
    if not response or not response.candidates:
        return ""
    text_parts = []
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text_parts.append(part.text)
    return "".join(text_parts).strip()


async def send_safe_message(message: types.Message, text: str, reply_markup=None):
    """Безопасная отправка длинных сообщений с разбивкой и обработкой HTML."""
    clean_text = sanitize_telegram_html(text.strip())
    max_len = 3900

    if len(clean_text) <= max_len:
        try:
            return await message.answer(clean_text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception as e:
            logging.warning(f"Ошибка HTML-парсинга, отправка без разметки: {e}")
            return await message.answer(text, reply_markup=reply_markup)

    paragraphs = clean_text.split("\n\n")
    chunks = []
    current_chunk = ""

    for p in paragraphs:
        if len(current_chunk) + len(p) + 2 > max_len:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = p
        else:
            current_chunk = f"{current_chunk}\n\n{p}" if current_chunk else p

    if current_chunk:
        chunks.append(current_chunk.strip())

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        markup = reply_markup if is_last else None
        try:
            await message.answer(chunk, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await message.answer(chunk, reply_markup=markup)
        await asyncio.sleep(0.3)


async def generate_with_gemini(contents: list | str, max_tokens: int = 3500) -> str:
    """Асинхронный вызов Gemini API без потери токенов на рассуждения и без обрывов текста."""
    last_error = None

    # thinking_budget=0 отключает трату токенов на скрытые размышления,
    # что гарантирует быстрый ответ и исключает обрыв текста на середине.
    config = g_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        temperature=0.3,
        thinking_config=g_types.ThinkingConfig(thinking_budget=0)
    )

    for attempt in range(1, 4):
        for model_name in MODELS_TO_TRY:
            try:
                response = await ai.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
                text = extract_clean_text(response)
                if text:
                    return text
            except APIError as e:
                last_error = e
                if "503" in str(e) or "429" in str(e):
                    logging.warning(f"Модель {model_name} временно занята (попытка {attempt}): {e}")
                    await asyncio.sleep(2.0)
                    continue
                logging.error(f"Ошибка модели {model_name}: {e}")
            except Exception as e:
                last_error = e
                logging.error(f"Непредвиденная ошибка с {model_name}: {e}")

    raise RuntimeError(f"Все попытки вызова моделей Gemini исчерпаны: {last_error}")


# ================= КЛАВИАТУРЫ =================
def get_main_reply_keyboard():
    """Постоянная клавиатура внизу экрана над полем ввода."""
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="🚀 Подобрать билд"),
                types.KeyboardButton(text="⚡ Быстрый ввод")
            ],
            [
                types.KeyboardButton(text="🔄 Главное меню"),
                types.KeyboardButton(text="❌ Отмена")
            ]
        ],
        resize_keyboard=True,
        persistent=True
    )


def get_start_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Подобрать билд (Пошагово)", callback_data="start_build_flow")
    builder.button(text="⚡ Быстрый ввод (в 1 сообщение)", callback_data="fast_mode_info")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_position_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="1 ⚔️ Керри (Safe)", callback_data="pos_1_Керри")
    builder.button(text="2 🧙‍♂️ Мидер (Mid)", callback_data="pos_2_Мид")
    builder.button(text="3 🛡️ Тройка (Offlane)", callback_data="pos_3_Оффлейн")
    builder.button(text="4 ⚡ Семи-саппорт (4)", callback_data="pos_4_Саппорт")
    builder.button(text="5 🩹 Фулл-саппорт (5)", callback_data="pos_5_Саппорт")
    builder.button(text="❌ Отмена", callback_data="cancel_flow")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def get_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_flow")
    return builder.as_markup()


def get_skip_lane_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⏩ Пропустить (только линия)", callback_data="skip_team_enemies")
    builder.button(text="❌ Отмена", callback_data="cancel_flow")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_build_result_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Краткий чек-лист", callback_data="quick_checklist")
    builder.button(text="⚔️ Совет по геймплею", callback_data="gameplay_tips")
    builder.button(text="🔄 Новый билд", callback_data="start_build_flow")
    builder.adjust(2, 1)
    return builder.as_markup()


# ================= ОБРАБОТЧИКИ КОМАНД И СТАРТА =================

@dp.message(CommandStart())
@dp.message(F.text == "🔄 Главное меню")
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Добро пожаловать в Dota 2 AI Coach!</b>\n\n"
        "Я помогу подобрать идеальную сборку предметов, тайминги и раскачку "
        "прямо во время драфта матча!\n\n"
        "✨ <b>Возможности:</b>\n"
        "• 🛡️ <b>Умный стартовый закуп:</b> математика ровно в лимит <b>600 золота</b> под твою линию.\n"
        "• ⏱️ <b>Core-предметы с таймингами:</b> знаешь точные минуты, когда айтем должен быть в инвентаре.\n"
        "• 🎯 <b>Точечные контрпики:</b> сборка против конкретных вражеских способностей и героев.\n"
        "• 📸 <b>Анализ по фото:</b> скинь скриншот экрана выбора героев — я сам распознаю пики!\n"
        "• ⚡ <b>Быстрый ввод:</b> напиши пик одной строчкой, когда мало времени.\n\n"
        "Нажимай кнопку внизу экрана или выбери формат ниже: 👇"
    )
    # Показываем нижнюю постоянную клавиатуру и прикрепляем инлайн-кнопки
    await message.answer(welcome_text, reply_markup=get_main_reply_keyboard(), parse_mode="HTML")
    await message.answer("👇 Выбери режим подбора:", reply_markup=get_start_keyboard())


@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
@dp.callback_query(F.data == "cancel_flow")
async def cancel_handler(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "❌ Действие отменено. Чтобы начать заново, нажми кнопку ниже."
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_start_keyboard())
        await event.answer()
    else:
        await event.answer(text, reply_markup=get_start_keyboard())


@dp.message(F.text == "⚡ Быстрый ввод")
@dp.callback_query(F.data == "fast_mode_info")
async def fast_mode_info_callback(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    info_text = (
        "⚡ <b>Быстрый ввод (когда на пике мало времени):</b>\n\n"
        "Просто отправь мне одно сообщение в таком формате:\n"
        "<code>/fast [Твой_герой] [Позиция] против [Герои_на_линии] | [Остальные_враги]</code>\n\n"
        "<b>Примеры:</b>\n"
        "• <code>/fast Сларк 1 против Акс, Дазл | СФ, Пудж, Марс</code>\n"
        "• <code>/fast Инвокер 2 против Снайпер</code>\n"
        "• <code>/fast Кентавр 3 против Джаггернаут, ЦМ</code>\n\n"
        "Или нажми <b>🚀 Подобрать билд</b> внизу для пошагового мастера! 👇"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(info_text, reply_markup=get_start_keyboard(), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(info_text, reply_markup=get_start_keyboard(), parse_mode="HTML")


# ================= БЫСТРЫЙ ВВОД ОДНОЙ СТРОКОЙ =================
@dp.message(Command("fast"))
async def fast_build_cmd(message: types.Message, state: FSMContext, bot: Bot):
    query = message.text.replace("/fast", "").strip()
    if not query:
        await message.answer(
            "⚠️ Пожалуйста, укажи данные после команды, например:\n"
            "<code>/fast Сларк 1 против Акс, Дазл | СФ, Пудж, Марс</code>",
            parse_mode="HTML"
        )
        return

    await execute_build_analysis(message=message, state=state, bot=bot, query_text=query, context_info=query)


# ================= ПОШАГОВЫЙ ДИАЛОГ (FSM) =================

@dp.message(F.text == "🚀 Подобрать билд")
@dp.callback_query(F.data == "start_build_flow")
async def start_build_flow_callback(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "🎮 <b>Шаг 1:</b> Напиши имя своего <b>героя</b> <i>(например: Slark, Axe, Invoker)</i>\n"
        "<i>(или прямо сейчас скинь фото/скриншот экрана драфта)</i>"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await state.set_state(BuildDialog.waiting_for_hero)


@dp.message(BuildDialog.waiting_for_hero)
async def hero_chosen(message: types.Message, state: FSMContext, bot: Bot):
    text = message.text.strip()

    # Проверка: вдруг пользователь ввел всю связку сразу ("Сларк 1 против Акс...")
    if "против" in text.lower() or " vs " in text.lower():
        await execute_build_analysis(message=message, state=state, bot=bot, query_text=text, context_info=text)
        return

    await state.update_data(hero=text)
    await message.answer(
        f"Отлично, играем за <b>{text}</b>! 🎮\n\n"
        "👉 <b>Шаг 2:</b> На какой позиции ты играешь? Выбери кнопкой:",
        reply_markup=get_position_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(BuildDialog.waiting_for_position)


@dp.callback_query(BuildDialog.waiting_for_position, F.data.startswith("pos_"))
async def position_chosen(callback: types.CallbackQuery, state: FSMContext):
    pos_data = callback.data.replace("pos_", "").replace("_", " ")
    await state.update_data(position=pos_data)

    await callback.message.edit_text(
        f"Позиция: <b>{pos_data}</b> ✅\n\n"
        "👉 <b>Шаг 3:</b> Кто стоит <b>ПРОТИВ ТЕБЯ НА ЛИНИИ</b>?\n"
        "<i>(Напиши 1–2 героев через запятую, например: Axe, Dazzle или Viper)</i>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()
    await state.set_state(BuildDialog.waiting_for_lane_enemies)


@dp.message(BuildDialog.waiting_for_lane_enemies)
async def lane_enemies_chosen(message: types.Message, state: FSMContext):
    lane_text = message.text.strip()
    await state.update_data(lane_enemies=lane_text)

    await message.answer(
        f"Противники на линии: <b>{lane_text}</b> ⚔️\n\n"
        "👉 <b>Шаг 4:</b> Кто <b>ОСТАЛЬНЫЕ ВРАГИ</b> в команде?\n"
        "<i>(Напиши остальных героев через запятую или нажми 'Пропустить')</i>",
        reply_markup=get_skip_lane_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(BuildDialog.waiting_for_team_enemies)


@dp.callback_query(BuildDialog.waiting_for_team_enemies, F.data == "skip_team_enemies")
async def skip_team_enemies_callback(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    data = await state.get_data()
    hero = data.get("hero", "Герой")
    position = data.get("position", "1")
    lane_enemies = data.get("lane_enemies", "Не указаны")

    user_query = (
        f"Герой игрока: {hero}\n"
        f"Позиция: {position}\n"
        f"Прямые соперники на линии: {lane_enemies}\n"
        f"Остальные враги: Неизвестны (сделай упор на сильнейшую линию и универсальные кор-предметы)"
    )
    context_info = f"{hero} ({position}) против {lane_enemies}"
    await execute_build_analysis(message=callback.message, state=state, bot=bot, query_text=user_query, context_info=context_info)


@dp.message(BuildDialog.waiting_for_team_enemies)
async def team_enemies_chosen(message: types.Message, state: FSMContext, bot: Bot):
    team_enemies = message.text.strip()
    data = await state.get_data()
    hero = data.get("hero", "Герой")
    position = data.get("position", "1")
    lane_enemies = data.get("lane_enemies", "Не указаны")

    user_query = (
        f"Герой игрока: {hero}\n"
        f"Позиция: {position}\n"
        f"Прямые соперники на линии: {lane_enemies}\n"
        f"Остальные враги в матче: {team_enemies}"
    )
    context_info = f"{hero} ({position}) против линии [{lane_enemies}], пик [{team_enemies}]"
    await execute_build_analysis(message=message, state=state, bot=bot, query_text=user_query, context_info=context_info)


# ================= ВЫПОЛНЕНИЕ АНАЛИЗА БИЛДА С КЭШЕМ =================
async def execute_build_analysis(
    message: types.Message,
    state: FSMContext,
    bot: Bot,
    query_text: str,
    context_info: str
):
    # Проверяем кэш перед отправкой запроса в Google API
    cached_response = get_from_cache(query_text)
    if cached_response:
        logging.info(f"Ответ взят из кэша для: {context_info}")
        await state.set_state(BuildDialog.build_ready)
        await state.update_data(last_build=cached_response, last_query=context_info)
        await send_safe_message(message=message, text=cached_response, reply_markup=get_build_result_keyboard())
        return

    status = await message.answer("🧠 <b>Тренер анализирует матч и считает тайминги...</b>", parse_mode="HTML")

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    except Exception:
        pass

    prompt = f"{SYSTEM_PROMPT}\n\nЗАПРОС ИГРОКА:\n{query_text}"

    try:
        # Увеличиваем лимит токенов до 3500, чтобы билд и скиллбилд 1-6 уровней гарантированно помещались полностью
        result_text = await generate_with_gemini(prompt, max_tokens=3500)

        # Сохраняем в кэш
        save_to_cache(query_text, result_text)

        await state.set_state(BuildDialog.build_ready)
        await state.update_data(last_build=result_text, last_query=context_info)

        # Сначала отправляем билд игроку
        await send_safe_message(message=message, text=result_text, reply_markup=get_build_result_keyboard())

        # Затем удаляем временное статусное сообщение
        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logging.error(f"Ошибка в execute_build_analysis: {e}")
        try:
            await status.edit_text(
                f"⚠️ Не удалось сформировать билд: {e}\n\n"
                "Пожалуйста, нажми кнопку ниже и повтори запрос через 10 секунд.",
                reply_markup=get_start_keyboard()
            )
        except Exception:
            await message.answer(
                f"⚠️ Не удалось сформировать билд: {e}\n\n"
                "Пожалуйста, нажми кнопку ниже и повтори запрос через 10 секунд.",
                reply_markup=get_start_keyboard()
            )


# ================= ИНТЕРАКТИВНЫЕ ДЕЙСТВИЯ ПОСЛЕ БИЛДА =================

@dp.callback_query(F.data == "quick_checklist")
async def quick_checklist_callback(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer("Генерирую чек-лист...")
    data = await state.get_data()
    last_build = data.get("last_build", "")
    last_query = data.get("last_query", "")

    if not last_build:
        await callback.message.answer("⚠️ Данные предыдущего билда не найдены. Создай новый билд!", reply_markup=get_start_keyboard())
        return

    # Проверяем кэш для чек-листа
    cache_key = f"checklist_{last_query}"
    cached = get_from_cache(cache_key)
    if cached:
        await send_safe_message(callback.message, cached, reply_markup=get_build_result_keyboard())
        return

    status = await callback.message.answer("⚡ <b>Формирую компактную шпаргалку для игры...</b>", parse_mode="HTML")

    prompt = (
        f"Матч: {last_query}\n\n"
        f"Билд: {last_build}\n\n"
        "Составь СУПЕР-КОМПАКТНЫЙ ЧЕК-ЛИСТ ПОКУПОК ДЛЯ ИГРЫ СТРОГО НА РУССКОМ ЯЗЫКЕ (до 8 строк).\n"
        "Формат:\n"
        "📋 <b>ЧЕК-ЛИСТ ДЛЯ ИГРЫ:</b>\n"
        "• 🌿 Старт: [список айтемов] (сумма/600)\n"
        "• 🪄 Донос: [1-2 предмета]\n"
        "• ⚔️ 1-й слот: [Название] ([Тайминг])\n"
        "• ⚔️ 2-й слот: [Название] ([Тайминг])\n"
        "• ⚔️ 3-й слот: [Название] ([Тайминг])\n"
        "• 👑 Лейт: [3 ключевых предмета]\n"
        "• 🎯 Топ-контрпик: [предмет против кого]"
    )

    try:
        summary = await generate_with_gemini(prompt, max_tokens=1000)
        save_to_cache(cache_key, summary)
        await send_safe_message(callback.message, summary, reply_markup=get_build_result_keyboard())
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        try:
            await status.edit_text(f"⚠️ Ошибка: {e}", reply_markup=get_build_result_keyboard())
        except Exception:
            await callback.message.answer(f"⚠️ Ошибка: {e}", reply_markup=get_build_result_keyboard())


@dp.callback_query(F.data == "gameplay_tips")
async def gameplay_tips_callback(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer("Готовлю советы по игре...")
    data = await state.get_data()
    last_build = data.get("last_build", "")
    last_query = data.get("last_query", "")

    if not last_build:
        await callback.message.answer("⚠️ Данные билда устарели. Начни новый подбор!", reply_markup=get_start_keyboard())
        return

    cache_key = f"tips_{last_query}"
    cached = get_from_cache(cache_key)
    if cached:
        await send_safe_message(callback.message, cached, reply_markup=get_build_result_keyboard())
        return

    status = await callback.message.answer("⚔️ <b>Анализирую тактику и позиционку...</b>", parse_mode="HTML")

    prompt = (
        f"Матч: {last_query}\n\n"
        f"Билд: {last_build}\n\n"
        "Дай 3 ЧЕТКИХ, ПРАКТИЧЕСКИХ СОВЕТА ПО ГЕЙМПЛЕЮ НА ЭТОТ МАТЧ СТРОГО НА РУССКОМ ЯЗЫКЕ (кратко и емко):\n"
        "1. 🛡️ <b>План на лайн:</b> как вести себя на линии против этих врагов (агрессировать, добивать под вышкой, пушить).\n"
        "2. 🗺️ <b>Мидгейм-макро:</b> где фармить / с кем выходить в смоки после первых слотов.\n"
        "3. 🎯 <b>Фокус в драке:</b> кого из вражеской команды убивать ПЕРВЫМ и каких способностей опасаться."
    )

    try:
        tips = await generate_with_gemini(prompt, max_tokens=1000)
        save_to_cache(cache_key, tips)
        await send_safe_message(callback.message, tips, reply_markup=get_build_result_keyboard())
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        try:
            await status.edit_text(f"⚠️ Ошибка: {e}", reply_markup=get_build_result_keyboard())
        except Exception:
            await callback.message.answer(f"⚠️ Ошибка: {e}", reply_markup=get_build_result_keyboard())


# ================= ОБРАБОТКА СКРИНШОТОВ И ФОТО =================

@dp.message(F.photo)
async def photo_draft_handler(message: types.Message, state: FSMContext, bot: Bot):
    status = await message.answer("👁️ <b>Разглядываю скриншот...</b>", parse_mode="HTML")

    try:
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo, destination=file_io)
        image_bytes = file_io.getvalue()

        caption_text = message.caption or ""

        check_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"ПОЛЬЗОВАТЕЛЬ ПРИСЛАЛ ИЗОБРАЖЕНИЕ ИЗ DOTA 2.\n"
            f"Подпись к фото: '{caption_text}'.\n\n"
            "ИНСТРУКЦИЯ ПО АНАЛИЗУ:\n"
            "1. Определи: это изображение ТОЛЬКО ОДНОГО героя (портрет, иконка, моделька) "
            "ИЛИ это ПОЛНЫЙ ЭКРАН ДРАФТА (где видны обе команды или несколько врагов)?\n\n"
            "ЕСЛИ ЭТО ТОЛЬКО ОДИН ГЕРОЙ:\n"
            "Напиши строго в первой строчке:\n"
            "РЕЖИМ: ОДИН_ГЕРОЙ\n"
            "ГЕРОЙ: [Название распознанного героя]\n\n"
            "ЕСЛИ ЭТО ПОЛНЫЙ ЭКРАН ДРАФТА:\n"
            "Напиши в первой строчке:\n"
            "РЕЖИМ: ПОЛНЫЙ_ДРАФТ\n"
            "А далее сразу составь полный структурированный билд по заданному формату."
        )

        contents = [
            g_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            check_prompt
        ]

        result_text = await generate_with_gemini(contents, max_tokens=3500)

        if "РЕЖИМ: ОДИН_ГЕРОЙ" in result_text:
            hero_name = "Твой герой"
            for line in result_text.splitlines():
                if "ГЕРОЙ:" in line:
                    hero_name = line.replace("ГЕРОЙ:", "").strip()
                    break

            await state.update_data(hero=hero_name)
            await message.answer(
                f"👁️ Я узнал героя на фото: это <b>{hero_name}</b>! 🎮\n\n"
                "👉 <b>Шаг 2:</b> На какой позиции ты играешь? Выбери кнопкой ниже:",
                reply_markup=get_position_keyboard(),
                parse_mode="HTML"
            )
            await state.set_state(BuildDialog.waiting_for_position)
        else:
            clean_text = result_text.replace("РЕЖИМ: ПОЛНЫЙ_ДРАФТ", "").strip()
            await state.set_state(BuildDialog.build_ready)
            await state.update_data(last_build=clean_text, last_query="Анализ по скриншоту драфта")
            await send_safe_message(message, clean_text, reply_markup=get_build_result_keyboard())

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logging.error(f"Ошибка распознавания фото: {e}")
        try:
            await status.edit_text(f"⚠️ Ошибка обработки фото: {e}", reply_markup=get_start_keyboard())
        except Exception:
            await message.answer(f"⚠️ Ошибка обработки фото: {e}", reply_markup=get_start_keyboard())


# ================= ВЕБ-СЕРВЕР ДЛЯ ХОСТИНГА 24/7 =================
async def ping_handler(request: web.Request):
    return web.Response(text="Bot is running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", ping_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")


# ================= ТОЧКА ВХОДА =================
async def main():
    global bot

    if not BOT_TOKEN:
        print("❌ ОШИБКА: BOT_TOKEN пустой! Задай токен в файле .env")
        return

    bot = Bot(token=BOT_TOKEN)

    if "PORT" in os.environ:
        await start_web_server()

    print("\n=======================================================")
    print("⏳ Проверяю физическую связь с серверами Telegram...")
    try:
        user = await bot.get_me()
        print(f"✅ УСПЕШНО! Подключен бот: @{user.username} (ID: {user.id})")

        # Регистрируем команды в официальном меню Telegram (кнопка "Меню" в левом нижнем углу)
        await bot.set_my_commands([
            types.BotCommand(command="start", description="🚀 Главное меню"),
            types.BotCommand(command="fast", description="⚡ Быстрый ввод в 1 строчку"),
            types.BotCommand(command="cancel", description="❌ Отменить текущее действие"),
        ])

        print(">>> Очищаю старую очередь сообщений...")
        await bot.delete_webhook(drop_pending_updates=True)
        print(">>> Бот готов и слушает сообщения! <<<")
        print("=======================================================\n")
    except Exception as e:
        print(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ К TELEGRAM: {e}")
        print("Проверь токен или подключение к api.telegram.org.")
        print("=======================================================\n")
        return

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен.")