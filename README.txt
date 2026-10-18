SWAYAM QUIZ BOT — LIVE GROUP QUIZ

WHAT IT DOES
- Create a quiz in the bot's PRIVATE chat by sending a .txt MCQ file.
- Save quiz title, topic, intro, funny completion message and timer.
- Choose a registered Telegram group and start the quiz there.
- Participants answer with buttons; one answer per user per question.
- Final Top 10 leaderboard appears when the quiz ends.
- Bot tries to delete its intro/question messages after the quiz, but keeps the final leaderboard.
- Saved quiz data is stored in SQLite.

SETUP
1. Replace bot.py and requirements.txt in your existing GitHub repository with the files in this package. You may upload the other files too.
2. In Render, make sure the service is a Background Worker with start command: python bot.py
3. Render > Environment: set BOT_TOKEN to the CURRENT token from BotFather. Do not share this token.
4. Deploy/redeploy and check logs.
5. In Telegram, open the bot privately and send /start.

HOW TO USE
1. Add the bot to your Telegram group and promote it to Admin. Enable permission to delete messages.
2. In the group, a group admin sends /register.
3. In the bot's private chat, upload a .txt MCQ file.
4. Follow prompts for title, topic, intro, completion/funny message, and timer (5-120 seconds). Send - to skip optional text.
5. Send /myquizzes, choose a quiz, then choose the group.
6. In a group, use /stopquiz to stop a running quiz (group admins only).

TXT FORMAT
Q1. What is the capital of India?
A) Mumbai
B) New Delhi
C) Kolkata
D) Chennai
Answer: B

SCORING
Each question needs an answer key like Answer: B. Without keys, quiz questions still show but scores cannot be calculated.

IMPORTANT
- This is a first complete implementation; test in a small test group before using it in a large group.
- Render free-service availability and sleep/restart behavior may change. If the database file is on ephemeral storage, quizzes can be lost after redeploy/restart. Use a persistent disk/database for reliable long-term storage where supported.
- Telegram only permits deletion when the bot has the required admin permissions and within Telegram's message-deletion rules.
- Bot restarts interrupt live quizzes; saved quizzes persist only if the SQLite file persists.
