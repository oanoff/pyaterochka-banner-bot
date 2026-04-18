import os
import io
import json
import base64
import logging
import requests
from PIL import Image, ImageEnhance, ImageFilter
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- НАСТРОЙКА ЛОГИРОВАНИЯ ----------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==============================================================================
#                         ВАШИ ДАННЫЕ (УЖЕ ВСТАВЛЕНЫ)
# ==============================================================================
FOLDER_ID = "b1g6irlklro22jcs1i2c"
API_KEY = "AQVNzuXu-feyxUlpOzTXEAL1U7lB_h7lwDjhh4kQ"
# ==============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

OCR_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
MODEL_URI = f"gpt://{FOLDER_ID}/yandexgpt-lite/latest"

TARGET_WIDTH = 984
TARGET_HEIGHT = 570
MAX_FILE_SIZE_MB = 5

# Лимиты символов
MAX_CHARS_XS_S = 45
MAX_CHARS_TITLE_M_L = 30
MAX_CHARS_SUBTITLE_M_L = 55

def preprocess_image(pil_image: Image.Image) -> Image.Image:
    enhancer = ImageEnhance.Sharpness(pil_image)
    pil_image = enhancer.enhance(2.0)
    enhancer = ImageEnhance.Contrast(pil_image)
    pil_image = enhancer.enhance(1.5)
    pil_image = pil_image.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    return pil_image

def ocr_with_yandex(pil_image: Image.Image) -> str:
    processed_img = preprocess_image(pil_image)

    img_byte_arr = io.BytesIO()
    processed_img.save(img_byte_arr, format='JPEG', quality=95)
    encoded_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

    body = {
        "mimeType": "image/jpeg",
        "languageCodes": ["ru", "en"],
        "content": encoded_image
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {API_KEY}",
        "x-folder-id": FOLDER_ID,
        "x-data-logging-enabled": "false"
    }

    try:
        logger.info("Отправка изображения в Yandex Vision OCR...")
        response = requests.post(OCR_URL, json=body, headers=headers, timeout=30)
        logger.info(f"OCR статус ответа: {response.status_code}")
        response.raise_for_status()
        data = response.json()
        text = data.get("result", {}).get("textAnnotation", {}).get("fullText", "")
        logger.info(f"Извлечённый текст: {text[:100] if text else 'пусто'}")
        return text
    except Exception as e:
        logger.error(f"Yandex Vision OCR error: {e}")
        return ""

