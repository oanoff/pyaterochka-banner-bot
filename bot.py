import os
import io
import json
import base64
import logging
import requests
import numpy as np
import cv2
from PIL import Image, ImageEnhance, ImageFilter, ImageStat
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from colorthief import ColorThief

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

# --- Эндпоинты Yandex Cloud ---
OCR_URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
GPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
VISION_CLASSIFY_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"

# --- Параметры гайдлайнов Пятёрочки ---
MODEL_URI = f"gpt://{FOLDER_ID}/yandexgpt-lite/latest"
TARGET_WIDTH = 984
TARGET_HEIGHT = 570
ASPECT_RATIO = TARGET_WIDTH / TARGET_HEIGHT
SIZE_TOLERANCE = 0.0
MAX_FILE_SIZE_MB = 5
ALLOWED_MIME_TYPES = {'image/jpeg', 'image/png', 'image/jpg'}

TEXT_COLOR_DARK = "#302E33"
TEXT_COLOR_LIGHT = "#FFFFFF"
MAX_TEXT_AREA_PERCENT = 52

MIN_SATURATION_ACID = 50
MAX_LIGHTNESS_PASTEL = 85
TEXTURE_THRESHOLD = 30

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_TEMPLATE_PATH = os.path.join(BASE_DIR, "assets", "pyaterochka_logo.png")

MAX_CHARS_XS_S = 45
MAX_CHARS_TITLE_M_L = 30
MAX_CHARS_SUBTITLE_M_L = 55

SAFETY_THRESHOLD = 0.7

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПРОВЕРКИ ИЗОБРАЖЕНИЯ ----------
def rgb_to_hsl(r, g, b):
    r, g, b = r/255.0, g/255.0, b/255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        h = s = 0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            h = (g - b) / d + (6 if g < b else 0)
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h /= 6
    return h * 360, s * 100, l * 100

def get_dominant_colors(image, n=3):
    temp = io.BytesIO()
    image.save(temp, format='PNG')
    temp.seek(0)
    color_thief = ColorThief(temp)
    palette = color_thief.get_palette(color_count=n)
    return palette

def is_background_bad(image):
    img = image.convert('RGB')
    palette = get_dominant_colors(img, 5)
    issues = []
    for rgb in palette:
        h, s, l = rgb_to_hsl(*rgb)
        if l < 10:
            issues.append("чёрный цвет фона")
        if l > 90 or (l > MAX_LIGHTNESS_PASTEL and s < 20):
            issues.append("белый/пастельный фон")
        if s > MIN_SATURATION_ACID and l > 40 and l < 80:
            issues.append("кислотный цвет фона")
        if len(palette) >= 3 and all(rgb_to_hsl(*c)[1] > 40 for c in palette[:3]):
            issues.append("пёстрый фон")
    cv_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    if laplacian_var > TEXTURE_THRESHOLD:
        issues.append("текстурный фон")
    return list(set(issues))

