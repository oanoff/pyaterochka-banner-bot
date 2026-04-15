# Используем официальный образ Python 3.11 (полная версия для совместимости)
FROM python:3.11

# Обновляем список пакетов и устанавливаем нужные системные зависимости
# libgl1-mesa-dri и libglib2.0-0 - для OpenCV
# tesseract-ocr и tesseract-ocr-rus - для распознавания текста
RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    libgl1-mesa-dri \
    libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Создаём рабочую директорию
WORKDIR /app

# Копируем файл зависимостей Python
COPY requirements.txt .

# Устанавливаем Python-библиотеки
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]