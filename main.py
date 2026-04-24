import os
import re
import logging
import asyncio
import tempfile
import shutil
import time
import uuid
from typing import Dict, Any, Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

# ─── Настройки ───────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

COOKIES_FILE = os.environ.get("COOKIES_FILE", "/app/cookies.txt")

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|live/|source/|embed/|v/)|youtu\.be/)"
    r"[^\s]+"
)

INSTAGRAM_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(instagram\.com/(reel|reels|p|tv)/"
    r"|instagr\.am/)"
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

# Хранилище задач в памяти
# Структура: { "task_id": { "status": "downloading"|"done"|"error", "title": "...", "file_path": "...", "error": "...", "work_dir": "..." } }
tasks: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="Krechet YT & IG Saver")

# Mount static files (will be created soon)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Модели API ──────────────────────────────────────────────────────────────

class DownloadRequest(BaseModel):
    url: str
    format: str = "video"  # "video" или "audio"

class TaskStatusResponse(BaseModel):
    status: str
    title: Optional[str] = None
    error: Optional[str] = None


# ─── Утилиты yt-dlp ─────────────────────────────────────────────────────────

def is_source_url(url: str) -> bool:
    return "youtube.com/source/" in url

def normalize_youtube_url(url: str) -> str:
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

    return opts

def find_downloaded_file(work_dir: str, prefix: str, preferred_ext: str = "mp4") -> str:
    candidates = []
    for f in os.listdir(work_dir):
        if f.startswith(prefix):
            full = os.path.join(work_dir, f)
            if os.path.isfile(full) and os.path.getsize(full) > 0:
                candidates.append(full)

    if not candidates:
        for f in os.listdir(work_dir):
            full = os.path.join(work_dir, f)
            if f.endswith(f".{preferred_ext}") and os.path.isfile(full) and os.path.getsize(full) > 0:
                candidates.append(full)

    if not candidates:
        raise FileNotFoundError(f"Файл не найден в {work_dir}")

    preferred = [c for c in candidates if c.endswith(f".{preferred_ext}")]
    if preferred:
        return max(preferred, key=os.path.getsize)
    return max(candidates, key=os.path.getsize)

def sanitize_filename(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\[\]]', '', title)
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "video"
    return cleaned[:100]

