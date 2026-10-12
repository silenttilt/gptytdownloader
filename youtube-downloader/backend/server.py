#!/usr/bin/env python3
"""A token-protected, loopback-only YouTube downloader using yt-dlp.

The browser submits only a YouTube URL and a quality preset. Format selection,
paths, yt-dlp options, and FFmpeg arguments are always controlled by this server.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    import yt_dlp
    from yt_dlp.version import __version__ as YT_DLP_VERSION
except ImportError:
    yt_dlp = None
    YT_DLP_VERSION = "not installed"

LOGGER = logging.getLogger("youtube-downloader")
VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")
FORMAT_ID_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
ALLOWED_ORIGINS = frozenset({
    "https://www.youtube.com", "https://youtube.com", "https://m.youtube.com",
})
QUALITIES = (360, 480, 720, 1080, 1440, 2160)
ACTIVE_STATUSES = frozenset({"preparing", "downloading", "processing"})
MAX_BODY_BYTES = 4096
MAX_TASKS = 100
TASK_TTL_SECONDS = 24 * 60 * 60


class APIError(ValueError):
    """A message that is safe to expose to the local API client."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_youtube_url(url: str) -> str:
    """Reject arbitrary destinations, credentials, ports and playlist-only URLs."""
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\x7f]", url):
        raise APIError("Укажите корректный URL видео YouTube.")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.port is not None):
            raise ValueError("unsupported URL")
        hostname = parsed.hostname
    except (ValueError, TypeError):
        raise APIError("Разрешены только обычные ссылки YouTube без порта и логина.") from None
    video_id = None
    if hostname in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            if len(values) == 1:
                video_id = values[0]
        else:
            match = re.fullmatch(r"/shorts/([A-Za-z0-9_-]{11})/?", parsed.path)
            if match:
                video_id = match.group(1)
    elif hostname == "youtu.be":
        match = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parsed.path)
        if match:
            video_id = match.group(1)
    if not isinstance(video_id, str) or not VIDEO_ID_RE.fullmatch(video_id):
        raise APIError("Поддерживаются только ссылки YouTube /watch, /shorts и youtu.be с videoId.")
    return f"https://www.youtube.com/watch?v={video_id}"


def safe_filename(value: str, max_length: int = 140) -> str:
    """Make a cross-platform basename while preserving readable Unicode titles."""
    value = unicodedata.normalize("NFKC", str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value)
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:max_length].rstrip(" .") or "YouTube"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.I):
        value = "_" + value
    # A UTF-8 filename must also fit on filesystems with a 255-byte limit.
    while len(value.encode("utf-8")) > 180:
        value = value[:-1]
    return value or "YouTube"


def _number(value) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _height(fmt: dict) -> int:
    height = max(0, int(_number(fmt.get("height"))))
    width = max(0, int(_number(fmt.get("width"))))
    # Portrait Shorts use the short edge: 1080×1920 is a 1080p option.
    return min(width, height) if width and height and width < height else height


def _has_video(fmt: dict) -> bool:
    return fmt.get("vcodec") not in {None, "none"} and _height(fmt) > 0


def _has_audio(fmt: dict) -> bool:
    return fmt.get("acodec") not in {None, "none"}


def _needs_remux(fmt: dict) -> bool:
    # HLS can deliver MPEG-TS despite an MP4 extension. Let yt-dlp/FFmpeg fix
    # that container; without FFmpeg offer only ready-to-save progressive files.
    return str(fmt.get("protocol", "")).startswith("m3u8")


def _formats(info: dict) -> list[dict]:
    if info.get("has_drm") or info.get("is_drm"):
        raise APIError("Загрузка видео с DRM не поддерживается.", 422)
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise APIError("Дождитесь окончания прямой трансляции: загружаются только готовые видео.", 422)
    result = []
    for fmt in info.get("formats") or []:
        if (isinstance(fmt, dict) and not fmt.get("has_drm") and not fmt.get("is_drm")
                and FORMAT_ID_RE.fullmatch(str(fmt.get("format_id", "")))
                and fmt.get("url") and fmt.get("protocol") != "mhtml"):
            result.append(fmt)
    return result


def _mp4_video(fmt: dict) -> bool:
    codec = str(fmt.get("vcodec", "")).lower()
    return fmt.get("ext") in {"mp4", "m4v"} and codec.startswith(("avc", "h264", "av01", "hevc", "h265", "hev1", "hvc1"))


