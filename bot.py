"""
YouTube Saver Telegram Bot v1.1.0
Скачивает YouTube видео в лучшем качестве + отдельно аудио.
Если видео > 50MB — сжимает через ffmpeg с сохранением aspect ratio.
"""

import os
import re
import logging
import asyncio
import subprocess
import tempfile
import shutil
import time
from pathlib import Path

from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# ─── Настройки ───────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/app/cookies.txt")
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # 50 MB

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|live/|source/|embed/|v/)|youtu\.be/)"
    r"[^\s]+"
)

VIDEO_ID_REGEX = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/|v/|source/)|youtu\.be/)"
    r"([a-zA-Z0-9_-]{11})"
)

# Наборы player_client для retry — от самых надёжных к fallback
PLAYER_CLIENT_STRATEGIES = [
    ["ios", "web"],
    ["android", "web"],
    ["ios"],
    ["mweb"],
    ["default"],
]


# ─── Утилиты ─────────────────────────────────────────────────────────────────


def is_source_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на 'оригинальный звук' (source/)."""
    return "youtube.com/source/" in url


def normalize_youtube_url(url: str) -> str:
    """
    Нормализует YouTube URL.
    Для source/ ссылок — оставляет как есть (это плейлист, скачиваем первое видео).
    Для остальных — извлекает video ID и формирует чистый URL.
    """
    if is_source_url(url):
        if not url.startswith("http"):
            url = "https://" + url
        return url

    id_match = VIDEO_ID_REGEX.search(url)
    if id_match:
        video_id = id_match.group(1)
        if "shorts/" in url:
            return f"https://www.youtube.com/shorts/{video_id}"
        return f"https://www.youtube.com/watch?v={video_id}"

    return url


def get_base_yt_opts(for_source: bool = False, player_clients: list = None) -> dict:
    """Базовые опции для yt-dlp."""
    if player_clients is None:
        player_clients = ["ios", "web"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 120,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "extractor_retries": 3,
        "extractor_args": {"youtube": {"player_client": player_clients}},
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if for_source:
        opts["noplaylist"] = False
        opts["playlist_items"] = "1"
    else:
        opts["noplaylist"] = True

    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logger.info(f"Используем cookies из {COOKIES_FILE}")

    return opts


def get_file_size(path: str) -> int:
    """Размер файла в байтах."""
    return os.path.getsize(path)


def compress_video(input_path: str, output_path: str, target_size_mb: float = 49.0) -> str:
    """
    Сжимает видео до target_size_mb, сохраняя aspect ratio.
    Двухпроходное кодирование для точного попадания в размер.
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            input_path,
        ],
        capture_output=True, text=True,
    )
    duration = float(probe.stdout.strip())

    target_total_bitrate = (target_size_mb * 8 * 1024 * 1024) / duration
    audio_bitrate = 128 * 1024  # 128 kbps
    video_bitrate = int(target_total_bitrate - audio_bitrate)

    if video_bitrate < 100_000:
        video_bitrate = 100_000

    video_bitrate_k = video_bitrate // 1024
    work_dir = str(Path(output_path).parent)

    logger.info(
        f"Сжатие: длительность={duration:.1f}s, "
        f"целевой битрейт видео={video_bitrate_k}k, аудио=128k"
    )

    # Проход 1
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264", "-b:v", f"{video_bitrate_k}k",
            "-preset", "medium", "-pass", "1",
            "-passlogfile", os.path.join(work_dir, "ffmpeg2pass"),
            "-an", "-f", "null", "/dev/null",
        ],
        capture_output=True, check=True,
    )

    # Проход 2
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264", "-b:v", f"{video_bitrate_k}k",
            "-preset", "medium", "-pass", "2",
            "-passlogfile", os.path.join(work_dir, "ffmpeg2pass"),
            "-c:a", "aac", "-b:a", "128k",
            output_path,
        ],
        capture_output=True, check=True,
    )

    return output_path


def sanitize_filename(title: str) -> str:
    """Очищает название файла от спецсимволов."""
    cleaned = re.sub(r'[<>:"/\\|?*\[\]]', '', title)
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "video"
    return cleaned[:60]


