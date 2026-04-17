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
# Токен бота (задаётся через переменную окружения на Bothost)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

# CoPilot API X5
COPILOT_API_KEY = "2ae44ef3-95a9-4c53-a8b1-9002f9807196"   # <-- ВСТАВЬТЕ СВОЙ КЛЮЧ СЮДА
COPILOT_BASE_URL = "https://api-copilot.x5.ru/aigw/v1"  # можно изменить на https://api-copilot.x5.ru/v1
COPILOT_MODEL = "x5/x5-airun-vlm-medium"  # мультимодальная модель

# Если мультимодальная модель недоступна, раскомментируйте строки ниже для текстового режима
# COPILOT_MODEL = "x5/x5-airun-medium-prod"
# USE_VISION = False
USE_VISION = True

# Гайдлайны
TARGET_WIDTH = 984
TARGET_HEIGHT = 570
MAX_FILE_SIZE_MB = 5

# ---------- ПРОВЕРКА ДОСТУПНОСТИ API ПРИ ЗАПУСКЕ ----------
def check_copilot_api():
    """Проверяет соединение с CoPilot API и выводит статус в логи."""
    try:
        headers = {"Authorization": f"Bearer {COPILOT_API_KEY}"}
        # Пробуем получить список моделей
        r = requests.get(f"{COPILOT_BASE_URL}/models", headers=headers, timeout=15)
        if r.status_code == 200:
            models = r.json().get("data", [])
            model_ids = [m.get("id") for m in models if "id" in m]
            logger.info(f"CoPilot API доступен. Доступные модели: {model_ids}")
            return True
        else:
            logger.error(f"CoPilot API вернул статус {r.status_code}: {r.text}")
            return False
    except Exception as e:
        logger.error(f"Не удалось подключиться к CoPilot API: {e}")
        return False

# ---------- ФУНКЦИЯ АНАЛИЗА ЧЕРЕЗ COPILOT ----------
def analyze_banner_with_copilot(pil_image: Image.Image) -> dict | None:
    """Отправляет изображение (или текст) в CoPilot и возвращает вердикт."""
    if not COPILOT_API_KEY:
        logger.error("CoPilot API ключ не настроен!")
        return None

    # Системный промпт
    system_prompt = """
Ты — эксперт по проверке баннеров для приложения Пятёрочки.
Проанализируй предоставленное изображение баннера и проверь его на соответствие следующим гайдлайнам:

1. Размер: должен быть ровно 984x570 пикселей.
2. Текстовый блок: должен занимать не более 52% площади баннера.
3. Цвет текста: только #302E33 (на светлом фоне) или #FFFFFF (на тёмном фоне).
4. Фон: не должен быть чёрным, белым, кислотным, пастельным или текстурным.
5. Логотип Пятёрочки: запрещён на баннере.
6. Текстовые правила:
   - Обращение на "Вы".
   - Конкретное предложение с очевидной пользой, без абстрактных слов.
   - Использование буквы "ё".
   - Кавычки-ёлочки «».
   - Отсутствие капса (ЗАГОЛОВОК ПРОПИСНЫМИ — ошибка).
   - Не более одного восклицательного знака.
   - Для XS/S баннеров: до 45 символов.
   - Для M/L баннеров: заголовок до 30 символов, подзаголовок до 55 символов.
7. Имидж: не должен содержать оружия, мрачных образов, антропоморфизма, стоковых клише.

Верни ответ строго в формате JSON:
{
  "verdict": "ok" или "error",
  "issues": ["список", "конкретных", "нарушений"],
  "recommendations": "краткая рекомендация по исправлению"
}
"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {COPILOT_API_KEY}"
    }

    if USE_VISION:
        # Мультимодальный запрос с изображением
        buffered = io.BytesIO()
        pil_image.save(buffered, format="JPEG", quality=95)
        img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{img_base64}"

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Проверь этот баннер по гайдам Пятёрочки."},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ]
    else:
        # Текстовый режим (если VLM недоступна)
        # Здесь предполагается, что вы предварительно распознали текст с помощью OCR
        # и передали его в изображении (в данном примере не реализовано)
        logger.error("Текстовый режим требует предварительного OCR. Переключите USE_VISION = True.")
        return None

    payload = {
        "model": COPILOT_MODEL,
        "messages": messages,
        "max_tokens": 1000,
        "temperature": 0.1,
        "stream": False
    }

    try:
        logger.info(f"Отправка запроса в {COPILOT_BASE_URL}/chat/completions")
        response = requests.post(
            f"{COPILOT_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=60
        )
        logger.info(f"Статус ответа: {response.status_code}")
        if response.status_code != 200:
            logger.error(f"Ошибка API: {response.text}")
            return None

        result = response.json()
        content = result["choices"][0]["message"]["content"]
        # Пытаемся извлечь JSON из ответа (иногда модель добавляет пояснения)
        try:
            # Ищем первую фигурную скобку
            start = content.find('{')
            end = content.rfind('}') + 1
            if start != -1 and end > start:
                json_str = content[start:end]
                return json.loads(json_str)
            else:
                # Если нет JSON, считаем весь ответ рекомендацией
                return {"verdict": "error", "issues": ["Не удалось разобрать ответ модели"], "recommendations": content}
        except json.JSONDecodeError:
            logger.error(f"Не удалось распарсить JSON: {content}")
            return {"verdict": "error", "issues": ["Некорректный ответ от модели"], "recommendations": content}

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса: {e}")
        return None

# ---------- ОБРАБОТЧИКИ TELEGRAM ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — умный агент проверки баннеров для Пятёрочки (ИИ CoPilot).\n\n"
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
    await update.message.reply_text("🔍 Анализирую оригинальный файл с помощью CoPilot AI...")
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

    status_msg = await update.message.reply_text(
        f"{size_msg}\n🤖 Отправляю в CoPilot AI на анализ... (может занять ~10-15 сек)"
    )

    copilot_result = analyze_banner_with_copilot(img_pil)

    if copilot_result is None:
        await status_msg.edit_text(
            f"{size_msg}\n❌ Ошибка при обращении к CoPilot AI. Проверьте API-ключ или доступность сервиса."
        )
        return

    verdict = copilot_result.get("verdict", "error")
    issues = copilot_result.get("issues", [])
    recommendations = copilot_result.get("recommendations", "")

    if verdict == "ok":
        final_verdict = "✅ Баннер полностью соответствует гайдам Пятёрочки!"
    else:
        final_verdict = "❌ Баннер имеет нарушения."

    lines = [
        f"*Результаты проверки (CoPilot AI):*",
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

    await status_msg.edit_text("\n".join(lines), parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ---------- ЗАПУСК ----------
def main():
    # Проверяем доступность API при старте
    if not check_copilot_api():
        logger.warning("CoPilot API недоступен. Бот продолжит работу, но проверка баннеров не будет работать.")
    else:
        logger.info("CoPilot API успешно проверен.")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    application.add_error_handler(error_handler)

    logger.info("Бот для проверки баннеров Пятёрочки с CoPilot AI запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
