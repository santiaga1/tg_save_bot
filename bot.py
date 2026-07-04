"""
Telegram-бот для автоматической отправки видео из Instagram, TikTok и YouTube Shorts.

Как работает:
1. Бот получает каждое текстовое сообщение в группе.
2. Ищет в тексте ссылки на Instagram, TikTok или YouTube Shorts.
3. Скачивает первое найденное видео через yt-dlp.
4. Отправляет скачанный файл обратно в тот же чат.

Перед запуском:
- создайте .env по примеру .env.example;
- добавьте бота в группу;
- отключите privacy mode у бота в BotFather, иначе бот не увидит обычные
  сообщения участников группы.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


# Логирование нужно не только для ошибок: по нему удобно понять, какие ссылки
# бот увидел, что скачал и почему мог пропустить сообщение.
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# Регулярное выражение достает URL из обычного текста.
# После извлечения мы дополнительно проверяем домен, чтобы не трогать чужие ссылки.
URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)

# Домены, которые бот считает поддерживаемыми.
# Список можно расширить, если yt-dlp начнет поддерживать новые короткие домены.
SUPPORTED_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
    "www.instagr.am",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}

YOUTUBE_SHORTS_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
}

TELEGRAM_VIDEO_LIMIT_MB = 50
TELEGRAM_VIDEO_LIMIT_BYTES = TELEGRAM_VIDEO_LIMIT_MB * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    """Настройки приложения, считанные из переменных окружения."""

    bot_token: str
    max_video_mb: int
    cookies_file: Path | None

    @property
    def max_video_bytes(self) -> int:
        """Лимит размера файла в байтах."""

        return self.max_video_mb * 1024 * 1024


@dataclass(frozen=True)
class DownloadedVideo:
    """Результат скачивания видео."""

    path: Path
    title: str | None


def load_settings() -> Settings:
    """Загружает настройки из .env и окружения."""

    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN. Создайте .env по примеру .env.example.")

    max_video_mb_raw = os.getenv("MAX_VIDEO_MB", "45").strip()
    try:
        max_video_mb = int(max_video_mb_raw)
    except ValueError as exc:
        raise RuntimeError("MAX_VIDEO_MB должен быть целым числом.") from exc

    cookies_file_raw = os.getenv("COOKIES_FILE", "").strip()
    cookies_file = Path(cookies_file_raw) if cookies_file_raw else None

    if cookies_file and not cookies_file.exists():
        raise RuntimeError(f"Файл cookies не найден: {cookies_file}")

    return Settings(
        bot_token=bot_token,
        max_video_mb=max_video_mb,
        cookies_file=cookies_file,
    )


def is_supported_url(url: str) -> bool:
    """Проверяет, относится ли ссылка к поддерживаемым форматам видео."""

    parsed_url = urlparse(url)
    hostname = parsed_url.hostname.lower() if parsed_url.hostname else ""

    if hostname in SUPPORTED_DOMAINS:
        return True

    if hostname in YOUTUBE_SHORTS_DOMAINS:
        return parsed_url.path.startswith("/shorts/")

    return False


def extract_supported_urls(text: str) -> list[str]:
    """
    Достает из текста ссылки Instagram/TikTok/YouTube Shorts.

    Telegram может прислать ссылку с пунктуацией в конце, например:
    "смотри https://vm.tiktok.com/abc/."
    Поэтому после regex мы чистим хвостовые символы.
    """

    urls: list[str] = []

    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,!?;:)]}")

        if is_supported_url(url):
            urls.append(url)

    return urls


def build_ydl_options(download_dir: Path, settings: Settings) -> dict:
    """
    Собирает настройки yt-dlp.

    Важно: yt-dlp синхронный и может работать несколько секунд. Ниже мы запускаем
    его в отдельном потоке через asyncio.to_thread, чтобы не блокировать Telegram-бота.
    """

    options = {
        # Скачиваем один лучший mp4-файл, если он доступен. Если платформа отдает
        # видео/аудио отдельно, yt-dlp попробует собрать их через ffmpeg.
        "format": "best[ext=mp4]/best",
        "outtmpl": str(download_dir / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Этот лимит не гарантирует, что файл всегда будет меньше лимита Telegram,
        # но помогает yt-dlp заранее отказаться от слишком больших роликов.
        "max_filesize": settings.max_video_bytes,
    }

    if settings.cookies_file:
        options["cookiefile"] = str(settings.cookies_file)

    return options


def find_downloaded_file(before: Iterable[Path], download_dir: Path) -> Path:
    """
    Находит файл, который появился после работы yt-dlp.

    yt-dlp сам выбирает итоговое расширение и имя файла, поэтому надежнее сравнить
    содержимое папки "до" и "после", чем пытаться заранее угадать путь.
    """

    before_set = set(before)
    after_set = {path for path in download_dir.iterdir() if path.is_file()}
    new_files = sorted(after_set - before_set, key=lambda path: path.stat().st_mtime)

    if not new_files:
        raise RuntimeError("yt-dlp не создал видеофайл.")

    return new_files[-1]


def download_video_sync(url: str, settings: Settings) -> DownloadedVideo:
    """
    Синхронно скачивает видео и возвращает путь к файлу.

    Функция специально отделена от async-кода: так проще контролировать временную
    папку, параметры yt-dlp и ошибки скачивания.
    """

    download_dir = Path(tempfile.mkdtemp(prefix="tg-video-"))
    before = list(download_dir.iterdir())

    try:
        with YoutubeDL(build_ydl_options(download_dir, settings)) as ydl:
            info = ydl.extract_info(url, download=True)

        video_path = find_downloaded_file(before, download_dir)
        return DownloadedVideo(path=video_path, title=info.get("title") if info else None)
    except Exception:
        shutil.rmtree(download_dir, ignore_errors=True)
        raise


async def download_video(url: str, settings: Settings) -> DownloadedVideo:
    """Запускает скачивание в отдельном рабочем потоке."""

    return await asyncio.to_thread(download_video_sync, url, settings)


def cleanup_download(video: DownloadedVideo) -> None:
    """Удаляет временную папку со скачанным файлом."""

    shutil.rmtree(video.path.parent, ignore_errors=True)


def format_file_size_mb(file_size: int) -> str:
    """Форматирует размер файла в мегабайтах для сообщений пользователю."""

    return f"{file_size / 1024 / 1024:.1f} MB"


def is_telegram_file_too_large_error(exc: TelegramError) -> bool:
    """Проверяет, похожа ли ошибка Telegram на отказ из-за размера файла."""

    message = str(exc).lower()
    return (
        "file is too big" in message
        or "file_too_big" in message
        or "request entity too large" in message
    )


def is_ydl_file_too_large_error(exc: DownloadError) -> bool:
    """Проверяет, отказался ли yt-dlp скачивать файл из-за max_filesize."""

    message = str(exc).lower()
    return (
        "max-filesize" in message
        or "max_filesize" in message
        or "larger than" in message and "file" in message
    )


async def show_video_too_large_message(status_message: Message, file_size: int) -> None:
    """Показывает понятную ошибку, когда видео нельзя отправить из-за размера."""

    await status_message.edit_text(
        f"Видео получилось слишком большим: {format_file_size_mb(file_size)}. "
        f"Telegram не дает боту отправлять видео больше {TELEGRAM_VIDEO_LIMIT_MB} MB."
    )


async def show_download_too_large_message(
    status_message: Message,
    settings: Settings,
) -> None:
    """Показывает ошибку, когда yt-dlp не скачал видео из-за лимита размера."""

    await status_message.edit_text(
        f"Видео слишком большое и не может быть отправлено. "
        f"Бот не стал его скачивать, потому что лимит сейчас: {settings.max_video_mb} MB."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на /start в личке или группе."""

    if update.message:
        await update.message.reply_text(
            "Готов отслеживать Instagram и TikTok ссылки. "
            "Добавьте меня в группу и отключите privacy mode в BotFather."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Основной обработчик сообщений группы."""

    message = update.effective_message
    if not message:
        return

    text = message.text or message.caption
    if not text:
        return

    urls = extract_supported_urls(text)
    if not urls:
        return

    # Чтобы не заспамить чат, обрабатываем первую поддерживаемую ссылку из сообщения.
    # При желании здесь можно пройтись циклом по всем urls.
    url = urls[0]
    settings: Settings = context.application.bot_data["settings"]

    logger.info("Найдена ссылка в чате %s: %s", update.effective_chat.id, url)
    await message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    status_message = await message.reply_text("Скачиваю видео...")
    video: DownloadedVideo | None = None

    try:
        video = await download_video(url, settings)
        file_size = video.path.stat().st_size

        if file_size > TELEGRAM_VIDEO_LIMIT_BYTES:
            await show_video_too_large_message(status_message, file_size)
            return

        if file_size > settings.max_video_bytes:
            await status_message.edit_text(
                f"Видео получилось слишком большим: {format_file_size_mb(file_size)}. "
                f"Лимит сейчас: {settings.max_video_mb} MB."
            )
            return

        caption = video.title[:900] if video.title else None
        with video.path.open("rb") as video_file:
            await message.reply_video(
                video=video_file,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

        await status_message.delete()
    except TelegramError as exc:
        if video and is_telegram_file_too_large_error(exc):
            logger.warning("Telegram отказался отправить слишком большое видео: %s", url)
            await show_video_too_large_message(status_message, video.path.stat().st_size)
            return

        logger.exception("Не удалось отправить видео по ссылке %s", url)
        await status_message.edit_text(
            "Не получилось отправить видео в Telegram. Попробуйте ссылку на ролик поменьше."
        )
    except DownloadError as exc:
        if is_ydl_file_too_large_error(exc):
            logger.warning("yt-dlp отказался скачивать слишком большое видео: %s", url)
            await show_download_too_large_message(status_message, settings)
            return

        logger.exception("yt-dlp не смог скачать видео по ссылке %s", url)
        await status_message.edit_text(
            "Не получилось скачать видео. "
            "Возможно, ссылка приватная, нужен cookies-файл или платформа временно изменила защиту."
        )
    except Exception as exc:
        logger.exception("Не удалось обработать ссылку %s", url)
        await status_message.edit_text(
            "Не получилось скачать видео. "
            "Возможно, ссылка приватная, нужен cookies-файл или платформа временно изменила защиту."
        )
    finally:
        if video:
            cleanup_download(video)


def build_application(settings: Settings) -> Application:
    """Создает Telegram Application и регистрирует обработчики."""

    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", start))

    # Слушаем все сообщения: в handle_message мы сами проверяем text/caption.
    # Так бот поймает ссылку и в обычном сообщении, и в подписи к медиа.
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    return application


def main() -> None:
    """Точка входа приложения."""

    settings = load_settings()
    application = build_application(settings)

    logger.info("Бот запущен. Остановить можно Ctrl+C.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