def _aac_audio(fmt: dict) -> bool:
    return str(fmt.get("acodec", "")).lower().startswith(("mp4a", "aac"))


def _webm_video(fmt: dict) -> bool:
    return str(fmt.get("vcodec", "")).lower().startswith(("vp8", "vp9", "vp09", "av01"))


def _webm_audio(fmt: dict) -> bool:
    return str(fmt.get("acodec", "")).lower().startswith(("opus", "vorbis"))


def _video_rank(fmt: dict) -> tuple:
    codec = str(fmt.get("vcodec", "")).lower()
    # Resolution is filtered first. MP4/H.264 is the compatibility preference
    # within that resolution, never a reason to downgrade the best preset.
    return (_mp4_video(fmt), codec.startswith(("avc", "h264")),
            _number(fmt.get("fps")), _number(fmt.get("tbr")), _has_audio(fmt))


@dataclass(frozen=True)
class FormatSelection:
    selector: str
    height: int
    container: str
    requires_ffmpeg: bool


def select_format(info: dict, quality: str, ffmpeg_available: bool) -> FormatSelection:
    """Choose concrete extractor-provided IDs, never a client format expression."""
    if quality not in {"best", *(str(value) for value in QUALITIES)}:
        raise APIError("Неизвестное качество. Выберите один из предложенных вариантов.")
    formats = _formats(info)
    videos = [fmt for fmt in formats if _has_video(fmt)]
    audios = [fmt for fmt in formats if _has_audio(fmt) and not _has_video(fmt)]
    if quality != "best":
        videos = [fmt for fmt in videos if _height(fmt) == int(quality)]
    if not videos:
        raise APIError("Выбранное качество сейчас недоступно. Обновите список доступных качеств.", 422)
    if not audios and not any(_has_audio(fmt) for fmt in videos):
        raise APIError("Для этого видео не найдена доступная аудиодорожка.", 422)
    if not ffmpeg_available:
        videos = [fmt for fmt in videos if _has_audio(fmt) and not _needs_remux(fmt)]
    else:
        videos = [fmt for fmt in videos if _has_audio(fmt) or audios]
    if not videos:
        if not ffmpeg_available:
            raise APIError("Это качество требует объединения видео и аудио. Установите FFmpeg и перезапустите сервер.", 409)
        raise APIError("Выбранное качество сейчас недоступно либо у видео нет доступной аудиодорожки. Обновите список.", 422)
    if quality == "best":
        best_height = max(_height(fmt) for fmt in videos)
        videos = [fmt for fmt in videos if _height(fmt) == best_height]
    video = max(videos, key=_video_rank)
    if _has_audio(video):
        container = str(video.get("ext") or "mkv").lower()
        if not re.fullmatch(r"[a-z0-9]{1,8}", container):
            raise APIError("Неизвестный контейнер исходного видео.", 422)
        return FormatSelection(str(video["format_id"]), _height(video), container, _needs_remux(video))
    compatible_audio = [audio for audio in audios if _aac_audio(audio)] if _mp4_video(video) else []
    if not compatible_audio and _webm_video(video):
        compatible_audio = [audio for audio in audios if _webm_audio(audio)]
    audio = max(compatible_audio or audios, key=lambda fmt: (_number(fmt.get("abr")), _number(fmt.get("tbr"))))
    if _mp4_video(video) and _aac_audio(audio):
        container = "mp4"
    elif _webm_video(video) and _webm_audio(audio):
        container = "webm"
    else:
        container = "mkv"
    return FormatSelection(f"{video['format_id']}+{audio['format_id']}", _height(video), container, True)


def list_qualities(info: dict, ffmpeg_available: bool) -> list[dict]:
    selections = [("best", select_format(info, "best", ffmpeg_available))]
    for height in QUALITIES:
        try:
            selections.append((str(height), select_format(info, str(height), ffmpeg_available)))
        except APIError:
            continue
    return [{
        "value": value,
        "label": (f"Лучшее ({selection.height}p)" if value == "best" else f"{selection.height}p"),
        "height": selection.height,
        "container": selection.container,
        "requires_ffmpeg": selection.requires_ffmpeg,
    } for value, selection in selections]


