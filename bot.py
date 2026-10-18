import os, re, json, sqlite3, asyncio, time, html, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import TelegramError, BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)
TOKEN = os.getenv('BOT_TOKEN', '').strip()
DB_PATH = os.getenv('DB_PATH', 'swayam_quiz.db')
DEFAULT_TIMER = int(os.getenv('DEFAULT_TIMER', '15'))
if not TOKEN:
    raise RuntimeError('Missing BOT_TOKEN. Add it in Render > Environment.')

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute('PRAGMA foreign_keys=ON')
db.executescript('''
CREATE TABLE IF NOT EXISTS quizzes(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, title TEXT NOT NULL, topic TEXT DEFAULT '', intro TEXT DEFAULT '', funny TEXT DEFAULT '', timer INTEGER NOT NULL, questions_json TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS groups_seen(chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, owner_id INTEGER NOT NULL, seen_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS active_quizzes(chat_id INTEGER PRIMARY KEY, quiz_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, qindex INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'running', message_id INTEGER);
CREATE TABLE IF NOT EXISTS scores(chat_id INTEGER NOT NULL, quiz_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, answered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,quiz_id,user_id));
''')
db.commit()
runtime = {}


def parse_txt(raw):
    lines = [x.strip() for x in raw.replace('\r', '').split('\n')]
    result, cur = [], None
    def finish():
        nonlocal cur
        if not cur: return
        opts = cur['options']; ans = cur.get('answer')
        if len(opts) >= 2 and cur['question']:
            for i, opt in enumerate(opts):
                if opt.startswith('*'):
                    opts[i] = opt.lstrip('* ').strip()
                    if ans is None: ans = i
            cur['options'] = [o.lstrip('* ').strip() for o in opts]
            if ans is not None and 0 <= ans < len(opts): cur['answer'] = ans
            else: cur['answer'] = None
            result.append(cur)
        cur = None
    for line in lines:
        if not line: continue
        if re.match(r'^(?:Q(?:uestion)?\s*)?\d{1,3}\s*[).:\-]', line, re.I):
            finish()
            cur = {'question': re.sub(r'^(?:Q(?:uestion)?\s*)?\d{1,3}\s*[).:\-]\s*', '', line, flags=re.I), 'options': [], 'answer': None}
            continue
        am = re.match(r'^(?:correct\s*)?(?:answer|ans)\s*[:\-.)]\s*([A-Da-d]|[1-4])\b', line, re.I)
        if am and cur:
            a = am.group(1).upper(); cur['answer'] = ord(a)-65 if a in 'ABCD' else int(a)-1; continue
        om = re.match(r'^[\[(]?([A-Da-d])[\]).:\-]\s*(.+)$', line)
        if om and cur:
            val = om.group(2).strip()
            if val.startswith('*'):
                cur['answer'] = ord(om.group(1).upper())-65
            cur['options'].append(val)
            continue
        if cur and not cur['options']:
            cur['question'] += ' ' + line
    finish()
    return result[:200]


def quiz_for(qid, owner=None):
    if owner is None: return db.execute('SELECT * FROM quizzes WHERE id=?', (qid,)).fetchone()
    return db.execute('SELECT * FROM quizzes WHERE id=? AND owner_id=?', (qid, owner)).fetchone()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE: return
    await update.message.reply_text(
        '👋 ନମସ୍କାର! Swayam Quiz Bot କୁ ସ୍ୱାଗତ।\n\n'
        '1. ଏହି Private Chat ରେ .txt MCQ ଫାଇଲ୍ ପଠାନ୍ତୁ।\n'
        '2. Quiz ର ନାମ, Topic, Intro, Funny Message ଓ Timer ଦିଅନ୍ତୁ।\n'
        '3. /myquizzes ଦେଇ Quiz ବାଛି Group ରେ Share କରନ୍ତୁ।\n\n'
        'Group ରେ Bot କୁ Admin କରି /register ପଠାନ୍ତୁ।\n'
        '/myquizzes — Saved Quiz\n/groups — Registered Groups\n/help — ସହାୟତା'
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        'TXT format:\nQ1. Question?\nA) Option 1\nB) Option 2\nC) Option 3\nD) Option 4\nAnswer: B\n\n'
        'Quiz ବନାଇବା: Private Chat ରେ .txt ପଠାନ୍ତୁ।\n'
        'Group setup: Bot କୁ Admin କରନ୍ତୁ, Group ରେ /register ପଠାନ୍ତୁ।\n'
        'Quiz share: Private Chat ରେ /myquizzes ବ୍ୟବହାର କରନ୍ତୁ।\n'
        'Group Admin /stopquiz ଦେଇ Quiz ବନ୍ଦ କରିପାରିବେ।\n'
        'Score ପାଇଁ ପ୍ରତ୍ୟେକ ପ୍ରଶ୍ନରେ Answer: B ପରି answer key ରଖନ୍ତୁ.'
    )

