import os
from dotenv import load_dotenv

# Load .env file if running locally
load_dotenv()

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))

# Always download + re-upload instead of trying copy_message first (1/true/yes).
# Normally not needed: protected chats are detected automatically.
FORCE_REUPLOAD = os.environ.get("FORCE_REUPLOAD", "").strip().lower() in ("1", "true", "yes")

# Optional extra cap (in MB) on files the bot will download + re-upload.
# 0 = only Telegram's own limits apply (2000 MB, or 4000 MB for Premium accounts).
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", 0) or 0)