class QuietYtdlpLogger:
    """Keep request URLs, extractor payloads and tokens out of console output."""

    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def _friendly_error(error: Exception) -> str:
    if isinstance(error, APIError):
        return str(error)
    message = re.sub(r"\x1b\[[0-9;]*m", "", str(error))
    lowered = message.lower()
    if any(term in lowered for term in ("sign in", "login", "cookies", "private video", "members-only", "age-restricted", "not a bot")):
        return "YouTube требует авторизацию или проверку доступа. Сервер не использует cookies и не обходит ограничения."
    if any(term in lowered for term in ("geo-restricted", "not available in your country", "copyright", "video unavailable", "removed", "not available")):
        return "Видео недоступно для обычного публичного доступа. Ограничения YouTube не обходятся."
    if "ffmpeg" in lowered or "ffprobe" in lowered:
        return "Ошибка FFmpeg: проверьте установку FFmpeg/FFprobe и свободное место, затем повторите загрузку."
    if "requested format" in lowered:
        return "Формат перестал быть доступен. Обновите список качеств и повторите загрузку."
    if any(term in lowered for term in ("timed out", "timeout", "connection", "network", "http error", "urlopen")):
        return "Сетевая ошибка при обращении к YouTube. Проверьте подключение и повторите попытку; при 403 обновите yt-dlp."
    if "javascript" in lowered or "js runtime" in lowered:
        return "Не удалось выполнить JavaScript-проверку YouTube. Установите Node.js 22+ и обновите yt-dlp[default]."
    if isinstance(error, OSError):
        return "Не удалось сохранить файл. Проверьте права на папку загрузок и свободное место."
    return "yt-dlp не смог загрузить видео. Обновите yt-dlp[default] и проверьте доступность видео без авторизации."


@dataclass
class DownloadTask:
    task_id: str
    url: str
    quality: str
    status: str = "preparing"
    progress: float | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated: float = field(default_factory=time.monotonic)
    title: str | None = None
    filename: str | None = None
    filepath: str | None = None
    container: str | None = None
    error: str | None = None

    def response(self) -> dict:
        return {key: value for key, value in vars(self).items() if key not in {"updated", "url"} and value is not None}


