import os
import logging
import asyncio
from threading import Thread
from flask import Flask
from pyrogram import Client, idle
from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Keep Alive Server ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Personal Save Bot is alive."

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Clients ---
bot = Client("SaveBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, plugins=dict(root="plugins"))
user = Client("SaveUser", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# --- Background Dialog Syncing ---
async def sync_dialogs():
    """Runs in the background to cache chat IDs without blocking the bot."""
    try:
        logger.info("Syncing Userbot dialogs in background...")
        async for _ in user.get_dialogs():
            pass
        logger.info("Syncing Bot dialogs in background...")
        async for _ in bot.get_dialogs():
            pass
        logger.info("✅ Dialog sync complete. Peer IDs are cached.")
    except Exception as e:
        logger.error(f"Failed to sync dialogs: {e}")

# --- Main Async Function ---
async def main():
    # 1. Start the bot FIRST so it can reply immediately
    logger.info("Starting Bot Client...")
    await bot.start()
    
    # 2. Start the userbot
    logger.info("Starting User Client...")
    await user.start()
    
    logger.info("🚀 Both clients started successfully. Bot is running!")
    
    # 3. Run the dialog sync in the background so it doesn't block replies
    asyncio.create_task(sync_dialogs())
    
    # 4. Keep the event loop alive
    await idle()
    
    logger.info("Stopping clients...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    logger.info("Starting Keep-Alive Server...")
    Thread(target=run_flask, daemon=True).start()
    
    # Run the async loop
    asyncio.run(main())
