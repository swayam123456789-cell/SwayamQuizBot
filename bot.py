import os
import re
import uuid
import random
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("swayamquizbot")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN. Add it in Render > Environment.")

TIMER_CHOICES = {
    "10": 10, "15": 15, "30": 30, "45": 45,
    "1m": 60, "2m": 120, "3m": 180, "4m": 240, "5m": 300,
}
DEFAULT_TIMER = 15

@dataclass
class Question:
    english: str
    odia: str
    options: Dict[str, str]
    correct: str

@dataclass
class Quiz:
    quiz_id: str
    title: str
    questions: List[Question]
    chat_id: int
    owner_id: int
    timer: int = DEFAULT_TIMER
    shuffle_questions: bool = False
    shuffle_options: bool = False
    index: int = 0
    paused: bool = False
    stopped: bool = False
    scores: Dict[int, int] = field(default_factory=dict)
    names: Dict[int, str] = field(default_factory=dict)
    answered: set = field(default_factory=set)
    current_message_id: Optional[int] = None
    timer_job: object = None

quizzes_by_chat: Dict[int, Quiz] = {}
quizzes_by_id: Dict[str, Quiz] = {}
# Published quiz definitions are held in memory; redeploy/restart clears them.
published_quizzes: Dict[str, Quiz] = {}
bot_username: Optional[str] = None

