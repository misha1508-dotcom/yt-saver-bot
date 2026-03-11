FROM python:3.12-slim

WORKDIR /app

# Устанавливаем ffmpeg и зависимости
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Копируем и ставим Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py .

# Если есть cookies-файл — копируем (опционально)
COPY cookies.tx[t] /app/

# Создаём non-root пользователя
RUN useradd -m -u 1000 botuser && \
    mkdir -p /tmp/yt-saver-downloads && \
    chown -R botuser:botuser /app /tmp/yt-saver-downloads

USER botuser

CMD ["python", "bot.py"]
