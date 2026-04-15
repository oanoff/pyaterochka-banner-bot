import os
import io
import re
import asyncio
import logging
from PIL import Image, ImageDraw, ImageStat
import cv2
import numpy as np
import pytesseract
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from colorthief import ColorThief

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Попытка настроить Tesseract
try:
    pytesseract.get_tesseract_version()
    TESSERACT_AVAILABLE = True
except pytesseract.TesseractNotFoundError:
    TESSERACT_AVAILABLE = False
    logger.warning("Tesseract OCR не найден! Проверка текста будет отключена.")

# ---------- КОНФИГУРАЦИЯ ГАЙДЛАЙНОВ ПЯТЁРОЧКИ ----------
TARGET_WIDTH = 984
TARGET_HEIGHT = 570
ASPECT_RATIO = TARGET_WIDTH / TARGET_HEIGHT
SIZE_TOLERANCE = 0.0
MAX_FILE_SIZE_MB = 5
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png'}

# Текстовые цвета
TEXT_COLOR_DARK = "#302E33"
TEXT_COLOR_LIGHT = "#FFFFFF"

# Максимальная площадь текстового блока (%) – обновлено до 52%
MAX_TEXT_AREA_PERCENT = 52

# Пороги для определения "плохого" фона
MIN_SATURATION_ACID = 50
MAX_LIGHTNESS_PASTEL = 85
TEXTURE_THRESHOLD = 30

# Файл с логотипом Пятёрочки
LOGO_TEMPLATE_PATH = "assets/pyaterochka_logo.png"

# Допустимое отклонение цвета текста
COLOR_TOLERANCE = 60

# ---------- ЛИМИТЫ ПО СИМВОЛАМ (ВКЛЮЧАЯ ПРОБЕЛЫ) ----------
MAX_CHARS_XS_S = 45
MAX_CHARS_TITLE_M_L = 30
MAX_CHARS_SUBTITLE_M_L = 50

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
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

def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def color_distance(c1, c2):
    return np.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))

def is_color_allowed(rgb, bg_is_light):
    target = hex_to_rgb(TEXT_COLOR_DARK) if bg_is_light else hex_to_rgb(TEXT_COLOR_LIGHT)
    return color_distance(rgb, target) <= COLOR_TOLERANCE

def get_local_background_type(image, x, y, w, h):
    padding = 10
    x1 = max(0, x - padding)
    y1 = max(0, y - padding)
    x2 = min(image.width, x + w + padding)
    y2 = min(image.height, y + h + padding)
    
    if (x2 - x1) < 5 or (y2 - y1) < 5:
        x1, y1 = 0, 0
        x2, y2 = image.width, image.height
    
    region = image.crop((x1, y1, x2, y2))
    gray_region = region.convert('L')
    stat = ImageStat.Stat(gray_region)
    avg_brightness = stat.mean[0]
    return avg_brightness > 128

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

def check_text_styles(text):
    issues = []
    if re.search(r'\bещ[её]\b', text) and 'ё' not in text:
        issues.append("возможно, пропущена буква 'ё'")
    if '"' in text or "'" in text or '“' in text or '”' in text:
        issues.append("используйте кавычки-ёлочки «»")
    words = re.findall(r'\b[А-ЯA-Z]{3,}\b', text)
    if words and len(words) / len(text.split()) > 0.3:
        issues.append("текст написан капсом")
    if text.count('!') > 1:
        issues.append("слишком много восклицательных знаков")
    return issues

def check_char_count(text, banner_type='auto'):
    char_count = len(text.strip())
    if banner_type == 'xs_s':
        if char_count > MAX_CHARS_XS_S:
            return False, f"превышен лимит символов для XS/S баннера (макс. {MAX_CHARS_XS_S}, сейчас {char_count})"
    elif banner_type == 'm_l':
        lines = text.strip().split('\n')
        title = lines[0] if lines else ''
        subtitle = ' '.join(lines[1:]) if len(lines) > 1 else ''
        title_chars = len(title)
        subtitle_chars = len(subtitle)
        if title_chars > MAX_CHARS_TITLE_M_L:
            return False, f"заголовок превышает {MAX_CHARS_TITLE_M_L} символов (сейчас {title_chars})"
        if subtitle_chars > MAX_CHARS_SUBTITLE_M_L:
            return False, f"подзаголовок превышает {MAX_CHARS_SUBTITLE_M_L} символов (сейчас {subtitle_chars})"
    else:
        if char_count <= MAX_CHARS_XS_S + 10:
            return check_char_count(text, 'xs_s')
        else:
            return check_char_count(text, 'm_l')
    return True, ""

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

