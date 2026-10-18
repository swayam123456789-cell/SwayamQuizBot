SwayamQuizBot — Telegram-only starter

IMPORTANT
- This is a starter implementation. Test with a private test quiz before using it publicly.
- Never upload your Telegram bot token to GitHub.
- Hosting must support HTTPS webhooks and a running Python web service.
- SQLite data on many free hosts can be lost when the service restarts/redeploys unless persistent storage is configured. Do not rely on this for important exam results without persistent database storage.
- Render free web services may sleep and have resource limitations. Free hosting availability/terms can change.

Environment variables
TELEGRAM_BOT_TOKEN = token from BotFather (store only in hosting secrets)
RENDER_EXTERNAL_URL = public HTTPS URL of your deployed service (Render provides this)
WEBHOOK_SECRET = a long random secret string
PORT = usually supplied automatically by host
DATABASE_PATH = optional path for SQLite file

Commands in Telegram
/start
/help
/newquiz Quiz title
/addq CODE
/import CODE
/link CODE
/leaderboard CODE
/close CODE
/cancel

Question format (one line)
Question text | Option A | Option B | Option C | Option D | correct option number (1-4)

For bulk import, send one question per line after /import CODE.
Participants open the link and complete the quiz in the bot's private chat. The bot uses non-anonymous Telegram quiz polls to track answers. Each participant may attempt each quiz once.

Top 10 sorts by score, then by shortest elapsed time. The cutoff is the score of rank 10 when at least 10 participants have completed the quiz. The /close command prevents new starts; it does not force unfinished attempts to submit.