def analyze_text_with_yandexgpt(ocr_text: str) -> dict | None:
    if not ocr_text:
        return {"verdict": "error", "issues": ["Текст не обнаружен."], "recommendations": ""}

    # Подсчитываем символы для проверки лимитов
    char_count = len(ocr_text.strip())
    lines = ocr_text.strip().split('\n')
    title = lines[0] if lines else ''
    subtitle = ' '.join(lines[1:]) if len(lines) > 1 else ''
    title_chars = len(title)
    subtitle_chars = len(subtitle)

    # Определяем тип баннера по количеству символов
    if char_count <= MAX_CHARS_XS_S + 10:
        banner_type = 'xs_s'
        char_limit_msg = f"Общее количество символов: {char_count} (макс. {MAX_CHARS_XS_S} для XS/S)"
    else:
        banner_type = 'm_l'
        char_limit_msg = f"Заголовок: {title_chars} симв. (макс. {MAX_CHARS_TITLE_M_L}), подзаголовок: {subtitle_chars} симв. (макс. {MAX_CHARS_SUBTITLE_M_L})"

    system_prompt = f"""
Ты — эксперт по проверке баннеров для приложения Пятёрочки.
Проанализируй предоставленный текст и проверь его на соответствие гайдлайнам. Твоя задача — найти ТОЛЬКО реальные нарушения. Не придумывай ошибок.

ГАЙДЛАЙНЫ:
1. **Обращение к пользователю**: должно быть на "Вы" (получите, купите). Нарушение: "получи", "купи" (обращение на "ты").
2. **Конкретное предложение**: должно описывать выгоду без абстрактных слов. Нарушение: фразы вроде "Живите оранжево!", "Больше орехов, больше призов!".
3. **Буква "ё"**: обязательно использовать "ё" в словах, где она нужна. Нарушение: "еще" вместо "ещё", "пятерочка" вместо "пятёрочка".
4. **Кавычки**: только «ёлочки». Нарушение: "прямые" или “английские” кавычки.
5. **Капс (ЗАГЛАВНЫЕ БУКВЫ)**: запрещён. Нарушение: когда весь текст или целый заголовок написан капсом. Отдельные заглавные буквы в начале предложений и аббревиатуры (VIP, X5) — НЕ нарушение.
6. **Восклицательные знаки**: не более одного на ВЕСЬ текст. Нарушение: два и более "!". Один "!" — НЕ нарушение.

{banner_type.upper()} БАННЕР: {char_limit_msg}
(Лимиты символов уже проверены автоматически, НЕ включай их в список нарушений).

Верни ответ строго в формате JSON:
{{
  "verdict": "ok" или "error",
  "issues": ["конкретное нарушение 1", "конкретное нарушение 2"],
  "recommendations": "что исправить (только если есть нарушения)"
}}
"""

    payload = {
        "modelUri": MODEL_URI,
        "completionOptions": {
            "stream": False,
            "temperature": 0.0,
            "maxTokens": 1000
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": f"Проверь текст:\n{ocr_text}"}
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {API_KEY}",
        "x-folder-id": FOLDER_ID
    }

    try:
        logger.info("Отправка текста в YandexGPT...")
        response = requests.post(GPT_URL, json=payload, headers=headers, timeout=60)
        logger.info(f"YandexGPT статус ответа: {response.status_code}")
        response.raise_for_status()
        result = response.json()
        content = result["result"]["alternatives"][0]["message"]["text"]
        logger.info(f"YandexGPT ответ: {content[:200]}...")

        # Парсим JSON
        try:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start != -1 and end > start:
                gpt_result = json.loads(content[start:end])
            else:
                return {"verdict": "error", "issues": ["Не удалось разобрать ответ модели"], "recommendations": content}
        except json.JSONDecodeError:
            return {"verdict": "error", "issues": ["Некорректный JSON в ответе"], "recommendations": content}

        # --- ПОСТОБРАБОТКА: исправляем ошибки модели ---
        issues = gpt_result.get("issues", [])
        corrected_issues = []

        # 1. Проверка восклицательных знаков
        exclamation_count = ocr_text.count('!')
        if exclamation_count <= 1:
            for issue in issues:
                if 'восклицательных' not in issue.lower() and 'знак' not in issue.lower():
                    corrected_issues.append(issue)
            if len(corrected_issues) < len(issues):
                logger.info("Исправлено: удалено ложное нарушение о восклицательных знаках")
        else:
            corrected_issues = issues

        # 2. Проверка капса (если модель ошибочно его указала)
        final_issues = []
        for issue in corrected_issues:
            if 'капс' in issue.lower() or 'заглавн' in issue.lower():
                # Проверяем, действительно ли есть капс (весь текст заглавными или целая строка)
                has_caps = False
                for line in ocr_text.split('\n'):
                    if line and line == line.upper() and len(line.strip()) > 3:
                        has_caps = True
                        break
                if has_caps:
                    final_issues.append(issue)
                else:
                    logger.info("Исправлено: удалено ложное нарушение о капсе")
            else:
                final_issues.append(issue)

        # 3. Проверка лимитов символов
        if banner_type == 'xs_s' and char_count > MAX_CHARS_XS_S:
            final_issues.append(f"Превышен лимит символов: {char_count} (макс. {MAX_CHARS_XS_S})")
        elif banner_type == 'm_l':
            if title_chars > MAX_CHARS_TITLE_M_L:
                final_issues.append(f"Заголовок превышает {MAX_CHARS_TITLE_M_L} символов (сейчас {title_chars})")
            if subtitle_chars > MAX_CHARS_SUBTITLE_M_L:
                final_issues.append(f"Подзаголовок превышает {MAX_CHARS_SUBTITLE_M_L} символов (сейчас {subtitle_chars})")

        # Пересчитываем вердикт
        final_verdict = "ok" if not final_issues else "error"
        gpt_result["verdict"] = final_verdict
        gpt_result["issues"] = final_issues

        if final_verdict == "ok":
            gpt_result["recommendations"] = ""

        return gpt_result

    except Exception as e:
        logger.error(f"YandexGPT error: {e}")
        return None

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
    file_size_mb = len(image_bytes) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(f"❌ Размер файла {file_size_mb:.2f} МБ превышает лимит {MAX_FILE_SIZE_MB} МБ.")
        return

    try:
        img_pil = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось открыть изображение: {e}")
        return

    width, height = img_pil.size
    size_ok = (width == TARGET_WIDTH and height == TARGET_HEIGHT)
    size_msg = f"📏 Размер: {width}x{height} {'✅' if size_ok else '❌ (ожидается 984x570)'}"

    await update.message.reply_text(
        f"{size_msg}\n🤖 Распознаю текст (с предобработкой)..."
    )

    ocr_text = ocr_with_yandex(img_pil)
    if not ocr_text:
        await update.message.reply_text(
            f"{size_msg}\n❌ Не удалось распознать текст. "
            "Убедитесь, что текст на баннере хорошо читается."
        )
        return

    await update.message.reply_text(
        f"{size_msg}\n📝 Распознанный текст:\n{ocr_text[:200]}...\n\n🤖 Анализирую с помощью YandexGPT..."
    )

    gpt_result = analyze_text_with_yandexgpt(ocr_text)

    if gpt_result is None:
        await update.message.reply_text(
            f"{size_msg}\n❌ Ошибка при обращении к YandexGPT. Проверьте настройки или повторите позже."
        )
        return

    verdict = gpt_result.get("verdict", "error")
    issues = gpt_result.get("issues", [])
    recommendations = gpt_result.get("recommendations", "")

    final_verdict = "✅ Текст полностью соответствует гайдам!" if verdict == "ok" else "❌ Текст имеет нарушения."
    lines = [
        f"*Результаты проверки (Yandex AI):*",
        size_msg,
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
    lines.append(f"\n📝 *Распознанный текст:*\n{ocr_text}")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

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
    logger.info("Бот запущен с Yandex Vision + YandexGPT (полная постобработка)...")
    
    application.run_polling(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()