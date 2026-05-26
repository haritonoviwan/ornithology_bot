import os
import json
import logging
import asyncio
import re
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, LinkPreviewOptions
from aiogram.filters import Command
from pydub import AudioSegment
import uvicorn
from fastapi import FastAPI

BOT_TOKEN = os.getenv("BOT_TOKEN")
HF_URL = os.getenv("HF_URL")

USER_LOCATIONS = {
    347493302: {"lat": 55.71, "lng": 37.87},
    1108794476:  {"lat": 53.01, "lng": 50.15},
    1200611413: {"lat": 55.53, "lng": 37.46},
    5227786902:  {"lat": 53.01, "lng": 50.15},
}

AUDIO_CACHE = {}

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

def make_bird_html_link(display_name: str) -> str:
    """
    Парсит display_name и оборачивает нужную часть в HTML-ссылку на eBird,
    если для этой птицы есть ebird_code.
    """
    match = re.match(r"(.+?)\s*\((.+?)\)", display_name)
    
    if match:
        ru_name = match.group(1).strip()
        latin_name = match.group(2).strip()
        
        bird_info = TAXONOMY.get(latin_name)
        if bird_info and "ebird_code" in bird_info:
            url = f"https://ebird.org/species/{bird_info['ebird_code']}"
            return f'<a href="{url}">{ru_name}</a> ({latin_name})'
        return display_name
    else:
        latin_name = display_name.strip()
        bird_info = TAXONOMY.get(latin_name)
        if bird_info and "ebird_code" in bird_info:
            url = f"https://ebird.org/species/{bird_info['ebird_code']}"
            return f'<a href="{url}">{latin_name}</a>'
        return display_name

@app.get("/")
def read_root():
    return {"status": "bot_alive"}

def get_user_geo(user_id: int):
    return USER_LOCATIONS.get(user_id, {"lat": 55.53, "lng": 37.46})

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🕊️ Привет! Я бот-орнитолог\n\n"
        
        "📸 Отправь мне фото - я найду и распознаю птиц\n"
        "🎶 Отправь голосовое, аудио или видео - я определю птиц по пению\n\n"
        
        "🌍 Чтобы точность была выше, отправь мне свою геопозицию"
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
            await waiting_msg.edit_text(f"❌ Не удалось связаться с сервером: {e}")
            return

    if result.get('status') == 'loading':
        await waiting_msg.edit_text("⏳ Модели на сервере сейчас просыпаются и подгружаются. Попробуй еще раз через минуту!")
        return

    predictions = result.get('predictions', [])
    if not predictions:
        await waiting_msg.edit_text("🤔 Птиц на фото не обнаружено или я не смог их рассмотреть")
        return

    # Считаем суммарное количество птиц (вариантов) во всем ответе нейросети
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
        
    # Настраиваем превью в зависимости от общего числа птиц
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
            await waiting_msg.edit_text(f"❌ Ошибка отправки аудио: {e}")
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

    detailed_text = "⏳ <b>Подробный таймлайн:</b>\n\n"
    for i, det in enumerate(detections):
        bird_html = make_bird_html_link(det['name'])
        detailed_text += f"{i+1}. <b>{bird_html}</b> ({det['start']:.1f}с - {det['end']:.1f}с) — {det['confidence']:.1%}\n"

    cache_key = f"{message.chat.id}_{waiting_msg.message_id}"
    AUDIO_CACHE[cache_key] = detailed_text

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ Кто когда пел?", callback_data=f"audio_details:{cache_key}")]
    ])

    # Настраиваем превью для аудио (смотрим на количество уникальных услышанных видов)
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
        # В развернутом таймлайне превью всегда выключено для чистоты
        await callback.message.edit_text(detailed_text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
    else:
        await callback.answer("⚠️ Данные таймлайна устарели или бот был перезапущен", show_alert=True)

@dp.message(F.voice | F.audio)
async def handle_audio(message: Message):
    waiting_msg = await message.reply("🎵 Слушаю аудио...")
    
    audio_obj = message.voice if message.voice else message.audio
    file_info = await bot.get_file(audio_obj.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    
    ext = file_info.file_path.split('.')[-1]
    await process_audio_bytes(file_bytes.read(), f"track.{ext}", message, waiting_msg)

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
        audio_track = AudioSegment.from_file(temp_video_name)
        audio_track.export(temp_audio_name, format="mp3")
        
        with open(temp_audio_name, "rb") as f:
            mp3_bytes = f.read()
            
        await waiting_msg.edit_text("🎵 Распознаю голоса...")
        await process_audio_bytes(mp3_bytes, "track.mp3", message, waiting_msg)
        
    except Exception as e:
        await waiting_msg.edit_text(f"❌ Ошибка конвертации видео: {e}")
    finally:
        if os.path.exists(temp_video_name): os.remove(temp_video_name)
        if os.path.exists(temp_audio_name): os.remove(temp_audio_name)

async def run_bot():
    await dp.start_polling(bot)

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    
    await asyncio.gather(
        server.serve(),
        run_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