class DownloaderService:
    def __init__(self, download_dir: str | Path, token: str, *, ffmpeg_available: bool | None = None,
                 workers: int = 2, youtube_dl_factory=None):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise ValueError("Token must contain 32–256 URL-safe characters")
        self.token = token
        self.download_dir = Path(download_dir).expanduser().resolve()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_available = bool(shutil.which("ffmpeg") and shutil.which("ffprobe")) if ffmpeg_available is None else ffmpeg_available
        self.youtube_dl_factory = youtube_dl_factory or (yt_dlp.YoutubeDL if yt_dlp is not None else None)
        if self.youtube_dl_factory is None:
            raise RuntimeError("Установите зависимости: python -m pip install -r backend/requirements.txt")
        self.js_runtime = next(((name, shutil.which(name)) for name in ("node", "deno", "bun") if shutil.which(name)), None)
        self._lock = threading.RLock()
        self._tasks: dict[str, DownloadTask] = {}
        self._active: dict[tuple[str, str], str] = {}
        self._cache: dict[str, tuple[float, dict]] = {}
        self._extract_slots = threading.BoundedSemaphore(2)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download")
        self._closed = False

    def close(self, wait: bool = True):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _options(self) -> dict:
        options = {
            "quiet": True, "no_warnings": True, "logger": QuietYtdlpLogger(),
            "noplaylist": True, "socket_timeout": 20, "retries": 2,
            "fragment_retries": 2, "extractor_retries": 1, "geo_bypass": False,
            "allow_unplayable_formats": False, "cachedir": False,
            "remote_components": [], "concurrent_fragment_downloads": 2,
        }
        if self.js_runtime:
            name, path = self.js_runtime
            options["js_runtimes"] = {name: {"path": path}}
        return options

    def health(self) -> dict:
        return {"ok": True, "ffmpeg_available": self.ffmpeg_available,
                "download_dir": str(self.download_dir), "yt_dlp_version": YT_DLP_VERSION,
                "js_runtime_available": self.js_runtime is not None,
                "js_runtime": self.js_runtime[0] if self.js_runtime else None}

    def _extract(self, url: str, *, cached: bool) -> dict:
        with self._lock:
            entry = self._cache.get(url)
            if cached and entry and time.monotonic() - entry[0] < 60:
                return entry[1]
        if not self._extract_slots.acquire(timeout=1):
            raise APIError("Сервер уже получает сведения о видео. Повторите запрос через несколько секунд.", 429)
        try:
            with self.youtube_dl_factory(self._options()) as downloader:
                info = downloader.extract_info(url, download=False)
            if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"}:
                raise APIError("Не удалось получить сведения об отдельном видео.", 422)
            _formats(info)
            with self._lock:
                if len(self._cache) >= 32:
                    oldest = min(self._cache, key=lambda key: self._cache[key][0])
                    self._cache.pop(oldest, None)
                self._cache[url] = (time.monotonic(), info)
            return info
        except APIError:
            raise
        except Exception as error:
            raise APIError(_friendly_error(error), 502) from None
        finally:
            self._extract_slots.release()

    def formats(self, url: str) -> dict:
        url = normalize_youtube_url(url)
        info = self._extract(url, cached=True)
        qualities = list_qualities(info, self.ffmpeg_available)
        video_id = parse_qs(urlsplit(url).query)["v"][0]
        thumbnail = info.get("thumbnail")
        if not isinstance(thumbnail, str) or urlsplit(thumbnail).hostname != "i.ytimg.com":
            thumbnail = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        response = {"video_id": video_id, "title": str(info.get("title") or video_id),
                    "thumbnail": thumbnail, "qualities": qualities,
                    "ffmpeg_available": self.ffmpeg_available}
        if not self.ffmpeg_available:
            response["notice"] = "FFmpeg не найден: доступны только форматы с готовой аудиодорожкой. Установите FFmpeg для остальных качеств."
        return response

    def _prune_tasks(self):
        now = time.monotonic()
        finished = [task for task in self._tasks.values() if task.status not in ACTIVE_STATUSES]
        for task in finished:
            if now - task.updated > TASK_TTL_SECONDS:
                self._tasks.pop(task.task_id, None)
        if len(self._tasks) >= MAX_TASKS:
            finished = sorted((task for task in self._tasks.values() if task.status not in ACTIVE_STATUSES), key=lambda task: task.updated)
            while len(self._tasks) >= MAX_TASKS and finished:
                self._tasks.pop(finished.pop(0).task_id, None)

    def create_task(self, url: str, quality: str) -> dict:
        url = normalize_youtube_url(url)
        if not isinstance(quality, str) or quality not in {"best", *(str(value) for value in QUALITIES)}:
            raise APIError("Неизвестное качество загрузки.")
        key = (url, quality)
        with self._lock:
            if self._closed:
                raise APIError("Сервер завершает работу.", 503)
            existing = self._active.get(key)
            if existing:
                return {"task_id": existing, "status": self._tasks[existing].status, "deduplicated": True}
            if len(self._active) >= 6:
                raise APIError("Очередь загрузок заполнена. Дождитесь завершения текущих задач.", 429)
            self._prune_tasks()
            task = DownloadTask(secrets.token_hex(16), url, quality)
            self._tasks[task.task_id] = task
            self._active[key] = task.task_id
            self._executor.submit(self._download, task)
            return {"task_id": task.task_id, "status": task.status, "deduplicated": False}

    def get_task(self, task_id: str) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise APIError("Задача не найдена: сервер мог быть перезапущен.", 404)
            result = task.response()
            result.setdefault("progress", None)
            return result

    def _update(self, task: DownloadTask, **values):
        with self._lock:
            for name, value in values.items():
                setattr(task, name, value)
            task.updated = time.monotonic()

    def _download(self, task: DownloadTask):
        work_dir = self.download_dir / f".ytd-work-{task.task_id}"
        try:
            info = self._extract(task.url, cached=False)
            selection = select_format(info, task.quality, self.ffmpeg_available)
            video_id = parse_qs(urlsplit(task.url).query)["v"][0]
            title = str(info.get("title") or video_id)
            self._update(task, title=title, container=selection.container)
            work_dir.mkdir(mode=0o700)
            basename = f"{safe_filename(title)} [{video_id}]"
            component_progress: dict[str, float] = {}
            expected_parts = 2 if "+" in selection.selector else 1

            def progress_hook(data):
                status = data.get("status")
                if status == "error":
                    return
                key = str(data.get("filename") or (data.get("info_dict") or {}).get("format_id") or "media")
                if status == "finished":
                    component_progress[key] = 1.0
                elif status == "downloading":
                    total = _number(data.get("total_bytes") or data.get("total_bytes_estimate"))
                    if total:
                        component_progress[key] = min(1.0, max(0.0, _number(data.get("downloaded_bytes")) / total))
                else:
                    return
                value = min(99.0, sum(component_progress.values()) / expected_parts * 100) if component_progress else None
                with self._lock:
                    if value is not None and task.progress is not None:
                        value = max(task.progress, value)
                    finished = len(component_progress) >= expected_parts and all(part == 1 for part in component_progress.values())
                    self._update(task, status="processing" if finished else "downloading", progress=value)

            def postprocessor_hook(data):
                if data.get("status") in {"started", "processing", "finished"}:
                    self._update(task, status="processing", progress=max(task.progress or 0, 98.0))

            options = self._options()
            options.update({
                "format": selection.selector,
                "outtmpl": str(work_dir / (basename.replace("%", "%%") + ".%(ext)s")),
                "paths": {"home": str(work_dir), "temp": str(work_dir)},
                "merge_output_format": selection.container,
                "progress_hooks": [progress_hook], "postprocessor_hooks": [postprocessor_hook],
                "overwrites": False, "continuedl": False,
            })
            with self.youtube_dl_factory(options) as downloader:
                result = downloader.extract_info(task.url, download=True)
            self._update(task, status="processing", progress=max(task.progress or 0, 98.0))
            final_source = self._find_output(work_dir, result or {}, selection.container)
            # The extension comes from the actual completed file, never the UI.
            extension = final_source.suffix.lower().lstrip(".")
            if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
                raise APIError("yt-dlp создал файл с неподдерживаемым расширением.", 500)
            filename = f"{basename} ({selection.height}p) [{task.task_id[:8]}].{extension}"
            destination = self.download_dir / filename
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            try:
                os.replace(final_source, destination)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            self._update(task, status="completed", progress=100.0, filename=filename,
                         filepath=str(destination), container=extension)
        except Exception as error:
            self._update(task, status="error", error=_friendly_error(error))
            LOGGER.warning("Task %s failed (%s)", task.task_id[:8], type(error).__name__)
        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
            with self._lock:
                self._active.pop((task.url, task.quality), None)

    @staticmethod
    def _find_output(work_dir: Path, result: dict, container: str) -> Path:
        paths = [result.get("filepath"), result.get("_filename")]
        paths.extend(item.get("filepath") for item in result.get("requested_downloads") or [] if isinstance(item, dict))
        root = work_dir.resolve()
        for name in paths:
            if not isinstance(name, str):
                continue
            path = Path(name).resolve()
            if path.parent == root and path.is_file() and path.suffix == f".{container}":
                return path
        files = [path for path in work_dir.iterdir() if path.is_file() and path.suffix == f".{container}"]
        if len(files) == 1:
            return files[0]
        raise APIError("yt-dlp не создал ожидаемый итоговый файл. Проверьте FFmpeg и свободное место.", 500)


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler, service: DownloaderService):
        self.service = service
        self._http_slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._http_slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._http_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._http_slots.release()


