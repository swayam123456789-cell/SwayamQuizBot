import os
import re
import secrets
import sqlite3
import asyncio
import threading
import logging
from datetime import datetime

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("SwayamQuizBot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "").strip()
DB_PATH = os.getenv("DB_PATH", "swayam_quiz.db")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN missing. Add BOT_TOKEN in Render Environment.")
if not OWNER_ID_RAW.isdigit():
    raise RuntimeError("OWNER_ID missing/invalid. Add your numeric Telegram user ID in Render Environment.")
if len(OWNER_PASSWORD) < 6:
    raise RuntimeError("OWNER_PASSWORD missing/too short. Set a password of at least 6 characters in Render Environment.")
OWNER_ID = int(OWNER_ID_RAW)

web = Flask(__name__)

@web.get("/")
def home():
    return "SwayamQuizBot is running", 200

@web.get("/health")
def health():
    return {"status": "ok"}, 200


def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS quizzes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            topic TEXT DEFAULT '',
            intro TEXT DEFAULT '',
            timer INTEGER DEFAULT 15,
            motivation TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            q_no INTEGER NOT NULL,
            question TEXT NOT NULL,
            a TEXT NOT NULL, b TEXT NOT NULL, c TEXT NOT NULL, d TEXT NOT NULL,
            answer TEXT NOT NULL,
            UNIQUE(quiz_id, q_no)
        );
        CREATE TABLE IF NOT EXISTS groups_seen(
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            added_by INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS responses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            q_no INTEGER NOT NULL,
            choice TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            run_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quiz_id, chat_id, user_id, q_no, run_id)
        );
        CREATE TABLE IF NOT EXISTS quiz_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            started_by INTEGER NOT NULL,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        );
        """)
        # Migrate older database column names safely.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(quizzes)").fetchall()}
        if "motivation" not in cols:
            db.execute("ALTER TABLE quizzes ADD COLUMN motivation TEXT DEFAULT ''")
        if "funny" in cols:
            db.execute("UPDATE quizzes SET motivation=COALESCE(NULLIF(motivation,''), funny)")
        response_cols = {r["name"] for r in db.execute("PRAGMA table_info(responses)").fetchall()}
        if "run_id" not in response_cols:
            db.execute("ALTER TABLE responses ADD COLUMN run_id INTEGER NOT NULL DEFAULT 0")


# Manual quiz conversation states
TITLE, TOPIC, INTRO, TIMER, MOTIVATION, QUESTION, A, B, C, D, ANSWER = range(11)
EDIT_TEXT = 50


def is_owner(user_id):
    return user_id == OWNER_ID


def is_authorized(user_id, context):
    return user_id == OWNER_ID and bool(context.user_data.get("owner_unlocked"))


def owner_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Quiz ତିଆରି", callback_data="create")],
        [InlineKeyboardButton("📚 ସମସ୍ତ Quiz", callback_data="myquizzes"),
         InlineKeyboardButton("📊 Quiz Stats", callback_data="stats_list")],
        [InlineKeyboardButton("✏️ Quiz Edit", callback_data="edit_list"),
         InlineKeyboardButton("🔗 Quiz Share", callback_data="share_list")],
        [InlineKeyboardButton("👥 My Groups", callback_data="groups")],
        [InlineKeyboardButton("📄 Copy-Paste / TXT Help", callback_data="txthelp"),
         InlineKeyboardButton("❓ Help", callback_data="help")]
    ])


def denied_text():
    return "ଏହି bot ର Owner Menu କେବଳ ମାଲିକଙ୍କ ପାଇଁ। Quiz ଖେଳିବାକୁ ମାଲିକଙ୍କ share link ଖୋଲନ୍ତୁ।"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if update.effective_chat.type != "private":
        await msg.reply_text("👋 ନମସ୍କାର! Quiz ଖେଳିବାକୁ option ରେ click କରନ୍ତୁ। Bot ର menu ମାଲିକଙ୍କ Private Chat ରେ ରହିବ।")
        return
    if is_owner(user.id):
        payload = context.args[0] if context.args else ""
        if payload.startswith("quiz_") and payload[5:].isdigit():
            await start_private_quiz(update, context, int(payload[5:]))
            return
        if not context.user_data.get("owner_unlocked"):
            await msg.reply_text("🔐 Owner Menu locked ଅଛି। Password ଖୋଲିବା ପାଇଁ /unlock ଲେଖି password ପଠାନ୍ତୁ।")
            return
        await msg.reply_text(
            "👋 ନମସ୍କାର! SwayamQuizBot କୁ ସ୍ୱାଗତ।\n\n"
            "ଏହା ତୁମର Owner Menu। ଏଠାରୁ Quiz ତିଆରି, Edit, Share ଓ Stats ଦେଖିପାରିବ।\n\n"
            "💪 ପ୍ରତିଦିନ ଅଭ୍ୟାସ କର, ପ୍ରତିଦିନ ଉନ୍ନତି କର!",
            reply_markup=owner_menu()
        )
    else:
        payload = context.args[0] if context.args else ""
        if payload.startswith("quiz_") and payload[5:].isdigit():
            await start_private_quiz(update, context, int(payload[5:]))
        else:
            await msg.reply_text("👋 ନମସ୍କାର! Quiz ଖେଳିବାକୁ ମାଲିକଙ୍କଠାରୁ ମିଳିଥିବା Quiz Link ଖୋଲନ୍ତୁ।")



async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id, context):
        await update.effective_message.reply_text(denied_text())
        return
    supplied = " ".join(context.args).strip()
    if not supplied:
        await update.effective_message.reply_text("🔐 /unlock ତାପରେ space ଦେଇ owner password ଲେଖନ୍ତୁ।")
        return
    if not secrets.compare_digest(supplied, OWNER_PASSWORD):
        await update.effective_message.reply_text("❌ Password ଭୁଲ। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।")
        return
    context.user_data["owner_unlocked"] = True
    await update.effective_message.reply_text("✅ Owner login ସଫଳ।", reply_markup=owner_menu())


async def help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📘 ବ୍ୟବହାର:\n"
        "• Quiz ତିଆରି: Private Chat → Quiz ତିଆରି\n"
        "• Copy-Paste: MCQ text ସିଧା ପଠାନ୍ତୁ\n"
        "• TXT: .txt file upload କରନ୍ତୁ\n"
        "• Group: bot କୁ admin କରି group ରେ /register ପଠାନ୍ତୁ\n"
        "• /done: Manual Quiz ତିଆରି ସମ୍ପୂର୍ଣ୍ଣ\n"
        "• /undo: ଶେଷ ପ୍ରଶ୍ନ ହଟାନ୍ତୁ\n"
        "• /cancel: Quiz draft ବାତିଲ୍"
    )
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(text, reply_markup=owner_menu() if is_authorized(q.from_user.id, context) else None)
    else:
        await update.effective_message.reply_text(text)


async def txt_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "📄 MCQ Copy-Paste / TXT Format:\n\n"
        "Q1. ପ୍ରଶ୍ନ ଲେଖନ୍ତୁ\n"
        "A. Option A\nB. Option B\nC. Option C\nD. Option D\n"
        "Correct Answer: B\n\n"
        "ପ୍ରତ୍ୟେକ ପ୍ରଶ୍ନ ପାଇଁ ଏହି format ଦୋହରାନ୍ତୁ। "
        "Text ସିଧା ପଠାଇପାରିବେ କିମ୍ବା ଏହି text କୁ .txt file କରି upload କରିପାରିବେ।"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id, context):
        await update.effective_message.reply_text(denied_text())
        return ConversationHandler.END
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("❌ Quiz ତିଆରି ବାତିଲ୍ ହେଲା।", reply_markup=owner_menu())
    return ConversationHandler.END


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id, context):
        await update.effective_message.reply_text(denied_text())
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if not draft:
        await update.effective_message.reply_text("ଏବେ କୌଣସି Quiz draft ଚାଲୁ ନାହିଁ।")
        return ConversationHandler.END
    if not draft.get("questions"):
        await update.effective_message.reply_text("ପ୍ରଥମେ ଅତି କମରେ 1ଟି ପ୍ରଶ୍ନ ଯୋଡ଼ନ୍ତୁ।")
        return QUESTION
    quiz_id = save_quiz(OWNER_ID, draft)
    count = len(draft["questions"])
    title = draft["title"]
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text(
        f"🎉 Quiz ସମ୍ପୂର୍ଣ୍ଣ ହେଲା!\nନାମ: {title}\nପ୍ରଶ୍ନ: {count}\nQuiz ID: {quiz_id}",
        reply_markup=owner_menu()
    )
    return ConversationHandler.END


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id, context):
        await update.effective_message.reply_text(denied_text())
        return
    draft = context.user_data.get("draft")
    if not draft:
        await update.effective_message.reply_text("କୌଣସି Quiz draft ଚାଲୁ ନାହିଁ।")
        return
    if draft.get("current"):
        draft.pop("current", None)
        await update.effective_message.reply_text("↩️ ଚାଲୁଥିବା ପ୍ରଶ୍ନଟି ବାତିଲ୍ ହେଲା। ପୁଣି ପ୍ରଶ୍ନ ଲେଖନ୍ତୁ।")
        return QUESTION
    if draft.get("questions"):
        draft["questions"].pop()
        await update.effective_message.reply_text(f"↩️ ଶେଷ ପ୍ରଶ୍ନ ହଟାଗଲା। ବର୍ତ୍ତମାନ {len(draft['questions'])}ଟି ପ୍ରଶ୍ନ ଅଛି।")
    else:
        await update.effective_message.reply_text("ହଟାଇବା ପାଇଁ ପ୍ରଶ୍ନ ନାହିଁ।")
    return QUESTION


async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return ConversationHandler.END
    context.user_data["draft"] = {"questions": [], "timer": 15, "motivation": ""}
    await q.message.reply_text("📝 Quiz ର ନାମ ଲେଖନ୍ତୁ।\nବାତିଲ୍: /cancel")
    return TITLE


async def get_title(update, context):
    context.user_data["draft"]["title"] = update.message.text.strip()[:120]
    await update.message.reply_text("📌 Topic ଲେଖନ୍ତୁ। ନଥିଲେ - ଲେଖନ୍ତୁ।")
    return TOPIC


async def get_topic(update, context):
    value = update.message.text.strip()
    context.user_data["draft"]["topic"] = "" if value == "-" else value[:120]
    await update.message.reply_text("📣 Quiz ଆରମ୍ଭ ପୂର୍ବରୁ ଦେଖାଯିବା Intro ଲେଖନ୍ତୁ। ନଥିଲେ -।")
    return INTRO


async def get_intro(update, context):
    value = update.message.text.strip()
    context.user_data["draft"]["intro"] = "" if value == "-" else value[:1000]
    await update.message.reply_text("⏱️ ପ୍ରତି ପ୍ରଶ୍ନର Timer seconds ଲେଖନ୍ତୁ (5–120)।")
    return TIMER


async def get_timer(update, context):
    try:
        seconds = int(update.message.text.strip())
        if not 5 <= seconds <= 120:
            raise ValueError
    except ValueError:
        await update.message.reply_text("ଦୟାକରି 5 ରୁ 120 ମଧ୍ୟରେ ସଂଖ୍ୟା ଲେଖନ୍ତୁ।")
        return TIMER
    context.user_data["draft"]["timer"] = seconds
    await update.message.reply_text("💬 Leaderboard ତଳେ ଦେଖାଯିବା Motivational Message ଲେଖନ୍ତୁ। ନଥିଲେ -।")
    return MOTIVATION


async def get_motivation(update, context):
    value = update.message.text.strip()
    context.user_data["draft"]["motivation"] = "" if value == "-" else value[:1000]
    await update.message.reply_text("❓ ପ୍ରଶ୍ନ 1 ଲେଖନ୍ତୁ।\n/undo = ଶେଷ ପ୍ରଶ୍ନ ହଟାନ୍ତୁ; /done = Quiz ସମ୍ପୂର୍ଣ୍ଣ; /cancel = ବାତିଲ୍")
    return QUESTION


async def get_question(update, context):
    context.user_data["draft"]["current"] = {"question": update.message.text.strip()[:1500]}
    await update.message.reply_text("Option A ଲେଖନ୍ତୁ।")
    return A


async def get_a(update, context):
    context.user_data["draft"]["current"]["a"] = update.message.text.strip()[:500]
    await update.message.reply_text("Option B ଲେଖନ୍ତୁ।")
    return B


async def get_b(update, context):
    context.user_data["draft"]["current"]["b"] = update.message.text.strip()[:500]
    await update.message.reply_text("Option C ଲେଖନ୍ତୁ।")
    return C


async def get_c(update, context):
    context.user_data["draft"]["current"]["c"] = update.message.text.strip()[:500]
    await update.message.reply_text("Option D ଲେଖନ୍ତୁ।")
    return D


async def get_d(update, context):
    context.user_data["draft"]["current"]["d"] = update.message.text.strip()[:500]
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ {letter}", callback_data=f"ans_{letter}") for letter in "ABCD"]])
    await update.message.reply_text("ସଠିକ୍ ଉତ୍ତର ବାଛନ୍ତୁ:", reply_markup=kb)
    return ANSWER


def save_quiz(owner_id, draft):
    with connect() as db:
        cur = db.execute(
            "INSERT INTO quizzes(owner_id,title,topic,intro,timer,motivation) VALUES(?,?,?,?,?,?)",
            (owner_id, draft["title"], draft.get("topic", ""), draft.get("intro", ""),
             int(draft.get("timer", 15)), draft.get("motivation", ""))
        )
        quiz_id = cur.lastrowid
        for number, item in enumerate(draft["questions"], 1):
            db.execute(
                "INSERT INTO questions(quiz_id,q_no,question,a,b,c,d,answer) VALUES(?,?,?,?,?,?,?,?)",
                (quiz_id, number, item["question"], item["a"], item["b"], item["c"], item["d"], item["answer"])
            )
    return quiz_id


async def get_answer(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if not draft or "current" not in draft:
        await q.message.reply_text("Draft ମିଳିଲା ନାହିଁ। /start କରନ୍ତୁ।")
        return ConversationHandler.END
    draft["current"]["answer"] = q.data[-1]
    draft["questions"].append(draft.pop("current"))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ ଆଉ ପ୍ରଶ୍ନ", callback_data="more")],
        [InlineKeyboardButton("✅ /done — Quiz ସମ୍ପୂର୍ଣ୍ଣ", callback_data="save")],
        [InlineKeyboardButton("❌ ବାତିଲ୍", callback_data="cancel_draft")]
    ])
    await q.message.reply_text(
        f"✅ ପ୍ରଶ୍ନ {len(draft['questions'])} ଯୋଡ଼ାଗଲା।\nଆଉ ପ୍ରଶ୍ନ ଯୋଡ଼ନ୍ତୁ କି Quiz ସମ୍ପୂର୍ଣ୍ଣ କରନ୍ତୁ?",
        reply_markup=kb
    )
    return QUESTION


async def draft_next(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return ConversationHandler.END
    data = q.data
    draft = context.user_data.get("draft")
    if data == "cancel_draft":
        context.user_data.pop("draft", None)
        await q.message.reply_text("❌ Draft ବାତିଲ୍ ହେଲା।", reply_markup=owner_menu())
        return ConversationHandler.END
    if not draft:
        await q.message.reply_text("Draft ମିଳିଲା ନାହିଁ। /start କରନ୍ତୁ।")
        return ConversationHandler.END
    if data == "more":
        await q.message.reply_text(f"❓ ପ୍ରଶ୍ନ {len(draft['questions']) + 1} ଲେଖନ୍ତୁ। /undo /done /cancel ଉପଲବ୍ଧ।")
        return QUESTION
    if data == "save":
        if not draft["questions"]:
            await q.message.reply_text("ଅତି କମରେ 1ଟି ପ୍ରଶ୍ନ ଦରକାର।")
            return QUESTION
        quiz_id = save_quiz(OWNER_ID, draft)
        title, count = draft["title"], len(draft["questions"])
        context.user_data.pop("draft", None)
        await q.message.reply_text(f"🎉 Quiz ସମ୍ପୂର୍ଣ୍ଣ ହେଲା!\nନାମ: {title}\nପ୍ରଶ୍ନ: {count}\nID: {quiz_id}", reply_markup=owner_menu())
        return ConversationHandler.END
    return QUESTION


# Parser for Q1./Question 1/1. plus A-D and Correct Answer: B.
def parse_mcqs(raw):
    lines = [line.strip() for line in raw.replace("\r", "\n").splitlines()]
    found, current = [], None
    qpat = re.compile(r"^(?:Q(?:UESTION)?\s*\d*\s*[.):\-]\s*|\d+[.)]\s+)(.+)$", re.I)
    opat = re.compile(r"^([A-D])[.):\-]\s*(.+)$", re.I)
    apat = re.compile(r"^(?:(?:correct|right)\s*)?answer\s*[:\-]\s*([A-D])\b", re.I)

    def finish():
        nonlocal current
        if current and all(k in current for k in ("A", "B", "C", "D", "answer")):
            found.append({
                "question": current["question"].strip(), "a": current["A"], "b": current["B"],
                "c": current["C"], "d": current["D"], "answer": current["answer"]
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
        om = opat.match(line)
        if om:
            current[om.group(1).upper()] = om.group(2).strip()
            continue
        am = apat.match(line)
        if am:
            current["answer"] = am.group(1).upper()
            continue
        if not any(k in current for k in ("A", "B", "C", "D")) and "answer" not in current:
            current["question"] += "\n" + line
    finish()
    return found


async def import_questions(update, context, raw, title):
    questions = parse_mcqs(raw)
    if not questions:
        await update.effective_message.reply_text(
            "❌ ପ୍ରଶ୍ନ ଚିହ୍ନଟ ହେଲା ନାହିଁ। Format:\nQ1. Question\nA. Option\nB. Option\nC. Option\nD. Option\nCorrect Answer: B"
        )
        return
    quiz_id = save_quiz(OWNER_ID, {
        "title": title[:120] or "Imported Quiz", "topic": "Imported MCQ",
        "intro": "", "timer": 15, "motivation": "", "questions": questions
    })
    await update.effective_message.reply_text(
        f"✅ Quiz ତିଆରି ହେଲା!\nନାମ: {title}\nପ୍ରଶ୍ନ: {len(questions)}\nQuiz ID: {quiz_id}",
        reply_markup=owner_menu()
    )


async def handle_text_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or not is_authorized(user.id, context) or not msg or not msg.text:
        return
    if context.user_data.get("draft"):
        return
    text = msg.text.strip()
    if context.user_data.get("expect_edit_question"):
        await handle_question_edit_text(update, context)
    elif context.user_data.get("edit_field"):
        await handle_edit_text(update, context)
    elif parse_mcqs(text):
        title = context.user_data.pop("import_title", None) or f"Quiz {datetime.now().strftime('%d-%m-%Y %H:%M')}"
        await import_questions(update, context, text, title)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_authorized(update.effective_user.id, context):
        await msg.reply_text(denied_text())
        return
    doc = msg.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await msg.reply_text("ଦୟାକରି .txt file upload କରନ୍ତୁ।")
        return
    if doc.file_size and doc.file_size > 1_500_000:
        await msg.reply_text("File 1.5 MB ଠାରୁ ଛୋଟ ହେବା ଦରକାର।")
        return
    try:
        tgfile = await context.bot.get_file(doc.file_id)
        raw = bytes(await tgfile.download_as_bytearray()).decode("utf-8-sig", errors="replace")
        await import_questions(update, context, raw, os.path.splitext(doc.file_name)[0])
    except Exception:
        log.exception("TXT import failed")
        await msg.reply_text("❌ TXT import ହେଲା ନାହିଁ। Render Logs ଯାଞ୍ଚ କରନ୍ତୁ।")


async def show_quizzes(update, context):
    q = update.callback_query
    if q:
        await q.answer()
        uid, target = q.from_user.id, q.message
    else:
        uid, target = update.effective_user.id, update.effective_message
    if not is_owner(uid):
        await target.reply_text(denied_text())
        return
    with connect() as db:
        rows = db.execute("SELECT id,title FROM quizzes WHERE owner_id=? ORDER BY id DESC", (OWNER_ID,)).fetchall()
    if not rows:
        await target.reply_text("ଏଯାଏଁ Quiz ନାହିଁ। Quiz ତିଆରି କରନ୍ତୁ କିମ୍ବା MCQ paste/upload କରନ୍ତୁ।", reply_markup=owner_menu())
        return
    buttons = [[InlineKeyboardButton(f"📚 {row['title']} (#{row['id']})", callback_data=f"quiz_detail_{row['id']}")] for row in rows]
    await target.reply_text("📚 ତୁମର ସମସ୍ତ Saved Quizzes:", reply_markup=InlineKeyboardMarkup(buttons))


async def quiz_detail(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, OWNER_ID)).fetchone()
        count = db.execute("SELECT COUNT(*) n FROM questions WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
    if not quiz:
        await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ।")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Private ରେ ଖେଳ", callback_data=f"ownerplay_{quiz_id}")],
        [InlineKeyboardButton("👥 Group ରେ ଚଲାଅ", callback_data=f"pickgroup_{quiz_id}")],
        [InlineKeyboardButton("🔗 Share Link", callback_data=f"share_{quiz_id}")],
        [InlineKeyboardButton("📊 Stats", callback_data=f"stats_{quiz_id}")],
        [InlineKeyboardButton("✏️ Title / Intro / Message Edit", callback_data=f"editmeta_{quiz_id}")],
        [InlineKeyboardButton("✏️ ପ୍ରଶ୍ନ Edit", callback_data=f"editq_{quiz_id}")],
        [InlineKeyboardButton("🗑 Delete Quiz", callback_data=f"deleteask_{quiz_id}")],
        [InlineKeyboardButton("⬅️ ସମସ୍ତ Quiz", callback_data="myquizzes")]
    ])
    await q.message.reply_text(
        f"📚 {quiz['title']}\nTopic: {quiz['topic'] or '-'}\nପ୍ରଶ୍ନ: {count}\nTimer: {quiz['timer']} seconds",
        reply_markup=kb
    )


async def show_groups(update, context):
    q = update.callback_query
    if q:
        await q.answer()
        uid, target = q.from_user.id, q.message
    else:
        uid, target = update.effective_user.id, update.effective_message
    if not is_owner(uid):
        await target.reply_text(denied_text())
        return
    with connect() as db:
        rows = db.execute("SELECT title,chat_id FROM groups_seen WHERE added_by=? ORDER BY title", (OWNER_ID,)).fetchall()
    if not rows:
        await target.reply_text("Group ନାହିଁ। Bot କୁ group admin କରି group ଭିତରେ /register ପଠାନ୍ତୁ।")
        return
    await target.reply_text("👥 Registered Groups:\n" + "\n".join(f"• {r['title']}" for r in rows))


async def register_group(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.effective_message.reply_text("Group ଭିତରେ /register ପଠାନ୍ତୁ।")
        return
    if not is_owner(user.id):
        await update.effective_message.reply_text("Group register କରିବାର ଅଧିକାର କେବଳ bot owner ଙ୍କ ପାଖରେ ଅଛି।")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if member.status not in ("administrator", "creator"):
            await update.effective_message.reply_text("ପ୍ରଥମେ bot କୁ Group Admin କରନ୍ତୁ।")
            return
    except Exception:
        await update.effective_message.reply_text("Bot କୁ Group Admin କରି ପୁଣି /register ପଠାନ୍ତୁ।")
        return
    with connect() as db:
        db.execute(
            "INSERT INTO groups_seen(chat_id,title,added_by) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, added_by=excluded.added_by",
            (chat.id, chat.title or "Telegram Group", OWNER_ID)
        )
    await update.effective_message.reply_text("✅ Group register ହେଲା। ଏବେ Private Chat ର My Quizzes ରୁ Quiz ଚଲାନ୍ତୁ।")


async def pick_group(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        groups = db.execute("SELECT chat_id,title FROM groups_seen WHERE added_by=? ORDER BY title", (OWNER_ID,)).fetchall()
    if not groups:
        await q.message.reply_text("ପ୍ରଥମେ Group ରେ bot admin କରି /register ପଠାନ୍ତୁ।")
        return
    await q.message.reply_text("କେଉଁ Group ରେ Quiz ଚଲାଇବେ?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(g["title"], callback_data=f"run_{quiz_id}_{g['chat_id']}")] for g in groups
    ]))


async def share_quiz(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        quiz = db.execute("SELECT title FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, OWNER_ID)).fetchone()
    if not quiz:
        await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ।")
        return
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=quiz_{quiz_id}"
    await q.message.reply_text(
        f"🔗 Quiz Share Link\n{quiz['title']}\n\n{link}\n\nଏହି link share କଲେ ବ୍ୟକ୍ତି Private Chat ରେ Quiz ଖେଳିପାରିବେ।",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📤 Share Link", url=link)]])
    )


async def start_private_quiz(update, context, quiz_id):
    user = update.effective_user
    chat_id = update.effective_chat.id
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        count = db.execute("SELECT COUNT(*) n FROM questions WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
    if not quiz or count == 0:
        await update.effective_message.reply_text("ଏହି Quiz ମିଳିଲା ନାହିଁ କିମ୍ବା ଏଥିରେ ପ୍ରଶ୍ନ ନାହିଁ।")
        return
    await start_run(context, quiz_id, chat_id, user.id, is_private=True)


async def owner_play(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    await start_run(context, quiz_id, q.from_user.id, OWNER_ID, is_private=True)


async def start_run(context, quiz_id, chat_id, started_by, is_private=False):
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        count = db.execute("SELECT COUNT(*) n FROM questions WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
        run_id = db.execute("INSERT INTO quiz_runs(quiz_id,chat_id,started_by) VALUES(?,?,?)", (quiz_id, chat_id, started_by)).lastrowid
    if not quiz or not count:
        await context.bot.send_message(chat_id, "Quiz ମିଳିଲା ନାହିଁ।")
        return
    active = context.application.bot_data.setdefault("active_runs", {})
    key = (quiz_id, chat_id)
    if key in active:
        active.pop(key, None)
    active[key] = {
        "index": 0, "count": count, "quiz_id": quiz_id, "chat_id": chat_id,
        "started_by": started_by, "is_private": is_private, "message_id": None,
        "deadline_task": None, "run_id": run_id
    }
    if quiz["intro"]:
        await context.bot.send_message(chat_id, f"📣 {quiz['title']}\n\n{quiz['intro']}")
    await context.bot.send_message(chat_id, f"🎯 Quiz ଆରମ୍ଭ!\n{quiz['title']}\nମୋଟ ପ୍ରଶ୍ନ: {count}\nTimer: {quiz['timer']} seconds")
    await send_question(context, quiz_id, chat_id, 0)


async def send_question(context, quiz_id, chat_id, index):
    key = (quiz_id, chat_id)
    active_runs = context.application.bot_data.setdefault("active_runs", {})
    run = active_runs.get(key)
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        row = db.execute("SELECT * FROM questions WHERE quiz_id=? AND q_no=?", (quiz_id, index + 1)).fetchone()
    if not run or not quiz or not row:
        if run:
            await finish_quiz(context, quiz_id, chat_id)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"A. {row['a']}", callback_data=f"vote_{quiz_id}_{index}_A")],
        [InlineKeyboardButton(f"B. {row['b']}", callback_data=f"vote_{quiz_id}_{index}_B")],
        [InlineKeyboardButton(f"C. {row['c']}", callback_data=f"vote_{quiz_id}_{index}_C")],
        [InlineKeyboardButton(f"D. {row['d']}", callback_data=f"vote_{quiz_id}_{index}_D")]
    ])
    message = await context.bot.send_message(
        chat_id,
        f"❓ ପ୍ରଶ୍ନ {index+1}/{run['count']}\n⏱️ ସମୟ: {quiz['timer']} seconds\n\n{row['question']}",
        reply_markup=keyboard
    )
    run["message_id"] = message.message_id
    run["deadline_task"] = asyncio.create_task(question_timeout(context, quiz_id, chat_id, index, quiz["timer"]))


async def question_timeout(context, quiz_id, chat_id, index, seconds):
    await asyncio.sleep(seconds)
    key = (quiz_id, chat_id)
    run = context.application.bot_data.setdefault("active_runs", {}).get(key)
    if not run or run["index"] != index:
        return
    try:
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=run["message_id"], reply_markup=None)
    except Exception:
        pass
    with connect() as db:
        qrow = db.execute("SELECT answer FROM questions WHERE quiz_id=? AND q_no=?", (quiz_id, index + 1)).fetchone()
        rows = db.execute(
            "SELECT choice,COUNT(*) n FROM responses WHERE quiz_id=? AND chat_id=? AND q_no=? AND run_id=? GROUP BY choice",
            (quiz_id, chat_id, index + 1, run["run_id"])
        ).fetchall()
        total = sum(r["n"] for r in rows)
        correct = qrow["answer"] if qrow else "?"
    pct_lines = []
    for letter in "ABCD":
        n = next((r["n"] for r in rows if r["choice"] == letter), 0)
        pct = round((n * 100 / total)) if total else 0
        pct_lines.append(f"{'✅' if letter == correct else '▫️'} {letter}: {pct}% ({n})")
    await context.bot.send_message(
        chat_id,
        f"📊 ଉତ୍ତର ଫଳାଫଳ (ମୋଟ ଉତ୍ତର: {total})\n" + "\n".join(pct_lines) +
        f"\n\n✅ ସଠିକ୍ ଉତ୍ତର: {correct}"
    )
    run["index"] = index + 1
    await send_question(context, quiz_id, chat_id, index + 1)


async def vote(update, context):
    q = update.callback_query
    try:
        _, qs, ix, choice = q.data.split("_")
        quiz_id, index = int(qs), int(ix)
        chat_id = update.effective_chat.id
    except Exception:
        await q.answer("ଏହି option ଠିକ୍ ନୁହେଁ।", show_alert=True)
        return
    user = q.from_user
    run = context.application.bot_data.setdefault("active_runs", {}).get((quiz_id, chat_id))
    if not run or run["index"] != index:
        await q.answer("ଏହି ପ୍ରଶ୍ନର ସମୟ ସରିଯାଇଛି।", show_alert=True)
        return
    if update.effective_chat.id != chat_id:
        await q.answer("ଏହି Quiz ଅନ୍ୟ chat ପାଇଁ।", show_alert=True)
        return
    with connect() as db:
        exists = db.execute(
            "SELECT id FROM responses WHERE quiz_id=? AND chat_id=? AND user_id=? AND q_no=? AND run_id=?",
            (quiz_id, chat_id, user.id, index + 1, run["run_id"])
        ).fetchone()
        if exists:
            await q.answer("ତୁମେ ଏହି ପ୍ରଶ୍ନର ଉତ୍ତର ଦେଇସାରିଛ।", show_alert=True)
            return
        qrow = db.execute("SELECT answer FROM questions WHERE quiz_id=? AND q_no=?", (quiz_id, index + 1)).fetchone()
        correct = bool(qrow and choice == qrow["answer"])
        name = user.full_name or user.username or str(user.id)
        db.execute(
            "INSERT INTO responses(quiz_id,chat_id,user_id,username,q_no,choice,is_correct,run_id) VALUES(?,?,?,?,?,?,?,?)",
            (quiz_id, chat_id, user.id, name, index + 1, choice, int(correct), run["run_id"])
        )
    await q.answer("✅ ସଠିକ୍ ଉତ୍ତର!" if correct else "❌ ଭୁଲ ଉତ୍ତର!", show_alert=True)


async def finish_quiz(context, quiz_id, chat_id):
    key = (quiz_id, chat_id)
    run = context.application.bot_data.setdefault("active_runs", {}).pop(key, None)
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        rows = db.execute(
            "SELECT username,SUM(is_correct) points,COUNT(*) answered FROM responses "
            "WHERE quiz_id=? AND chat_id=? AND run_id=? GROUP BY user_id,username ORDER BY points DESC,answered DESC,username LIMIT 10",
            (quiz_id, chat_id, run["run_id"] if run else 0)
        ).fetchall()
        db.execute("UPDATE quiz_runs SET finished_at=CURRENT_TIMESTAMP WHERE quiz_id=? AND chat_id=? AND finished_at IS NULL", (quiz_id, chat_id))
        total_questions = db.execute("SELECT COUNT(*) n FROM questions WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
        participants = db.execute("SELECT COUNT(DISTINCT user_id) n FROM responses WHERE quiz_id=? AND chat_id=?", (quiz_id, chat_id)).fetchone()["n"]
    text = "🏆 Quiz ସମାପ୍ତ!\n\n📊 Top 10 Leaderboard\n\n"
    text += "\n".join(f"{i}. {r['username']} — {r['points']}/{total_questions}" for i, r in enumerate(rows, 1)) if rows else "କେହି ଉତ୍ତର ଦେଇନାହାନ୍ତି।"
    text += f"\n\n👥 ଅଂଶଗ୍ରହଣକାରୀ: {participants}"
    await context.bot.send_message(chat_id, text)
    if quiz and quiz["motivation"]:
        await context.bot.send_message(chat_id, "💪 " + quiz["motivation"])


async def stats_list(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    with connect() as db:
        rows = db.execute("SELECT id,title FROM quizzes WHERE owner_id=? ORDER BY id DESC", (OWNER_ID,)).fetchall()
    if not rows:
        await q.message.reply_text("Stats ଦେଖିବାକୁ Quiz ନାହିଁ।")
        return
    await q.message.reply_text("କେଉଁ Quiz ର Stats ଦେଖିବେ?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(r["title"], callback_data=f"stats_{r['id']}")] for r in rows
    ]))


async def show_stats(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, OWNER_ID)).fetchone()
        if not quiz:
            await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ।")
            return
        question_count = db.execute("SELECT COUNT(*) n FROM questions WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
        total_people = db.execute("SELECT COUNT(DISTINCT user_id) n FROM responses WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
        total_answers = db.execute("SELECT COUNT(*) n FROM responses WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
        correct_count = db.execute("SELECT COUNT(*) n FROM responses WHERE quiz_id=? AND is_correct=1", (quiz_id,)).fetchone()["n"]
        chats = db.execute("SELECT COUNT(DISTINCT chat_id) n FROM quiz_runs WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
        per_q = db.execute(
            "SELECT q_no,COUNT(*) n,SUM(is_correct) correct FROM responses WHERE quiz_id=? GROUP BY q_no ORDER BY q_no",
            (quiz_id,)
        ).fetchall()
    text = (
        f"📊 Quiz Stats: {quiz['title']}\n\n"
        f"❓ ମୋଟ ପ୍ରଶ୍ନ: {question_count}\n"
        f"👥 Unique Participants: {total_people}\n"
        f"🗳️ ମୋଟ ଉତ୍ତର: {total_answers}\n"
        f"✅ ସଠିକ୍ ଉତ୍ତର: {correct_count}\n"
        f"💯 Accuracy: {round(correct_count * 100 / total_answers) if total_answers else 0}%\n"
        f"▶️ Chat/Group Runs: {chats}\n\nପ୍ରଶ୍ନ ଅନୁସାରେ ଉତ୍ତର:"
    )
    text += "\n" + "\n".join(f"Q{r['q_no']}: {r['n']} responses, correct {r['correct'] or 0}" for r in per_q)
    await q.message.reply_text(text)


async def edit_list(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    with connect() as db:
        rows = db.execute("SELECT id,title FROM quizzes WHERE owner_id=? ORDER BY id DESC", (OWNER_ID,)).fetchall()
    if not rows:
        await q.message.reply_text("Edit କରିବାକୁ Quiz ନାହିଁ।")
        return
    await q.message.reply_text("କେଉଁ Quiz Edit କରିବେ?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(r["title"], callback_data=f"editmeta_{r['id']}")] for r in rows
    ]))


async def edit_meta_menu(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        quiz = db.execute("SELECT id,title FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, OWNER_ID)).fetchone()
    if not quiz:
        await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ।")
        return
    await q.message.reply_text("କ'ଣ Edit କରିବେ?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Quiz Title", callback_data=f"set_title_{quiz_id}")],
        [InlineKeyboardButton("Quiz Intro", callback_data=f"set_intro_{quiz_id}")],
        [InlineKeyboardButton("Motivational Message", callback_data=f"set_motivation_{quiz_id}")],
        [InlineKeyboardButton("Timer", callback_data=f"set_timer_{quiz_id}")],
    ]))


async def begin_edit_field(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    _, field, quiz_id_text = q.data.split("_", 2)
    quiz_id = int(quiz_id_text)
    context.user_data["edit_field"] = (field, quiz_id)
    prompts = {
        "title": "ନୂଆ Quiz Title ଲେଖନ୍ତୁ।",
        "intro": "ନୂଆ Intro ଲେଖନ୍ତୁ। Intro ଖାଲି କରିବାକୁ - ଲେଖନ୍ତୁ।",
        "motivation": "ନୂଆ Motivational Message ଲେଖନ୍ତୁ। ଖାଲି କରିବାକୁ - ଲେଖନ୍ତୁ।",
        "timer": "Timer seconds ଲେଖନ୍ତୁ (5–120)।"
    }
    await q.message.reply_text(prompts[field] + "\n/cancel_edit ଦେଇ ବାତିଲ୍ କରିପାରିବେ।")


async def handle_edit_text(update, context):
    if not is_authorized(update.effective_user.id, context):
        return
    field_data = context.user_data.get("edit_field")
    if not field_data:
        return
    field, quiz_id = field_data
    value = update.effective_message.text.strip()
    if field == "timer":
        try:
            n = int(value)
            if not 5 <= n <= 120:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("5–120 ମଧ୍ୟରେ seconds ଲେଖନ୍ତୁ।")
            return
        value = n
    else:
        if value == "-":
            value = ""
        value = value[:1000 if field != "title" else 120]
    with connect() as db:
        db.execute(f"UPDATE quizzes SET {field}=? WHERE id=? AND owner_id=?", (value, quiz_id, OWNER_ID))
    context.user_data.pop("edit_field", None)
    await update.effective_message.reply_text("✅ Edit save ହେଲା।", reply_markup=owner_menu())


async def edit_question_menu(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        rows = db.execute("SELECT q_no,question FROM questions WHERE quiz_id=? ORDER BY q_no", (quiz_id,)).fetchall()
    if not rows:
        await q.message.reply_text("ପ୍ରଶ୍ନ ନାହିଁ।")
        return
    await q.message.reply_text(
        "Edit କରିବାକୁ ପ୍ରଶ୍ନ ବାଛନ୍ତୁ। ତା'ପରେ ପୁରା question block (Q1. ... A. ... B. ... C. ... D. ... Correct Answer: B) ପଠାନ୍ତୁ।",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Q{r['q_no']}: {r['question'][:45]}", callback_data=f"editquestion_{quiz_id}_{r['q_no']}")] for r in rows])
    )


async def choose_edit_question(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    _, quiz_id, q_no = q.data.split("_")
    context.user_data["expect_edit_question"] = (int(quiz_id), int(q_no))
    await q.message.reply_text(
        f"Q{q_no} ପାଇଁ ନୂଆ question block ପଠାନ୍ତୁ:\n\n"
        f"Q{q_no}. ପ୍ରଶ୍ନ\nA. Option A\nB. Option B\nC. Option C\nD. Option D\nCorrect Answer: B"
    )


async def handle_question_edit_text(update, context):
    if not is_authorized(update.effective_user.id, context):
        return
    state = context.user_data.get("expect_edit_question")
    if not state:
        return
    parsed = parse_mcqs(update.effective_message.text)
    if len(parsed) != 1:
        await update.effective_message.reply_text("ଗୋଟିଏ ପ୍ରଶ୍ନର ସଠିକ୍ block ପଠାନ୍ତୁ।")
        return
    quiz_id, q_no = state
    item = parsed[0]
    with connect() as db:
        db.execute(
            "UPDATE questions SET question=?,a=?,b=?,c=?,d=?,answer=? WHERE quiz_id=? AND q_no=?",
            (item["question"], item["a"], item["b"], item["c"], item["d"], item["answer"], quiz_id, q_no)
        )
        db.execute("DELETE FROM responses WHERE quiz_id=? AND q_no=?", (quiz_id, q_no))
    context.user_data.pop("expect_edit_question", None)
    await update.effective_message.reply_text("✅ ପ୍ରଶ୍ନ update ହେଲା।", reply_markup=owner_menu())


async def delete_ask(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    await q.message.reply_text("ଏହି Quiz delete କରିବେ?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("ହଁ, Delete", callback_data=f"delete_{quiz_id}"),
         InlineKeyboardButton("ନା", callback_data=f"quiz_detail_{quiz_id}")]
    ]))


async def delete_quiz(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    quiz_id = int(q.data.rsplit("_", 1)[1])
    with connect() as db:
        db.execute("DELETE FROM questions WHERE quiz_id=?", (quiz_id,))
        db.execute("DELETE FROM responses WHERE quiz_id=?", (quiz_id,))
        db.execute("DELETE FROM quiz_runs WHERE quiz_id=?", (quiz_id,))
        db.execute("DELETE FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, OWNER_ID))
    await q.message.reply_text("🗑 Quiz delete ହେଲା।", reply_markup=owner_menu())


async def run_quiz_group(update, context):
    q = update.callback_query
    await q.answer()
    if not is_authorized(q.from_user.id, context):
        await q.message.reply_text(denied_text())
        return
    _, quiz_id_s, chat_id_s = q.data.split("_", 2)
    quiz_id, chat_id = int(quiz_id_s), int(chat_id_s)
    with connect() as db:
        quiz = db.execute("SELECT id FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, OWNER_ID)).fetchone()
    if not quiz:
        await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ।")
        return
    await start_run(context, quiz_id, chat_id, OWNER_ID)
    await q.message.reply_text("🚀 Group ରେ Quiz ଆରମ୍ଭ ହେଲା।")


async def button_router(update, context):
    q = update.callback_query
    data = q.data or ""
    if data.startswith("vote_"):
        return await vote(update, context)
    if data == "myquizzes":
        return await show_quizzes(update, context)
    if data == "groups":
        return await show_groups(update, context)
    if data == "help":
        return await help_text(update, context)
    if data == "txthelp":
        return await txt_help(update, context)
    if data == "stats_list":
        return await stats_list(update, context)
    if data == "edit_list":
        return await edit_list(update, context)
    if data == "share_list":
        return await show_quizzes(update, context)
    if data.startswith("quiz_detail_"):
        return await quiz_detail(update, context)
    if data.startswith("pickgroup_"):
        return await pick_group(update, context)
    if data.startswith("share_"):
        return await share_quiz(update, context)
    if data.startswith("stats_"):
        return await show_stats(update, context)
    if data.startswith("editmeta_"):
        return await edit_meta_menu(update, context)
    if data.startswith("set_title_") or data.startswith("set_intro_") or data.startswith("set_motivation_") or data.startswith("set_timer_"):
        return await begin_edit_field(update, context)
    if data.startswith("editq_"):
        return await edit_question_menu(update, context)
    if data.startswith("editquestion_"):
        return await choose_edit_question(update, context)
    if data.startswith("deleteask_"):
        return await delete_ask(update, context)
    if data.startswith("delete_"):
        return await delete_quiz(update, context)
    if data.startswith("ownerplay_"):
        return await owner_play(update, context)
    if data.startswith("run_"):
        return await run_quiz_group(update, context)
    if data in ("more", "save", "cancel_draft"):
        return await draft_next(update, context)
    await q.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Telegram handler error: %s", context.error, exc_info=context.error)


def run_web():
    web.run(host="0.0.0.0", port=PORT, use_reloader=False)


def main():
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_start, pattern=r"^create$")],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_topic)],
            INTRO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_intro)],
            TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_timer)],
            MOTIVATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_motivation)],
            QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_question),
                CallbackQueryHandler(draft_next, pattern=r"^(more|save|cancel_draft)$")
            ],
            A: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_a)],
            B: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_b)],
            C: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_c)],
            D: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_d)],
            ANSWER: [CallbackQueryHandler(get_answer, pattern=r"^ans_[ABCD]$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("done", done_command),
            CommandHandler("undo", undo_command),
        ],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("unlock", unlock))
    app.add_handler(CommandHandler("help", help_text))
    app.add_handler(CommandHandler("register", register_group))
    app.add_handler(CommandHandler("myquizzes", show_quizzes))
    app.add_handler(CommandHandler("groups", show_groups))
    app.add_handler(CommandHandler("cancel_edit", cancel_edit))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_text_import), group=1)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_edit_text), group=2)
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_question_edit_text), group=3)
    app.add_handler(conv)
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button_router))
    app.add_error_handler(on_error)

    log.info("SwayamQuizBot starting polling")
    app.run_polling(drop_pending_updates=False)


async def cancel_edit(update, context):
    if not is_authorized(update.effective_user.id, context):
        await update.effective_message.reply_text(denied_text())
        return
    context.user_data.pop("edit_field", None)
    context.user_data.pop("expect_edit_question", None)
    context.user_data.pop("expect_motivation", None)
    await update.effective_message.reply_text("Edit ବାତିଲ୍ ହେଲା।", reply_markup=owner_menu())


if __name__ == "__main__":
    main()