async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text('ଏହି command Group ଭିତରେ ଦିଅନ୍ତୁ।'); return
    admins = await context.bot.get_chat_administrators(chat.id)
    if not any(a.user.id == user.id for a in admins):
        await update.effective_message.reply_text('କେବଳ Group Admin register କରିପାରିବେ।'); return
    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if me.status not in ('administrator', 'creator'):
        await update.effective_message.reply_text('ପ୍ରଥମେ Bot କୁ Group Admin କରନ୍ତୁ।'); return
    db.execute('INSERT INTO groups_seen VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,owner_id=excluded.owner_id,seen_at=excluded.seen_at', (chat.id, chat.title or 'Telegram Group', user.id, int(time.time())))
    db.commit()
    await update.effective_message.reply_text('✅ Group register ହେଲା। ଏବେ Private Chat ରେ /myquizzes ଦେଇ Quiz Share କରନ୍ତୁ।')

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE: return
    doc = update.message.document
    if not doc or not (doc.file_name or '').lower().endswith('.txt'): return
    try:
        tgfile = await context.bot.get_file(doc.file_id)
        raw = (await tgfile.download_as_bytearray()).decode('utf-8-sig', errors='replace')
    except Exception:
        await update.message.reply_text('ଫାଇଲ୍ ପଢ଼ିପାରିଲି ନାହିଁ। ପୁଣିଥରେ .txt ପଠାନ୍ତୁ।'); return
    qs = parse_txt(raw)
    if not qs:
        await update.message.reply_text('MCQ ମିଳିଲା ନାହିଁ। Format ଦେଖନ୍ତୁ /help'); return
    context.user_data['draft'] = {'questions': qs, 'step': 'title'}
    await update.message.reply_text(f'✅ {len(qs)}ଟି ପ୍ରଶ୍ନ ମିଳିଲା।\nQuiz ର ନାମ ପଠାନ୍ତୁ।\nବାତିଲ୍ କରିବାକୁ /cancel')

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('draft', None)
    await update.effective_message.reply_text('Draft ବାତିଲ୍ ହେଲା।')

async def draft_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE: return
    d = context.user_data.get('draft')
    if not d: return
    text = (update.message.text or '').strip()
    if text.lower() in ('-', 'skip', 'none'): text = ''
    step = d['step']
    if step == 'title': d['title'] = text[:90] or 'My Quiz'; d['step'] = 'topic'; await update.message.reply_text('📚 Topic ପଠାନ୍ତୁ (ନଥିଲେ -)।')
    elif step == 'topic': d['topic'] = text[:160]; d['step'] = 'intro'; await update.message.reply_text('🎤 Quiz start Intro ପଠାନ୍ତୁ (ନଥିଲେ -)।')
    elif step == 'intro': d['intro'] = text[:500]; d['step'] = 'funny'; await update.message.reply_text('😂 Quiz ସରିଲେ ଦେଖାଇବା Funny Message ପଠାନ୍ତୁ (ନଥିଲେ -)।')
    elif step == 'funny': d['funny'] = text[:500]; d['step'] = 'timer'; await update.message.reply_text('⏱️ ପ୍ରତି ପ୍ରଶ୍ନ Timer 5–120 seconds ମଧ୍ୟରେ ଦିଅନ୍ତୁ।')
    elif step == 'timer':
        try: timer = int(text)
        except ValueError: await update.message.reply_text('5 ରୁ 120 ମଧ୍ୟରେ ସଂଖ୍ୟା ଦିଅନ୍ତୁ।'); return
        if not 5 <= timer <= 120: await update.message.reply_text('5 ରୁ 120 ମଧ୍ୟରେ Timer ଦିଅନ୍ତୁ।'); return
        cur = db.execute('INSERT INTO quizzes(owner_id,title,topic,intro,funny,timer,questions_json,created_at) VALUES(?,?,?,?,?,?,?,?)', (update.effective_user.id,d['title'],d['topic'],d['intro'],d['funny'],timer,json.dumps(d['questions'],ensure_ascii=False),int(time.time())))
        db.commit(); qid = cur.lastrowid; count = len(d['questions']); title = d['title']; context.user_data.pop('draft',None)
        await update.message.reply_text(f'💾 Quiz Save ହୋଇଗଲା!\n\n🆔 ID: {qid}\n📚 {title}\n📝 {count} ପ୍ରଶ୍ନ\n⏱️ {timer} seconds\n\nGroup ରେ ଦେଖାଯାଇନାହିଁ। /myquizzes ଦେଇ Share କରନ୍ତୁ।')

