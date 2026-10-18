import os
import sqlite3
import asyncio
import threading
import logging
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("swayamquiz")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "swayam_quiz.db")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add BOT_TOKEN in Render Environment.")

# Render Web Service needs an HTTP port; Telegram polling runs in a background thread.
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
            owner_id INTEGER NOT NULL, title TEXT NOT NULL, topic TEXT DEFAULT '',
            intro TEXT DEFAULT '', timer INTEGER DEFAULT 15, funny TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id INTEGER NOT NULL, q_no INTEGER NOT NULL, question TEXT NOT NULL,
            a TEXT NOT NULL, b TEXT NOT NULL, c TEXT NOT NULL, d TEXT NOT NULL,
            answer TEXT NOT NULL, FOREIGN KEY(quiz_id) REFERENCES quizzes(id)
        );
        CREATE TABLE IF NOT EXISTS groups_seen(
            chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, added_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS scores(
            id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL,
            points INTEGER DEFAULT 0, UNIQUE(quiz_id, chat_id, user_id)
        );
        """)

TITLE, TOPIC, INTRO, TIMER, QUESTION, A, B, C, D, ANSWER = range(11)

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Create Quiz", callback_data="create")],
        [InlineKeyboardButton("📚 My Quizzes", callback_data="myquizzes"),
         InlineKeyboardButton("👥 My Groups", callback_data="groups")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    await update.effective_message.reply_text(
        "👋 ନମସ୍କାର! Swayam Quiz Bot କୁ ସ୍ୱାଗତ।\n\n"
        "TXT file ଦରକାର ନାହିଁ। Create Quiz ଦବାଇ ପ୍ରଶ୍ନଗୁଡ଼ିକୁ ଗୋଟିଏ ପରେ ଗୋଟିଏ ଲେଖନ୍ତୁ।",
        reply_markup=main_menu()
    )

async def help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ("📘 ବ୍ୟବହାର:\n"
            "1. Private chat ରେ Create Quiz ଦବାନ୍ତୁ।\n"
            "2. Quiz ନାମ, Topic, Intro, Timer ଓ Funny message ଦିଅନ୍ତୁ।\n"
            "3. Question ଏବଂ 4ଟି option ଦିଅନ୍ତୁ; correct answer A/B/C/D ବାଛନ୍ତୁ।\n"
            "4. Group ରେ bot କୁ admin କରି /register ପଠାନ୍ତୁ।\n"
            "5. Private chat ର My Quizzes ରୁ Start ବାଛନ୍ତୁ।")
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=main_menu())
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("କାମ ବାତିଲ୍ ହେଲା।", reply_markup=main_menu())
    return ConversationHandler.END

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["draft"] = {"questions": [], "timer": 15}
    await query.message.reply_text("📝 Quiz ର ନାମ ଲେଖନ୍ତୁ।\nବାତିଲ୍ ପାଇଁ /cancel")
    return TITLE

async def get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["title"] = update.message.text.strip()[:120]
    await update.message.reply_text("📌 Topic ଲେଖନ୍ତୁ (ନଥିଲେ - ଲେଖନ୍ତୁ)।")
    return TOPIC

async def get_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.text.strip()
    context.user_data["draft"]["topic"] = "" if v == "-" else v[:120]
    await update.message.reply_text("📣 Quiz ଆରମ୍ଭରେ ଦେଖାଯିବା Intro ଲେଖନ୍ତୁ (ନଥିଲେ -)।")
    return INTRO

async def get_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.text.strip()
    context.user_data["draft"]["intro"] = "" if v == "-" else v[:800]
    await update.message.reply_text("⏱️ ପ୍ରତି ପ୍ରଶ୍ନର timer କେତେ seconds? 5 ରୁ 120 ମଧ୍ୟରେ ଲେଖନ୍ତୁ।")
    return TIMER

async def get_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if not 5 <= n <= 120:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("5 ରୁ 120 ମଧ୍ୟରେ ଗୋଟିଏ number ଲେଖନ୍ତୁ।")
        return TIMER
    context.user_data["draft"]["timer"] = n
    await update.message.reply_text("😂 Quiz ଶେଷରେ Funny message ଲେଖନ୍ତୁ (ନଥିଲେ -)।")
    return FUNNY

async def get_funny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.text.strip()
    context.user_data["draft"]["funny"] = "" if v == "-" else v[:500]
    await update.message.reply_text("❓ ପ୍ରଶ୍ନ 1 ଲେଖନ୍ତୁ।")
    return QUESTION

async def get_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["current"] = {"question": update.message.text.strip()[:1000]}
    await update.message.reply_text("Option A ଲେଖନ୍ତୁ।")
    return A

async def get_a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["current"]["a"] = update.message.text.strip()[:300]
    await update.message.reply_text("Option B ଲେଖନ୍ତୁ।")
    return B

async def get_b(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["current"]["b"] = update.message.text.strip()[:300]
    await update.message.reply_text("Option C ଲେଖନ୍ତୁ।")
    return C

async def get_c(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["current"]["c"] = update.message.text.strip()[:300]
    await update.message.reply_text("Option D ଲେଖନ୍ତୁ।")
    return D

async def get_d(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["draft"]["current"]["d"] = update.message.text.strip()[:300]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data="ans_A"),
        InlineKeyboardButton("B", callback_data="ans_B"),
        InlineKeyboardButton("C", callback_data="ans_C"),
        InlineKeyboardButton("D", callback_data="ans_D")
    ]])
    await update.message.reply_text("✅ ସଠିକ୍ ଉତ୍ତର ବାଛନ୍ତୁ:", reply_markup=kb)
    return ANSWER

async def get_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    draft = context.user_data.get("draft")
    if not draft:
        await q.message.reply_text("Draft ମିଳିଲା ନାହିଁ। ପୁଣି Create Quiz କରନ୍ତୁ।")
        return ConversationHandler.END
    current = draft["current"]
    current["answer"] = q.data[-1]
    draft["questions"].append(current)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add another question", callback_data="more")],
        [InlineKeyboardButton("💾 Save Quiz", callback_data="save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_draft")]
    ])
    await q.message.reply_text(
        f"✅ ପ୍ରଶ୍ନ {len(draft['questions'])} ଯୋଡ଼ାଗଲା। ଆଉ ପ୍ରଶ୍ନ ଯୋଡ଼ିବେ କି Save କରିବେ?",
        reply_markup=kb
    )
    return QUESTION

async def draft_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel_draft":
        context.user_data.pop("draft", None)
        await q.message.reply_text("Draft ବାତିଲ୍ ହେଲା।", reply_markup=main_menu())
        return ConversationHandler.END
    draft = context.user_data.get("draft")
    if not draft:
        await q.message.reply_text("Draft ମିଳିଲା ନାହିଁ। /start କରନ୍ତୁ।")
        return ConversationHandler.END
    if q.data == "more":
        await q.message.reply_text(f"❓ ପ୍ରଶ୍ନ {len(draft['questions']) + 1} ଲେଖନ୍ତୁ।")
        return QUESTION
    if q.data == "save":
        if not draft["questions"]:
            await q.message.reply_text("ଅତି କମରେ 1ଟି ପ୍ରଶ୍ନ ଦରକାର।")
            return QUESTION
        with connect() as db:
            cur = db.execute(
                "INSERT INTO quizzes(owner_id,title,topic,intro,timer,funny) VALUES(?,?,?,?,?,?)",
                (q.from_user.id, draft["title"], draft["topic"], draft["intro"], draft["timer"], draft["funny"])
            )
            quiz_id = cur.lastrowid
            for i, item in enumerate(draft["questions"], 1):
                db.execute("INSERT INTO questions(quiz_id,q_no,question,a,b,c,d,answer) VALUES(?,?,?,?,?,?,?,?)",
                           (quiz_id, i, item["question"], item["a"], item["b"], item["c"], item["d"], item["answer"]))
        context.user_data.pop("draft", None)
        await q.message.reply_text(f"🎉 Quiz save ହୋଇଗଲା!\nନାମ: {draft['title']}\nପ୍ରଶ୍ନ: {len(draft['questions'])}\nID: {quiz_id}",
                                   reply_markup=main_menu())
        return ConversationHandler.END

async def show_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    user_id = query.from_user.id if query else update.effective_user.id
    with connect() as db:
        rows = db.execute("SELECT id,title,topic FROM quizzes WHERE owner_id=? ORDER BY id DESC", (user_id,)).fetchall()
    target = query.message if query else update.effective_message
    if not rows:
        await target.reply_text("ଏଯାଏଁ Quiz save ହୋଇନାହିଁ। Create Quiz ଦବାନ୍ତୁ।", reply_markup=main_menu())
        return
    buttons = [[InlineKeyboardButton(f"▶️ {r['title']} (#{r['id']})", callback_data=f"pickquiz_{r['id']}")] for r in rows]
    await target.reply_text("📚 ତୁମ Saved Quizzes — ଆରମ୍ଭ କରିବାକୁ ବାଛନ୍ତୁ:", reply_markup=InlineKeyboardMarkup(buttons))

async def show_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()
    user_id = query.from_user.id if query else update.effective_user.id
    with connect() as db:
        rows = db.execute("SELECT chat_id,title FROM groups_seen WHERE added_by=? ORDER BY title", (user_id,)).fetchall()
    target = query.message if query else update.effective_message
    if not rows:
        await target.reply_text("ପ୍ରଥମେ ତୁମ group ରେ bot କୁ admin କରି /register ପଠାଅ।", reply_markup=main_menu())
        return
    await target.reply_text("👥 Registered Groups:\n" + "\n".join("• " + r["title"] for r in rows))

async def register_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("ଏହି command କୁ ତୁମ group ଭିତରେ /register ଭାବେ ପଠାଅ।")
        return
    try:
        member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("ପ୍ରଥମେ ଏହି group ରେ bot କୁ admin କରନ୍ତୁ।")
            return
    except Exception:
        await update.message.reply_text("Bot କୁ admin କରି ପୁଣି /register କରନ୍ତୁ।")
        return
    with connect() as db:
        db.execute("INSERT INTO groups_seen(chat_id,title,added_by) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, added_by=excluded.added_by",
                   (chat.id, chat.title or "Telegram Group", user.id))
    await update.message.reply_text("✅ Group register ହେଲା। Private chat ରେ My Quizzes ରୁ quiz ଆରମ୍ଭ କରନ୍ତୁ।")

async def pick_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    quiz_id = int(q.data.split("_", 1)[1])
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, q.from_user.id)).fetchone()
        groups = db.execute("SELECT chat_id,title FROM groups_seen WHERE added_by=?", (q.from_user.id,)).fetchall()
    if not quiz:
        await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ।")
        return
    if not groups:
        await q.message.reply_text("ପ୍ରଥମେ group ରେ bot admin କରି /register ପଠାନ୍ତୁ।")
        return
    context.user_data["pending_quiz"] = quiz_id
    await q.message.reply_text(f"Quiz: {quiz['title']}\nକେଉଁ group ରେ ଚଲାଇବେ?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(g["title"], callback_data=f"run_{quiz_id}_{g['chat_id']}")] for g in groups]))

async def send_question(context: ContextTypes.DEFAULT_TYPE, quiz_id: int, chat_id: int, index: int):
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        questions = db.execute("SELECT * FROM questions WHERE quiz_id=? ORDER BY q_no", (quiz_id,)).fetchall()
    if not quiz or index >= len(questions):
        await finish_quiz(context, quiz_id, chat_id, quiz)
        return
    qrow = questions[index]
    options = [(letter, qrow[letter.lower()]) for letter in "ABCD"]
    keyboard = [[InlineKeyboardButton(f"{letter}. {value}", callback_data=f"vote_{quiz_id}_{chat_id}_{index}_{letter}")]
               for letter, value in options]
    msg = await context.bot.send_message(chat_id, f"❓ {index+1}/{len(questions)}\n{qrow['question']}",
                                         reply_markup=InlineKeyboardMarkup(keyboard))
    context.application.bot_data.setdefault("active", {})[(quiz_id, chat_id)] = {
        "index": index, "answer": qrow["answer"], "message_id": msg.message_id,
        "deadline": asyncio.get_running_loop().time() + int(quiz["timer"]), "locked": False
    }
    context.application.create_task(question_timeout(context, quiz_id, chat_id, index, int(quiz["timer"])))

async def question_timeout(context, quiz_id, chat_id, index, seconds):
    await asyncio.sleep(seconds)
    active = context.application.bot_data.get("active", {}).get((quiz_id, chat_id))
    if not active or active["index"] != index or active["locked"]:
        return
    active["locked"] = True
    try:
        await context.bot.edit_message_reply_markup(chat_id, active["message_id"], reply_markup=None)
    except Exception: pass
    await context.bot.send_message(chat_id, "⏰ ସମୟ ଶେଷ!")
    await send_question(context, quiz_id, chat_id, index + 1)

async def run_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, quiz_s, chat_s = q.data.split("_", 2)
    quiz_id, chat_id = int(quiz_s), int(chat_s)
    with connect() as db:
        quiz = db.execute("SELECT * FROM quizzes WHERE id=? AND owner_id=?", (quiz_id, q.from_user.id)).fetchone()
        questions = db.execute("SELECT COUNT(*) n FROM questions WHERE quiz_id=?", (quiz_id,)).fetchone()["n"]
    if not quiz or not questions:
        await q.message.reply_text("Quiz ମିଳିଲା ନାହିଁ କିମ୍ବା ପ୍ରଶ୍ନ ନାହିଁ।")
        return
    if quiz["intro"]:
        await context.bot.send_message(chat_id, f"📣 {quiz['title']}\n{quiz['intro']}")
    await context.bot.send_message(chat_id, f"🎯 Quiz ଆରମ୍ଭ!\nTopic: {quiz['topic'] or 'General'}\nପ୍ରଶ୍ନ: {questions}")
    with connect() as db:
        db.execute("DELETE FROM scores WHERE quiz_id=? AND chat_id=?", (quiz_id, chat_id))
    await send_question(context, quiz_id, chat_id, 0)
    await q.message.reply_text("🚀 Quiz group ରେ ଆରମ୍ଭ ହୋଇଗଲା।")

async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, quiz_s, chat_s, index_s, choice = q.data.split("_")
    quiz_id, chat_id, index = int(quiz_s), int(chat_s), int(index_s)
    active = context.application.bot_data.get("active", {}).get((quiz_id, chat_id))
    if not active or active["index"] != index or active["locked"]:
        await q.answer("ସମୟ ଶେଷ!", show_alert=True)
        return
    active["locked"] = True
    correct = choice == active["answer"]
    user = q.from_user
    name = user.full_name or user.username or str(user.id)
    with connect() as db:
        db.execute("INSERT INTO scores(quiz_id,chat_id,user_id,username,points) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(quiz_id,chat_id,user_id) DO UPDATE SET points=points+excluded.points, username=excluded.username",
                   (quiz_id, chat_id, user.id, name, 1 if correct else 0))
    await q.answer("✅ ସଠିକ୍!" if correct else f"❌ ଭୁଲ! ଉତ୍ତର {active['answer']}", show_alert=True)
    try: await context.bot.edit_message_reply_markup(chat_id, active["message_id"], reply_markup=None)
    except Exception: pass
    await context.bot.send_message(chat_id, f"{name}: {'+1 point 🎉' if correct else '0 point'}")
    await send_question(context, quiz_id, chat_id, index + 1)

async def finish_quiz(context, quiz_id, chat_id, quiz):
    context.application.bot_data.get("active", {}).pop((quiz_id, chat_id), None)
    with connect() as db:
        rows = db.execute("SELECT username,points FROM scores WHERE quiz_id=? AND chat_id=? ORDER BY points DESC, username LIMIT 10",
                          (quiz_id, chat_id)).fetchall()
    text = "🏆 TOP 10 LEADERBOARD\n\n"
    if not rows: text += "କେହି score କରିନାହାନ୍ତି।"
    else:
        text += "\n".join(f"{i}. {r['username']} — {r['points']} point" for i, r in enumerate(rows, 1))
    await context.bot.send_message(chat_id, text)
    if quiz and quiz["funny"]:
        await context.bot.send_message(chat_id, quiz["funny"])
    await context.bot.send_message(chat_id, "🎉 Quiz ସମାପ୍ତ! ପୁଣି ଖେଳିବାକୁ owner ଙ୍କୁ କୁହନ୍ତୁ।")

async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data == "myquizzes": return await show_quizzes(update, context)
    if data == "groups": return await show_groups(update, context)
    if data == "help": return await help_text(update, context)
    if data.startswith("pickquiz_"): return await pick_quiz(update, context)
    if data.startswith("run_"): return await run_quiz(update, context)
    if data.startswith("vote_"): return await vote(update, context)
    await q.answer()

def run_bot():
    async def runner():
        init_db()
        app = Application.builder().token(TOKEN).build()
        conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(create_start, pattern="^create$")],
            states={
                TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_title)],
                TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_topic)],
                INTRO: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_intro)],
                TIMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_timer)],
                FUNNY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_funny)],
                QUESTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, get_question),
                    CallbackQueryHandler(draft_next, pattern="^(more|save|cancel_draft)$")
                ],
                A: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_a)],
                B: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_b)],
                C: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_c)],
                D: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_d)],
                ANSWER: [CallbackQueryHandler(get_answer, pattern="^ans_[ABCD]$")],
                     D: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_d)],
                ANSWER: [CallbackQueryHandler(get_answer, pattern="^ans_[ABCD]$")],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            allow_reentry=True,
        )
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_text))
        app.add_handler(CommandHandler("register", register_group))
        app.add_handler(CommandHandler("myquizzes", show_quizzes))
        app.add_handler(CommandHandler("groups", show_groups))
        app.add_handler(conv)
        app.add_handler(CallbackQueryHandler(button_router))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("SwayamQuizBot polling started")
        await asyncio.Event().wait()
    asyncio.run(runner())

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port)
       