# ---------- ОСНОВНОЙ АНАЛИЗ ----------
async def analyze_image(image_bytes: bytes, filename: str = "", update: Update = None) -> dict:
    results = {
        "file_size_ok": False,
        "format_ok": False,
        "dimensions_ok": False,
        "aspect_ratio_ok": False,
        "text_block_area_ok": False,
        "text_color_ok": False,
        "background_ok": False,
        "logo_ok": True,
        "text_rules_ok": False,
        "char_count_ok": False,
        "width": 0,
        "height": 0,
        "file_size_mb": 0,
        "verdict": "",
        "details": [],
        "ocr_text": ""
    }

    # Размер файла
    file_size_bytes = len(image_bytes)
    results["file_size_mb"] = file_size_bytes / (1024 * 1024)
    results["file_size_ok"] = results["file_size_mb"] <= MAX_FILE_SIZE_MB
    if not results["file_size_ok"]:
        results["details"].append(f"⚠️ Размер файла {results['file_size_mb']:.2f} МБ > {MAX_FILE_SIZE_MB} МБ")

    # Формат
    ext = os.path.splitext(filename)[1].lower()
    results["format_ok"] = ext in ALLOWED_EXTENSIONS
    if not results["format_ok"]:
        results["details"].append(f"❌ Формат {ext} не поддерживается. Допустимы: {', '.join(ALLOWED_EXTENSIONS)}")

    # Открытие изображения
    try:
        img_pil = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        results["width"], results["height"] = img_pil.size
    except Exception as e:
        results["details"].append(f"❌ Не удалось открыть изображение: {e}")
        results["verdict"] = "Невозможно проверить"
        return results

    # Размер и соотношение сторон
    results["dimensions_ok"] = (results["width"] == TARGET_WIDTH and results["height"] == TARGET_HEIGHT)
    if not results["dimensions_ok"]:
        results["details"].append(f"⚠️ Размер {results['width']}x{results['height']} не соответствует {TARGET_WIDTH}x{TARGET_HEIGHT}")

    actual_ratio = results["width"] / results["height"] if results["height"] else 0
    results["aspect_ratio_ok"] = abs(actual_ratio - ASPECT_RATIO) < 0.01
    if not results["aspect_ratio_ok"]:
        results["details"].append(f"⚠️ Соотношение сторон {actual_ratio:.3f} (требуется {ASPECT_RATIO:.3f})")

    # OCR текста
    text = ""
    if TESSERACT_AVAILABLE:
        try:
            img_cv = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            text = pytesseract.image_to_string(gray, lang='rus+eng').strip()
            results["ocr_text"] = text
        except Exception as e:
            results["details"].append(f"⚠️ Ошибка распознавания текста: {e}")
    else:
        results["details"].append("ℹ️ Tesseract OCR не установлен на сервере. Проверка текста отключена.")

    # Площадь текстового блока
    text_area_percent = 0
    if TESSERACT_AVAILABLE and text:
        try:
            data = pytesseract.image_to_data(gray, lang='rus+eng', output_type=pytesseract.Output.DICT)
            boxes = 0
            total_area = results["width"] * results["height"]
            for i in range(len(data['text'])):
                if int(data['conf'][i]) > 30:
                    x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
                    boxes += w * h
            text_area_percent = (boxes / total_area) * 100 if total_area else 0
            results["text_block_area_ok"] = text_area_percent <= MAX_TEXT_AREA_PERCENT
            if not results["text_block_area_ok"]:
                results["details"].append(f"⚠️ Текстовый блок занимает {text_area_percent:.1f}% (макс. {MAX_TEXT_AREA_PERCENT}%)")
        except Exception as e:
            results["details"].append(f"⚠️ Не удалось оценить площадь текста: {e}")
            results["text_block_area_ok"] = True
    else:
        results["text_block_area_ok"] = True

    # Проверка цвета текста
    if TESSERACT_AVAILABLE and text:
        text_color_issues = []
        try:
            data = pytesseract.image_to_data(gray, lang='rus+eng', output_type=pytesseract.Output.DICT)
            for i in range(len(data['text'])):
                if int(data['conf'][i]) > 30:
                    x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
                    if w > 0 and h > 0:
                        local_bg_light = get_local_background_type(img_pil, x, y, w, h)
                        crop = img_pil.crop((x, y, x+w, y+h))
                        stat = ImageStat.Stat(crop)
                        avg_color = tuple(map(int, stat.mean[:3]))
                        if not is_color_allowed(avg_color, local_bg_light):
                            expected = TEXT_COLOR_DARK if local_bg_light else TEXT_COLOR_LIGHT
                            text_color_issues.append(f"{avg_color} (ожидался {expected})")
            if text_color_issues:
                results["text_color_ok"] = False
                examples = text_color_issues[:3]
                results["details"].append(f"⚠️ Цвет текста не соответствует гайду. Примеры: {', '.join(examples)}")
            else:
                results["text_color_ok"] = True
        except Exception as e:
            results["text_color_ok"] = True
            results["details"].append(f"⚠️ Не удалось проверить цвет текста: {e}")
    else:
        results["text_color_ok"] = True

    # Проверка фона
    bg_issues = is_background_bad(img_pil)
    results["background_ok"] = len(bg_issues) == 0
    if not results["background_ok"]:
        results["details"].append(f"⚠️ Проблемы с фоном: {', '.join(bg_issues)}")

    # Проверка логотипа
    logo_found, logo_msg = detect_logo_pyaterochka(img_pil)
    results["logo_ok"] = not logo_found
    if logo_found:
        results["details"].append(f"❌ {logo_msg}")

    # Текстовые правила
    if TESSERACT_AVAILABLE and text:
        text_style_issues = check_text_styles(text)
        results["text_rules_ok"] = len(text_style_issues) == 0
        if not results["text_rules_ok"]:
            results["details"].extend([f"⚠️ {issue}" for issue in text_style_issues])

        # Проверка символов
        banner_type = 'xs_s' if text_area_percent < 25 else 'm_l'
        char_ok, char_msg = check_char_count(text, banner_type)
        results["char_count_ok"] = char_ok
        if not char_ok:
            results["details"].append(f"⚠️ {char_msg}")
    else:
        results["text_rules_ok"] = True
        results["char_count_ok"] = True

    # Доп. предупреждение
    results["details"].append("ℹ️ Требуется ручная проверка имиджа на соответствие стилистическим запретам")

    # Вердикт
    critical = ["file_size_ok", "format_ok", "dimensions_ok", "aspect_ratio_ok",
                "text_block_area_ok", "text_color_ok", "background_ok", "logo_ok",
                "text_rules_ok", "char_count_ok"]
    if all(results.get(k, False) for k in critical):
        results["verdict"] = "✅ Баннер соответствует основным требованиям гайдов Пятёрочки!"
    else:
        results["verdict"] = "❌ Баннер не соответствует гайдам. Смотрите детали."

    return results

