import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, Update
from telegram.error import Unauthorized
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler, Filters, MessageHandler, Updater

BASE_DIR = Path(__file__).resolve().parent
CONTENT_PATH = BASE_DIR / "content.txt"
VIDEOS_DIR = BASE_DIR / "videos"
DB_PATH = BASE_DIR / "users.db"

# Заполни file_id для каждого видео (после загрузки в Telegram)
FILE_ID_MAP = {
    "1": "BAACAgIAAxkBAAIBG2lnYDlUKaeVt8uXlw2rpNYDThFyAALAggACXCU5S3Ub1VLNB04VOAQ",
    "2": "BAACAgIAAxkBAAIBHWlnYH8XJ3ABnnooQM5lZA0cTm3gAALHggACXCU5Syzpb2AJPS3ZOAQ",
    "3": "BAACAgIAAxkBAAIBH2lnYMoVKRuPXON3GWW8Je3UuMCsAALRggACXCU5SxTCPQ44P9HVOAQ",
}


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_step INTEGER,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _ensure_columns(conn, "users", {"last_step": "INTEGER", "completed_at": "TEXT"})
        conn.commit()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for column, col_type in columns.items():
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def upsert_user(user) -> None:
    if not user:
        return
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            UPDATE users SET
                username=?,
                first_name=?,
                last_name=?,
                language_code=?,
                is_active=1,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=?
            """,
            (
                user.username,
                user.first_name,
                user.last_name,
                user.language_code,
                user.id,
            ),
        )
        if cur.rowcount == 0:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, language_code, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    user.id,
                    user.username,
                    user.first_name,
                    user.last_name,
                    user.language_code,
                ),
            )
        conn.commit()


def set_user_inactive(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (user_id,),
        )
        conn.commit()


def update_user_progress(user_id: int, last_step: int, completed: bool = False) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE users SET
                last_step=?,
                completed_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=?
            """,
            (last_step, 1 if completed else 0, user_id),
        )
        conn.commit()


@dataclass
class Step:
    text: str
    button: Optional[str]
    videos: List[Path]


def _load_token() -> str:
    token = os.getenv("BOT_TOKEN")
    if token:
        return token

    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("BOT_TOKEN="):
                return line.split("=", 1)[1].strip()

    raise RuntimeError("BOT_TOKEN is not set. Add it to .env or environment variables.")


def _load_admin_ids() -> List[int]:
    raw = os.getenv("ADMIN_IDS")
    if not raw:
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("ADMIN_IDS="):
                    raw = line.split("=", 1)[1].strip()
                    break
    if not raw:
        return []
    ids: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids

def _clean_chunk_text(chunk: str) -> str:
    lines = []
    for line in chunk.splitlines():
        if re.search(r"^\s*Кнопка \[.+?\]\s*$", line):
            continue
        line = re.sub(r"\[video \d+\]", "", line)
        lines.append(line)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _bold_first_line(text: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip():
            lines[i] = f"<b>{line}</b>"
            break
    return "\n".join(lines)


def load_steps(content_path: Path, videos_dir: Path) -> List[Step]:
    raw = content_path.read_text(encoding="utf-8")
    chunks = [c.strip() for c in raw.split("________________") if c.strip()]

    steps: List[Step] = []

    for chunk in chunks:
        button_match = re.search(r"Кнопка \[(.+?)\]", chunk)
        button_label = button_match.group(1).strip() if button_match else None

        videos: List[Path] = []
        for video_match in re.finditer(r"\[video (\d+)\]", chunk):
            video_num = video_match.group(1)
            video_path = videos_dir / f"video {video_num}.mp4"
            videos.append(video_path)

        cleaned = _clean_chunk_text(chunk)
        steps.append(Step(text=cleaned, button=button_label, videos=videos))

    return steps


def _split_text(text: str, max_len: int = 3500) -> List[str]:
    if len(text) <= max_len:
        return [text]

    parts: List[str] = []
    current = ""
    for para in text.split("\n\n"):
        if not current:
            current = para
            continue

        if len(current) + 2 + len(para) <= max_len:
            current = f"{current}\n\n{para}"
        else:
            parts.append(current)
            current = para

    if current:
        parts.append(current)

    final_parts: List[str] = []
    for part in parts:
        if len(part) <= max_len:
            final_parts.append(part)
            continue
        for i in range(0, len(part), max_len):
            final_parts.append(part[i : i + max_len])
    return final_parts


def send_step(chat_id: int, context: CallbackContext, step: Step, step_index: int) -> None:
    keyboard = None
    if step.button:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(step.button, callback_data=f"step:{step_index}")]]
        )

    has_videos = len(step.videos) > 0

    if step.text:
        text = _bold_first_line(step.text)
        chunks = _split_text(text)
        for i, chunk in enumerate(chunks):
            attach_keyboard = (not has_videos) and keyboard and i == len(chunks) - 1
            try:
                context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard if attach_keyboard else None,
                )
            except Unauthorized:
                set_user_inactive(chat_id)
                return

    for i, video_path in enumerate(step.videos):
        attach_keyboard = keyboard and i == len(step.videos) - 1
        file_id = FILE_ID_MAP.get(video_path.stem.split()[-1])
        if file_id:
            try:
                context.bot.send_video(
                    chat_id=chat_id,
                    video=file_id,
                    reply_markup=keyboard if attach_keyboard else None,
                )
            except Unauthorized:
                set_user_inactive(chat_id)
                return
            except Exception:
                try:
                    context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Не удалось отправить видео: {video_path.name}",
                        reply_markup=keyboard if attach_keyboard else None,
                    )
                except Unauthorized:
                    set_user_inactive(chat_id)
                    return
            continue
        if not video_path.exists():
            try:
                context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Видео не найдено: {video_path.name}",
                    reply_markup=keyboard if attach_keyboard else None,
                )
            except Unauthorized:
                set_user_inactive(chat_id)
            continue
        try:
            with video_path.open("rb") as f:
                context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    reply_markup=keyboard if attach_keyboard else None,
                )
        except Unauthorized:
            set_user_inactive(chat_id)
            return
        except Exception:
            try:
                context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Не удалось отправить видео: {video_path.name}",
                    reply_markup=keyboard if attach_keyboard else None,
                )
            except Unauthorized:
                set_user_inactive(chat_id)
                return

    if not step.text and not step.videos:
        try:
            context.bot.send_message(chat_id=chat_id, text="(Пустой шаг)", reply_markup=keyboard)
        except Unauthorized:
            set_user_inactive(chat_id)