async def myquizzes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE: await update.effective_message.reply_text('Private Chat ରେ ବ୍ୟବହାର କରନ୍ତୁ।'); return
    rows = db.execute('SELECT * FROM quizzes WHERE owner_id=? ORDER BY id DESC LIMIT 30', (update.effective_user.id,)).fetchall()
    if not rows: await update.effective_message.reply_text('Quiz Save ହୋଇନାହିଁ। ପ୍ରଥମେ .txt file ପଠାନ୍ତୁ।'); return
    kb = [[InlineKeyboardButton(f"📚 {r['title']} (ID {r['id']})", callback_data=f"quiz:{r['id']}")] for r in rows]
    await update.effective_message.reply_text('ଆପଣଙ୍କ Saved Quiz ବାଛନ୍ତୁ:', reply_markup=InlineKeyboardMarkup(kb))

async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.execute('SELECT * FROM groups_seen WHERE owner_id=? ORDER BY title', (update.effective_user.id,)).fetchall()
    if not rows: await update.effective_message.reply_text('Group ମିଳିଲା ନାହିଁ। Group ରେ Bot Admin କରି /register ପଠାନ୍ତୁ।'); return
    await update.effective_message.reply_text('Registered Groups:\n' + '\n'.join('• '+r['title'] for r in rows))

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query; await cq.answer(); data = cq.data or ''; user = cq.from_user
    if data.startswith('quiz:'):
        qid = int(data.split(':')[1]); quiz = quiz_for(qid, user.id)
        if not quiz: await cq.edit_message_text('Quiz ମିଳିଲା ନାହିଁ।'); return
        groups = db.execute('SELECT * FROM groups_seen WHERE owner_id=? ORDER BY title', (user.id,)).fetchall()
        if not groups: await cq.edit_message_text('ପ୍ରଥମେ Group ରେ Bot କୁ Admin କରି /register ପଠାନ୍ତୁ।'); return
        kb = [[InlineKeyboardButton('📣 '+g['title'], callback_data=f"share:{qid}:{g['chat_id']}")] for g in groups]
        await cq.edit_message_text(f"📚 {quiz['title']}\n📝 {len(json.loads(quiz['questions_json']))} questions\nକେଉଁ Group ରେ Start କରିବେ?", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith('share:'):
        _, qid_s, chat_s = data.split(':'); qid, chat_id = int(qid_s), int(chat_s); quiz = quiz_for(qid, user.id)
        if not quiz: await cq.edit_message_text('Quiz ମିଳିଲା ନାହିଁ।'); return
        active = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat_id,)).fetchone()
        if active: await cq.edit_message_text('ଏହି Group ରେ Quiz ଚାଲିଛି। ପ୍ରଥମେ ସେହି Quiz ସରିବାକୁ ଦିଅନ୍ତୁ।'); return
        try:
            member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if member.status not in ('administrator','creator'): await cq.edit_message_text('Bot ଏହି Group ର Admin ନୁହେଁ।'); return
            await cq.edit_message_text('🚀 Quiz Start କରୁଛି…')
            await start_quiz(context, chat_id, quiz, user.id)
        except TelegramError: await cq.edit_message_text('Start ହେଲା ନାହିଁ। Bot Admin permission ଯାଞ୍ଚ କରନ୍ତୁ।')

