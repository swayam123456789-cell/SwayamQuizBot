import os
import re
import sqlite3
import secrets
import logging
import asyncio
import time
from threading import Thread

from flask import Flask
from telegram import Update, Poll, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, PollAnswerHandler,
    CallbackQueryHandler, ContextTypes, filters
)

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

web = Flask(__name__)
@web.get("/")
def home(): return "SwayamQuizBot is running", 200
@web.get("/health")
def health(): return {"status": "ok"}, 200

def db_connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL, title TEXT NOT NULL,
            timer INTEGER NOT NULL DEFAULT 20, correct_marks REAL NOT NULL DEFAULT 1,
            negative_marks REAL NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id INTEGER NOT NULL, q_no INTEGER NOT NULL,
            question TEXT NOT NULL, option_a TEXT NOT NULL, option_b TEXT NOT NULL,
            option_c TEXT NOT NULL, option_d TEXT NOT NULL, answer TEXT NOT NULL,
            UNIQUE(quiz_id, q_no)
        );
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL, correct INTEGER NOT NULL DEFAULT 0, wrong INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
            elapsed REAL NOT NULL DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quiz_id, user_id)
        );
        """)

# Active quiz sessions keyed by chat ID; poll lookup lets answer updates reach the right session.
ACTIVE_SESSIONS = {}
POLL_LOOKUP = {}
TIMER_CHOICES = (15, 20, 25, 30)
SPECIAL_MESSAGE = "<b>ଅଭ୍ୟାସ ହିଁ ସଫଳତା ର ଚାବିକାଠି</b>"

def parse_mcqs(raw_text):
    lines = [line.strip() for line in raw_text.replace("\r", "\n").splitlines()]
    result, current = [], None
    qpat = re.compile(r"^(?:Q(?:UESTION)?\s*\d*\s*[.):\-]\s*|\d+[.)]\s+)(.+)$", re.I)
    opat = re.compile(r"^([A-D])[).:\-]\s*(.+)$", re.I)
    apat = re.compile(r"^(?:correct\s*answer|answer)\s*[:\-]\s*([A-D])\b", re.I)
    def finish():
        nonlocal current
        if current and all(k in current for k in "ABCD") and current.get("answer"):
            result.append({"question": current["question"].strip(), "a": current["A"].strip(),
                           "b": current["B"].strip(), "c": current["C"].strip(),
                           "d": current["D"].strip(), "answer": current["answer"]})
        current = None
    for line in lines:
        if not line: continue
        qm = qpat.match(line)
        if qm:
            finish(); current = {"question": qm.group(1).strip()}; continue
        if current is None: continue
        am = apat.match(line)
        if am: current["answer"] = am.group(1).upper(); continue
        om = opat.match(line)
        if om:
            letter, value = om.group(1).upper(), om.group(2).strip()
            if any(mark in value for mark in ("✅", "✔️", "✔", "☑️", "☑")):
                current["answer"] = letter
                value = re.sub(r"\s*(?:✅|✔️?|☑️?)\s*", "", value).strip()
            current[letter] = value
    finish()
    return result

def create_quiz(title, questions, timer):
    code = secrets.token_hex(3).upper()
    with db_connect() as con:
        cur = con.execute("INSERT INTO quizzes(code,owner_id,title,timer) VALUES(?,?,?,?)",
                          (code, OWNER_ID, title[:120], int(timer)))
        quiz_id = cur.lastrowid
        for n, q in enumerate(questions, 1):
            con.execute("""INSERT INTO questions
                (quiz_id,q_no,question,option_a,option_b,option_c,option_d,answer)
                VALUES(?,?,?,?,?,?,?,?)""",
                (quiz_id, n, q["question"][:300], q["a"][:100], q["b"][:100],
                 q["c"][:100], q["d"][:100], q["answer"]))
    return code, quiz_id

def get_quiz_by_code(code):
    with db_connect() as con:
        return con.execute("SELECT * FROM quizzes WHERE code=?", (code.upper(),)).fetchone()

def is_private(update):
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)

async def delete_group_command(update):
    msg = update.effective_message
    chat = update.effective_chat
    if msg and chat and chat.type != ChatType.PRIVATE:
        try: await msg.delete()
        except TelegramError: log.info("Could not delete group command; check bot Delete Messages permission.")

async def private_owner_only(update, reply=True):
    if not is_private(update) or not update.effective_user or update.effective_user.id != OWNER_ID:
        if reply and is_private(update) and update.effective_message:
            await update.effective_message.reply_text("ଏହି menu/command କେବଳ owner ଙ୍କ private chat ରେ କାମ କରେ।")
        return False
    return True

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Preserve existing /start and /start@BotName activation behavior in groups.
    if is_private(update):
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await update.effective_message.reply_text(main_menu_text())
        else:
            await update.effective_message.reply_text("🤖 SwayamQuizBot\nQuiz ଖେଳିବା ପାଇଁ group ରେ /run QUIZ_ID ବ୍ୟବହାର କରନ୍ତୁ।")
    else:
        await update.effective_message.reply_text("🤖 SwayamQuizBot ready!\nQuiz ଆରମ୍ଭ: /run QUIZ_ID\nQuiz ବନ୍ଦ: /stop")
        await delete_group_command(update)

def main_menu_text():
    return ("🤖 SwayamQuizBot — Main Menu\n\n"
            "/create — ନୂଆ Quiz ତିଆରି\n/done — Quiz save କରିବା\n/cancel — Draft ବାତିଲ୍\n"
            "/quizzes — Saved quizzes\n/run QUIZ_ID — Quiz ଖେଳିବା\n"
            "/leaderboard QUIZ_ID — Name ଓ Score\n/result QUIZ_ID — Result summary\n"
            "/edit QUIZ_ID New Name — Quiz ନାମ edit\n/share QUIZ_ID — Share text\n"
            "/delete QUIZ_ID — Quiz delete\n/help — MCQ format")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    await update.effective_message.reply_text(
        "📋 MCQ format:\n\nQ1. ଭାରତର ରାଜଧାନୀ କ'ଣ?\nA) ମୁମ୍ବାଇ\nB) ନୂଆଦିଲ୍ଲୀ ✅\nC) କୋଲକାତା\nD) ଚେନ୍ନାଇ\n\n"
        "ସଠିକ୍ option ପାଖରେ ✅ ରଖନ୍ତୁ। ଅନେକ MCQ ଏକାଥରେ paste କରି /done ଦିଅନ୍ତୁ।")

async def create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    context.user_data["quiz_draft"] = {"title": None, "questions": []}
    context.user_data["waiting_quiz_title"] = True
    await update.effective_message.reply_text("🏷️ Quiz name ପଠାନ୍ତୁ।")

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    context.user_data.pop("quiz_draft", None); context.user_data.pop("waiting_quiz_title", None)
    await update.effective_message.reply_text("❌ Quiz creation cancelled.")

async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    draft = context.user_data.get("quiz_draft")
    if not draft:
        await update.effective_message.reply_text("କୌଣସି draft ନାହିଁ। /create ଦିଅନ୍ତୁ।"); return
    if not draft["questions"]:
        await update.effective_message.reply_text("ପ୍ରଥମେ MCQ paste କରନ୍ତୁ।"); return
    keyboard = [[InlineKeyboardButton(f"{s} seconds", callback_data=f"timer:{s}") for s in (15, 20)],
                [InlineKeyboardButton(f"{s} seconds", callback_data=f"timer:{s}") for s in (25, 30)]]
    await update.effective_message.reply_text("⏱️ ଏହି quiz ପାଇଁ timer ବାଛନ୍ତୁ (କୌଣସି default ନାହିଁ):",
                                              reply_markup=InlineKeyboardMarkup(keyboard))

async def timer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.from_user or query.from_user.id != OWNER_ID or not query.message or query.message.chat.type != ChatType.PRIVATE:
        await query.edit_message_text("ଏହି option କେବଳ owner ଙ୍କ private chat ପାଇଁ।"); return
    try: timer = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError): return
    if timer not in TIMER_CHOICES: return
    draft = context.user_data.get("quiz_draft")
    if not draft or not draft.get("questions"):
        await query.edit_message_text("Draft ମିଳିଲା ନାହିଁ। ପୁଣି /create କରନ୍ତୁ।"); return
    title, total = draft.get("title") or "My Quiz", len(draft["questions"])
    code, _ = create_quiz(title, draft["questions"], timer)
    context.user_data.pop("quiz_draft", None); context.user_data.pop("waiting_quiz_title", None)
    await query.edit_message_text(
        f"🎉 Quiz Created!\n━━━━━━━━━━━━━━\n🏷️ Name: {title}\n❓ Questions: {total}\n⏱️ Timer: {timer}s\n"
        f"🆔 ID: {code}\n\n▶️ Group ରେ start: /run {code}\n🏆 Leaderboard: /leaderboard {code}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    if not is_private(update) or not user or user.id != OWNER_ID or not msg or not msg.text: return
    draft = context.user_data.get("quiz_draft")
    if not draft: return
    if context.user_data.get("waiting_quiz_title"):
        draft["title"] = msg.text.strip()[:120]; context.user_data["waiting_quiz_title"] = False
        await msg.reply_text(f"🏷️ Name: {draft['title']}\n➡️ MCQ paste କରନ୍ତୁ। Correct option ପାଖରେ ✅ ରଖନ୍ତୁ। /done ଦେଲେ timer ବାଛିବେ।")
        return
    parsed = parse_mcqs(msg.text)
    if not parsed:
        await msg.reply_text("❌ Format ଠିକ୍ ନୁହେଁ।\nQ1. Question?\nA) Option A\nB) Option B ✅\nC) Option C\nD) Option D")
        return
    draft["questions"].extend(parsed)
    await msg.reply_text(f"✅ ଏଥର {len(parsed)}ଟି MCQ add ହେଲା। Total: {len(draft['questions'])}। ଆଉ paste କରନ୍ତୁ କିମ୍ବା /done ଦିଅନ୍ତୁ।")

async def quizzes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    with db_connect() as con:
        rows = con.execute("""SELECT q.code,q.title,q.timer,COUNT(x.id) AS total FROM quizzes q
            LEFT JOIN questions x ON x.quiz_id=q.id WHERE q.owner_id=? GROUP BY q.id ORDER BY q.id DESC LIMIT 30""", (OWNER_ID,)).fetchall()
    if not rows: await update.effective_message.reply_text("Saved quiz ନାହିଁ। /create ଦିଅନ୍ତୁ।"); return
    await update.effective_message.reply_text("📚 Your Quizzes\n\n" + "\n\n".join(
        f"🏷️ {r['title']}\n❓ Questions: {r['total']} | ⏱️ {r['timer']}s\n🆔 ID: {r['code']}\n/run {r['code']}" for r in rows))

async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    if len(context.args) < 2:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /edit QUIZ_ID New Quiz Name"); return
    code, title = context.args[0].upper(), " ".join(context.args[1:])[:120]
    with db_connect() as con:
        cur = con.execute("UPDATE quizzes SET title=? WHERE code=? AND owner_id=?", (title, code, OWNER_ID))
    await update.effective_message.reply_text("✏️ Quiz name updated." if cur.rowcount else "Quiz ID ମିଳିଲା ନାହିଁ।")

async def share_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    if not context.args: await update.effective_message.reply_text("ବ୍ୟବହାର: /share QUIZ_ID"); return
    code = context.args[0].upper(); quiz = get_quiz_by_code(code)
    if not quiz: await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।"); return
    await update.effective_message.reply_text(f"📣 Share this quiz!\n📝 {quiz['title']}\n🆔 ID: {code}\nGroup ରେ: /run {code}")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    if not context.args: await update.effective_message.reply_text("ବ୍ୟବହାର: /delete QUIZ_ID"); return
    code = context.args[0].upper()
    with db_connect() as con:
        row = con.execute("SELECT id FROM quizzes WHERE code=? AND owner_id=?", (code, OWNER_ID)).fetchone()
        if not row: await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।"); return
        con.execute("DELETE FROM questions WHERE quiz_id=?", (row["id"],))
        con.execute("DELETE FROM results WHERE quiz_id=?", (row["id"],))
        con.execute("DELETE FROM quizzes WHERE id=?", (row["id"],))
    await update.effective_message.reply_text(f"🗑️ Quiz {code} deleted.")

async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat: return
    if chat.type == ChatType.PRIVATE and (not update.effective_user or update.effective_user.id != OWNER_ID):
        await update.effective_message.reply_text("Quiz run command group ରେ ବ୍ୟବହାର କରନ୍ତୁ।"); return
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /run QUIZ_ID")
        if chat.type != ChatType.PRIVATE: await delete_group_command(update)
        return
    code = context.args[0].upper(); quiz = get_quiz_by_code(code)
    if not quiz:
        await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।")
        if chat.type != ChatType.PRIVATE: await delete_group_command(update)
        return
    with db_connect() as con:
        questions = con.execute("SELECT * FROM questions WHERE quiz_id=? ORDER BY q_no", (quiz["id"],)).fetchall()
    if not questions:
        await update.effective_message.reply_text("ଏହି Quiz ରେ ପ୍ରଶ୍ନ ନାହିଁ।")
        if chat.type != ChatType.PRIVATE: await delete_group_command(update)
        return
    if chat.id in ACTIVE_SESSIONS:
        await update.effective_message.reply_text("ଏହି chat ରେ ଗୋଟିଏ quiz ଆଗରୁ ଚାଲୁଛି। ପ୍ରଥମେ /stop କରନ୍ତୁ।")
        if chat.type != ChatType.PRIVATE: await delete_group_command(update)
        return
    session = {"chat_id": chat.id, "quiz": quiz, "questions": questions, "stop": asyncio.Event(),
               "participants": {}, "poll_ids": set(), "started": time.monotonic()}
    ACTIVE_SESSIONS[chat.id] = session
    if chat.type != ChatType.PRIVATE: await delete_group_command(update)
    context.application.create_task(run_quiz_session(context, session), update=update, name=f"quiz-{code}-{chat.id}")

async def run_quiz_session(context, session):
    chat_id = session["chat_id"]; quiz = session["quiz"]; questions = session["questions"]
    try:
        intro = await context.bot.send_message(chat_id, f"🎯 <b>{quiz['title']}</b>\n📊 Questions: {len(questions)}\n⏱️ Timer: {quiz['timer']}s\nGet ready!", parse_mode=ParseMode.HTML)
        for n, digit in ((3, "3️⃣"), (2, "2️⃣"), (1, "1️⃣")):
            if session["stop"].is_set(): return
            try: await intro.edit_text(f"🎯 <b>{quiz['title']}</b>\n\n{digit}", parse_mode=ParseMode.HTML)
            except TelegramError:
                await context.bot.send_message(chat_id, f"{n}️⃣")
            try: await asyncio.wait_for(session["stop"].wait(), timeout=1)
            except asyncio.TimeoutError: pass
        if session["stop"].is_set(): return
        try: await intro.edit_text("🚀 <b>Quiz Started!</b>", parse_mode=ParseMode.HTML)
        except TelegramError: pass
        timer = int(quiz["timer"])
        for row in questions:
            if session["stop"].is_set(): break
            poll_message = await context.bot.send_poll(
                chat_id=chat_id, question=f"[{row['q_no']}/{len(questions)}] {row['question']}"[:300],
                options=[row["option_a"], row["option_b"], row["option_c"], row["option_d"]],
                type=Poll.QUIZ, correct_option_id="ABCD".index(row["answer"]),
                is_anonymous=False, open_period=timer)
            POLL_LOOKUP[poll_message.poll.id] = {"session": session, "correct_option": "ABCD".index(row["answer"]), "q_no": row["q_no"]}
            session["poll_ids"].add(poll_message.poll.id)
            try: await asyncio.wait_for(session["stop"].wait(), timeout=timer + 1)
            except asyncio.TimeoutError: pass
        if session["stop"].is_set():
            await context.bot.send_message(chat_id, "⏹️ Quiz stopped.")
        else:
            await save_session_results(session)
            await context.bot.send_message(chat_id, f"🏁 <b>Quiz Completed!</b>\n{quiz['title']}\n\nLeaderboard: /leaderboard {quiz['code']}", parse_mode=ParseMode.HTML)
    except asyncio.CancelledError:
        log.info("Quiz task cancelled for chat %s", chat_id)
    except Exception:
        log.exception("Quiz session failed")
        try: await context.bot.send_message(chat_id, "❌ Quiz ଚାଲୁ କରିବାରେ ସମସ୍ୟା ହେଲା। Bot logs check କରନ୍ତୁ।")
        except TelegramError: pass
    finally:
        for poll_id in session["poll_ids"]: POLL_LOOKUP.pop(poll_id, None)
        if ACTIVE_SESSIONS.get(chat_id) is session: ACTIVE_SESSIONS.pop(chat_id, None)

async def save_session_results(session):
    quiz_id = session["quiz"]["id"]
    with db_connect() as con:
        for user_id, p in session["participants"].items():
            correct, wrong = p["correct"], p["wrong"]
            # The leaderboard is per quiz and stores the latest completed run's score per user.
            con.execute("""INSERT INTO results(quiz_id,user_id,user_name,correct,wrong,score,total,elapsed,updated_at)
                VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(quiz_id,user_id) DO UPDATE SET user_name=excluded.user_name,
                correct=excluded.correct,wrong=excluded.wrong,score=excluded.score,total=excluded.total,
                elapsed=excluded.elapsed,updated_at=CURRENT_TIMESTAMP""",
                (quiz_id, user_id, p["name"], correct, wrong, correct, len(session["questions"]),
                 time.monotonic() - session["started"]))

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat: return
    if chat.type == ChatType.PRIVATE and (not update.effective_user or update.effective_user.id != OWNER_ID):
        await update.effective_message.reply_text("/stop group ରେ ବ୍ୟବହାର କରନ୍ତୁ।"); return
    session = ACTIVE_SESSIONS.get(chat.id)
    if session:
        session["stop"].set()
        await update.effective_message.reply_text("⏹️ Quiz stop କରାଯାଉଛି।")
    else:
        await update.effective_message.reply_text("ଏହି chat ରେ କୌଣସି quiz ଚାଲୁନାହିଁ।")
    if chat.type != ChatType.PRIVATE: await delete_group_command(update)

async def poll_answer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer: return
    info = POLL_LOOKUP.get(answer.poll_id)
    if not info: return
    session = info["session"]
    if session["stop"].is_set(): return
    user = answer.user
    p = session["participants"].setdefault(user.id, {"name": user.full_name, "correct": 0, "wrong": 0, "answers": {}})
    p["name"] = user.full_name
    q_no = info["q_no"]
    previous = p["answers"].get(q_no)
    if previous is not None:
        if previous: p["correct"] -= 1
        else: p["wrong"] -= 1
    # Empty option_ids means the user withdrew their answer.
    if not answer.option_ids:
        p["answers"].pop(q_no, None)
        return
    is_correct = answer.option_ids[0] == info["correct_option"]
    p["answers"][q_no] = is_correct
    if is_correct: p["correct"] += 1
    else: p["wrong"] += 1

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /leaderboard QUIZ_ID"); return
    code = context.args[0].upper(); quiz = get_quiz_by_code(code)
    if not quiz: await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।"); return
    with db_connect() as con:
        rows = con.execute("SELECT user_name,score FROM results WHERE quiz_id=? ORDER BY score DESC,correct DESC,elapsed ASC LIMIT 50", (quiz["id"],)).fetchall()
    lines = [f"{['🥇','🥈','🥉'][i] if i < 3 else str(i+1)+'.'} {r['user_name']} — {r['score']:g}" for i, r in enumerate(rows)]
    body = f"🏆 Leaderboard: {quiz['title']}\n\n" + ("\n".join(lines) if lines else "ଏଯାଏଁ score ନାହିଁ।") + "\n\n" + SPECIAL_MESSAGE
    await update.effective_message.reply_text(body, parse_mode=ParseMode.HTML)

async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await private_owner_only(update): return
    if not context.args: await update.effective_message.reply_text("ବ୍ୟବହାର: /result QUIZ_ID"); return
    quiz = get_quiz_by_code(context.args[0].upper())
    if not quiz: await update.effective_message.reply_text("Quiz ID ମିଳିଲା ନାହିଁ।"); return
    with db_connect() as con:
        row = con.execute("SELECT COUNT(*) AS players,COALESCE(MAX(score),0) AS top_score FROM results WHERE quiz_id=?", (quiz["id"],)).fetchone()
    await update.effective_message.reply_text(f"📊 Result Summary\n📝 {quiz['title']}\n👥 Players: {row['players']}\n🏆 Top score: {row['top_score']:g}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)

def run_web(): web.run(host="0.0.0.0", port=PORT, use_reloader=False)
def main():
    init_db(); Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("create", create_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CallbackQueryHandler(timer_callback, pattern=r"^timer:(15|20|25|30)$"))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("quizzes", quizzes_cmd))
    app.add_handler(CommandHandler("run", run_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
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

if __name__ == "__main__": main()
