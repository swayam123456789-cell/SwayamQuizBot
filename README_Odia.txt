SwayamQuizBot — Friendly Quiz Builder Starter (Odia)

IMPORTANT HONEST LIMITATION:
This package includes the Telegram bot menu, manual question builder, copy-paste MCQ parser, SQLite quiz/question/attempt schema, result/leaderboard commands, and cutoff calculation based on saved attempts.
A real shareable web quiz page that collects participant answers and writes attempts to the database is NOT included in this starter yet. Until that web page/backend is added, the participant leaderboard will remain empty unless attempts are recorded. Do not advertise the public link as working yet.

SECURITY:
- Never upload the Telegram bot token to GitHub.
- Set TELEGRAM_BOT_TOKEN only as a hosting secret/environment variable.
- If token was shown in screenshots, revoke it in BotFather and use a new token.
- Keep the repository public only for source code without secrets.

Environment:
TELEGRAM_BOT_TOKEN = your new BotFather token (secret only)
PUBLIC_BASE_URL = public HTTPS URL of the deployed web app (leave unset until web app exists)
DATABASE_PATH = swayam_quiz.db (optional; use persistent storage)

Run locally:
pip install -r requirements.txt
python bot.py

Telegram commands:
 /start - menu
 /newquiz - manual quiz
 /addq - add another question
 /publishquiz - save draft quiz
 /results CODE - results and Top 10
 /leaderboard CODE - Top 10
 /cancel - cancel current flow

Copy-paste format:
Q1. What is the capital of India?
A) Mumbai
B) New Delhi
C) Cuttack
D) Chennai
Answer: B

Hosting and the actual web quiz page/backend must be completed before users can take quizzes via a shareable link and before automatic score collection works.