async def start_quiz(context, chat_id, quiz, owner_id):
    qs = json.loads(quiz['questions_json'])
    db.execute('DELETE FROM scores WHERE chat_id=? AND quiz_id=?', (chat_id, quiz['id']))
    db.execute("INSERT INTO active_quizzes(chat_id,quiz_id,owner_id,qindex,state,message_id) VALUES(?,?,?,0,'running',NULL) ON CONFLICT(chat_id) DO UPDATE SET quiz_id=excluded.quiz_id,owner_id=excluded.owner_id,qindex=0,state='running',message_id=NULL", (chat_id,quiz['id'],owner_id)); db.commit()
    runtime[chat_id] = {'message_ids': [], 'answered': set(), 'task': None}
    intro = f"🎯 <b>{html.escape(quiz['title'])}</b>\n"
    if quiz['topic']: intro += f"📚 <b>Topic:</b> {html.escape(quiz['topic'])}\n"
    intro += f"📝 <b>Questions:</b> {len(qs)}\n⏱️ <b>Timer:</b> {quiz['timer']} sec\n"
    if quiz['intro']: intro += '\n' + html.escape(quiz['intro'])
    intro += '\n\nପ୍ରସ୍ତୁତ ହୁଅନ୍ତୁ! Quiz ଆରମ୍ଭ ହେଉଛି।'
    msg = await context.bot.send_message(chat_id, intro, parse_mode='HTML'); runtime[chat_id]['message_ids'].append(msg.message_id)
    await asyncio.sleep(2); await ask_question(context, chat_id)

async def ask_question(context, chat_id):
    active = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat_id,)).fetchone()
    if not active: return
    quiz = quiz_for(active['quiz_id']); qs = json.loads(quiz['questions_json']); idx = active['qindex']
    if idx >= len(qs): await finish_quiz(context, chat_id); return
    q = qs[idx]; kb = [[InlineKeyboardButton(f"{chr(65+i)}. {opt[:75]}", callback_data=f"ans:{chat_id}:{idx}:{i}")] for i,opt in enumerate(q['options'])]
    msg = await context.bot.send_message(chat_id, f"❓ <b>Question {idx+1}/{len(qs)}</b>\n\n{html.escape(q['question'])}", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    runtime[chat_id]['message_ids'].append(msg.message_id); runtime[chat_id]['answered'] = set()
    db.execute('UPDATE active_quizzes SET message_id=? WHERE chat_id=?', (msg.message_id,chat_id)); db.commit()
    runtime[chat_id]['task'] = asyncio.create_task(question_timer(context, chat_id, idx, quiz['timer']))

async def question_timer(context, chat_id, idx, seconds):
    try:
        await asyncio.sleep(seconds)
        active = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat_id,)).fetchone()
        if active and active['qindex'] == idx: await next_question(context, chat_id)
    except asyncio.CancelledError: pass
    except Exception: log.exception('Question timer failed')

async def answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    try: _, chat_s, idx_s, opt_s = cq.data.split(':'); chat_id, idx, opt = int(chat_s), int(idx_s), int(opt_s)
    except Exception: await cq.answer('Invalid answer', show_alert=True); return
    active = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat_id,)).fetchone()
    if not active or active['qindex'] != idx: await cq.answer('ଏହି ପ୍ରଶ୍ନର ସମୟ ସରିଗଲା।', show_alert=True); return
    rt = runtime.get(chat_id)
    if not rt: await cq.answer('Quiz restart ହୋଇଛି।', show_alert=True); return
    uid = cq.from_user.id
    if uid in rt['answered']: await cq.answer('ଏହି ପ୍ରଶ୍ନରେ ଉତ୍ତର ଦେଇସାରିଛ।', show_alert=True); return
    quiz = quiz_for(active['quiz_id']); q = json.loads(quiz['questions_json'])[idx]
    rt['answered'].add(uid); correct = q.get('answer'); points = 1 if correct is not None and opt == correct else 0
    existing = db.execute('SELECT * FROM scores WHERE chat_id=? AND quiz_id=? AND user_id=?', (chat_id,quiz['id'],uid)).fetchone()
    name = cq.from_user.full_name[:100]
    if existing: db.execute('UPDATE scores SET score=score+?,answered=answered+1,username=? WHERE chat_id=? AND quiz_id=? AND user_id=?', (points,name,chat_id,quiz['id'],uid))
    else: db.execute('INSERT INTO scores VALUES(?,?,?,?,?,1)', (chat_id,quiz['id'],uid,name,points))
    db.commit(); await cq.answer('ଉତ୍ତର ଗ୍ରହଣ କରାଗଲା।')

