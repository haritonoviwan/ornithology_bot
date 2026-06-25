import os
import json
import logging
import asyncio
import re
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, LinkPreviewOptions, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from pydub import AudioSegment
import uvicorn
from fastapi import FastAPI
import io

BOT_TOKEN = os.getenv("BOT_TOKEN")
HF_URL = os.getenv("HF_URL")

USER_LOCATIONS = {
    347493302: {"lat": 55.71, "lng": 37.87},
    1108794476: {"lat": 53.01, "lng": 50.15},
    1200611413: {"lat": 55.53, "lng": 37.46},
    5227786902: {"lat": 53.01, "lng": 50.15},
    1946978013: {"lat": 53.01, "lng": 50.15},
}

AUDIO_CACHE = {}
MORPH_CACHE = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app = FastAPI()

TAXONOMY_PATH = "taxonomy.json"
TAXONOMY = {}

if os.path.exists(TAXONOMY_PATH):
    try:
        with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
            TAXONOMY = json.load(f)
        logging.info(f"✅ Успешно загружено {len(TAXONOMY)} таксонов для генерации ссылок.")
    except Exception as e:
        logging.error(f"❌ Ошибка при чтении файла {TAXONOMY_PATH}: {e}")
else:
    logging.warning(f"⚠️ Файл {TAXONOMY_PATH} не найден рядом с bot.py. Ссылки генерироваться не будут.")

async def keep_hf_alive():
    if not HF_URL:
        logging.warning("⚠️ Переменная HF_URL не настроена. Пингер выключен.")
        return
        
    while True:
        await asyncio.sleep(3600)  # Раз в час вполне ок
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(HF_URL, timeout=15) as resp:
                    logging.info(f"Пинг HF Space. Статус: {resp.status}")
        except Exception as e:
            logging.error(f"Не удалось пингануть HF Space: {e}")

def make_bird_html_link(display_name: str) -> str:
    match = re.match(r"(.+?)\s*\((.+?)\)", display_name)
    if match:
        ru_name = match.group(1).strip()
        latin_name = match.group(2).strip()
        bird_info = TAXONOMY.get(latin_name)
        if bird_info and "ebird_code" in bird_info:
            url = f"https://ebird.org/species/{bird_info['ebird_code']}?siteLanguage=ru"
            return f'<a href="{url}">{ru_name}</a> ({latin_name})'
        return display_name
    else:
        latin_name = display_name.strip()
        bird_info = TAXONOMY.get(latin_name)
        if bird_info and "ebird_code" in bird_info:
            url = f"https://ebird.org/species/{bird_info['ebird_code']}?siteLanguage=ru"
            return f'<a href="{url}">{latin_name}</a>'
        return display_name

@app.get("/")
def read_root():
    return {"status": "bot_alive"}

def get_user_geo(user_id: int):
    return USER_LOCATIONS.get(user_id, {"lat": 55.53, "lng": 37.46})

@dp.message(Command("start"))
async def cmd_start(message: Message):
    # Нижняя клавиатура с кнопкой мастера
    main_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🪶 Определитель")]],
        resize_keyboard=True
    )
    await message.answer(
        "🕊️ Привет! Я бот-орнитолог\n\n"
        "📸 Отправь мне фото - я найду и распознаю птиц\n"
        "🎶 Отправь аудио или видео - я определю птиц по пению\n"
        "🪶 Нет ни фото, ни звука? Нажми кнопку внизу, ответь на пару вопросов, и я помогу её определить\n\n"
        "🌍 Чтобы точность была выше, отправь мне свою геопозицию",
        reply_markup=main_kb
    )

@dp.message(F.content_type == ContentType.LOCATION)
async def handle_location(message: Message):
    user_id = message.from_user.id
    lat = message.location.latitude
    lng = message.location.longitude
    USER_LOCATIONS[user_id] = {"lat": lat, "lng": lng}
    await message.answer(f"📍 Локация сохранена\n Текущие координаты: {lat:.2f}, {lng:.2f}")

