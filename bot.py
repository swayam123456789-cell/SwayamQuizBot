import os
import re
import time
import sqlite3
import secrets
import logging
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
)
from telegram.constants import PollType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
DB_PATH = os.environ.get("DATABASE_PATH", "swayam_quiz.db")
if not TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN as a secret/environment variable.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
QUESTION, OPTIONS, ANSWER, TITLE = range(4)
DB = None

def db():
    global DB
    if DB is None:
        DB = sqlite3.connect(DB_PATH, check_same_thread=False)
        DB.row_factory = sqlite3.Row
        DB.execute("PRAGMA journal_mode=WAL")
        DB.executescript("""
        CREATE TABLE IF NOT EXISTS quizzes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          code TEXT UNIQUE NOT NULL,
          owner_id INTEGER NOT NULL,
          title TEXT NOT NULL,
          created_at TEXT NOT NULL,
          is_published INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS questions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          quiz_code TEXT NOT NULL,
          position INTEGER NOT NULL,
          question TEXT NOT NULL,
          options_json TEXT NOT NULL,
          correct_index INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          quiz_code TEXT NOT NULL,
          participant_id TEXT NOT NULL,
          display_name TEXT NOT NULL,
          score INTEGER NOT NULL,
          total INTEGER NOT NULL,
          elapsed_seconds INTEGER NOT NULL,
          submitted_at TEXT NOT NULL,
          UNIQUE(quiz_code, participant_id)
        );
        """)
        DB.commit()
    return DB

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def parse_mcqs(text):
    blocks = re.split(r"(?im)(?=^\s*(?:Q(?:uestion)?\s*)?\d+\s*[\).:\-])", text.strip())
    out = []
    for block in blocks:
        lines = [x.strip() for x in block.splitlines() if x.strip()]
        if not lines:
            continue
        qlines, opts, ans = [], {}, None
        for line in lines:
            m = re.match(r"^\s*([A-D])\s*[\)\.\:\-]\s*(.+)$", line, re.I)
            if m:
                opts[m.group(1).upper()] = m.group(2).strip()
                continue
            m = re.match(r"^\s*(?:correct\s*)?(?:answer|ans)\s*[:\-]?\s*([A-D])\b", line, re.I)
            if m:
                ans = m.group(1).upper()
                continue
            qlines.append(line)
        q = "\n".join(qlines).strip()
        q = re.sub(r"^\s*(?:Q(?:uestion)?\s*)?\d+\s*[\).:\-]\s*", "", q, flags=re.I).strip()
        if q and all(k in opts for k in "ABCD") and ans in opts:
            out.append({"question": q[:300], "options": [opts[k][:100] for k in "ABCD"], "correct_index": "ABCD".index(ans)})
    return out

def create_quiz(owner_id, title, questions):
    d = db()
    code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
    d.execute("INSERT INTO quizzes(code,owner_id,title,created_at,is_published) VALUES(?,?,?,?,0)",
              (code, owner_id, title[:100], now_iso()))
    for i, q in enumerate(questions, 1):
        d.execute("INSERT INTO questions(quiz_code,position,question,options_json,correct_index) VALUES(?,?,?,?,?)",
                  (code, i, q["question"], json.dumps(q["options"], ensure_ascii=False), q["correct_index"]))
    d.commit()
    return code

def get_questions(code):
    return db().execute("SELECT * FROM questions WHERE quiz_code=? ORDER BY position", (code,)).fetchall()

def leaderboard(code):
    return db().execute(
        "SELECT * FROM attempts WHERE quiz_code=? ORDER BY score DESC, elapsed_seconds ASC, submitted_at ASC LIMIT 10",
        (code,)
    ).fetchall()

def cutoff_score(code):
    rows = db().execute(
        "SELECT score FROM attempts WHERE quiz_code=? ORDER BY score DESC, elapsed_seconds ASC, submitted_at ASC LIMIT 10",
        (code,)
    ).fetchall()
    return rows[9]["score"] if len(rows) >= 10 else (min([r["score"] for r in rows]) if rows else None)