# ---------- ОБРАБОТЧИКИ TELEGRAM ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Я — агент проверки баннеров для приложения Пятёрочки.\n"
        "Отправьте мне изображение, и я проверю его по гайдлайнам:\n"
        f"• Размер: {TARGET_WIDTH}x{TARGET_HEIGHT} px (строго)\n"
        f"• Текстовый блок ≤ {MAX_TEXT_AREA_PERCENT}%\n"
        f"• Цвет текста: только {TEXT_COLOR_DARK} или {TEXT_COLOR_LIGHT}\n"
        f"• Фон: без запрещённых цветов и текстур\n"
        f"• Логотип Пятёрочки — запрещён\n"
        f"• Лимит символов: XS/S до {MAX_CHARS_XS_S}, M/L заголовок до {MAX_CHARS_TITLE_M_L}, подзаголовок до {MAX_CHARS_SUBTITLE_M_L}"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Анализирую изображение...")
    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    results = await analyze_image(image_bytes, filename="image.jpg", update=update)
    await send_results(update, results)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.mime_type or not document.mime_type.startswith('image/'):
        await update.message.reply_text("❌ Пожалуйста, отправьте изображение.")
        return
    await update.message.reply_text("🔍 Анализирую изображение...")
    file = await document.get_file()
    image_bytes = await file.download_as_bytearray()
    results = await analyze_image(image_bytes, filename=document.file_name or "image", update=update)
    await send_results(update, results)

async def send_results(update: Update, results: dict):
    status_emoji = lambda ok: "✅" if ok else "❌"
    lines = [
        f"*Результаты проверки баннера:*\n",
        f"📏 *Размер:* {results['width']}x{results['height']} {status_emoji(results['dimensions_ok'])}",
        f"📐 *Соотношение:* {results['width']/results['height']:.3f} {status_emoji(results['aspect_ratio_ok'])}",
        f"💾 *Размер файла:* {results['file_size_mb']:.2f} МБ {status_emoji(results['file_size_ok'])}",
        f"🖼 *Формат:* {status_emoji(results['format_ok'])}",
        f"📝 *Площадь текста:* {status_emoji(results['text_block_area_ok'])}",
        f"🎨 *Цвет текста:* {status_emoji(results['text_color_ok'])}",
        f"🌄 *Фон:* {status_emoji(results['background_ok'])}",
        f"🏷 *Логотип:* {status_emoji(results['logo_ok'])}",
        f"🔤 *Текстовые правила:* {status_emoji(results['text_rules_ok'])}",
        f"🔢 *Лимит символов:* {status_emoji(results['char_count_ok'])}",
        f"\n*Вердикт:* {results['verdict']}",
    ]
    if results['details']:
        lines.append("\n📋 *Подробности:*")
        lines.extend([f"• {d}" for d in results['details']])
    if results.get('ocr_text'):
        lines.append(f"\n📝 *Распознанный текст:*\n{results['ocr_text'][:200]}...")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    TOKEN = os.environ.get("BOT_TOKEN")
    if not TOKEN:
        raise ValueError("Переменная окружения BOT_TOKEN не установлена!")

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))
    application.add_error_handler(error_handler)

    logger.info("Бот для проверки баннеров Пятёрочки запущен с обновлённой площадью текста 52%...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()