def detect_logo_pyaterochka(image):
    if not os.path.exists(LOGO_TEMPLATE_PATH):
        return False, "файл шаблона логотипа не найден"
    img_cv = cv2.cvtColor(np.array(image.convert('RGB')), cv2.COLOR_RGB2BGR)
    template = cv2.imread(LOGO_TEMPLATE_PATH)
    if template is None:
        return False, "не удалось загрузить шаблон логотипа"
    found = False
    for scale in np.linspace(0.5, 1.5, 10):
        resized = cv2.resize(template, (0,0), fx=scale, fy=scale)
        if resized.shape[0] > img_cv.shape[0] or resized.shape[1] > img_cv.shape[1]:
            continue
        res = cv2.matchTemplate(img_cv, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        if max_val > 0.7:
            found = True
            break
    return found, "обнаружен логотип Пятёрочки (запрещено)"

def preprocess_image(pil_image: Image.Image) -> Image.Image:
    """Предобработка изображения для улучшения распознавания текста."""
    enhancer = ImageEnhance.Sharpness(pil_image)
    pil_image = enhancer.enhance(2.0)
    enhancer = ImageEnhance.Contrast(pil_image)
    pil_image = enhancer.enhance(1.5)
    pil_image = pil_image.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    return pil_image

def check_image_safety(pil_image: Image.Image) -> tuple[bool, str]:
    """
    Проверяет изображение с помощью Yandex Vision Classification (модель 'moderation').
    Возвращает (True, "") если безопасно, иначе (False, "причина").
    """
    try:
        img_byte_arr = io.BytesIO()
        pil_image.save(img_byte_arr, format='JPEG', quality=85)
        encoded_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

        body = {
            "folderId": FOLDER_ID,
            "analyze_specs": [{
                "content": encoded_image,
                "features": [{
                    "type": "CLASSIFICATION",
                    "classificationConfig": {
                        "model": "moderation"
                    }
                }]
            }]
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {API_KEY}"
        }

        logger.info("Проверка изображения на безопасность...")
        response = requests.post(VISION_CLASSIFY_URL, json=body, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            return True, ""
        
        properties = results[0].get("results", [{}])[0].get("classification", {}).get("properties", [])
        
        for prop in properties:
            if prop.get("probability", 0) > SAFETY_THRESHOLD:
                category = prop.get("name", "").lower()
                if "adult" in category or "shocking" in category or "violence" in category:
                    return False, f"обнаружен нежелательный контент ({category})"
        
        return True, ""

    except Exception as e:
        logger.error(f"Ошибка при проверке безопасности изображения: {e}")
        return True, ""  # В случае ошибки не блокируем

def ocr_with_yandex(pil_image: Image.Image) -> str:
    """Отправляет изображение в Yandex Vision OCR и возвращает распознанный текст."""
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
        response.raise_for_status()
        data = response.json()
        text = data.get("result", {}).get("textAnnotation", {}).get("fullText", "")
        logger.info(f"Извлечённый текст: {text[:100] if text else 'пусто'}")
        return text
    except Exception as e:
        logger.error(f"Yandex Vision OCR error: {e}")
        return ""

def analyze_text_with_yandexgpt(ocr_text: str) -> dict | None:
    """Анализирует текст с помощью YandexGPT."""
    if not ocr_text:
        return {"verdict": "error", "issues": ["Текст не обнаружен."], "recommendations": ""}

    char_count = len(ocr_text.strip())
    lines = ocr_text.strip().split('\n')
    title = lines[0] if lines else ''
    subtitle = ' '.join(lines[1:]) if len(lines) > 1 else ''
    title_chars = len(title)
    subtitle_chars = len(subtitle)

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
        response.raise_for_status()
        result = response.json()
        content = result["result"]["alternatives"][0]["message"]["text"]
        logger.info(f"YandexGPT ответ: {content[:200]}...")

        try:
            start = content.find('{')
            end = content.rfind('}') + 1
            if start != -1 and end > start:
                gpt_result = json.loads(content[start:end])
            else:
                return {"verdict": "error", "issues": ["Не удалось разобрать ответ модели"], "recommendations": content}
        except json.JSONDecodeError:
            return {"verdict": "error", "issues": ["Некорректный JSON в ответе"], "recommendations": content}

        # --- ПОСТОБРАБОТКА ---
        issues = gpt_result.get("issues", [])
        corrected_issues = []

        exclamation_count = ocr_text.count('!')
        if exclamation_count <= 1:
            for issue in issues:
                if 'восклицательных' not in issue.lower() and 'знак' not in issue.lower():
                    corrected_issues.append(issue)
            if len(corrected_issues) < len(issues):
                logger.info("Исправлено: удалено ложное нарушение о восклицательных знаках")
        else:
            corrected_issues = issues

        final_issues = []
        for issue in corrected_issues:
            if 'капс' in issue.lower() or 'заглавн' in issue.lower():
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

        if banner_type == 'xs_s' and char_count > MAX_CHARS_XS_S:
            final_issues.append(f"Превышен лимит символов: {char_count} (макс. {MAX_CHARS_XS_S})")
        elif banner_type == 'm_l':
            if title_chars > MAX_CHARS_TITLE_M_L:
                final_issues.append(f"Заголовок превышает {MAX_CHARS_TITLE_M_L} символов (сейчас {title_chars})")
            if subtitle_chars > MAX_CHARS_SUBTITLE_M_L:
                final_issues.append(f"Подзаголовок превышает {MAX_CHARS_SUBTITLE_M_L} символов (сейчас {subtitle_chars})")

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
    if document.mime_type not in ALLOWED_MIME_TYPES:
        await update.message.reply_text("❌ Пожалуйста, отправьте изображение в формате JPEG или PNG.")
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

    # Проверка безопасности
    is_safe, safety_reason = check_image_safety(img_pil)
    if not is_safe:
        lines = [
            f"*Результаты проверки (Yandex AI):*",
            size_msg,
            f"\n*Вердикт:* ❌ Баннер имеет нарушения.",
            f"\n*Обнаруженные проблемы:*",
            f"• {safety_reason}",
            f"\n*Рекомендация:* Замените изображение.",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
        return

    # Проверка фона
    bg_issues = is_background_bad(img_pil)
    if bg_issues:
        issues_str = ", ".join(bg_issues)
        lines = [
            f"*Результаты проверки (Yandex AI):*",
            size_msg,
            f"\n*Вердикт:* ❌ Баннер имеет нарушения.",
            f"\n*Обнаруженные проблемы:*",
            f"• Проблемы с фоном: {issues_str}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
        return

    # Проверка логотипа
    logo_found, logo_msg = detect_logo_pyaterochka(img_pil)
    if logo_found:
        lines = [
            f"*Результаты проверки (Yandex AI):*",
            size_msg,
            f"\n*Вердикт:* ❌ Баннер имеет нарушения.",
            f"\n*Обнаруженные проблемы:*",
            f"• {logo_msg}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
        return

    await update.message.reply_text(f"{size_msg}\n🤖 Распознаю текст...")

    ocr_text = ocr_with_yandex(img_pil)
    if not ocr_text:
        await update.message.reply_text(
            f"{size_msg}\n❌ Не удалось распознать текст. Убедитесь, что текст на баннере хорошо читается."
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

    lines.append("\n⚠️ *Требуется ручная проверка:* убедитесь, что цвет текста соответствует гайду (#302E33 на светлом фоне или #FFFFFF на тёмном), и что изображение не содержит оружия, мрачных готических образов или антропоморфизма.")
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
    logger.info("Бот запущен с полным набором проверок (фон, логотип, текст, безопасность)...")
    
    application.run_polling(
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()