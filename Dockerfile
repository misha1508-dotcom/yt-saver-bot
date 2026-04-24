FROM python:3.12-slim

WORKDIR /app

# Ставим ТОЛЬКО ffmpeg, без рекомендуемых пакетов (экономия ~1.5ГБ)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade "yt-dlp[default]"

# Код
COPY main.py .
COPY static /app/static

# Cookies (опционально)
COPY cookies.tx[t] /app/

# Non-root пользователь
RUN useradd -m -u 1000 botuser && \
    mkdir -p /tmp/yt-saver-downloads && \
    chown -R botuser:botuser /app /tmp/yt-saver-downloads

USER botuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
