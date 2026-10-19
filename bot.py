import os
import re
import sqlite3
import secrets
import logging
import asyncio
import time
from threading import Thread
from collections import defaultdict

from flask import Flask
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, PollAnswerHandler,
    ContextTypes, filters
)

# ==========================================================
# SwayamQuizBot - simple quiz creator, native quiz polls,
# saved results, leaderboard, edit title, share/run commands.
# Render Environment: BOT_TOKEN, OWNER_ID
# ==========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("SwayamQuizBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_TEXT = os.getenv("OWNER_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "swayam_quiz.db")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN in Render Environment.")
if not OWNER_ID_TEXT.isdigit():
    raise RuntimeError("OWNER_ID is missing/invalid. Add your numeric Telegram user ID.")
OWNER_ID = int(OWNER_ID_TEXT)

ACTIVE_POLLS = {}
ACTIVE_SESSIONS = {}

web = Flask(__name__)

@web.get("/")
def home():
    return "SwayamQuizBot is running", 200

@web.get("/health")
def health():
    return {"status": "ok"}, 200


def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            timer INTEGER NOT NULL DEFAULT 20,
            correct_marks REAL NOT NULL DEFAULT 1,
            negative_marks REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            q_no INTEGER NOT NULL,
            question TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            answer TEXT NOT NULL,
            UNIQUE(quiz_id, q_no)
        );
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            correct INTEGER NOT NULL DEFAULT 0,
            wrong INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            total INTEGER NOT NULL DEFAULT 0,
            elapsed REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quiz_id, user_id)
        );
        """)


def parse_mcqs(raw_text):
    lines = [line.strip() for line in raw_text.replace("\r", "\n").splitlines()]
    result, current = [], None
    qpat = re.compile(r"^(?:Q(?:UESTION)?\s*\d*\s*[.):\-]\s*|\d+[.)]\s+)(.+)$", re.I)
    opat = re.compile(r"^([A-D])[).:\-]\s*(.+)$", re.I)
    apat = re.compile(r"^(?:correct\s*answer|answer)\s*[:\-]\s*([A-D])\b", re.I)

    def finish():
        nonlocal current
        if current and all(k in current for k in "ABCD") and current.get("answer"):
            result.append({
                "question": current["question"].strip(),
                "a": current["A"].strip(), "b": current["B"].strip(),
                "c": current["C"].strip(), "d": current["D"].strip(),
                "answer": current["answer"]
            })
        current = None

    for line in lines:
        if not line:
            continue
        qm = qpat.match(line)
        if qm:
            finish()
            current = {"question": qm.group(1).strip()}
            continue
        if current is None:
            continue
        am = apat.match(line)
        if am:
            current["answer"] = am.group(1).upper()
            continue
        om = opat.match(line)
        if om:
            letter, value = om.group(1).upper(), om.group(2).strip()
            if any(mark in value for mark in ("✅", "✔️", "✔", "☑️", "☑")):
                current["answer"] = letter
                value = re.sub(r"\s*(?:✅|✔️?|☑️?)\s*", "", value).strip()
            current[letter] = value
    finish()
    return result


def create_quiz(title, questions):
    code = secrets.token_hex(3).upper()
    with db_connect() as con:
        cur = con.execute(
            "INSERT INTO quizzes(code,owner_id,title,timer) VALUES(?,?,?,20)",
            (code, OWNER_ID, title[:120])
        )
        quiz_id = cur.lastrowid
        for n, q in enumerate(questions, 1):
            con.execute(
                """INSERT INTO questions
                   (quiz_id,q_no,question,option_a,option_b,option_c,option_d,answer)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (quiz_id, n, q["question"][:300], q["a"][:100], q["b"][:100],
                 q["c"][:100], q["d"][:100], q["answer"])
            )
    return code, quiz_id


def get_quiz_by_code(code):
    with db_connect() as con:
        return con.execute("SELECT * FROM quizzes WHERE code=?", (code.upper(),)).fetchone()


