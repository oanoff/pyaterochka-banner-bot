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

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

# Настройки Yandex Cloud
FOLDER_ID = "ajeesnnfjttiol4miepk"
API_KEY = "AQVNzuXu-feyxUlpOzTXEAL1U7lB_h7lwDjhh4kQ"
VISION_OCR_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
YANDEXGPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEXGPT_MODEL = "gpt://{}/yandexgpt/latest".format(FOLDER_ID)

# Гайдлайны Пятёрочки
TARGET_WIDTH = 984
TARGET_HEIGHT = 570
MAX_FILE_SIZE_MB = 5

# ---------- ФУНКЦИЯ РАСПОЗНАВАНИЯ ТЕКСТА (Yandex Vision) ----------
def ocr_with_yandex_vision(pil_image: Image.Image) -> str:
    """Отправляет изображение в Yandex Vision OCR и возвращает распознанный текст."""
    if not API_KEY or not FOLDER_ID:
        logger.error("Yandex Cloud credentials are not set.")
        return ""

    # Сохраняем изображение в байтовый буфер
    img_byte_arr = io.BytesIO()
    pil_image.save(img_byte_arr, format='JPEG', quality=95)
    img_byte_arr = img_byte_arr.getvalue()
    encoded_image = base64.b64encode(img_byte_arr).decode('utf-8')

    # Формируем тело запроса согласно документации
    body = {
        "mimeType": "image/jpeg",
        "languageCodes": ["ru", "en"],
        "model": "page",
        "content": encoded_image
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {API_KEY}",
        "x-folder-id": FOLDER_ID,
        "x-data-logging-enabled": "false"
    }

    try:
        logger.info("Sending image to Yandex Vision OCR...")
        response = requests.post(VISION_OCR_URL, json=body, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        full_text = data.get("textAnnotation", {}).get("fullText", "")
        logger.info(f"OCR result: {full_text[:100]}...")
        return full_text
    except Exception as e:
        logger.error(f"Yandex Vision OCR error: {e}")
        return ""

# ---------- ФУНКЦИЯ АНАЛИЗА ТЕКСТА (YandexGPT) ----------
def analyze_text_with_yandexgpt(ocr_text: str) -> dict | None:
    """Отправляет распознанный текст в YandexGPT и получает вердикт."""
    if not API_KEY or not FOLDER_ID:
        logger.error("Yandex Cloud credentials are not set.")
        return None
    if not ocr_text:
        logger.warning("No text to analyze.")
        return {"verdict": "error", "issues": ["Текст не обнаружен."], "recommendations": ""}

    # Системный промпт с гайдлайнами
    system_prompt = """
Ты — ассистент по проверке баннеров для приложения Пятёрочки.
Проанализируй предоставленный текст и проверь его на соответствие следующим гайдлайнам:

1. Обращение к пользователю на "Вы".
2. Конкретное предложение с очевидной пользой, без абстрактных слов (например, "Живите оранжево!").
3. Использование буквы "ё" (например, "ещё", а не "еще").
4. Кавычки-ёлочки «».
5. Отсутствие капса (например, "РОЗЫГРЫШ" — ошибка).
6. Не более одного восклицательного знака.

Верни ответ строго в формате JSON:
{
  "verdict": "ok" или "error",
  "issues": ["список", "конкретных", "нарушений"],
  "recommendations": "краткая рекомендация по исправлению"
}
"""

    # Формируем запрос к YandexGPT
    payload = {
        "modelUri": YANDEXGPT_MODEL,
        "completionOptions": {
            "stream": False,
            "temperature": 0.1,
            "maxTokens": "1000"
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": f"Проверь этот текст: {ocr_text}"}
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {API_KEY}",
        "x-folder-id": FOLDER_ID
    }

    try:
        logger.info("Sending text to YandexGPT for analysis...")
        response = requests.post(YANDEXGPT_URL, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()
        content = result["result"]["alternatives"][0]["message"]["text"]

        # Пытаемся извлечь JSON из ответа
        try:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start != -1 and end > start:
                json_str = content[start:end]
                return json.loads(json_str)
            else:
                return {"verdict": "error", "issues": ["Не удалось разобрать ответ модели"], "recommendations": content}
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON: {content}")
            return {"verdict": "error", "issues": ["Некорректный ответ от модели"], "recommendations": content}

    except Exception as e:
        logger.error(f"YandexGPT error: {e}")
        return None

# ---------- ОБРАБОТЧИКИ TELEGRAM ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — умный агент проверки баннеров для Пятёрочки (Yandex AI).\n\n"
        "📌 *ВАЖНО:* Отправляйте баннер *как документ (файл)*, "
        "а не как фото. Telegram сжимает фото, искажая размеры.\n\n"
        "Я проверю баннер по всем гайдлайнам и дам подробный отчёт.",
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
    await update.message.reply_text("🔍 Анализирую оригинальный файл с помощью Yandex AI...")
    file = await document.get_file()
    image_bytes = await file.download_as_bytearray()
    await process_image(update, image_bytes, is_compressed=False)

async def process_image(update: Update, image_bytes: bytes, is_compressed: bool):
    # Проверка размера файла
    file_size_mb = len(image_bytes) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(f"❌ Размер файла {file_size_mb:.2f} МБ превышает лимит {MAX_FILE_SIZE_MB} МБ.")
        return

    try:
        img_pil = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось открыть изображение: {e}")
        return

    # Быстрая проверка размера
    width, height = img_pil.size
    size_ok = (width == TARGET_WIDTH and height == TARGET_HEIGHT)
    size_msg = f"📏 Размер: {width}x{height} {'✅' if size_ok else '❌ (ожидается 984x570)'}"

    # Отправляем промежуточное сообщение
    status_msg = await update.message.reply_text(
        f"{size_msg}\n🤖 Распознаю текст с помощью Yandex Vision..."
    )

    # 1. Распознаём текст с помощью Yandex Vision OCR
    ocr_text = ocr_with_yandex_vision(img_pil)
    if not ocr_text:
        await status_msg.edit_text(
            f"{size_msg}\n❌ Не удалось распознать текст на изображении. Возможно, его нет или он нечитаем."
        )
        return

    await status_msg.edit_text(
        f"{size_msg}\n📝 Распознанный текст:\n{ocr_text[:200]}...\n\n🤖 Анализирую текст с помощью YandexGPT..."
    )

    # 2. Анализируем текст с помощью YandexGPT
    gpt_result = analyze_text_with_yandexgpt(ocr_text)

    if gpt_result is None:
        await status_msg.edit_text(
            f"{size_msg}\n❌ Ошибка при обращении к YandexGPT. Проверьте API-ключ или доступность сервиса."
        )
        return

    # Формируем итоговое сообщение
    verdict = gpt_result.get("verdict", "error")
    issues = gpt_result.get("issues", [])
    recommendations = gpt_result.get("recommendations", "")

    if verdict == "ok":
        final_verdict = "✅ Текст баннера полностью соответствует гайдам Пятёрочки!"
    else:
        final_verdict = "❌ Текст баннера имеет нарушения."

    lines = [
        f"*Результаты проверки (Yandex AI):*",
        size_msg,
        f"\n*Вердикт:* {final_verdict}",
    ]

    if is_compressed:
        lines.append("\n⚠️ *Внимание:* анализ проводился по сжатому фото. Результаты могут быть неточными.")

    if issues:
        lines.append("\n*Обнаруженные проблемы:*")
        for issue in issues:
            lines.append(f"• {issue}")

    if recommendations:
        lines.append(f"\n*Рекомендация:* {recommendations}")

    lines.append(f"\n📝 *Распознанный текст:*\n{ocr_text}")

    await status_msg.edit_text("\n".join(lines), parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ---------- ЗАПУСК ----------
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    application.add_error_handler(error_handler)

    logger.info("Бот для проверки баннеров Пятёрочки с Yandex Vision + YandexGPT запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
