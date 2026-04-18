import os
import io
import json
import base64
import logging
import requests
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- НАСТРОЙКА ЛОГИРОВАНИЯ ----------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==============================================================================
#                         ДАННЫЕ ДЛЯ YANDEX CLOUD
# ==============================================================================
FOLDER_ID = "b1g6irlklro22jcs1i2c"
API_KEY = "AQVNzuXu-feyxUlpOzTXEAL1U7lB_h7lwDjhh4kQ"
# ==============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

# Эндпоинт для мультимодальной модели YandexGPT Vision
VISION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
VISION_MODEL_URI = f"gpt://{FOLDER_ID}/yandexgpt/vision"

TARGET_WIDTH = 984
TARGET_HEIGHT = 570
MAX_FILE_SIZE_MB = 5
MAX_CHARS_XS_S = 45
MAX_CHARS_TITLE_M_L = 30
MAX_CHARS_SUBTITLE_M_L = 55

def analyze_banner_with_vision(pil_image: Image.Image) -> dict | None:
    """
    Отправляет изображение в YandexGPT Vision и получает готовый вердикт.
    """
    # Кодируем изображение в base64
    buffered = io.BytesIO()
    pil_image.save(buffered, format="JPEG", quality=90)
    img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

    system_prompt = f"""
Ты — эксперт по проверке баннеров для приложения Пятёрочки.
Проанализируй предоставленное изображение баннера и проверь его на соответствие гайдлайнам.

ГАЙДЛАЙНЫ:
1. **Размер**: должен быть ровно {TARGET_WIDTH}x{TARGET_HEIGHT} пикселей.
2. **Текстовый блок**: должен занимать не более 52% площади баннера.
3. **Цвет текста**: только #302E33 (на светлом фоне) или #FFFFFF (на тёмном фоне).
4. **Фон**: не должен быть чёрным, белым, кислотным, пастельным или текстурным.
5. **Логотип Пятёрочки**: запрещён на баннере.
6. **Текстовые правила**:
   - Обращение на "Вы".
   - Конкретное предложение с очевидной пользой, без абстрактных слов.
   - Использование буквы "ё".
   - Кавычки-ёлочки «».
   - Отсутствие капса.
   - Не более одного восклицательного знака.
   - Для XS/S баннеров: до {MAX_CHARS_XS_S} символов.
   - Для M/L баннеров: заголовок до {MAX_CHARS_TITLE_M_L} символов, подзаголовок до {MAX_CHARS_SUBTITLE_M_L} символов.
7. **Имидж**: не должен содержать оружия, мрачных готических образов, антропоморфизма, стоковых клише.

Верни ответ строго в формате JSON:
{{
  "verdict": "ok" или "error",
  "issues": ["список", "конкретных", "нарушений"],
  "recommendations": "краткая рекомендация по исправлению"
}}
"""

    payload = {
        "modelUri": VISION_MODEL_URI,
        "completionOptions": {
            "stream": False,
            "temperature": 0.1,
            "maxTokens": 1500
        },
        "messages": [
            {
                "role": "system",
                "text": system_prompt
            },
            {
                "role": "user",
                "text": "Проверь этот баннер по гайдлайнам Пятёрочки.",
                "attachments": [
                    {
                        "content_type": "image/jpeg",
                        "content": img_base64
                    }
                ]
            }
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {API_KEY}",
        "x-folder-id": FOLDER_ID
    }

    try:
        logger.info("Отправка изображения в YandexGPT Vision...")
        response = requests.post(VISION_URL, json=payload, headers=headers, timeout=60)
        response.raise_for_status()
        result = response.json()
        content = result["result"]["alternatives"][0]["message"]["text"]

        # Парсим JSON из ответа
        try:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start != -1 and end > start:
                return json.loads(content[start:end])
            else:
                return {"verdict": "error", "issues": ["Не удалось разобрать ответ модели"], "recommendations": content}
        except json.JSONDecodeError:
            return {"verdict": "error", "issues": ["Некорректный JSON в ответе"], "recommendations": content}
    except Exception as e:
        logger.error(f"YandexGPT Vision error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — умный агент проверки баннеров для Пятёрочки (YandexGPT Vision).\n\n"
        "📌 *ВАЖНО:* Отправляйте баннер *как документ (файл)*, "
        "а не как фото. Telegram сжимает фото, искажая размеры.\n\n"
        "Я проанализирую изображение с помощью ИИ и дам подробный отчёт.",
        parse_mode='Markdown'
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Вы отправили сжатое фото. Размеры могли измениться.\n"
        "🔍 Всё равно анализирую, но для точной проверки отправьте файл (как документ)."
    )
    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    await process_image(update, image_bytes, is_compressed=True)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.mime_type or not document.mime_type.startswith('image/'):
        await update.message.reply_text("❌ Пожалуйста, отправьте изображение.")
        return
    await update.message.reply_text("🔍 Анализирую оригинальный файл с помощью YandexGPT Vision...")
    file = await document.get_file()
    image_bytes = await file.download_as_bytearray()
    await process_image(update, image_bytes, is_compressed=False)

async def process_image(update: Update, image_bytes: bytes, is_compressed: bool):
    file_size_mb = len(image_bytes) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(f"❌ Размер файла {file_size_mb:.2f} МБ превышает лимит {MAX_FILE_SIZE_MB} МБ.")
        return

    try:
        img_pil = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось открыть изображение: {e}")
        return

    status_msg = await update.message.reply_text(
        "🤖 Анализирую изображение с помощью ИИ... (может занять ~15-20 сек)"
    )

    result = analyze_banner_with_vision(img_pil)

    if result is None:
        await status_msg.edit_text(
            "❌ Ошибка при обращении к ИИ. Проверьте настройки или повторите позже."
        )
        return

    verdict = result.get("verdict", "error")
    issues = result.get("issues", [])
    recommendations = result.get("recommendations", "")

    final_verdict = "✅ Баннер полностью соответствует гайдам!" if verdict == "ok" else "❌ Баннер имеет нарушения."
    lines = [
        f"*Результаты проверки (YandexGPT Vision):*",
        f"\n*Вердикт:* {final_verdict}",
    ]
    if is_compressed:
        lines.append("\n⚠️ *Внимание:* анализ по сжатому фото, результаты могут быть неточными.")
    if issues:
        lines.append("\n*Обнаруженные проблемы:*")
        for issue in issues:
            lines.append(f"• {issue}")
    if recommendations:
        lines.append(f"\n*Рекомендация:* {recommendations}")

    await status_msg.edit_text("\n".join(lines), parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    if not BOT_TOKEN:
        raise ValueError("Переменная окружения BOT_TOKEN не установлена!")
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    application.add_error_handler(error_handler)
    logger.info("Бот запущен с YandexGPT Vision (полный AI-анализ)...")
    
    application.run_polling(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()