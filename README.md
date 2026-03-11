# 🎬 YouTube Saver — Telegram Bot

Telegram-бот для скачивания YouTube видео в лучшем качестве + аудио (MP3 320kbps).

## Возможности
- 📹 Скачивание видео в максимальном качестве
- 🎵 Отдельная отправка аудио (MP3 320kbps)
- 📦 Автоматическое сжатие видео > 50 МБ (двухпроходное кодирование, сохранение пропорций)
- Поддержка: обычные видео, Shorts, live

## Быстрый старт

```bash
# 1. Скопируй .env
cp .env.example .env
# Впиши свой TELEGRAM_BOT_TOKEN

# 2. Запусти
docker compose up --build -d
```

## Деплой на VPS

1. `git clone` в `/opt/vibe-projects/`
2. Добавить сервис в `/opt/vibe-projects/deployment/docker-compose.yml`
3. `cd deployment && docker compose up -d --build yt-saver`

## Стек
- Python 3.12
- python-telegram-bot
- yt-dlp
- ffmpeg