@dp.message(F.photo)
async def handle_photo(message: Message):
    waiting_msg = await message.reply("📸 Обрабатываю изображение, секунду...")
    
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    
    geo = get_user_geo(message.from_user.id)
    
    data = aiohttp.FormData()
    data.add_field('image', file_bytes.read(), filename='photo.jpg', content_type='image/jpeg')
    data.add_field('lat', str(geo['lat']))
    data.add_field('lng', str(geo['lng']))
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{HF_URL}/classify", data=data, timeout=60) as resp:
                if resp.status != 200:
                    await waiting_msg.edit_text("❌ Ошибка сервера классификации птиц")
                    return
                result = await resp.json()
        except Exception as e:
            logging.error(f"Ошибка связи с HF (фото): {e}")
            await waiting_msg.edit_text("⏳ Сервер нейросетей спал и сейчас просыпается. Пожалуйста, повтори отправку через пару минут")
            return

    if result.get('status') == 'loading':
        await waiting_msg.edit_text("⏳ Модели на сервере сейчас просыпаются и подгружаются. Попробуй еще раз через пару минут")
        return

    predictions = result.get('predictions', [])
    if not predictions:
        await waiting_msg.edit_text("🤔 Птиц на фото не обнаружено или я не смог их рассмотреть")
        return

    total_birds_count = 0
    response_text = "📸 Заметил:\n"
    
    for i, pred in enumerate(predictions):
        cands = pred.get('candidates', [])
        if not cands:
            continue
            
        total_birds_count += len(cands)
        if len(cands) == 1:
            bird_html = make_bird_html_link(cands[0]['name'])
            line = f"{i+1}. {bird_html} — {cands[0]['score']:.1%}"
        else:
            bird_html1 = make_bird_html_link(cands[0]['name'])
            bird_html2 = make_bird_html_link(cands[1]['name'])
            line = f"{i+1}. {bird_html1} — {cands[0]['score']:.1%} или {bird_html2} — {cands[1]['score']:.1%}"
        
        response_text += line + "\n"
        
    if total_birds_count == 1:
        photo_preview = LinkPreviewOptions(is_disabled=False, prefer_small_media=True)
    else:
        photo_preview = LinkPreviewOptions(is_disabled=True)
        
    await waiting_msg.edit_text(response_text, parse_mode="HTML", link_preview_options=photo_preview)

async def process_audio_bytes(audio_bytes: bytes, filename: str, message: Message, waiting_msg: Message):
    geo = get_user_geo(message.from_user.id)
    
    data = aiohttp.FormData()
    data.add_field('audio', audio_bytes, filename=filename)
    data.add_field('lat', str(geo['lat']))
    data.add_field('lng', str(geo['lng']))
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{HF_URL}/analyze-audio", data=data, timeout=60) as resp:
                if resp.status != 200:
                    await waiting_msg.edit_text("❌ Ошибка сервера при анализе звука")
                    return
                result = await resp.json()
        except Exception as e:
            logging.error(f"Ошибка связи с HF (аудио): {e}")
            await waiting_msg.edit_text("⏳ Сервер нейросетей спал и сейчас просыпается. Пожалуйста, повтори отправку через пару минут")
            return

    if result.get('status') == 'loading':
        await waiting_msg.edit_text("⏳ Акустические модели подгружаются. Повтори запрос через минуту")
        return

    detections = result.get('detections', [])
    if not detections:
        await waiting_msg.edit_text("😔 Голоса знакомых птиц на записи не обнаружены")
        return

    audio_summary = {}
    for det in detections:
        bird_name = det['name']
        confidence = det['confidence']
        if bird_name not in audio_summary or confidence > audio_summary[bird_name]:
            audio_summary[bird_name] = confidence

    sorted_birds = sorted(audio_summary.items(), key=lambda x: x[1], reverse=True)
    
    response_text = "🎧 Услышал:\n"
    for i, (bird_name, confidence) in enumerate(sorted_birds):
        bird_html = make_bird_html_link(bird_name)
        response_text += f"{i+1}. {bird_html} — {confidence:.1%}\n"

    detailed_text = "⏳ Подробный таймлайн:\n\n"
    for i, det in enumerate(detections):
        bird_html = make_bird_html_link(det['name'])
        detailed_text += f"{i+1}. {bird_html} ({det['start']:.1f}с - {det['end']:.1f}с) — {det['confidence']:.1%}\n"

    cache_key = f"{message.chat.id}_{waiting_msg.message_id}"
    AUDIO_CACHE[cache_key] = detailed_text

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ Кто когда пел?", callback_data=f"audio_details:{cache_key}")]
    ])

    if len(sorted_birds) == 1:
        audio_preview = LinkPreviewOptions(is_disabled=False, prefer_small_media=True)
    else:
        audio_preview = LinkPreviewOptions(is_disabled=True)

    await waiting_msg.edit_text(response_text, parse_mode="HTML", reply_markup=keyboard, link_preview_options=audio_preview)

