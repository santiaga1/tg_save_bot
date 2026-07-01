# Telegram video saver bot

Бот следит за сообщениями в группе. Если кто-то отправляет ссылку на Instagram или TikTok видео, бот скачивает ролик через `yt-dlp` и отправляет видео обратно в тот же чат.

## Важные условия

1. Добавьте бота в группу.
2. В BotFather отключите privacy mode: `Bot Settings` -> `Group Privacy` -> `Turn off`.
3. Дайте боту право читать сообщения в группе.
4. Для Instagram может понадобиться cookies-файл, иначе часть ссылок не будет скачиваться.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Откройте `.env` и вставьте `BOT_TOKEN`.

## Запуск

```bash
python bot.py
```

## Cookies для Instagram/TikTok

Если `yt-dlp` пишет, что видео недоступно, требует логин или не может скачать Instagram, экспортируйте cookies из браузера в файл `cookies.txt`, положите его в папку проекта и укажите:

```env
COOKIES_FILE=cookies.txt
```

Используйте cookies только своего аккаунта и не публикуйте этот файл.