class APIHandler(BaseHTTPRequestHandler):
    server_version = "YouTubeDownloader/1.0"
    sys_version = ""

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, format, *args):
        # Do not log complete URL query strings or authentication headers.
        pass

    @property
    def service(self) -> DownloaderService:
        return self.server.service

    def _json(self, status: int, payload: dict):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(encoded)
        self.close_connection = True

    def _check_access(self, *, preflight: bool = False):
        hosts = self.headers.get_all("Host", [])
        expected = f"127.0.0.1:{self.server.server_port}"
        if len(hosts) != 1 or hosts[0] != expected:
            raise APIError("Разрешён только локальный адрес 127.0.0.1.", 403)
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] not in ALLOWED_ORIGINS):
            raise APIError("Запросы с этого Origin запрещены.", 403)
        if preflight:
            if not origins:
                raise APIError("Preflight требует разрешённый Origin YouTube.", 403)
            return
        tokens = self.headers.get_all("X-YTD-Token", [])
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.service.token):
            raise APIError("Неверный или отсутствующий токен. Скопируйте токен локального сервера в настройки userscript.", 401)

    def do_OPTIONS(self):
        try:
            self._check_access(preflight=True)
            requested_method = self.headers.get("Access-Control-Request-Method", "")
            requested_headers = {value.strip().lower() for value in self.headers.get("Access-Control-Request-Headers", "").split(",") if value.strip()}
            if requested_method not in {"GET", "POST"} or not requested_headers.issubset({"content-type", "x-ytd-token"}):
                raise APIError("Недопустимый preflight-запрос.", 403)
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-YTD-Token")
            # Some Chromium versions preflight localhost with Private Network Access.
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
        except APIError as error:
            self._json(error.status, {"error": str(error)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _read_json(self) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise APIError("Потоковые тела запросов не поддерживаются.")
        if self.headers.get_content_type() != "application/json":
            raise APIError("Требуется Content-Type: application/json.", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise APIError("Укажите корректный Content-Length.", 411)
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise APIError("Укажите корректный Content-Length.")
        length = int(lengths[0])
        if length < 1 or length > MAX_BODY_BYTES:
            raise APIError("Слишком большое или пустое тело запроса.", 413)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")

            def no_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate field")
                    result[key] = value
                return result

            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")))
        except (UnicodeError, ValueError, socket.timeout):
            raise APIError("Тело запроса должно быть корректным JSON без повторяющихся полей.") from None
        if not isinstance(payload, dict) or set(payload) != {"url", "quality"}:
            raise APIError("Укажите только поля url и quality.")
        return payload

    def _dispatch(self, method: str):
        try:
            self._check_access()
            if len(self.path) > 8192:
                raise APIError("Слишком длинный запрос.", 414)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise APIError("Абсолютные HTTP-адреса не поддерживаются.")
            if method == "GET" and parsed.path == "/api/health" and not parsed.query:
                self._json(200, self.service.health())
            elif method == "GET" and parsed.path == "/api/formats":
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                if set(query) != {"url"} or len(query["url"]) != 1:
                    raise APIError("Укажите один параметр url.")
                self._json(200, self.service.formats(query["url"][0]))
            elif method == "GET" and re.fullmatch(r"/api/tasks/[a-f0-9]{32}", parsed.path) and not parsed.query:
                self._json(200, self.service.get_task(parsed.path.rsplit("/", 1)[1]))
            elif method == "POST" and parsed.path == "/api/download" and not parsed.query:
                payload = self._read_json()
                self._json(202, self.service.create_task(payload["url"], payload["quality"]))
            else:
                raise APIError("Эндпоинт не найден.", 404)
        except APIError as error:
            self._json(error.status, {"error": str(error)})
        except (ValueError, TypeError):
            self._json(400, {"error": "Некорректные параметры запроса."})
        except Exception:
            LOGGER.exception("Unexpected API failure (request data omitted)")
            self._json(500, {"error": "Внутренняя ошибка сервера. Проверьте консоль сервера."})


def make_server(service: DownloaderService, host: str = "127.0.0.1", port: int = 8765) -> LocalHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("The API may bind only to 127.0.0.1")
    return LocalHTTPServer((host, port), APIHandler, service)


def default_token_path() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "youtube-downloader" / "token"
    return Path.home() / ".config" / "youtube-downloader" / "token"


def load_or_create_token(path: str | Path) -> str:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Файл токена не должен быть символической ссылкой.")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="utf-8").strip()
        if not TOKEN_RE.fullmatch(token):
            raise ValueError("Файл токена повреждён: нужен токен из 32–256 URL-safe символов.")
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(token + "\n")
    return token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Локальный YouTube Downloader API (127.0.0.1:8765)")
    parser.add_argument("--download-dir", type=Path, default=Path.home() / "Downloads" / "YouTube")
    parser.add_argument("--token-file", type=Path, default=default_token_path())
    parser.add_argument("--show-token", action="store_true", help="Показать токен для настройки userscript и завершить работу")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        token = load_or_create_token(args.token_file)
        if args.show_token:
            print(token)
            return 0
        service = DownloaderService(args.download_dir, token)
        server = make_server(service)
    except (OSError, ValueError, RuntimeError) as error:
        LOGGER.error("Не удалось запустить сервер: %s", error)
        return 1
    LOGGER.info("API запущен на http://127.0.0.1:8765; файлы: %s", service.download_dir)
    LOGGER.info("Чтобы получить токен, выполните эту же команду с --show-token.")
    if not service.ffmpeg_available:
        LOGGER.warning("FFmpeg/FFprobe не найдены: загрузка раздельных видео и аудио недоступна.")
    if not service.js_runtime:
        LOGGER.warning("Не найден JavaScript runtime. Для актуального YouTube установите Node.js 22+.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        LOGGER.info("Завершение работы; ожидание текущих загрузок…")
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
