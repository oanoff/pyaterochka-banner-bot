FROM python:3.11

RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends \
    libgl1-mesa-dri \
    libglib2.0-0 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]