@dp.callback_query(F.data.startswith("audio_details:"))
async def handle_audio_details(callback: CallbackQuery):
    cache_key = callback.data.split(":")[1]
    detailed_text = AUDIO_CACHE.get(cache_key)
    if detailed_text:
        await callback.message.edit_text(detailed_text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        await callback.answer("⚠️ Данные таймлайна устарели или бот был перезапущен", show_alert=True)

@dp.callback_query(F.data.startswith("more_birds:"))
async def handle_more_birds(callback: CallbackQuery):
    # Разбираем callback_data (формат: more_birds:cache_key:offset)
    _, cache_key, offset_str = callback.data.split(":")
    offset = int(offset_str)
    
    # Достаем данные из кэша
    cached = MORPH_CACHE.get(cache_key)
    if not cached:
        await callback.answer("⚠️ Данные устарели или бот был перезапущен. Повторите поиск.", show_alert=True)
        return
        
    predictions = cached["predictions"]
    base_text = cached["base_text"]
    
    # Вычисляем, сколько птиц показать теперь (текущий offset + следующие 5)
    next_offset = offset + 5
    visible_predictions = predictions[:next_offset]
    
    response_text = base_text + "🎯 <b>Возможные кандидаты (Режим отладки):</b>\n\n"
    
    # Отрисовываем обновленный расширенный список
    for i, pred in enumerate(visible_predictions):
        bird_html = make_bird_html_link(pred['name'])
        geo_score = pred.get('geo_score', 0.0)
        morph_score = pred.get('morph_score', 0.0)
        final_rank = pred.get('final_rank', 0.0)
        
        debug_info = f"<code>[G:{geo_score:.3f} * M:{morph_score:.2f} = {final_rank:.4f}]</code>"
        response_text += f"{i+1}. {bird_html}\n└ {debug_info}\n\n"
        
    # Проверяем, остались ли еще скрытые птицы в запасе
    keyboard = None
    if len(predictions) > next_offset:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ещё варианты 🔄", callback_data=f"more_birds:{cache_key}:{next_offset}")]
        ])
        
    # Редактируем сообщение: текст увеличится, а кнопка либо обновит offset, либо исчезнет
    await callback.message.edit_text(response_text, parse_mode="HTML", reply_markup=keyboard, link_preview_options=LinkPreviewOptions(is_disabled=True))
    await callback.answer()

