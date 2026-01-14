import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from telegram import ParseMode, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import CallbackContext, CommandHandler, Filters, MessageHandler, Updater

BASE_DIR = Path(__file__).resolve().parent
CONTENT_PATH = BASE_DIR / "content.txt"
VIDEOS_DIR = BASE_DIR / "videos"

# Заполни file_id для каждого видео (после загрузки в Telegram)
FILE_ID_MAP = {
    "1": "BAACAgIAAxkBAAIBG2lnYDlUKaeVt8uXlw2rpNYDThFyAALAggACXCU5S3Ub1VLNB04VOAQ",
    "2": "BAACAgIAAxkBAAIBHWlnYH8XJ3ABnnooQM5lZA0cTm3gAALHggACXCU5Syzpb2AJPS3ZOAQ",
    "3": "BAACAgIAAxkBAAIBH2lnYMoVKRuPXON3GWW8Je3UuMCsAALRggACXCU5SxTCPQ44P9HVOAQ",
}


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


def send_step(chat_id: int, context: CallbackContext, step: Step) -> None:
    keyboard = None
    if step.button:
        keyboard = ReplyKeyboardMarkup([[step.button]], resize_keyboard=True, one_time_keyboard=False)
    else:
        keyboard = ReplyKeyboardRemove()

    if step.text:
        text = _bold_first_line(step.text)
        for i, chunk in enumerate(_split_text(text)):
            context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard if i == 0 else None,
            )
        keyboard = None

    for video_path in step.videos:
        if not video_path.exists():
            context.bot.send_message(
                chat_id=chat_id,
                text=f"Видео не найдено: {video_path.name}",
                reply_markup=keyboard,
            )
            keyboard = None
            continue
        file_id = FILE_ID_MAP.get(video_path.stem.split()[-1])
        try:
            if file_id:
                context.bot.send_video(
                    chat_id=chat_id,
                    video=file_id,
                    reply_markup=keyboard,
                )
            else:
                with video_path.open("rb") as f:
                    context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        reply_markup=keyboard,
                    )
        except Exception:
            context.bot.send_message(
                chat_id=chat_id,
                text=f"Не удалось отправить видео: {video_path.name}",
                reply_markup=keyboard,
            )
        keyboard = None

    if not step.text and not step.videos:
        context.bot.send_message(chat_id=chat_id, text="(Пустой шаг)", reply_markup=keyboard)


def send_from_index(chat_id: int, context: CallbackContext, index: int) -> None:
    steps = context.bot_data["steps"]
    idx = index
    while idx < len(steps):
        step = steps[idx]
        send_step(chat_id, context, step)
        if step.button:
            context.user_data["step_index"] = idx
            return
        idx += 1
    context.bot.send_message(chat_id=chat_id, text="Курс завершен. Спасибо!")
    context.user_data["step_index"] = len(steps)


def start(update: Update, context: CallbackContext) -> None:
    context.user_data["step_index"] = 0
    send_from_index(update.effective_chat.id, context, 0)


def reset(update: Update, context: CallbackContext) -> None:
    start(update, context)


def handle_message(update: Update, context: CallbackContext) -> None:
    steps = context.bot_data["steps"]
    idx = context.user_data.get("step_index", 0)

    if idx >= len(steps):
        update.message.reply_text("Курс завершен. Напиши /start чтобы начать снова.")
        return

    expected = steps[idx].button
    if expected and update.message.text.strip() != expected:
        return

    idx += 1
    if idx >= len(steps):
        update.message.reply_text("Курс завершен. Спасибо!", reply_markup=ReplyKeyboardRemove())
        context.user_data["step_index"] = idx
        return

    send_from_index(update.effective_chat.id, context, idx)


def handle_media(update: Update, context: CallbackContext) -> None:
    message = update.message
    if not message:
        return
    if message.video:
        message.reply_text(f"video file_id: {message.video.file_id}")
        return
    if message.document:
        message.reply_text(f"document file_id: {message.document.file_id}")
        return


def main() -> None:
    token = _load_token()
    steps = load_steps(CONTENT_PATH, VIDEOS_DIR)
    if not steps:
        raise RuntimeError("content.txt does not contain any steps.")

    updater = Updater(token=token, use_context=True)
    dispatcher = updater.dispatcher
    dispatcher.bot_data["steps"] = steps

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("reset", reset))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.video | Filters.document, handle_media))

    print("Bot is running...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