def _try_download(url: str, opts: dict) -> dict:
    last_error = None
    for i, clients in enumerate(PLAYER_CLIENT_STRATEGIES):
        try:
            current_opts = {**opts}
            current_opts["extractor_args"] = {"youtube": {"player_client": clients}}
            logger.info(f"Попытка yt-dlp с player_client={clients}")
            with yt_dlp.YoutubeDL(current_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            error_str = str(e)
            skip_errors = ["Private video", "Video unavailable", "removed", "deleted", "Sign in"]
            if any(skip in error_str for skip in skip_errors):
                raise
            time.sleep(1)
            continue
    raise last_error

# ─── Функции загрузки (запускаются в пуле потоков) ─────────────────────────

def download_youtube(url: str, work_dir: str, format_type: str) -> dict:
    source = is_source_url(url)
    url = normalize_youtube_url(url)
    base_opts = get_base_yt_opts(for_source=source)

    if format_type == "audio":
        opts = {
            **base_opts,
            "format": "bestaudio/best",
            "outtmpl": os.path.join(work_dir, "audio.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}],
        }
        prefix, ext = "audio", "mp3"
    else:
        opts = {
            **base_opts,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": os.path.join(work_dir, "video.%(ext)s"),
        }
        prefix, ext = "video", "mp4"

    info = _try_download(url, opts)
    title = info.get("title", prefix)
    
    if "entries" in info:
        entries = list(info["entries"])
        if entries:
            title = entries[0].get("title", title)

    file_path = find_downloaded_file(work_dir, prefix, ext)
    return {"title": title, "file_path": file_path}

def download_instagram(url: str, work_dir: str, format_type: str) -> dict:
    ig_base_opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 120,
        "retries": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        },
    }
    if os.path.isfile(COOKIES_FILE):
        ig_base_opts["cookiefile"] = COOKIES_FILE

    if format_type == "audio":
        opts = {
            **ig_base_opts,
            "format": "bestaudio/best",
            "outtmpl": os.path.join(work_dir, "audio.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}],
        }
        prefix, ext = "audio", "mp3"
    else:
        opts = {
            **ig_base_opts,
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": os.path.join(work_dir, "video.%(ext)s"),
        }
        prefix, ext = "video", "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    
    title = info.get("title", "instagram_" + prefix)
    file_path = find_downloaded_file(work_dir, prefix, ext)
    return {"title": title, "file_path": file_path}

# ─── Фоновая задача загрузки ──────────────────────────────────────────────────

def process_download(task_id: str, url: str, format_type: str):
    work_dir = tasks[task_id]["work_dir"]
    try:
        if INSTAGRAM_REGEX.search(url):
            res = download_instagram(url, work_dir, format_type)
        else:
            res = download_youtube(url, work_dir, format_type)
            
        tasks[task_id]["status"] = "done"
        tasks[task_id]["title"] = res["title"]
        tasks[task_id]["file_path"] = res["file_path"]
        logger.info(f"Task {task_id} done: {res['title']}")
        
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        if "Sign in" in error_msg:
            err = "Требуется авторизация (видео 18+ или приватное)."
        elif "Private" in error_msg:
            err = "Это приватное видео."
        elif "unavailable" in error_msg.lower() or "removed" in error_msg.lower():
            err = "Видео недоступно или удалено."
        else:
            err = error_msg.split("ERROR:")[-1].strip() if "ERROR:" in error_msg else error_msg
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = err
        logger.error(f"Task {task_id} failed: {err}")
    except Exception as e:
        tasks[task_id]["status"] = "error"
        tasks[task_id]["error"] = "Внутренняя ошибка сервера"
        logger.exception(f"Task {task_id} failed with exception")

# ─── Маршруты (Endpoints) ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    if not YOUTUBE_REGEX.search(url) and not INSTAGRAM_REGEX.search(url):
        return JSONResponse(status_code=400, content={"error": "Неподдерживаемая ссылка. Только YouTube или Instagram."})
        
    task_id = str(uuid.uuid4())
    work_dir = tempfile.mkdtemp(prefix=f"ytsaver_web_{task_id}_")
    
    tasks[task_id] = {
        "status": "downloading",
        "work_dir": work_dir
    }
    
    # Запускаем загрузку в фоне
    background_tasks.add_task(process_download, task_id, url, req.format)
    
    return {"task_id": task_id}

@app.get("/api/status/{task_id}", response_model=TaskStatusResponse)
async def get_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    return TaskStatusResponse(
        status=task["status"],
        title=task.get("title"),
        error=task.get("error")
    )

def cleanup_task(task_id: str):
    """Удаляет рабочую папку и очищает словарь задач после скачивания"""
    task = tasks.get(task_id)
    if task:
        work_dir = task.get("work_dir")
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        tasks.pop(task_id, None)

@app.get("/api/file/{task_id}")
async def get_file(task_id: str, background_tasks: BackgroundTasks):
    task = tasks.get(task_id)
    if not task or task["status"] != "done":
        raise HTTPException(status_code=404, detail="File not ready or task not found")
        
    file_path = task["file_path"]
    ext = os.path.splitext(file_path)[1]
    title = task.get("title", "download")
    safe_title = sanitize_filename(title)
    
    filename = f"{safe_title}{ext}"
    
    # Добавляем задачу на удаление папки после отдачи файла
    background_tasks.add_task(cleanup_task, task_id)
    
    return FileResponse(
        path=file_path, 
        filename=filename, 
        media_type="application/octet-stream"
    )