@dp.message(F.voice | F.audio | F.document)
async def handle_audio(message: Message):
    if message.document:
        filename = message.document.file_name or ""
        ext = filename.split('.')[-1].lower()
        if ext not in ['aac', 'mp3', 'wav', 'm4a', 'flac', 'ogg', 'amr']:
            return  
        audio_obj = message.document
    else:
        audio_obj = message.voice if message.voice else message.audio
        ext = "ogg" if message.voice else (audio_obj.file_name or "mp3").split('.')[-1].lower()

    waiting_msg = await message.reply("🎵 Принял аудио, готовлю к анализу...")
    
    file_info = await bot.get_file(audio_obj.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    raw_data = file_bytes.read()

    if ext not in ['mp3', 'wav']:
        try:
            await waiting_msg.edit_text("⏳ Оптимизирую аудиоформат...")
            # Выносим тяжелый синхронный pydub в отдельный поток
            audio_segment = await asyncio.to_thread(
                AudioSegment.from_file, io.BytesIO(raw_data), format=ext
            )
            mp3_buffer = io.BytesIO()
            await asyncio.to_thread(audio_segment.export, mp3_buffer, format="mp3")
            mp3_bytes = mp3_buffer.getvalue()
            
            filename_to_send = "track.mp3"
            data_to_send = mp3_bytes
        except Exception as e:
            logging.error(f"Ошибка локальной конвертации: {e}")
            filename_to_send = f"track.{ext}"
            data_to_send = raw_data
    else:
        filename_to_send = f"track.{ext}"
        data_to_send = raw_data

    await waiting_msg.edit_text("🎵 Распознаю голоса птиц...")
    await process_audio_bytes(data_to_send, filename_to_send, message, waiting_msg)

@dp.message(F.video | F.video_note)
async def handle_video(message: Message):
    waiting_msg = await message.reply("🎬 Слушаю звук из видео...")
    
    video_obj = message.video if message.video else message.video_note
    file_info = await bot.get_file(video_obj.file_id)
    video_bytes = await bot.download_file(file_info.file_path)
    
    video_ext = file_info.file_path.split('.')[-1]
    temp_video_name = f"temp_vid.{video_ext}"
    temp_audio_name = "temp_aud.mp3"
    
    with open(temp_video_name, "wb") as f:
        f.write(video_bytes.read())
        
    try:
        # Выносим pydub из видео тоже в отдельный поток
        audio_track = await asyncio.to_thread(AudioSegment.from_file, temp_video_name)
        await asyncio.to_thread(audio_track.export, temp_audio_name, format="mp3")
        
        with open(temp_audio_name, "rb") as f:
            mp3_bytes = f.read()
            
        await waiting_msg.edit_text("🎵 Распознаю голоса...")
        await process_audio_bytes(mp3_bytes, "track.mp3", message, waiting_msg)
        
    except Exception as e:
        await waiting_msg.edit_text(f"❌ Ошибка конвертации видео: {e}")
    finally:
        if os.path.exists(temp_video_name): os.remove(temp_video_name)
        if os.path.exists(temp_audio_name): os.remove(temp_audio_name)

# === СОСТОЯНИЯ ДЛЯ НАШЕГО МАСТЕРА ===
class MorphSearchState(StatesGroup):
    choosing_size = State()
    choosing_colors = State()
    choosing_habitat = State()

# === СЛОВАРИ ПЕРЕВОДОВ ===
SIZE_MAP = {
    1: "С воробья или меньше",
    2: "Между воробьем и дроздом",
    3: "С дрозда",
    4: "Между дроздом и вороной",
    5: "С ворону",
    6: "Между вороной и гусем",
    7: "С гуся или крупнее"
}

COLOR_MAP = {
    "black": "Черная", "grey": "Серая", "white": "Белая", 
    "brown_beige": "Коричневая/Бежевая", "red_rufous": "Красная", 
    "yellow": "Желтая", "green_olive": "Зеленая/Оливковая", 
    "blue_cyan": "Синяя/Голубая", "orange": "Оранжевая"
}

HABITAT_MAP = {
    "feeder": "На кормушке", "water": "В воде", "ground": "На земле", 
    "trees_bushes": "В деревьях/кустах", "fence_wire": "На заборе/проводе", "air": "В воздухе"
}

def get_size_keyboard():
    buttons = []
    for num, text in SIZE_MAP.items():
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"set_size:{num}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_colors_keyboard(selected_colors: list):
    buttons = []
    row = []
    for eng_name, ru_name in COLOR_MAP.items():
        # Если цвет уже выбран пользователем, помечаем его галочкой
        mark = "✅ " if eng_name in selected_colors else ""
        row.append(InlineKeyboardButton(text=f"{mark}{ru_name}", callback_data=f"toggle_color:{eng_name}"))
        if len(row) == 2:  # Делаем сетку 2 кнопки в ряд
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    # Добавляем финальную кнопку подтверждения в самый низ
    buttons.append([InlineKeyboardButton(text="Далее ➡️", callback_data="colors_done")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_habitat_keyboard():
    buttons = []
    for eng_name, ru_name in HABITAT_MAP.items():
        buttons.append([InlineKeyboardButton(text=ru_name, callback_data=f"set_habitat:{eng_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Старт опроса при нажатии на кнопку
@dp.message(F.text == "🪶 Определитель")
async def start_morph_search(message: Message, state: FSMContext):
    await state.clear()  # Обнуляем старые сессии опроса
    await state.set_state(MorphSearchState.choosing_size)
    
    await message.answer(
        "🪶 Поиск по описанию\n\n"
        "Ответь на пару вопросов на основе своих наблюдений\n\n"
        "👉 <b>Шаг 1 из 3: Какого размера была птица?</b>",
        parse_mode="HTML",
        reply_markup=get_size_keyboard()
    )

# Обработка выбора размера -> Переход к выбору цвета
@dp.callback_query(F.data.startswith("set_size:"), MorphSearchState.choosing_size)
async def handle_size_choice(callback: CallbackQuery, state: FSMContext):
    size_num = int(callback.data.split(":")[1])
    await state.update_data(size=size_num, colors=[]) # Инициализируем пустой список цветов
    await state.set_state(MorphSearchState.choosing_colors)
    text = (
        f"🪶 Поиск по описанию\n\n"
        f"📏 Размер: {SIZE_MAP[size_num]}\n\n"
        f"👉 <b>Шаг 2 из 3: Какого цвета она была преимущественно?</b>\n"
        f"<i>(Можно выбрать до 4 вариантов)</i>"
    )
    # Изменяем сообщение на месте
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_colors_keyboard([]))
    await callback.answer()

# (Интерактивный) Переключение галочек цветов
@dp.callback_query(F.data.startswith("toggle_color:"), MorphSearchState.choosing_colors)
async def handle_color_toggle(callback: CallbackQuery, state: FSMContext):
    color_clicked = callback.data.split(":")[1]
    data = await state.get_data()
    current_colors = data.get("colors", [])
    
    if color_clicked in current_colors:
        current_colors.remove(color_clicked)
    else:
        # Лимит: если уже выбрано 4 цвета, а юзер жмет на 5-й
        if len(current_colors) >= 4:
            await callback.answer(
                text="⚠️ Можно выбрать не более 4 цветов!", 
                show_alert=True  # Всплывающее окошко по центру экрана
            )
            return  # Прерываем выполнение, ничего не добавляя и не перерисовывая
        current_colors.append(color_clicked)
    await state.update_data(colors=current_colors)
    
    # Обновляем разметку кнопок (галочки)
    await callback.message.edit_reply_markup(reply_markup=get_colors_keyboard(current_colors))
    await callback.answer()

# Завершение выбора цвета -> Переход к биотопу
@dp.callback_query(F.data == "colors_done", MorphSearchState.choosing_colors)
async def handle_colors_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    chosen_colors = data.get("colors", [])
    
    # Переводим на русский для красивого отображения в "бланке"
    colors_ru = ", ".join([COLOR_MAP[c] for c in chosen_colors]) if chosen_colors else "Не выбрано"
    await state.update_data(colors_ru_text=colors_ru)
    await state.set_state(MorphSearchState.choosing_habitat)
    
    text = (
        f"🪶 Поиск по описанию\n\n"
        f"📏 Размер: {SIZE_MAP[data['size']]}\n"
        f"🎨 Цвета: {colors_ru}\n\n"
        f"👉 <b>Шаг 3 из 3: Где была птица?</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_habitat_keyboard())
    await callback.answer()

# Сбор данных и отправка запроса на HF
@dp.callback_query(F.data.startswith("set_habitat:"), MorphSearchState.choosing_habitat)
async def handle_habitat_and_search(callback: CallbackQuery, state: FSMContext):
    habitat_key = callback.data.split(":")[1]
    data = await state.get_data()
    # Вытаскиваем геопозицию из словаря USER_LOCATIONS
    geo = get_user_geo(callback.from_user.id)
    # Обновляем текст, показывая, что пошел поиск
    text_loading = (
        f"🪶 Поиск по описанию\n\n"
        f"📏 Размер: {SIZE_MAP[data['size']]}\n"
        f"🎨 Цвет: {data['colors_ru_text']}\n"
        f"🏡 Место: {HABITAT_MAP[habitat_key]}\n\n"
        f"🔍 <i>Сверяюсь с орнитологической базой, секунду...</i>"
    )
    await callback.message.edit_text(text_loading, parse_mode="HTML", reply_markup=None)
    await callback.answer()
    # Формируем Payload для нашего нового эндпоинта на HF
    form_data = aiohttp.FormData()
    form_data.add_field('lat', str(geo['lat']))
    form_data.add_field('lng', str(geo['lng']))
    form_data.add_field('size', str(data['size']))
    form_data.add_field('colors', json.dumps(data['colors']))
    form_data.add_field('habitat', habitat_key)
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{HF_URL}/search-by-morphology", data=form_data, timeout=30) as resp:
                if resp.status != 200:
                    await callback.message.edit_text("❌ Ошибка сервера классификации при поиске по признакам.")
                    await state.clear()
                    return
                result = await resp.json()
        except Exception as e:
            logging.error(f"Ошибка связи с HF (морфология): {e}")
            await callback.message.edit_text("⏳ Сервер нейросетей ушел на перезагрузку. Попробуйте повторить операцию через пару минут")
            await state.clear()
            return
    if result.get('status') == 'loading':
        await callback.message.edit_text("⏳ Модели на сервере подгружаются. Попробуйте еще раз через пару минут")
        await state.clear()
        return
    predictions = result.get('predictions', [])
    
    # Базовая шапка сообщения (сохраняем её отдельно в кэш, чтобы потом перерисовывать)
    base_text = (
        f"🐦‍⬛ <b>Поиск по описанию</b>\n\n"
        f"📏 Размер: {SIZE_MAP[data['size']]}\n"
        f"🎨 Цвет: {data['colors_ru_text']}\n"
        f"🏡 Место: {HABITAT_MAP[habitat_key]}\n\n"
    )
    
    if not predictions:
        response_text = base_text + (
            f"😔 К сожалению, не могу найти птиц со схожими параметрами в этом регионе\n"
            f"<i>Попробуй немного расширить критерии (указать смежный размер или убрать редкий цвет)</i>"
        )
        await callback.message.edit_text(response_text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        await state.clear()
        return

    # Если птицы есть, собираем уникальный ключ для этого сообщения
    cache_key = f"{callback.message.chat.id}_{callback.message.message_id}"
    
    # Сохраняем в кэш шапку и ВСЕХ найденных птиц (все 15 штук от бэкенда)
    MORPH_CACHE[cache_key] = {
        "base_text": base_text,
        "predictions": predictions
    }
    
    # Формируем текст для первых 5 кандидатов
    response_text = base_text + "🎯 <b>Возможные кандидаты (Режим отладки):</b>\n\n"
    visible_predictions = predictions[:5]
    
    for i, pred in enumerate(visible_predictions):
        bird_html = make_bird_html_link(pred['name'])
        geo_score = pred.get('geo_score', 0.0)
        morph_score = pred.get('morph_score', 0.0)
        final_rank = pred.get('final_rank', 0.0)
        
        debug_info = f"<code>[G:{geo_score:.3f} * M:{morph_score:.2f} = {final_rank:.4f}]</code>"
        response_text += f"{i+1}. {bird_html}\n└ {debug_info}\n\n"
        
    # Если птиц всего больше 5, прикрепляем кнопку. Передаем в неё ключ кэша и offset=5
    keyboard = None
    if len(predictions) > 5:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ещё варианты 🔄", callback_data=f"more_birds:{cache_key}:5")]
        ])
            
    # Обновляем сообщение первой порцией данных
    await callback.message.edit_text(response_text, parse_mode="HTML", reply_markup=keyboard, link_preview_options=LinkPreviewOptions(is_disabled=True))
    await state.clear() # Очищаем состояние опроса, оно больше не нужно

async def run_bot():
    await dp.start_polling(bot)

async def main():
    # Динамический порт из окружения Render
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    
    asyncio.create_task(keep_hf_alive())
    
    await asyncio.gather(
        server.serve(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
