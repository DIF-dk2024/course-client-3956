# Квартирный Прогноз — учебный портал клиента

Простая Flask-доска карточек:
- публичная страница со списком карточек: название, описание, вложения;
- создание/редактирование карточек только через админку;
- вложения: видео, PDF, изображения и документы;
- хранение данных и загрузок на Render Persistent Disk: `/var/data`.

## Что изменено в версии v3

Большие файлы больше не отправляются вместе с HTML-формой карточки.

Теперь логика такая:
1. `/admin/new` создаёт лёгкую карточку: только название и описание.
2. `/admin/edit/<card_id>` сохраняет текст отдельно.
3. Файлы грузятся отдельным AJAX-запросом на `/admin/upload/<card_id>`.
4. Загрузчик показывает прогресс по каждому файлу.

Это убирает проблему, когда Flask/Gunicorn зависал на `request.form` при большом `multipart/form-data` и Render показывал `WORKER TIMEOUT`.

## Где что лежит

- Карточки: `/var/data/submissions.csv` — фактически JSONL, одна JSON-строка на карточку.
- Файлы: `/var/data/uploads/<card_id>/<file>`.

## Переменные окружения

Обязательные:

```text
SECRET_KEY=длинный_случайный_ключ
ADMIN_PASSWORD=твой_админский_пароль
DATA_DIR=/var/data
UPLOADS_DIR=/var/data/uploads
MAX_UPLOAD_MB=500
```

`MAX_UPLOAD_MB` задаёт лимит загрузки в мегабайтах. Можно также использовать `MAX_CONTENT_LENGTH` в байтах, но проще оставить `MAX_UPLOAD_MB`.

## Локальный запуск

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

set SECRET_KEY=dev
set ADMIN_PASSWORD=1234
set DATA_DIR=./data
set UPLOADS_DIR=./data/uploads
set MAX_UPLOAD_MB=500
python app.py
```

Открой: http://127.0.0.1:5000

## Deploy на Render

1. Залей репозиторий на GitHub.
2. Render → New → Web Service → выбери репозиторий.
3. Plan: `Starter` или выше.
4. Persistent Disk:
   - Name: `data`
   - Mount Path: `/var/data`
   - Size: лучше `5 GB` или больше.
5. Environment Variables:
   - `SECRET_KEY`
   - `ADMIN_PASSWORD`
   - `DATA_DIR=/var/data`
   - `UPLOADS_DIR=/var/data/uploads`
   - `MAX_UPLOAD_MB=500`
6. Start Command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --worker-class gthread --workers 1 --threads 4 --timeout 600 --graceful-timeout 600 --access-logfile - --error-logfile - --log-level info
```

## Админка

- `/admin/login` — вход.
- `/admin/new` — создать новую карточку.
- `/admin/edit/<id>` — редактировать текст и загрузить файлы через отдельный загрузчик.
- `/admin/upload/<id>` — внутренний endpoint для загрузки файла.