async def next_question(context, chat_id):
    rt = runtime.get(chat_id, {}); task = rt.get('task')
    if task and task is not asyncio.current_task() and not task.done(): task.cancel()
    active = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat_id,)).fetchone()
    if not active: return
    try: await context.bot.edit_message_reply_markup(chat_id, active['message_id'], reply_markup=None)
    except TelegramError: pass
    db.execute('UPDATE active_quizzes SET qindex=qindex+1 WHERE chat_id=?', (chat_id,)); db.commit()
    await ask_question(context, chat_id)

async def finish_quiz(context, chat_id):
    active = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat_id,)).fetchone()
    if not active: return
    quiz = quiz_for(active['quiz_id']); rt = runtime.get(chat_id, {})
    task = rt.get('task')
    if task and task is not asyncio.current_task() and not task.done(): task.cancel()
    db.execute("UPDATE active_quizzes SET state='finished' WHERE chat_id=?", (chat_id,)); db.commit()
    rows = db.execute('SELECT username,score FROM scores WHERE chat_id=? AND quiz_id=? ORDER BY score DESC,username LIMIT 10', (chat_id,quiz['id'])).fetchall()
    text = f"🏁 <b>{html.escape(quiz['title'])} — Quiz Completed!</b>\n\n🏆 <b>Final Leaderboard (Top 10)</b>"
    medals = ['🥇','🥈','🥉']
    if not rows: text += '\n\nକେହି ଉତ୍ତର ଦେଇନାହାନ୍ତି।'
    for i,r in enumerate(rows): text += f"\n{medals[i] if i<3 else str(i+1)+'.'} {html.escape(r['username'])} — <b>{r['score']}</b>"
    if quiz['funny']: text += '\n\n' + html.escape(quiz['funny'])
    await context.bot.send_message(chat_id, text, parse_mode='HTML')
    # Delete quiz intro and question messages; keep the final leaderboard.
    for mid in rt.get('message_ids', []):
        try: await context.bot.delete_message(chat_id, mid)
        except TelegramError: pass
    runtime.pop(chat_id, None)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (update.effective_chat.id,)).fetchone()
    if not row: await update.effective_message.reply_text('ଏହି Group ରେ Quiz ଚାଲୁ ନାହିଁ।')
    else:
        q = quiz_for(row['quiz_id']); await update.effective_message.reply_text(f"ଚାଲୁଥିବା Quiz: {q['title']} | Question {row['qindex']+1}")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP,ChatType.SUPERGROUP): await update.effective_message.reply_text('Group ରେ /stopquiz ବ୍ୟବହାର କରନ୍ତୁ।'); return
    admins = await context.bot.get_chat_administrators(chat.id)
    if not any(a.user.id == update.effective_user.id for a in admins): await update.effective_message.reply_text('କେବଳ Group Admin Quiz ବନ୍ଦ କରିପାରିବେ।'); return
    row = db.execute("SELECT * FROM active_quizzes WHERE chat_id=? AND state='running'", (chat.id,)).fetchone()
    if not row: await update.effective_message.reply_text('Quiz ଚାଲୁ ନାହିଁ।'); return
    rt = runtime.get(chat.id, {}); task = rt.get('task')
    if task and not task.done(): task.cancel()
    db.execute("UPDATE active_quizzes SET state='stopped' WHERE chat_id=?", (chat.id,)); db.commit()
    for mid in rt.get('message_ids',[]):
        try: await context.bot.delete_message(chat.id,mid)
        except TelegramError: pass
    runtime.pop(chat.id,None); await update.effective_message.reply_text('⛔ Quiz ବନ୍ଦ ହେଲା। Saved Quiz ରହିବ।')

async def post_init(app):
    db.execute("UPDATE active_quizzes SET state='interrupted' WHERE state='running'"); db.commit()

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start_cmd))
    app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(CommandHandler('register',register_cmd))
    app.add_handler(CommandHandler('myquizzes',myquizzes_cmd))
    app.add_handler(CommandHandler('groups',groups_cmd))
    app.add_handler(CommandHandler('cancel',cancel_cmd))
    app.add_handler(CommandHandler('statusquiz',status_cmd))
    app.add_handler(CommandHandler('stopquiz',stop_cmd))
    app.add_handler(CallbackQueryHandler(answer_button, pattern=r'^ans:'))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r'^(quiz:|share:)'))
    app.add_handler(MessageHandler(filters.Document.ALL,receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,draft_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__': main()