def _try_download(url: str, opts: dict) -> dict:
    """
    Пытается скачать видео с разными player_client стратегиями.
    Если первая попытка не удалась — перебирает fallback-варианты.
    """
    last_error = None

    for i, clients in enumerate(PLAYER_CLIENT_STRATEGIES):
        try:
            current_opts = {**opts}
            current_opts["extractor_args"] = {"youtube": {"player_client": clients}}

            logger.info(f"Попытка {i+1}/{len(PLAYER_CLIENT_STRATEGIES)}: player_client={clients}")

            with yt_dlp.YoutubeDL(current_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                logger.info(f"Успех с player_client={clients}")
                return info

        except yt_dlp.utils.DownloadError as e:
            error_str = str(e)
            last_error = e
            logger.warning(f"Неудача с player_client={clients}: {error_str[:100]}")

            # Если ошибка явно не связана с player_client — не перебираем дальше
            skip_errors = ["Private video", "Video unavailable", "removed", "deleted"]
            if any(skip in error_str for skip in skip_errors):
                raise

            # Пауза перед следующей попыткой
            time.sleep(1)
            continue

    # Все стратегии исчерпаны
    raise last_error


def download_video(url: str, work_dir: str) -> dict:
    """
    Скачивает видео в лучшем качестве с retry по разным player_client.
    Возвращает dict с путями к файлам и метаинфо.
    """
    source = is_source_url(url)
    url = normalize_youtube_url(url)
    video_path = os.path.join(work_dir, "video.mp4")
    audio_path = os.path.join(work_dir, "audio.mp3")

    base_opts = get_base_yt_opts(for_source=source)

    # ── Скачиваем видео с retry ──
    video_opts = {
        **base_opts,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": video_path,
        "merge_output_format": "mp4",
    }

    logger.info(f"Скачиваю видео: {url}")
    info = _try_download(url, video_opts)

    title = info.get("title", "video")
    duration = info.get("duration", 0)

    # Если это плейлист (source/), вытаскиваем info первого элемента
    if "entries" in info:
        entries = list(info["entries"])
        if entries:
            first = entries[0]
            title = first.get("title", title)
            duration = first.get("duration", duration)

    # ── Скачиваем аудио с retry ──
    audio_opts = {
        **base_opts,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(work_dir, "audio.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }

    logger.info(f"Скачиваю аудио: {url}")
    _try_download(url, audio_opts)

    return {
        "title": title,
        "duration": duration,
        "video_path": video_path,
        "audio_path": audio_path,
    }


def download_audio_only(url: str, work_dir: str) -> dict:
    """Скачивает только аудио с retry."""
    source = is_source_url(url)
    url = normalize_youtube_url(url)
    audio_path = os.path.join(work_dir, "audio.mp3")

    base_opts = get_base_yt_opts(for_source=source)
    audio_opts = {
        **base_opts,
        "format": "bestaudio/best",
        "outtmpl": os.path.join(work_dir, "audio.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }

    logger.info(f"Скачиваю аудио: {url}")
    info = _try_download(url, audio_opts)

    title = info.get("title", "audio")
    if "entries" in info:
        entries = list(info["entries"])
        if entries:
            title = entries[0].get("title", title)

    return {
        "title": title,
        "duration": info.get("duration", 0),
        "audio_path": audio_path,
    }


# ─── Обработчики Telegram ────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /start."""
    await update.message.reply_text(
        "🎬 *YouTube Saver*\n\n"
        "Отправь мне ссылку на YouTube видео, и я скачаю для тебя:\n"
        "📹 Видео в лучшем качестве\n"
        "🎵 Аудио отдельно (MP3 320kbps)\n\n"
        "Просто кинь ссылку!",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /help."""
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "1️⃣ Отправь ссылку на YouTube видео\n"
        "2️⃣ Подожди, пока я скачаю и обработаю\n"
        "3️⃣ Получи видео + аудио файлы\n\n"
        "🔗 *Поддерживаемые форматы ссылок:*\n"
        "• `youtube.com/watch?v=...`\n"
        "• `youtu.be/...`\n"
        "• `youtube.com/shorts/...`\n"
        "• `youtube.com/source/.../shorts`\n\n"
        "⚡ Если видео больше 50 МБ — автоматически сожму "
        "с сохранением качества и пропорций.",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает входящие сообщения со ссылками."""
    text = update.message.text or ""
    match = YOUTUBE_REGEX.search(text)

    if not match:
        await update.message.reply_text(
            "🤔 Не вижу ссылку на YouTube.\n"
            "Отправь ссылку вида: `youtube.com/watch?v=...`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    url = match.group(0)
    normalized = normalize_youtube_url(url)
    logger.info(f"Получена ссылка: {url} → {normalized}")

    status_msg = await update.message.reply_text("⏳ Скачиваю видео… Подожди немного.")

    work_dir = tempfile.mkdtemp(prefix="ytsaver_")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, download_video, url, work_dir
        )

        title = result["title"]
        safe_title = sanitize_filename(title)
        video_path = result["video_path"]
        audio_path = result["audio_path"]

        # ── Проверяем и сжимаем видео если надо ──
        video_size = get_file_size(video_path)
        video_compressed = False

        if video_size > TELEGRAM_FILE_LIMIT:
            await status_msg.edit_text(
                f"📦 Видео весит {video_size / 1024 / 1024:.1f} МБ — сжимаю…"
            )
            compressed_path = os.path.join(work_dir, "video_compressed.mp4")

            await asyncio.get_event_loop().run_in_executor(
                None, compress_video, video_path, compressed_path
            )

            video_path = compressed_path
            video_size = get_file_size(video_path)
            video_compressed = True

        await status_msg.edit_text("📤 Отправляю файлы…")

        # ── Отправляем видео ──
        if video_size <= TELEGRAM_FILE_LIMIT:
            with open(video_path, "rb") as vf:
                caption = f"🎬 *{title}*"
                if video_compressed:
                    caption += "\n📦 _Сжато для Telegram_"
                await update.message.reply_document(
                    document=vf,
                    filename=f"{safe_title}.mp4",
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
        else:
            await update.message.reply_text(
                f"⚠️ Даже после сжатия видео весит "
                f"{video_size / 1024 / 1024:.1f} МБ (лимит 50 МБ). "
                f"Отправляю только аудио."
            )

        # ── Отправляем аудио ──
        audio_size = get_file_size(audio_path)
        if audio_size <= TELEGRAM_FILE_LIMIT:
            with open(audio_path, "rb") as af:
                await update.message.reply_audio(
                    audio=af,
                    title=title,
                    filename=f"{safe_title}.mp3",
                    caption=f"🎵 *{title}*",
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
        else:
            await update.message.reply_text(
                "⚠️ Аудио тоже слишком большое для Telegram."
            )

        await status_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"Ошибка yt-dlp для {url}: {error_msg}", exc_info=True)

        if "Sign in" in error_msg or "confirm you" in error_msg:
            await status_msg.edit_text(
                "❌ YouTube требует авторизацию для этого видео.\n"
                "Возможно, видео имеет возрастные ограничения."
            )
        elif "Private video" in error_msg:
            await status_msg.edit_text(
                "❌ Это приватное видео — скачать нельзя."
            )
        elif "Video unavailable" in error_msg or "removed" in error_msg.lower():
            await status_msg.edit_text(
                "❌ Видео недоступно — удалено или заблокировано."
            )
        elif "reloaded" in error_msg.lower():
            await status_msg.edit_text(
                "❌ YouTube заблокировал запрос. "
                "Попробуй ещё раз через минуту."
            )
        else:
            # Обрезаем техническую часть ошибки для пользователя
            clean_error = error_msg.split("ERROR:")[-1].strip() if "ERROR:" in error_msg else error_msg
            await status_msg.edit_text(
                f"❌ Не удалось скачать:\n{clean_error[:200]}",
            )

    except Exception as e:
        logger.error(f"Ошибка при обработке {url}: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ Внутренняя ошибка:\n{str(e)[:200]}",
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ─── Запуск ───────────────────────────────────────────────────────────────────


def main() -> None:
    """Запуск бота."""
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не установлен!")
        return

    logger.info(f"yt-dlp версия: {yt_dlp.version.__version__}")
    logger.info(f"Cookies файл: {'найден' if os.path.isfile(COOKIES_FILE) else 'не найден'}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