def main_menu_text():
    return (
        "🤖 SwayamQuizBot — Main Menu\n\n"
        "/start — Bot ଆରମ୍ଭ ଓ menu\n"
        "/create — ନୂଆ Quiz ତିଆରି\n"
        "/done — Quiz save କରି ସମ୍ପୂର୍ଣ୍ଣ\n"
        "/cancel — Draft ବାତିଲ୍\n"
        "/quizzes — Saved quizzes ଦେଖିବା\n"
        "/run QUIZ_ID — Quiz ଖେଳିବା\n"
        "/leaderboard QUIZ_ID — Top players ଓ ranking\n"
        "/result QUIZ_ID — Result summary\n"
        "/edit QUIZ_ID — Quiz ନାମ edit\n"
        "/share QUIZ_ID — Shareable Quiz Link 🔗\n"
        "/delete QUIZ_ID — Quiz delete\n"
        "/help — MCQ format"
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Telegram deep link format: https://t.me/BOT_USERNAME?start=quiz_QUIZCODE
    # Keep normal /start behavior unchanged in private chats and groups.
    if context.args and update.effective_chat and update.effective_chat.type == "private":
        payload = context.args[0].strip()
        match = re.fullmatch(r"quiz_([A-Za-z0-9]+)", payload, flags=re.I)
        if match:
            await start_quiz_by_code(update, context, match.group(1).upper())
            return
    await update.effective_message.reply_text(main_menu_text())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📋 MCQ କୁ ଏହିଭଳି copy-paste କରନ୍ତୁ:\n\n"
        "Q1. ଭାରତର ରାଜଧାନୀ କ'ଣ?\n"
        "A) ମୁମ୍ବାଇ\nB) ନୂଆଦିଲ୍ଲୀ ✅\nC) କୋଲକାତା\nD) ଚେନ୍ନାଇ\n\n"
        "Q2. ଓଡ଼ିଶାର ରାଜଧାନୀ କ'ଣ?\n"
        "A) କଟକ\nB) ପୁରୀ\nC) ଭୁବନେଶ୍ୱର ✅\nD) ସମ୍ବଲପୁର\n\n"
        "ସଠିକ୍ option ପାଖରେ ✅ ରଖନ୍ତୁ। ଅନେକ MCQ ଏକାଥରେ paste କରନ୍ତୁ।\n"
        "ପ୍ରଶ୍ନ ଯୋଡ଼ି ସାରିଲେ /done ଦିଅନ୍ତୁ।"
    )


