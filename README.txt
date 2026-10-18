SWAYAMQUIZBOT ADVANCED PACKAGE

Included files:
- bot.py
- requirements.txt
- render.yaml
- sample_quiz.txt

FEATURES
- Upload a .txt file in a private chat or a channel where the bot is an administrator.
- English question first, optional Odia question on the next line.
- Each option can have English first and optional Odia on the next line.
- Default timer: 15 seconds.
- Timer choices: 10, 15, 30, 45 seconds and 1–5 minutes.
- Timer automatically advances the quiz in group/channel mode.
- Private practice advances after the user answers.
- Shuffle questions and/or options.
- Top 10 results, pause, resume, next, stop, and share link.
- /settings opens timer and shuffle buttons.

SETUP (Render + GitHub)
1. Download and extract this ZIP.
2. Upload the files to your existing GitHub repository, replacing the old bot.py and requirements.txt.
3. In Render, confirm Start Command is: python bot.py
4. In Render > Environment, add BOT_TOKEN = your BotFather token.
5. Deploy the latest commit.
6. Add the bot as an administrator to each channel where it should post quiz questions and results. Give it permission to post messages.
7. Test in private chat first by uploading sample_quiz.txt.

SUPPORTED TXT FORMAT
Q1. What is the capital of India?
ଭାରତର ରାଜଧାନୀ କ'ଣ?

A. Mumbai
ମୁମ୍ବାଇ
B. New Delhi
ନୂଆଦିଲ୍ଲୀ
C. Kolkata
କୋଲକାତା
D. Chennai
ଚେନ୍ନାଇ

Correct Answer: B

Then continue with Q2. etc. Correct Answer is required for every question.

COMMANDS
/start
/settings
/timer 15
/shuffle all
/shuffle questions
/shuffle options
/shuffle off
/pause
/resume
/next
/leaderboard
/share
/stop

IMPORTANT LIMITATIONS
- This package is a complete starter implementation, but it has not been installed or tested against your existing Render deployment. Test it in private chat before relying on it in a public channel.
- Active quizzes, settings, and share-link definitions are stored in memory and are lost if the Render process restarts or redeploys.
- Share links work while the process remains running and the quiz definition remains in memory.
- Telegram channels do not provide normal per-user message replies; users answer using inline buttons. Bot must be admin with posting permission.
- For production-quality persistence across restarts, a database would be needed.
- Never publish your BotFather token in GitHub. Use Render Environment Variables.
