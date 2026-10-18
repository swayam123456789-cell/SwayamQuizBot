import os
import re
import json
import time
import sqlite3
import secrets
import logging
from datetime import datetime, timezone
from telegram import Update, Poll
from telegram.ext import Application, CommandHandler, MessageHandler, PollAnswerHandler, ContextTypes, filters

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
PUBLIC_URL = os.environ.get('RENDER_EXTERNAL_URL', '').rstrip('/')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', secrets.token_urlsafe(24))
PORT = int(os.environ.get('PORT', '10000'))
DB_PATH = os.environ.get('DATABASE_PATH', 'swayam_quiz.db')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

if not TOKEN:
    raise RuntimeError('Set TELEGRAM_BOT_TOKEN in the hosting environment variables.')

# Temporary prompts for each Telegram user. Quiz data itself is stored in SQLite.
PENDING = {}
POLL_CACHE = {}

def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with conn() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS quizzes (
          code TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, title TEXT NOT NULL,
          created_at TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS questions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, pos INTEGER NOT NULL,
          question TEXT NOT NULL, options_json TEXT NOT NULL, correct_index INTEGER NOT NULL,
          UNIQUE(code, pos)
        );
        CREATE TABLE IF NOT EXISTS attempts (
          code TEXT NOT NULL, user_id INTEGER NOT NULL, display_name TEXT NOT NULL,
          score INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
          current_pos INTEGER NOT NULL DEFAULT 0, started_at REAL NOT NULL,
          finished_at REAL, finished INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(code, user_id)
        );
        CREATE TABLE IF NOT EXISTS polls (
          poll_id TEXT PRIMARY KEY, code TEXT NOT NULL, pos INTEGER NOT NULL,
          user_id INTEGER NOT NULL
        );
        ''')

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def make_code():
    return secrets.token_hex(3).upper()

def quiz_row(code):
    with conn() as c:
        return c.execute('SELECT * FROM quizzes WHERE code=?', (code.upper(),)).fetchone()

def get_questions(code):
    with conn() as c:
        return c.execute('SELECT * FROM questions WHERE code=? ORDER BY pos', (code.upper(),)).fetchall()

def parse_question_line(line):
    # Format: Question | option A | option B | option C | option D | correct option (1-4)
    parts = [p.strip() for p in line.split('|')]
    if len(parts) != 6 or any(not p for p in parts):
        raise ValueError('ପ୍ରତ୍ୟେକ ଲାଇନ୍‌ରେ ୬ଟି ଭାଗ ଦରକାର: ପ୍ରଶ୍ନ | A | B | C | D | ସଠିକ୍ ଅପ୍ସନ୍ ନମ୍ବର (1-4)')
    try:
        correct = int(parts[5]) - 1
    except ValueError:
        raise ValueError('ଶେଷ ଭାଗରେ ସଠିକ୍ ଅପ୍ସନ୍ ନମ୍ବର 1, 2, 3 କିମ୍ବା 4 ଲେଖ।')
    if correct not in range(4):
        raise ValueError('ସଠିକ୍ ଅପ୍ସନ୍ ନମ୍ବର 1 ରୁ 4 ମଧ୍ୟରେ ହେବା ଦରକାର।')
    if len(parts[0]) > 300 or any(len(x) > 100 for x in parts[1:5]):
        raise ValueError('ପ୍ରଶ୍ନ କିମ୍ବା ଅପ୍ସନ୍ ଅତ୍ୟଧିକ ଲମ୍ବା।')
    return parts[0], parts[1:5], correct

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.args:
        code = context.args[0].upper()
        await begin_quiz(update, context, code)
        return
    await update.effective_message.reply_text(
        'ନମସ୍କାର! ଏହା Swayam Quiz Bot.\n\n'
        'ନୂଆ Quiz: /newquiz Quiz ନାମ\n'
        'ପ୍ରଶ୍ନ ଯୋଡ଼: /addq CODE\n'
        'ଏକାଥରେ ଅନେକ ପ୍ରଶ୍ନ: /import CODE\n'
        'Quiz link: /link CODE\n'
        'Leaderboard: /leaderboard CODE\n'
        'Quiz ବନ୍ଦ: /close CODE\n'
        'Quiz ର code ଓ ସହାୟତା: /help')

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        'ବ୍ୟବହାର ପ୍ରଣାଳୀ\n\n'
        '1) /newquiz Indian Polity ଲେଖ।\n'
        '2) Bot ଯେଉଁ CODE ଦେବ, ତାହା ରଖ।\n'
        '3) /addq CODE ଲେଖି ଗୋଟିଏ ପ୍ରଶ୍ନ ପଠାଅ।\n'
        '4) ପ୍ରଶ୍ନର ଫର୍ମାଟ୍: ପ୍ରଶ୍ନ | ଅପ୍ସନ A | ଅପ୍ସନ B | ଅପ୍ସନ C | ଅପ୍ସନ D | ସଠିକ୍ ନମ୍ବର (1-4)\n'
        '5) ଅନେକ ପ୍ରଶ୍ନ ପାଇଁ /import CODE ଲେଖି ଏକାଧିକ ଲାଇନ୍ ପଠାଅ।\n'
        '6) /link CODE ଦ୍ୱାରା ଲିଙ୍କ୍ ନିଅ ଏବଂ share କର।\n'
        '7) /close CODE କଲେ quiz ବନ୍ଦ ହେବ ଏବଂ Top 10/cutoff ଦେଖାଯିବ।\n\n'
        'ଧ୍ୟାନ: ଗୋଟିଏ user ପ୍ରତି quiz ରେ ଗୋଟିଏ attempt ଦେଇପାରିବ।')

async def newquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = ' '.join(context.args).strip()
    if not title:
        await update.effective_message.reply_text('ଏଭଳି ଲେଖ: /newquiz September Current Affairs')
        return
    code = make_code()
    with conn() as c:
        while c.execute('SELECT 1 FROM quizzes WHERE code=?', (code,)).fetchone():
            code = make_code()
        c.execute('INSERT INTO quizzes(code, owner_id, title, created_at) VALUES(?,?,?,?)',
                   (code, update.effective_user.id, title[:100], now_iso()))
    await update.effective_message.reply_text(
        f'Quiz ତିଆରି ହେଲା!\nTitle: {title[:100]}\nCode: {code}\n\n'
        f'ଗୋଟିଏ ପ୍ରଶ୍ନ ପାଇଁ: /addq {code}\n'
        f'ଏକାଥରେ ଅନେକ ପ୍ରଶ୍ନ ପାଇଁ: /import {code}\n'
        f'ପ୍ରଶ୍ନ ଯୋଡ଼ି ସାରିଲେ: /link {code}')

async def addq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text('ଏଭଳି ଲେଖ: /addq CODE')
        return
    code = context.args[0].upper()
    q = quiz_row(code)
    if not q or q['owner_id'] != update.effective_user.id:
        await update.effective_message.reply_text('ଏହି quiz code ମିଳୁନାହିଁ କିମ୍ବା ଏହା ତୁମର quiz ନୁହେଁ।')
        return
    if q['closed']:
        await update.effective_message.reply_text('ଏହି quiz ବନ୍ଦ ହୋଇସାରିଛି।')
        return
    PENDING[update.effective_user.id] = ('one', code)
    await update.effective_message.reply_text(
        'ଏବେ ଗୋଟିଏ ଲାଇନ୍‌ରେ ପ୍ରଶ୍ନ ପଠାଅ:\n\n'
        'ପ୍ରଶ୍ନ | ଅପ୍ସନ A | ଅପ୍ସନ B | ଅପ୍ସନ C | ଅପ୍ସନ D | ସଠିକ୍ ଅପ୍ସନ ନମ୍ବର (1-4)\n\n'
        'ଉଦାହରଣ:\nଭାରତର ରାଜଧାନୀ କଣ? | ମୁମ୍ବାଇ | ନୂଆଦିଲ୍ଲୀ | କୋଲକାତା | ଚେନ୍ନାଇ | 2')

async def importq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text('ଏଭଳି ଲେଖ: /import CODE')
        return
    code = context.args[0].upper()
    q = quiz_row(code)
    if not q or q['owner_id'] != update.effective_user.id or q['closed']:
        await update.effective_message.reply_text('Quiz code ଭୁଲ୍, ଏହା ତୁମର ନୁହେଁ, କିମ୍ବା quiz ବନ୍ଦ।')
        return
    PENDING[update.effective_user.id] = ('bulk', code)
    await update.effective_message.reply_text(
        'ଏବେ ପ୍ରତ୍ୟେକ ପ୍ରଶ୍ନକୁ ଅଲଗା ଲାଇନ୍‌ରେ ପଠାଅ।\n'
        'ଫର୍ମାଟ୍: ପ୍ରଶ୍ନ | A | B | C | D | ସଠିକ୍ ନମ୍ବର (1-4)\n\n'
        'ପ୍ରତ୍ୟେକ ଲାଇନ୍‌ରେ ୬ଟି ଭାଗ ରହିବ।')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in PENDING:
        return
    mode, code = PENDING[uid]
    text = update.effective_message.text.strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    parsed = []
    try:
        for line in lines:
            parsed.append(parse_question_line(line))
    except ValueError as e:
        await update.effective_message.reply_text(f'ଫର୍ମାଟ୍ ଠିକ୍ ନୁହେଁ।\n{e}')
        return
    with conn() as c:
        quiz = c.execute('SELECT * FROM quizzes WHERE code=?', (code,)).fetchone()
        if not quiz or quiz['owner_id'] != uid or quiz['closed']:
            PENDING.pop(uid, None)
            await update.effective_message.reply_text('Quiz ମିଳୁନାହିଁ କିମ୍ବା ବନ୍ଦ ହୋଇଛି।')
            return
        pos = c.execute('SELECT COALESCE(MAX(pos),0) FROM questions WHERE code=?', (code,)).fetchone()[0]
        for question, options, correct in parsed:
            pos += 1
            c.execute('INSERT INTO questions(code,pos,question,options_json,correct_index) VALUES(?,?,?,?,?)',
                      (code, pos, question, json.dumps(options, ensure_ascii=False), correct))
    PENDING.pop(uid, None)
    await update.effective_message.reply_text(f'ସଫଳ! {len(parsed)}ଟି ପ୍ରଶ୍ନ ଯୋଡ଼ାଗଲା। ଏବେ /link {code} ଲେଖି link ନିଅ।')

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text('ଏଭଳି ଲେଖ: /link CODE')
        return
    code = context.args[0].upper()
    q = quiz_row(code)
    if not q or q['owner_id'] != update.effective_user.id:
        await update.effective_message.reply_text('Quiz code ମିଳୁନାହିଁ କିମ୍ବା ତୁମର ନୁହେଁ।')
        return
    questions = get_questions(code)
    if not questions:
        await update.effective_message.reply_text(f'ଏପର୍ଯ୍ୟନ୍ତ ପ୍ରଶ୍ନ ନାହିଁ। ପ୍ରଥମେ /addq {code} କିମ୍ବା /import {code} କର।')
        return
    me = await context.bot.get_me()
    url = f'https://t.me/{me.username}?start={code}'
    await update.effective_message.reply_text(f'Quiz: {q["title"]}\nପ୍ରଶ୍ନ: {len(questions)}\n\nShare କରିବା ପାଇଁ ଏହି link:\n{url}\n\nParticipant link ଖୋଲି Start କଲେ quiz ଆରମ୍ଭ ହେବ।')

async def begin_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    user = update.effective_user
    msg = update.effective_message
    qz = quiz_row(code)
    if not qz:
        await msg.reply_text('ଏହି quiz code ମିଳୁନାହିଁ। ଠିକ୍ link ଖୋଲ।')
        return
    if qz['closed']:
        await msg.reply_text('ଏହି quiz ବନ୍ଦ ହୋଇସାରିଛି।')
        return
    questions = get_questions(code)
    if not questions:
        await msg.reply_text('ଏହି quiz ରେ ପ୍ରଶ୍ନ ଯୋଡ଼ାଯାଇନାହିଁ।')
        return
    if msg.chat.type != 'private':
        await msg.reply_text('Quiz ଦେବା ପାଇଁ ଏହି bot କୁ private chat ରେ ଖୋଲ।')
        return
    with conn() as c:
        attempt = c.execute('SELECT * FROM attempts WHERE code=? AND user_id=?', (code, user.id)).fetchone()
        if attempt:
            if attempt['finished']:
                await msg.reply_text(f'ତୁମେ ଏହି quiz ଦେଇସାରିଛ। Score: {attempt["score"]}/{attempt["total"]}')
            else:
                await msg.reply_text('ତୁମର quiz attempt ପୂର୍ବରୁ ଆରମ୍ଭ ହୋଇଛି। ଦୟାକରି ପୂର୍ବ ପ୍ରଶ୍ନର ଉତ୍ତର ଦିଅ।')
            return
        display = user.full_name[:100]
        c.execute('INSERT INTO attempts(code,user_id,display_name,score,total,current_pos,started_at) VALUES(?,?,?,?,?,?,?)',
                  (code, user.id, display, 0, len(questions), 0, time.time()))
    await msg.reply_text(f'ଆରମ୍ଭ ହେଲା: {qz["title"]}\nମୋଟ ପ୍ରଶ୍ନ: {len(questions)}\nପ୍ରତ୍ୟେକ ପ୍ରଶ୍ନର ଗୋଟିଏ ଉତ୍ତର ବାଛ।')
    await send_question(context, code, user.id, 0, msg.chat_id)

async def send_question(context: ContextTypes.DEFAULT_TYPE, code: str, user_id: int, pos: int, chat_id: int):
    questions = get_questions(code)
    if pos >= len(questions):
        with conn() as c:
            c.execute('UPDATE attempts SET finished=1, finished_at=? WHERE code=? AND user_id=?', (time.time(), code, user_id))
            a = c.execute('SELECT * FROM attempts WHERE code=? AND user_id=?', (code, user_id)).fetchone()
        await context.bot.send_message(chat_id, f'Quiz ସମାପ୍ତ! ତୁମ Score: {a["score"]}/{a["total"]}\nLeaderboard ପାଇଁ quiz owner କୁ /leaderboard {code} ପଚାର।')
        return
    q = questions[pos]
    opts = json.loads(q['options_json'])
    sent = await context.bot.send_poll(chat_id=chat_id, question=f'Q{pos+1}. {q["question"]}', options=opts,
        type=Poll.QUIZ, correct_option_id=q['correct_index'], is_anonymous=False, allows_multiple_answers=False)
    with conn() as c:
        c.execute('INSERT OR REPLACE INTO polls(poll_id,code,pos,user_id) VALUES(?,?,?,?)', (sent.poll.id, code, pos, user_id))
        c.execute('UPDATE attempts SET current_pos=? WHERE code=? AND user_id=?', (pos, code, user_id))
    POLL_CACHE[sent.poll.id] = (code, pos, user_id)

async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pa = update.poll_answer
    if not pa or not pa.user or not pa.option_ids:
        return
    with conn() as c:
        p = c.execute('SELECT * FROM polls WHERE poll_id=?', (pa.poll_id,)).fetchone()
        if not p or p['user_id'] != pa.user.id:
            return
        code, pos, uid = p['code'], p['pos'], p['user_id']
        q = c.execute('SELECT correct_index FROM questions WHERE code=? AND pos=?', (code, pos+1)).fetchone()
        a = c.execute('SELECT * FROM attempts WHERE code=? AND user_id=?', (code, uid)).fetchone()
        if not q or not a or a['finished'] or a['current_pos'] != pos:
            return
        if pa.option_ids[0] == q['correct_index']:
            c.execute('UPDATE attempts SET score=score+1 WHERE code=? AND user_id=?', (code, uid))
        c.execute('DELETE FROM polls WHERE poll_id=?', (pa.poll_id,))
        next_pos = pos + 1
    await send_question(context, code, uid, next_pos, uid)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text('ଏଭଳି ଲେଖ: /leaderboard CODE')
        return
    code = context.args[0].upper()
    q = quiz_row(code)
    if not q or q['owner_id'] != update.effective_user.id:
        await update.effective_message.reply_text('Quiz code ମିଳୁନାହିଁ କିମ୍ବା ଏହା ତୁମର ନୁହେଁ।')
        return
    with conn() as c:
        rows = c.execute('SELECT display_name,score,total,finished_at,started_at FROM attempts WHERE code=? AND finished=1 ORDER BY score DESC, (finished_at-started_at) ASC, finished_at ASC LIMIT 10', (code,)).fetchall()
        total_people = c.execute('SELECT COUNT(*) FROM attempts WHERE code=? AND finished=1', (code,)).fetchone()[0]
    if not rows:
        await update.effective_message.reply_text('ଏପର୍ଯ୍ୟନ୍ତ କେହି quiz ସମାପ୍ତ କରିନାହାନ୍ତି।')
        return
    cutoff = rows[9]['score'] if len(rows) >= 10 else 'Top 10 ପୂରଣ ହୋଇନାହିଁ'
    lines = [f'🏆 {q["title"]} — Top 10', '']
    for i, r in enumerate(rows, 1):
        lines.append(f'{i}. {r["display_name"]} — {r["score"]}/{r["total"]}')
    lines += ['', f'Completed participants: {total_people}', f'Cutoff (10th rank score): {cutoff}']
    await update.effective_message.reply_text('\n'.join(lines))

async def close_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text('ଏଭଳି ଲେଖ: /close CODE')
        return
    code = context.args[0].upper()
    q = quiz_row(code)
    if not q or q['owner_id'] != update.effective_user.id:
        await update.effective_message.reply_text('Quiz code ମିଳୁନାହିଁ କିମ୍ବା ତୁମର ନୁହେଁ।')
        return
    with conn() as c:
        c.execute('UPDATE quizzes SET closed=1 WHERE code=?', (code,))
    await update.effective_message.reply_text('Quiz ବନ୍ଦ ହେଲା। ଏବେ ଫଳାଫଳ: /leaderboard ' + code)
    # If fewer than 10 completed, cutoff is displayed as not filled.

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    PENDING.pop(update.effective_user.id, None)
    await update.effective_message.reply_text('ଠିକ୍ ଅଛି, ଚାଲୁଥିବା input cancel ହେଲା।')

async def post_init(application: Application):
    if not PUBLIC_URL:
        log.warning('RENDER_EXTERNAL_URL not set. Webhook will not be configured.')
        return
    webhook_path = WEBHOOK_SECRET
    await application.bot.set_webhook(url=f'{PUBLIC_URL}/{webhook_path}', secret_token=WEBHOOK_SECRET,
                                      allowed_updates=['message', 'poll_answer', 'callback_query'])
    log.info('Webhook configured.')

async def main():
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('newquiz', newquiz))
    app.add_handler(CommandHandler('addq', addq))
    app.add_handler(CommandHandler('import', importq))
    app.add_handler(CommandHandler('link', link_cmd))
    app.add_handler(CommandHandler('leaderboard', leaderboard))
    app.add_handler(CommandHandler('close', close_quiz))
    app.add_handler(CommandHandler('cancel', cancel))
    app.add_handler(PollAnswerHandler(poll_answer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Render's webhook mode binds an HTTP server and receives Telegram updates.
    await app.initialize()
    await app.start()
    await app.updater.start_webhook(listen='0.0.0.0', port=PORT, url_path=WEBHOOK_SECRET,
        webhook_url=f'{PUBLIC_URL}/{WEBHOOK_SECRET}' if PUBLIC_URL else None,
        secret_token=WEBHOOK_SECRET if PUBLIC_URL else None,
        allowed_updates=['message', 'poll_answer', 'callback_query'])
    log.info('Bot started on port %s', PORT)
    import asyncio
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
