import os
import json
import logging
import uuid
import datetime as dt
import shutil
from functools import wraps
from urllib.parse import unquote

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_from_directory, abort, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from filelock import FileLock


# -----------------------------
# Config
# -----------------------------
DEFAULT_DATA_DIR = "/var/data"
DEFAULT_UPLOADS_DIR = "/var/data/uploads"

ALLOWED_EXTENSIONS = {
    # images
    "jpg", "jpeg", "png", "gif", "webp",
    # videos
    "mp4", "webm", "mov", "m4v", "avi", "mkv",
    # documents / archives (download-only)
    "pdf", "txt", "csv", "zip", "7z", "rar",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
}

DEFAULT_MAX_UPLOAD_MB = 500


def parse_max_content_length() -> int:
    """Return upload limit in bytes.

    Supports either:
      MAX_CONTENT_LENGTH=524288000   # bytes
      MAX_UPLOAD_MB=500              # megabytes

    If MAX_CONTENT_LENGTH is accidentally too small, Render may return a 500/413
    during video upload. The default here is intentionally generous for course videos.
    """
    raw_bytes = os.environ.get("MAX_CONTENT_LENGTH")
    if raw_bytes:
        try:
            return int(raw_bytes)
        except ValueError:
            # Do not crash the app because of a bad env var. Fall back to MB setting.
            pass

    try:
        mb = int(os.environ.get("MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB)))
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB

    return mb * 1024 * 1024


def create_app() -> Flask:
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["ADMIN_PASSWORD"] = os.environ.get("ADMIN_PASSWORD", "")
    app.config["DATA_DIR"] = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    app.config["UPLOADS_DIR"] = os.environ.get("UPLOADS_DIR", DEFAULT_UPLOADS_DIR)

    # Upload limit. Default is 500 MB; can be changed on Render via MAX_UPLOAD_MB.
    app.config["MAX_CONTENT_LENGTH"] = parse_max_content_length()

    logging.basicConfig(level=logging.INFO)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_file_too_large(error):
        max_mb = int(app.config["MAX_CONTENT_LENGTH"] / 1024 / 1024)
        flash(f"Файл слишком большой. Текущий лимит загрузки: {max_mb} MB.", "error")
        return redirect(request.referrer or url_for("admin_new"))

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        # Keep real traceback in Render Logs, but show a clean message in the browser.
        app.logger.exception("Unexpected server error: %s", error)
        flash("Ошибка сервера при сохранении. Проверь Render Logs: лимит файла, путь /var/data, свободное место или расширение файла.", "error")
        return redirect(request.referrer or url_for("index"))

    ensure_dirs(app)

    @app.route("/")
    def index():
        cards = load_cards(app)
        cards.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return render_template("index.html", cards=cards, is_admin=is_admin())

    @app.route("/uploads/<card_id>/<path:filename>")
    def uploaded_file(card_id: str, filename: str):
        safe_card = sanitize_id(card_id)
        if not safe_card:
            abort(404)

        folder = os.path.join(app.config["UPLOADS_DIR"], safe_card)
        return send_from_directory(folder, filename, as_attachment=False)

    # -----------------------------
    # Admin auth
    # -----------------------------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if not app.config["ADMIN_PASSWORD"]:
                flash("ADMIN_PASSWORD не задан. Укажи переменную окружения.", "error")
                return redirect(url_for("admin_login"))

            if password == app.config["ADMIN_PASSWORD"]:
                session["is_admin"] = True
                flash("Вход выполнен.", "ok")
                return redirect(url_for("admin_new"))

            flash("Неверный пароль.", "error")

        return render_template("admin_login.html", is_admin=is_admin())

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Вы вышли.", "ok")
        return redirect(url_for("index"))

    def admin_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_admin():
                return redirect(url_for("admin_login"))
            return fn(*args, **kwargs)
        return wrapper

    @app.route("/admin/new", methods=["GET", "POST"])
    @admin_required
    def admin_new():
        """Create a lightweight card first.

        Heavy files are uploaded later through /admin/upload/<card_id> as a
        separate streaming request. This avoids sending title/description and a
        large video in the same multipart/form-data request.
        """
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_new"))

            card_id = uuid.uuid4().hex[:10]  # short id
            created_at = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

            card_folder = os.path.join(app.config["UPLOADS_DIR"], card_id)
            os.makedirs(card_folder, exist_ok=True)

            card = {
                "id": card_id,
                "created_at": created_at,
                "title": title,
                "description": description,
                "files": [],
            }

            append_card(app, card)
            flash("Карточка создана. Теперь загрузи PDF/видео отдельным блоком ниже.", "ok")
            return redirect(url_for("admin_edit", card_id=card_id))

        return render_template("admin_new.html", is_admin=is_admin())

    @app.post("/admin/delete/<card_id>")
    @admin_required
    def admin_delete(card_id: str):
        safe = sanitize_id(card_id)
        if not safe:
            abort(404)

        deleted = delete_card(app, safe)
        if not deleted:
            flash("Карточка не найдена.", "error")
            return redirect(url_for("index"))

        folder = os.path.join(app.config["UPLOADS_DIR"], safe)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)

        flash("Карточка удалена.", "ok")
        return redirect(url_for("index"))



    @app.route("/admin/edit/<card_id>", methods=["GET", "POST"])
    @admin_required
    def admin_edit(card_id: str):
        safe = sanitize_id(card_id)
        if not safe:
            abort(404)

        card = get_card(app, safe)
        if not card:
            abort(404)

        if request.method == "POST":
            # This form is intentionally text-only. Files are uploaded through
            # /admin/upload/<card_id> so a big video cannot block metadata save.
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()

            if not title:
                flash("Заполни поле «Название».", "error")
                return redirect(url_for("admin_edit", card_id=safe))

            card["title"] = title
            card["description"] = description

            if update_card(app, safe, card):
                flash("Карточка обновлена.", "ok")
            else:
                flash("Не удалось обновить карточку.", "error")

            return redirect(url_for("admin_edit", card_id=safe))

        return render_template("admin_edit.html", card=card, is_admin=is_admin())

    @app.post("/admin/upload/<card_id>")
    @admin_required
    def admin_upload_file(card_id: str):
        """Stream one uploaded file into /var/data/uploads/<card_id>/.

        Frontend sends the selected file as the raw request body and passes the
        original filename in X-File-Name. We do NOT call request.form or
        request.files here, so Werkzeug does not need to parse a large multipart
        request before the app can start saving data.
        """
        safe = sanitize_id(card_id)
        if not safe:
            return jsonify({"ok": False, "error": "bad_card_id"}), 404

        card = get_card(app, safe)
        if not card:
            return jsonify({"ok": False, "error": "card_not_found"}), 404

        original = request.headers.get("X-File-Name") or request.args.get("filename") or ""
        original = unquote(original)
        filename = normalize_upload_filename(original)
        if not filename:
            return jsonify({"ok": False, "error": "empty_filename"}), 400
        if not allowed_file(filename):
            return jsonify({"ok": False, "error": f"unsupported_extension: {original}"}), 400

        content_length = request.content_length or 0
        max_len = int(app.config.get("MAX_CONTENT_LENGTH") or 0)
        if max_len and content_length and content_length > max_len:
            max_mb = int(max_len / 1024 / 1024)
            return jsonify({"ok": False, "error": f"file_too_large_limit_{max_mb}_mb"}), 413

        card_folder = os.path.join(app.config["UPLOADS_DIR"], safe)
        os.makedirs(card_folder, exist_ok=True)
        filename = unique_filename(card_folder, filename)
        save_path = os.path.join(card_folder, filename)

        bytes_written = 0
        try:
            with open(save_path, "wb") as out:
                while True:
                    chunk = request.stream.read(1024 * 1024)  # 1 MB
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if max_len and bytes_written > max_len:
                        out.close()
                        try:
                            os.remove(save_path)
                        except OSError:
                            pass
                        max_mb = int(max_len / 1024 / 1024)
                        return jsonify({"ok": False, "error": f"file_too_large_limit_{max_mb}_mb"}), 413
                    out.write(chunk)
        except Exception as exc:
            app.logger.exception("Streaming upload failed for %s to %s", original, save_path)
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except OSError:
                pass
            return jsonify({"ok": False, "error": str(exc)}), 500

        if bytes_written <= 0:
            try:
                os.remove(save_path)
            except OSError:
                pass
            return jsonify({"ok": False, "error": "empty_upload"}), 400

        file_entry = {
            "name": filename,
            "url": url_for("uploaded_file", card_id=safe, filename=filename),
            "ext": filename.rsplit(".", 1)[-1].lower(),
            "size_bytes": bytes_written,
        }

        if not append_file_to_card(app, safe, file_entry):
            try:
                os.remove(save_path)
            except OSError:
                pass
            return jsonify({"ok": False, "error": "could_not_update_card"}), 500

        app.logger.info("Uploaded file %s to card %s: %.2f MB", filename, safe, bytes_written / 1024 / 1024)
        return jsonify({"ok": True, "file": file_entry})

    @app.post("/admin/delete-file/<card_id>")
    @admin_required
    def admin_delete_file(card_id: str):
        safe = sanitize_id(card_id)
        if not safe:
            abort(404)

        filename = request.form.get("filename", "")
        if not filename:
            flash("Файл не указан.", "error")
            return redirect(url_for("admin_edit", card_id=safe))

        ok = delete_file_from_card(app, safe, filename)
        if ok:
            flash("Файл удалён.", "ok")
        else:
            flash("Не удалось удалить файл.", "error")

        return redirect(url_for("admin_edit", card_id=safe))

    return app


# -----------------------------
# Helpers
# -----------------------------
def ensure_dirs(app: Flask) -> None:
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    os.makedirs(app.config["UPLOADS_DIR"], exist_ok=True)

def sanitize_id(value: str) -> str:
    if not value:
        return ""
    value = value.lower()
    if all(c in "0123456789abcdef" for c in value) and 8 <= len(value) <= 32:
        return value
    return ""

def is_admin() -> bool:
    return bool(session.get("is_admin"))

def allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in ALLOWED_EXTENSIONS

def normalize_upload_filename(original: str) -> str:
    """Create a safe filename while preserving the extension.

    Werkzeug's secure_filename may strip non-Latin names completely. For files
    like "видео.mp4", keep the extension and generate a stable ASCII fallback.
    """
    original = original or ""
    safe = secure_filename(original)

    # If the whole name was Cyrillic/Unicode, secure_filename can return just
    # "mp4" or an empty string. Keep the original extension and create a name.
    ext = ""
    if "." in original:
        ext = original.rsplit(".", 1)[-1].lower().strip()
        ext = secure_filename(ext)

    if not safe or "." not in safe:
        if ext:
            return f"file_{uuid.uuid4().hex[:8]}.{ext}"
        return f"file_{uuid.uuid4().hex[:8]}"

    return safe


def unique_filename(folder: str, filename: str) -> str:
    base, dot, ext = filename.rpartition(".")
    if not dot:
        base, ext = filename, ""
    candidate = filename
    i = 2
    while os.path.exists(os.path.join(folder, candidate)):
        candidate = f"{base}_{i}.{ext}" if ext else f"{base}_{i}"
        i += 1
    return candidate

def cards_csv_path(app: Flask) -> str:
    # фактически JSONL (по строке JSON на карточку), оставляем имя submissions.csv как привычное
    return os.path.join(app.config["DATA_DIR"], "submissions.csv")

def load_cards(app: Flask):
    path = cards_csv_path(app)
    if not os.path.exists(path):
        return []
    cards = []
    lock = FileLock(path + ".lock")
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cards.append(json.loads(line))
                except Exception:
                    continue
    return cards


def get_card(app: Flask, card_id: str):
    """Return a single card dict by id or None."""
    for c in load_cards(app):
        if c.get("id") == card_id:
            return c
    return None

def update_card(app: Flask, card_id: str, new_card: dict) -> bool:
    """Replace a card by id in submissions.csv (JSONL). Returns True if updated."""
    path = cards_csv_path(app)
    if not os.path.exists(path):
        return False

    lock = FileLock(path + ".lock")
    updated = False
    kept = []

    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                if obj.get("id") == card_id:
                    kept.append(json.dumps(new_card, ensure_ascii=False))
                    updated = True
                else:
                    kept.append(json.dumps(obj, ensure_ascii=False))

        with open(path, "w", encoding="utf-8") as f:
            for l in kept:
                f.write(l + "\n")

    return updated

def delete_file_from_card(app: Flask, card_id: str, filename: str) -> bool:
    """Delete a file from disk and remove it from card's file list. Returns True if deleted."""
    safe_id = sanitize_id(card_id)
    if not safe_id:
        return False

    safe_name = secure_filename(filename)
    if not safe_name:
        return False

    card = get_card(app, safe_id)
    if not card:
        return False

    files = card.get("files") or []
    # keep only entries not matching filename
    new_files = [f for f in files if f.get("name") != safe_name]
    if len(new_files) == len(files):
        return False  # not found in record

    # delete from disk (only within card folder)
    folder = os.path.join(app.config["UPLOADS_DIR"], safe_id)
    path = os.path.join(folder, safe_name)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

    card["files"] = new_files
    return update_card(app, safe_id, card)

def append_file_to_card(app: Flask, card_id: str, file_entry: dict) -> bool:
    """Append one file entry to an existing card in submissions.csv (JSONL)."""
    safe_id = sanitize_id(card_id)
    if not safe_id:
        return False

    path = cards_csv_path(app)
    if not os.path.exists(path):
        return False

    lock = FileLock(path + ".lock")
    updated = False
    kept = []

    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                if obj.get("id") == safe_id:
                    files = obj.get("files") or []
                    files.append(file_entry)
                    obj["files"] = files
                    updated = True

                kept.append(json.dumps(obj, ensure_ascii=False))

        with open(path, "w", encoding="utf-8") as f:
            for l in kept:
                f.write(l + "\n")

    return updated


def append_card(app: Flask, card: dict) -> None:
    path = cards_csv_path(app)
    lock = FileLock(path + ".lock")
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")


def delete_card(app: Flask, card_id: str):
    """Delete a card by id from submissions.csv (JSONL). Returns deleted card dict or None."""
    path = cards_csv_path(app)
    if not os.path.exists(path):
        return None

    lock = FileLock(path + ".lock")
    deleted = None
    kept = []

    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                if obj.get("id") == card_id:
                    deleted = obj
                    continue

                kept.append(json.dumps(obj, ensure_ascii=False))

        with open(path, "w", encoding="utf-8") as f:
            for l in kept:
                f.write(l + "\n")

    return deleted


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