async def create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("ଏହି command କେବଳ bot owner ପାଇଁ।")
        return
    context.user_data["quiz_draft"] = {"title": None, "questions": []}
    context.user_data["waiting_quiz_title"] = True
    await update.effective_message.reply_text("🏷️ Send the quiz name.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    context.user_data.pop("quiz_draft", None)
    context.user_data.pop("waiting_quiz_title", None)
    await update.effective_message.reply_text("❌ Quiz creation cancelled.")


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    draft = context.user_data.get("quiz_draft")
    if not draft:
        await update.effective_message.reply_text("କୌଣସି draft ନାହିଁ। /create ଦିଅନ୍ତୁ।")
        return
    if not draft["questions"]:
        await update.effective_message.reply_text("ପ୍ରଥମେ MCQ paste କରନ୍ତୁ।")
        return
    code, qid = create_quiz(draft["title"] or "My Quiz", draft["questions"])
    title, total = draft["title"] or "My Quiz", len(draft["questions"])
    context.user_data.pop("quiz_draft", None)
    context.user_data.pop("waiting_quiz_title", None)
    bot_username = context.bot.username or (await context.bot.get_me()).username
    quiz_link = f"https://t.me/{bot_username}?start=quiz_{code}"
    share_url = (
        "https://t.me/share/url?url="
        + __import__("urllib.parse", fromlist=["quote"]).quote(quiz_link, safe="")
        + "&text="
        + __import__("urllib.parse", fromlist=["quote"]).quote(f"📝 {title}\nQuiz ଖେଳିବା ପାଇଁ link ଖୋଲନ୍ତୁ:", safe="")
    )
    group_url = (
        "https://t.me/share/url?url="
        + __import__("urllib.parse", fromlist=["quote"]).quote(quiz_link, safe="")
        + "&text="
        + __import__("urllib.parse", fromlist=["quote"]).quote(f"/run {code}", safe="")
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Start this quiz", url=quiz_link)],
        [InlineKeyboardButton("➕ Start quiz in group", url=group_url)],
        [InlineKeyboardButton("↗️ Share quiz", url=share_url)],
    ])
    await update.effective_message.reply_text(
        "🎉 Quiz Created!\n━━━━━━━━━━━━━━\n"
        f"🏷️ Name: {title}\n❓ Questions: {total}\n⏱️ Timer: 20s\n"
        f"🆔 ID: {code}\n🏷️ Type: Free 🆓\n"
        "✅ Correct marks: 1\n➖ Negative marks: 0\n"
        "🔀 Shuffle: questions OFF, options OFF\n📣 Promo: None\n"
        f"👤 Creator: {update.effective_user.full_name}\n\n"
        f"▶️ Start command: /run {code}\n🏆 Leaderboard: /leaderboard {code}\n\n"
        f"🔗 Share Link: {quiz_link}\n\n👇 ତଳର button ବାଛନ୍ତୁ:",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    if not user or user.id != OWNER_ID or not msg or not msg.text:
        return
    draft = context.user_data.get("quiz_draft")
    if not draft:
        return
    if context.user_data.get("waiting_quiz_title"):
        draft["title"] = msg.text.strip()[:120]
        context.user_data["waiting_quiz_title"] = False
        await msg.reply_text(
            f"🏷️ Name: {draft['title']}\n"
            "➡️ Send questions or paste MCQs.\n"
            "Correct option ପାଖରେ ✅ ରଖନ୍ତୁ। /done ଦେଲେ Quiz save ହେବ।"
        )
        return
    parsed = parse_mcqs(msg.text)
    if not parsed:
        await msg.reply_text(
            "❌ Invalid format.\n\nQ1. Question?\nA) Option A\nB) Option B ✅\nC) Option C\nD) Option D\n\n"
            "ସଠିକ୍ option ପାଖରେ ✅ ରଖି ପୁଣି paste କରନ୍ତୁ। /help ଦେଖନ୍ତୁ।"
        )
        return
    draft["questions"].extend(parsed)
    await msg.reply_text(f"✅ {len(draft['questions'])} saved! ➡️ Send more or /done")


async def quizzes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    with db_connect() as con:
        rows = con.execute(
            """SELECT q.code,q.title,COUNT(x.id) AS total FROM quizzes q
               LEFT JOIN questions x ON x.quiz_id=q.id
               WHERE q.owner_id=? GROUP BY q.id ORDER BY q.id DESC LIMIT 30""",
            (OWNER_ID,)
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("Saved quiz ନାହିଁ। /create ଦିଅନ୍ତୁ।")
        return
    await update.effective_message.reply_text(
        "📚 Your Quizzes\n\n" + "\n\n".join(
            f"🏷️ {r['title']}\n❓ Questions: {r['total']}\n🆔 ID: {r['code']}\n/run {r['code']}"
            for r in rows
        )
    )


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /edit QUIZ_ID New Quiz Name")
        return
    code = context.args[0].upper()
    title = " ".join(context.args[1:])[:120]
    with db_connect() as con:
        cur = con.execute("UPDATE quizzes SET title=? WHERE code=? AND owner_id=?", (title, code, OWNER_ID))
    await update.effective_message.reply_text("✏️ Quiz name updated." if cur.rowcount else "Quiz ID ମିଳିଲା ନାହିଁ।")


async def share_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /share QUIZ_ID")
        return
    code = context.args[0].upper()
    quiz = get_quiz_by_code(code)
    if not quiz:
        await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।")
        return
    bot_username = context.bot.username
    if not bot_username:
        bot = await context.bot.get_me()
        bot_username = bot.username
    link = f"https://t.me/{bot_username}?start=quiz_{code}"
    await update.effective_message.reply_text(
        f"🔗 Quiz Share Link\n\n"
        f"📝 {quiz['title']}\n"
        f"▶️ ଏହି link ଖୋଲି Quiz ଆରମ୍ଭ କରନ୍ତୁ:\n{link}\n\n"
        f"Group ରେ ପୁରୁଣା command ମଧ୍ୟ ଚାଲିବ: /run {code}",
        disable_web_page_preview=True
    )


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /delete QUIZ_ID")
        return
    code = context.args[0].upper()
    with db_connect() as con:
        row = con.execute("SELECT id FROM quizzes WHERE code=? AND owner_id=?", (code, OWNER_ID)).fetchone()
        if not row:
            await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।")
            return
        con.execute("DELETE FROM questions WHERE quiz_id=?", (row["id"],))
        con.execute("DELETE FROM results WHERE quiz_id=?", (row["id"],))
        con.execute("DELETE FROM quizzes WHERE id=?", (row["id"],))
    await update.effective_message.reply_text(f"🗑️ Quiz {code} deleted.")


async def render_leaderboard(context, chat_id, quiz_id, title):
    with db_connect() as con:
        rows = con.execute(
            "SELECT user_name,correct,total FROM results WHERE quiz_id=? ORDER BY score DESC,correct DESC,elapsed ASC LIMIT 10",
            (quiz_id,)
        ).fetchall()
    lines = [f"🏆 Leaderboard — {title}", ""]
    if rows:
        lines += [f"{i}. {r['user_name']} — {r['correct']}/{r['total']}" for i, r in enumerate(rows, 1)]
    else:
        lines.append("କୌଣସି player answer ଦେଇନାହାନ୍ତି।")
    lines += ["", "ଅଭ୍ୟାସ ହିଁ ସଫଳତା ର ଚାବିକାଠି"]
    await context.bot.send_message(chat_id=chat_id, text="\\n".join(lines))


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /run QUIZ_ID")
        return
    await start_quiz_by_code(update, context, context.args[0].upper())


async def start_quiz_by_code(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    code = code.upper()
    quiz = get_quiz_by_code(code)
    if not quiz:
        await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ। /quizzes ଦେଖନ୍ତୁ।")
        return
    with db_connect() as con:
        questions = con.execute("SELECT * FROM questions WHERE quiz_id=? ORDER BY q_no", (quiz["id"],)).fetchall()
    if not questions:
        await update.effective_message.reply_text("ଏହି Quiz ରେ ପ୍ରଶ୍ନ ନାହିଁ।")
        return
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text(
        f"🚀 Starting quiz: {quiz['title']}\\n📊 Total Questions: {len(questions)}\\n⏱️ Timer: {quiz['timer']}s"
    )
    for digit in ("3️⃣", "2️⃣", "1️⃣"):
        await update.effective_message.reply_text(digit)
        await asyncio.sleep(1)
    session_key = (quiz["id"], chat_id)
    ACTIVE_SESSIONS[session_key] = {"scores": {}, "total": len(questions), "title": quiz["title"], "started": time.monotonic()}
    timer = max(5, min(600, int(quiz["timer"])))
    for row in questions:
        correct_id = "ABCD".index(row["answer"].upper())
        try:
            msg = await context.bot.send_poll(
                chat_id=chat_id, question=f"[{row['q_no']}/{len(questions)}] {row['question']}"[:300],
                options=[row["option_a"], row["option_b"], row["option_c"], row["option_d"]],
                type=Poll.QUIZ, correct_option_id=correct_id, is_anonymous=False, open_period=timer
            )
            ACTIVE_POLLS[msg.poll.id] = {"quiz_id": quiz["id"], "chat_id": chat_id, "correct_id": correct_id, "q_no": row["q_no"]}
        except Exception as exc:
            log.exception("Failed to send poll")
            await update.effective_message.reply_text(f"❌ Poll ପଠାଇହେଲା ନାହିଁ: {exc}")
            ACTIVE_SESSIONS.pop(session_key, None)
            return
        await asyncio.sleep(timer + 1)
    session = ACTIVE_SESSIONS.pop(session_key)
    elapsed = int(time.monotonic() - session["started"])
    with db_connect() as con:
        for uid, player in session["scores"].items():
            con.execute(
                """INSERT INTO results (quiz_id,user_id,user_name,correct,wrong,score,total,elapsed)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(quiz_id,user_id) DO UPDATE SET
                user_name=excluded.user_name,correct=excluded.correct,wrong=excluded.wrong,
                score=excluded.score,total=excluded.total,elapsed=excluded.elapsed,updated_at=CURRENT_TIMESTAMP""",
                (quiz["id"], uid, player["name"], player["correct"], player["wrong"],
                 player["correct"], len(questions), elapsed)
            )
    await update.effective_message.reply_text(f"🏁 Quiz Completed!\\n📝 {quiz['title']}\\n📊 Total Questions: {len(questions)}")
    await render_leaderboard(context, chat_id, quiz["id"], quiz["title"])


async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /result QUIZ_ID")
        return
    code = context.args[0].upper()
    quiz = get_quiz_by_code(code)
    if not quiz:
        await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।")
        return
    with db_connect() as con:
        rows = con.execute(
            "SELECT COUNT(*) AS players, COALESCE(MAX(score),0) AS top_score FROM results WHERE quiz_id=?",
            (quiz["id"],)
        ).fetchone()
    await update.effective_message.reply_text(
        f"📊 Result Summary\n📝 {quiz['title']}\n"
        f"👥 Recorded players: {rows['players']}\n🏆 Top score: {rows['top_score']}\n"
        "ଟିପ୍ପଣୀ: detailed player scoring ପାଇଁ answer tracking ଆବଶ୍ୟକ।"
    )


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /leaderboard QUIZ_ID")
        return
    code = context.args[0].upper()
    quiz = get_quiz_by_code(code)
    if not quiz:
        await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।")
        return
    with db_connect() as con:
        rows = con.execute(
            """SELECT user_name,correct,wrong,score,total,elapsed FROM results
               WHERE quiz_id=? ORDER BY score DESC,correct DESC,elapsed ASC LIMIT 10""",
            (quiz["id"],)
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(
            f"🏆 Leaderboard: {quiz['title']}\n\nଏଯାଏଁ recorded score ନାହିଁ।\n"
            "ଟିପ୍ପଣୀ: ଏହି version ରେ poll result ଦେଖାଯାଏ; full per-user score tracking ଆଗକୁ ଯୋଡ଼ିବାକୁ ପଡ଼ିବ।"
        )
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        rank = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{rank} {r['user_name']} — {r['score']:.2f}/{r['total']} (✅{r['correct']} ❌{r['wrong']})")
    await update.effective_message.reply_text(f"🏆 Leaderboard: {quiz['title']}\n\n" + "\n".join(lines))


async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer or not answer.user:
        return
    info = ACTIVE_POLLS.get(answer.poll_id)
    if not info:
        return
    session = ACTIVE_SESSIONS.get((info["quiz_id"], info["chat_id"]))
    if not session:
        return
    player = session["scores"].setdefault(answer.user.id, {"name": answer.user.full_name, "correct": 0, "wrong": 0, "answered": set()})
    player["name"] = answer.user.full_name
    if info["q_no"] in player["answered"]:
        return
    player["answered"].add(info["q_no"])
    if answer.option_ids and answer.option_ids[0] == info["correct_id"]:
        player["correct"] += 1
    else:
        player["wrong"] += 1


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


def run_web():
    web.run(host="0.0.0.0", port=PORT, use_reloader=False)


def main():
    init_db()
    Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("create", create_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("quizzes", quizzes_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("share", share_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(PollAnswerHandler(poll_answer_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    log.info("SwayamQuizBot starting")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
        
