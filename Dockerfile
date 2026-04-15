# Используем официальный образ Python 3.11 (не slim, чтобы избежать проблем с зависимостями)
FROM python:3.11

# Устанавливаем системные зависимости, включая Tesseract OCR и русский язык
# Добавлен флаг --fix-missing для обхода возможных сетевых ошибок
RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    libgl1-mesa-glx \
    libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Создаём рабочую директорию
WORKDIR /app

# Копируем файлы зависимостей
COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальные файлы проекта
COPY . .

# Указываем команду для запуска бота
CMD ["python", "bot.py"]