def parse_mcqs(text: str) -> List[Question]:
    """English line first; optional Odia line below. Each option may have a second translation line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    starts = list(re.finditer(r"(?im)^\s*Q(?:uestion)?\s*\d+\s*[.):\-]\s*", text))
    if not starts:
        raise ValueError("Each question must begin with Q1. or Q2.")
    chunks = [
        text[m.start():(starts[i + 1].start() if i + 1 < len(starts) else len(text))].strip()
        for i, m in enumerate(starts)
    ]
    result = []
    for chunk in chunks:
        lines = [line.strip() for line in chunk.split("\n")]
        q_en = re.sub(r"(?i)^\s*Q(?:uestion)?\s*\d+\s*[.):\-]\s*", "", lines[0]).strip()
        if not q_en:
            raise ValueError("A question has no text.")
        i = 1
        q_odia = ""
        if i < len(lines) and lines[i] and not re.match(r"^[A-Da-d]\s*[.):\-]", lines[i]) and not re.match(r"(?i)^(correct\s*answer|answer)\s*:", lines[i]):
            q_odia = lines[i]
            i += 1
        raw_options, current, answer = {}, None, None
        while i < len(lines):
            line = lines[i]
            ans = re.match(r"(?i)^(?:correct\s*answer|answer)\s*:\s*([A-D])\s*$", line)
            opt = re.match(r"^([A-Da-d])\s*[.):\-]\s*(.*)$", line)
            if ans:
                answer = ans.group(1).upper()
                current = None
            elif opt:
                current = opt.group(1).upper()
                raw_options[current] = [opt.group(2).strip()]
            elif current and line:
                raw_options[current].append(line)
            elif line:
                raise ValueError(f"Could not understand line in question '{q_en}': {line}")
            i += 1
        if set(raw_options) != set("ABCD"):
            raise ValueError(f"Question '{q_en}' must have options A, B, C and D.")
        if not answer:
            raise ValueError(f"Question '{q_en}' needs 'Correct Answer: B'.")
        opts = {}
        for key in "ABCD":
            vals = raw_options[key]
            opts[key] = vals[0] + (("\n" + vals[1]) if len(vals) > 1 else "")
        result.append(Question(q_en, q_odia, opts, answer))
    if not result:
        raise ValueError("No valid questions found.")
    return result

def parse_duration(value: str) -> int:
    key = value.lower().strip()
    if key in TIMER_CHOICES:
        return TIMER_CHOICES[key]
    if key.isdigit() and int(key) in TIMER_CHOICES.values():
        return int(key)
    raise ValueError("Timer must be 10, 15, 30, 45 seconds or 1m, 2m, 3m, 4m, 5m.")

def format_duration(seconds: int) -> str:
    return f"{seconds} sec" if seconds < 60 else f"{seconds // 60} min"

def render_question(q: Question, idx: int, total: int, quiz: Quiz):
    body = f"📝 {quiz.title}\nQuestion {idx + 1}/{total}\n\n{q.english}"
    if q.odia:
        body += "\n" + q.odia
    body += f"\n\n⏱ Time: {format_duration(quiz.timer)}"
    keys = list("ABCD")
    if quiz.shuffle_options:
        random.shuffle(keys)
    keyboard = [[InlineKeyboardButton(q.options[k], callback_data=f"ans|{quiz.quiz_id}|{idx}|{k}")]
                for k in keys]
    return body, InlineKeyboardMarkup(keyboard)

async def cancel_timer(quiz: Quiz):
    if quiz.timer_job:
        quiz.timer_job.schedule_removal()
        quiz.timer_job = None

async def send_current(quiz: Quiz, context: ContextTypes.DEFAULT_TYPE):
    if quiz.stopped:
        return
    if quiz.paused:
        return
    if quiz.index >= len(quiz.questions):
        await finish_quiz(quiz, context, "Quiz completed")
        return
    await cancel_timer(quiz)
    q = quiz.questions[quiz.index]
    body, markup = render_question(q, quiz.index, len(quiz.questions), quiz)
    msg = await context.bot.send_message(quiz.chat_id, body, reply_markup=markup)
    quiz.current_message_id = msg.message_id
    quiz.timer_job = context.job_queue.run_once(
        timer_expired, when=quiz.timer,
        data={"quiz_id": quiz.quiz_id, "index": quiz.index},
        name=f"quiz-{quiz.quiz_id}",
    )

async def timer_expired(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    quiz = quizzes_by_id.get(data["quiz_id"])
    if not quiz or quiz.stopped or quiz.paused or quiz.index != data["index"]:
        return
    try:
        if quiz.current_message_id:
            await context.bot.edit_message_reply_markup(
                chat_id=quiz.chat_id, message_id=quiz.current_message_id, reply_markup=None
            )
    except Exception:
        pass
    quiz.index += 1
    await send_current(quiz, context)

async def finish_quiz(quiz: Quiz, context: ContextTypes.DEFAULT_TYPE, reason: str):
    await cancel_timer(quiz)
    quiz.stopped = True
    lines = [f"🏁 {reason}: {quiz.title}", "🏆 Top 10 Results", ""]
    ranking = sorted(quiz.scores.items(), key=lambda x: (-x[1], quiz.names.get(x[0], "").casefold()))
    if not ranking:
        lines.append("No answers recorded.")
    for rank, (uid, score) in enumerate(ranking[:10], 1):
        lines.append(f"{rank}. {quiz.names.get(uid, 'Participant')} — {score}/{len(quiz.questions)}")
    try:
        await context.bot.send_message(quiz.chat_id, "\n".join(lines))
    except Exception:
        log.exception("Could not post results")
    quizzes_by_chat.pop(quiz.chat_id, None)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /start <quiz_id> lets a user open a published quiz in a private chat.
    if context.args:
        quiz_id = context.args[0]
        source = published_quizzes.get(quiz_id)
        if source:
            user_chat = update.effective_chat.id
            clone = Quiz(
                quiz_id=uuid.uuid4().hex[:8], title=source.title,
                questions=list(source.questions), chat_id=user_chat,
                owner_id=update.effective_user.id, timer=source.timer,
                shuffle_questions=source.shuffle_questions,
                shuffle_options=source.shuffle_options,
            )
            quizzes_by_chat[user_chat] = clone
            quizzes_by_id[clone.quiz_id] = clone
            await update.effective_message.reply_text(f"Quiz opened: {clone.title}\nStarting now.")
            await send_current(clone, context)
            return
    await update.effective_message.reply_text(
        "Welcome to SwayamQuizbot!\n\n"
        "Upload a .txt MCQ file here or post it in a channel where I am admin.\n"
        "The quiz starts automatically after the file is parsed.\n\n"
        "Commands: /settings, /timer 15, /shuffle all, /shuffle questions, "
        "/shuffle options, /pause, /resume, /next, /leaderboard, /share, /stop\n"
        "Default timer: 15 seconds."
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    doc = msg.document if msg else None
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        return
    try:
        tgfile = await context.bot.get_file(doc.file_id)
        content = bytes(await tgfile.download_as_bytearray()).decode("utf-8-sig")
        questions = parse_mcqs(content)
    except Exception as exc:
        await msg.reply_text(f"❌ Could not load quiz file:\n{exc}")
        return
    chat = update.effective_chat
    if chat.id in quizzes_by_chat and not quizzes_by_chat[chat.id].stopped:
        await msg.reply_text("There is already an active quiz in this chat. Use /stop first.")
        return

    title = os.path.splitext(doc.file_name)[0].replace("_", " ").strip() or "Practice Quiz"
    timer = DEFAULT_TIMER
    # Apply optional per-chat settings saved in bot_data.
    settings = context.application.bot_data.setdefault("settings", {}).get(chat.id, {})
    timer = settings.get("timer", DEFAULT_TIMER)
    shuffle_questions = settings.get("shuffle_questions", False)
    shuffle_options = settings.get("shuffle_options", False)
    if shuffle_questions:
        random.shuffle(questions)
    quiz = Quiz(
        quiz_id=uuid.uuid4().hex[:8], title=title, questions=questions,
        chat_id=chat.id, owner_id=update.effective_user.id if update.effective_user else 0,
        timer=timer, shuffle_questions=shuffle_questions, shuffle_options=shuffle_options,
    )
    quizzes_by_chat[chat.id] = quiz
    quizzes_by_id[quiz.quiz_id] = quiz
    await msg.reply_text(
        f"✅ Quiz created: {title}\n{len(questions)} questions · ⏱ {format_duration(timer)}\n"
        "Starting now. Use /pause, /resume or /stop to control it."
    )
    await send_current(quiz, context)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("10 sec", callback_data="cfg|timer|10"),
         InlineKeyboardButton("15 sec", callback_data="cfg|timer|15"),
         InlineKeyboardButton("30 sec", callback_data="cfg|timer|30")],
        [InlineKeyboardButton("45 sec", callback_data="cfg|timer|45"),
         InlineKeyboardButton("1 min", callback_data="cfg|timer|1m"),
         InlineKeyboardButton("2 min", callback_data="cfg|timer|2m")],
        [InlineKeyboardButton("3 min", callback_data="cfg|timer|3m"),
         InlineKeyboardButton("4 min", callback_data="cfg|timer|4m"),
         InlineKeyboardButton("5 min", callback_data="cfg|timer|5m")],
        [InlineKeyboardButton("Shuffle all", callback_data="cfg|shuffle|all"),
         InlineKeyboardButton("No shuffle", callback_data="cfg|shuffle|none")],
        [InlineKeyboardButton("Only questions", callback_data="cfg|shuffle|questions"),
         InlineKeyboardButton("Only answers", callback_data="cfg|shuffle|options")],
    ]
    await update.effective_message.reply_text(
        "Quiz settings\nChoose the default timer and shuffle mode before uploading a .txt file.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, kind, value = query.data.split("|")
    except ValueError:
        return
    chat_id = query.message.chat.id
    settings = context.application.bot_data.setdefault("settings", {}).setdefault(chat_id, {})
    if kind == "timer":
        settings["timer"] = parse_duration(value)
        await query.edit_message_text(f"Default timer set to {format_duration(settings['timer'])}.")
    elif kind == "shuffle":
        settings["shuffle_questions"] = value in ("all", "questions")
        settings["shuffle_options"] = value in ("all", "options")
        if value == "none":
            settings["shuffle_questions"] = settings["shuffle_options"] = False
        await query.edit_message_text(
            f"Shuffle settings saved: questions={'ON' if settings['shuffle_questions'] else 'OFF'}, "
            f"options={'ON' if settings['shuffle_options'] else 'OFF'}."
        )

async def timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Use /timer 15 (or 10, 30, 45, 1m, 2m, 3m, 4m, 5m).")
        return
    try:
        seconds = parse_duration(context.args[0])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    settings = context.application.bot_data.setdefault("settings", {}).setdefault(update.effective_chat.id, {})
    settings["timer"] = seconds
    await update.effective_message.reply_text(f"Default timer set to {format_duration(seconds)} for the next quiz.")

async def shuffle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Use /shuffle all, /shuffle questions, /shuffle options, or /shuffle off.")
        return
    value = context.args[0].lower()
    settings = context.application.bot_data.setdefault("settings", {}).setdefault(update.effective_chat.id, {})
    if value == "all":
        settings["shuffle_questions"] = settings["shuffle_options"] = True
    elif value == "questions":
        settings["shuffle_questions"] = True
    elif value == "options":
        settings["shuffle_options"] = True
    elif value in ("off", "none"):
        settings["shuffle_questions"] = settings["shuffle_options"] = False
    else:
        await update.effective_message.reply_text("Choose all, questions, options, or off.")
        return
    await update.effective_message.reply_text(
        f"Shuffle saved: questions={'ON' if settings.get('shuffle_questions') else 'OFF'}, "
        f"options={'ON' if settings.get('shuffle_options') else 'OFF'}."
    )

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = quizzes_by_chat.get(update.effective_chat.id)
    if not quiz or quiz.stopped:
        await update.effective_message.reply_text("No active quiz.")
        return
    quiz.paused = True
    await cancel_timer(quiz)
    await update.effective_message.reply_text("⏸ Quiz paused. Use /resume to continue.")

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = quizzes_by_chat.get(update.effective_chat.id)
    if not quiz or quiz.stopped:
        await update.effective_message.reply_text("No active quiz.")
        return
    if not quiz.paused:
        await update.effective_message.reply_text("Quiz is already running.")
        return
    quiz.paused = False
    await update.effective_message.reply_text("▶️ Quiz resumed.")
    await send_current(quiz, context)

async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = quizzes_by_chat.get(update.effective_chat.id)
    if not quiz or quiz.stopped:
        await update.effective_message.reply_text("No active quiz.")
        return
    if quiz.paused:
        await update.effective_message.reply_text("Quiz is paused. Use /resume first.")
        return
    await cancel_timer(quiz)
    try:
        if quiz.current_message_id:
            await context.bot.edit_message_reply_markup(
                chat_id=quiz.chat_id, message_id=quiz.current_message_id, reply_markup=None
            )
    except Exception:
        pass
    quiz.index += 1
    await send_current(quiz, context)

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = quizzes_by_chat.get(update.effective_chat.id)
    if not quiz:
        await update.effective_message.reply_text("No quiz data in this chat.")
        return
    ranking = sorted(quiz.scores.items(), key=lambda x: (-x[1], quiz.names.get(x[0], "").casefold()))
    lines = ["🏆 Top 10 Leaderboard"]
    if not ranking:
        lines.append("No answers recorded yet.")
    for rank, (uid, score) in enumerate(ranking[:10], 1):
        lines.append(f"{rank}. {quiz.names.get(uid, 'Participant')} — {score}/{len(quiz.questions)}")
    await update.effective_message.reply_text("\n".join(lines))

async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = quizzes_by_chat.get(update.effective_chat.id)
    if not quiz:
        await update.effective_message.reply_text("Upload a .txt quiz file first, then use /share.")
        return
    published_quizzes[quiz.quiz_id] = quiz
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={quiz.quiz_id}"
    await update.effective_message.reply_text(
        f"🔗 Share this quiz:\n{link}\n\nPeople can open the link in a private chat with the bot."
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz = quizzes_by_chat.get(update.effective_chat.id)
    if not quiz:
        await update.effective_message.reply_text("No active quiz.")
        return
    await finish_quiz(quiz, context, "Quiz stopped")

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, quiz_id, idx_s, choice = query.data.split("|")
        idx = int(idx_s)
    except Exception:
        return
    quiz = quizzes_by_id.get(quiz_id)
    if not quiz or quiz.stopped or quiz.paused or quiz.index != idx:
        await query.answer("This question is no longer active.", show_alert=True)
        return
    user = query.from_user
    answer_key = (idx, user.id)
    if answer_key in quiz.answered:
        await query.answer("You already answered this question.", show_alert=True)
        return
    quiz.answered.add(answer_key)
    quiz.names[user.id] = user.full_name
    quiz.scores.setdefault(user.id, 0)
    if choice == quiz.questions[idx].correct:
        quiz.scores[user.id] += 1
        feedback = "✅ Correct!"
    else:
        feedback = f"❌ Incorrect. Correct answer: {quiz.questions[idx].correct}"
    await query.answer(feedback, show_alert=True)
    # Private practice: move on immediately after the user answers. In groups/channels,
    # the common timer governs the question for all participants.
    if query.message and query.message.chat.type == "private":
        await cancel_timer(quiz)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        quiz.index += 1
        await send_current(quiz, context)

async def post_init(app: Application):
    global bot_username
    me = await app.bot.get_me()
    bot_username = me.username
    log.info("Started as @%s", bot_username)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("timer", timer_command))
    app.add_handler(CommandHandler("shuffle", shuffle_command))
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("share", share_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(config_callback, pattern=r"^cfg\|"))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^ans\|"))
    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