def send_from_index(chat_id: int, context: CallbackContext, index: int) -> None:
    steps = context.bot_data["steps"]
    idx = index
    while idx < len(steps):
        step = steps[idx]
        send_step(chat_id, context, step, idx)
        if step.button:
            context.user_data["step_index"] = idx
            update_user_progress(chat_id, idx, completed=False)
            return
        idx += 1
    context.user_data["step_index"] = len(steps)
    update_user_progress(chat_id, len(steps), completed=True)


def start(update: Update, context: CallbackContext) -> None:
    upsert_user(update.effective_user)
    context.user_data["step_index"] = 0
    send_from_index(update.effective_chat.id, context, 0)


def reset(update: Update, context: CallbackContext) -> None:
    upsert_user(update.effective_user)
    start(update, context)


def handle_text(update: Update, context: CallbackContext) -> None:
    return


def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if not query:
        return
    upsert_user(query.from_user)
    query.answer()
    data = query.data or ""
    if not data.startswith("step:"):
        return
    try:
        idx = int(data.split(":", 1)[1])
    except ValueError:
        return
    send_from_index(query.message.chat_id, context, idx + 1)


def handle_media(update: Update, context: CallbackContext) -> None:
    message = update.message
    if not message:
        return
    upsert_user(message.from_user)
    if message.video:
        message.reply_text(f"video file_id: {message.video.file_id}")
        return
    if message.document:
        message.reply_text(f"document file_id: {message.document.file_id}")
        return


def _is_admin(update: Update, context: CallbackContext) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id in context.bot_data.get("admin_ids", set())


def _require_admin(update: Update, context: CallbackContext) -> bool:
    if _is_admin(update, context):
        return True
    if update.message:
        update.message.reply_text("Нет доступа.")
    return False


def stats(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update, context):
        return
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute("SELECT COALESCE(SUM(is_active), 0) FROM users").fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM users WHERE completed_at IS NOT NULL"
        ).fetchone()[0]
    update.message.reply_text(
        f"Пользователей: {total}\nАктивных: {active}\nДошли до конца: {completed}"
    )


def user_card(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update, context):
        return
    if not context.args:
        update.message.reply_text("Использование: /user <id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        update.message.reply_text("ID должен быть числом.")
        return
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT user_id, username, first_name, last_name, language_code,
                   is_active, last_step, completed_at, created_at, updated_at
            FROM users WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
    if not row:
        update.message.reply_text("Пользователь не найден.")
        return
    (
        uid,
        username,
        first_name,
        last_name,
        language_code,
        is_active,
        last_step,
        completed_at,
        created_at,
        updated_at,
    ) = row
    update.message.reply_text(
        "Пользователь:\n"
        f"ID: {uid}\n"
        f"username: {username}\n"
        f"first_name: {first_name}\n"
        f"last_name: {last_name}\n"
        f"language: {language_code}\n"
        f"active: {is_active}\n"
        f"last_step: {last_step}\n"
        f"completed_at: {completed_at}\n"
        f"created_at: {created_at}\n"
        f"updated_at: {updated_at}"
    )


def broadcast(update: Update, context: CallbackContext) -> None:
    if not _require_admin(update, context):
        return
    text = " ".join(context.args).strip()
    if not text:
        update.message.reply_text("Использование: /broadcast <текст>")
        return
    with sqlite3.connect(DB_PATH) as conn:
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users WHERE is_active=1")]
    sent = 0
    failed = 0
    for uid in user_ids:
        try:
            context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Unauthorized:
            set_user_inactive(uid)
            failed += 1
        except Exception:
            failed += 1
    update.message.reply_text(f"Готово. Отправлено: {sent}, ошибок: {failed}")


def main() -> None:
    token = _load_token()
    admin_ids = set(_load_admin_ids())
    steps = load_steps(CONTENT_PATH, VIDEOS_DIR)
    if not steps:
        raise RuntimeError("content.txt does not contain any steps.")
    init_db()

    updater = Updater(token=token, use_context=True)
    dispatcher = updater.dispatcher
    dispatcher.bot_data["steps"] = steps
    dispatcher.bot_data["admin_ids"] = admin_ids

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("reset", reset))
    dispatcher.add_handler(CommandHandler("stats", stats))
    dispatcher.add_handler(CommandHandler("broadcast", broadcast))
    dispatcher.add_handler(CommandHandler("user", user_card))
    dispatcher.add_handler(CallbackQueryHandler(handle_callback))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    dispatcher.add_handler(MessageHandler(Filters.video | Filters.document, handle_media))

    print("Bot is running...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