def quiz_url(code):
    return f"{PUBLIC_BASE_URL}/quiz/{code}" if PUBLIC_BASE_URL else ""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("➕ Manual Quiz ବନାଅ", callback_data="manual")],
        [InlineKeyboardButton("📋 Copy-Paste MCQ", callback_data="paste")],
        [InlineKeyboardButton("📊 ମୋ Quiz ଗୁଡ଼ିକ", callback_data="myquizzes")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    await update.effective_message.reply_text(
        "ନମସ୍କାର! 👋 ମୁଁ Swayam Quiz Bot।\n\n"
        "ସହଜରେ Quiz ବନାଅ, Link Share କର ଏବଂ Result ଦେଖ।\n"
        "Quiz ଶେଷରେ Score, Top 10 Leaderboard ଓ Top 10 Cutoff ମିଳିବ।\n\n"
        "ତଳର Menu ରୁ ବାଛ:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "manual":
        context.user_data["draft"] = []
        await q.message.reply_text("📝 Manual Quiz ଆରମ୍ଭ। ପ୍ରଥମେ Quiz ର ନାମ ପଠାଅ।")
        return TITLE
    if q.data == "paste":
        context.user_data["awaiting_paste"] = True
        await q.message.reply_text(
            "📋 ଏକ message ରେ MCQ ପଠାଅ। Format:\n\n"
            "Q1. ଭାରତର ରାଜଧାନୀ କ'ଣ?\nA) ମୁମ୍ବାଇ\nB) ନୂଆଦିଲ୍ଲୀ\nC) କଟକ\nD) ଚେନ୍ନାଇ\nAnswer: B\n\n"
            "ପ୍ରତ୍ୟେକ ପ୍ରଶ୍ନରେ Q1., A-D options ଓ Answer: B ରହିବ।"
        )
        return ConversationHandler.END
    if q.data == "myquizzes":
        rows = db().execute("SELECT code,title,is_published FROM quizzes WHERE owner_id=? ORDER BY id DESC LIMIT 10",
                            (q.from_user.id,)).fetchall()
        if not rows:
            await q.message.reply_text("ଏପର୍ଯ୍ୟନ୍ତ Quiz ନାହିଁ। ➕ Manual Quiz କିମ୍ବା 📋 Copy-Paste ବାଛ।")
            return ConversationHandler.END
        lines = ["📚 ତୁମର Quiz:"]
        for r in rows:
            lines.append(f"• {r['title']} — code: {r['code']} — {'Published' if r['is_published'] else 'Draft'}")
            lines.append(f"  /results_{r['code']}")
        await q.message.reply_text("\n".join(lines))
        return ConversationHandler.END
    if q.data == "help":
        await q.message.reply_text(
            "Manual: /newquiz → title → question → options → answer → /addq → /publishquiz\n"
            "Copy-Paste: Q1., A), B), C), D), Answer: B format ରେ ପଠାଅ।\n"
            "Leaderboard: /leaderboard CODE\n"
            "Results: /results CODE\n\n"
            "Shareable web quiz ଓ automatic scoring ପାଇଁ app କୁ host କରି PUBLIC_BASE_URL ସେଟ୍ କରିବା ଆବଶ୍ୟକ।"
        )
    return ConversationHandler.END

async def newquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"] = []
    await update.effective_message.reply_text("Quiz ର ନାମ ପଠାଅ:")
    return TITLE

async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft_title"] = (update.effective_message.text or "My Quiz").strip()[:100]
    await update.effective_message.reply_text(
        "ପ୍ରଶ୍ନ ପଠାଅ। ତା'ପରେ ଚାରିଟି option ପଠାଇବାକୁ କହିବି।"
    )
    return QUESTION

async def question_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending_q"] = (update.effective_message.text or "").strip()[:300]
    if not context.user_data["pending_q"]:
        await update.effective_message.reply_text("ପ୍ରଶ୍ନ text ପଠାଅ।")
        return QUESTION
    await update.effective_message.reply_text("ଚାରିଟି option ପଠାଅ:\nA) ...\nB) ...\nC) ...\nD) ...")
    return OPTIONS

async def options_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    opts = {}
    for line in (update.effective_message.text or "").splitlines():
        m = re.match(r"^\s*([A-D])\s*[\)\.\:\-]\s*(.+)$", line.strip(), re.I)
        if m:
            opts[m.group(1).upper()] = m.group(2).strip()
    if not all(k in opts for k in "ABCD"):
        await update.effective_message.reply_text("A), B), C), D) ଚାରିଟି option ଦିଅ।")
        return OPTIONS
    context.user_data["pending_opts"] = [opts[k][:100] for k in "ABCD"]
    await update.effective_message.reply_text("Correct answer କେଉଁଟି? କେବଳ A, B, C କିମ୍ବା D ପଠାଅ।")
    return ANSWER

async def answer_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = (update.effective_message.text or "").strip().upper()
    m = re.fullmatch(r"(?:ANSWER\s*[:\-]?\s*)?([A-D])", ans)
    if not m:
        await update.effective_message.reply_text("କେବଳ A, B, C କିମ୍ବା D ପଠାଅ।")
        return ANSWER
    context.user_data.setdefault("draft", []).append({
        "question": context.user_data.pop("pending_q"),
        "options": context.user_data.pop("pending_opts"),
        "correct_index": "ABCD".index(m.group(1))
    })
    n = len(context.user_data["draft"])
    await update.effective_message.reply_text(
        f"✅ ପ୍ରଶ୍ନ ଯୋଡ଼ାଗଲା। ମୋଟ {n}ଟି।\n"
        "ଆଉ ପ୍ରଶ୍ନ ପାଇଁ /addq ଲେଖ; ସବୁ ସରିଲେ /publishquiz ଲେଖ।"
    )
    return ConversationHandler.END

async def addq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "draft" not in context.user_data:
        context.user_data["draft"] = []
        await update.effective_message.reply_text("ପ୍ରଥମେ /newquiz କରି Quiz ଆରମ୍ଭ କର।")
        return ConversationHandler.END
    await update.effective_message.reply_text("ପରବର୍ତ୍ତୀ ପ୍ରଶ୍ନ ପଠାଅ:")
    return QUESTION

async def publishquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data.get("draft", [])
    if not draft:
        await update.effective_message.reply_text("ପ୍ରଥମେ /newquiz କରି ପ୍ରଶ୍ନ ଯୋଡ଼।")
        return
    title = context.user_data.get("draft_title", "My Quiz")
    code = create_quiz(update.effective_user.id, title, draft)
    context.user_data.pop("draft", None)
    context.user_data.pop("draft_title", None)
    d = db()
    d.execute("UPDATE quizzes SET is_published=1 WHERE code=?", (code,))
    d.commit()
    url = quiz_url(code)
    msg = f"🎉 Quiz ସେଭ୍ ହେଲା!\n\n📘 {title}\n🧮 ପ୍ରଶ୍ନ: {len(draft)}\n🔑 Code: {code}"
    if url:
        msg += f"\n\n🔗 Quiz Link: {url}\nଏହି Link Channel ରେ Share କର।"
    else:
        msg += "\n\n⚠️ Quiz Link ପାଇଁ Hosting ସେଟ୍ ହେବା ବାକି ଅଛି।"
    msg += f"\n\nResult: /results {code}\nTop 10: /leaderboard {code}"
    await update.effective_message.reply_text(msg)

async def copy_paste_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_paste"):
        # allow format to work without button too
        text = update.effective_message.text or ""
        if not re.search(r"(?im)^\s*(?:Q(?:uestion)?\s*)?\d+\s*[\).:\-]", text):
            return
    text = update.effective_message.text or ""
    questions = parse_mcqs(text)
    context.user_data["awaiting_paste"] = False
    if not questions:
        await update.effective_message.reply_text("MCQ ବୁଝିପାରିଲି ନାହିଁ। /help ଦେଖି format ଅନୁସାରେ ପଠାଅ।")
        return
    context.user_data["draft"] = questions
    context.user_data["draft_title"] = "MCQ Quiz " + datetime.now().strftime("%d-%m %H:%M")
    await update.effective_message.reply_text(
        f"✅ {len(questions)}ଟି MCQ ମିଳିଲା। /publishquiz ଲେଖି Quiz ସେଭ୍ କର।"
    )

async def results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("ବ୍ୟବହାର: /results QUIZCODE")
        return
    code = context.args[0]
    quiz = db().execute("SELECT * FROM quizzes WHERE code=?", (code,)).fetchone()
    if not quiz:
        await update.effective_message.reply_text("Quiz Code ମିଳିଲା ନାହିଁ।")
        return
    rows = db().execute("SELECT * FROM attempts WHERE quiz_code=? ORDER BY score DESC, elapsed_seconds ASC LIMIT 10",
                        (code,)).fetchall()
    total = db().execute("SELECT COUNT(*) n FROM attempts WHERE quiz_code=?", (code,)).fetchone()["n"]
    cut = cutoff_score(code)
    txt = f"📊 {quiz['title']} — Result\n👥 Participants: {total}\n"
    txt += f"🎯 Top 10 Cutoff: {cut if cut is not None else 'ଏପର୍ଯ୍ୟନ୍ତ Result ନାହିଁ'}\n\n"
    if rows:
        txt += "🏆 Top 10:\n"
        for i, r in enumerate(rows, 1):
            txt += f"{i}. {r['display_name']} — {r['score']}/{r['total']} — {r['elapsed_seconds']} sec\n"
    else:
        txt += "ଏପର୍ଯ୍ୟନ୍ତ କେହି Quiz ଦେଇନାହାନ୍ତି।"
    await update.effective_message.reply_text(txt)

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await results(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("ବାତିଲ ହେଲା। /start କରି ପୁଣି ଆରମ୍ଭ କର।")
    return ConversationHandler.END

def main():
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("newquiz", newquiz), CommandHandler("addq", addq)],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_received)],
            QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, question_received)],
            OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, options_received)],
            ANSWER: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", lambda u,c: help_command(u,c)))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(conv)
    app.add_handler(CommandHandler("publishquiz", publishquiz))
    app.add_handler(CommandHandler("results", results))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, copy_paste_handler))
    app.run_polling()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Manual: /newquiz → title → question → options → answer → /addq → /publishquiz\n"
        "Copy-Paste: Q1., A), B), C), D), Answer: B format ରେ ପଠାଅ।\n"
        "Leaderboard: /leaderboard CODE | Results: /results CODE\n"
        "Automatic score collection + shareable link ଏହି starter code ରେ web quiz backend ପରବର୍ତ୍ତୀ ପଦକ୍ଷେପରେ ଯୋଡ଼ିବାକୁ ପଡ଼ିବ।"
    )

if __name__ == "__main__":
    main()
