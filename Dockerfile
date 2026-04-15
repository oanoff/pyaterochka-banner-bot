# Используем официальный лёгкий образ Python 3.11
FROM python:3.11-slim

# Устанавливаем системные зависимости, включая Tesseract OCR и русский язык
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

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
