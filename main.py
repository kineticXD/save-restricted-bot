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

# --- Main Async Function ---
async def main():
    logger.info("Starting User Client...")
    await user.start()
    # Force Userbot to sync its dialogs (caches IDs)
    logger.info("Syncing Userbot dialogs...")
    await user.get_dialogs()
    
    logger.info("Starting Bot Client...")
    await bot.start()
    # Force Bot to sync its dialogs (caches IDs)
    logger.info("Syncing Bot dialogs...")
    await bot.get_dialogs()
    
    logger.info("🚀 Both clients started and synced successfully. Bot is running!")
    
    # This keeps the event loop running indefinitely for both clients
    await idle()
    
    logger.info("Stopping clients...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    logger.info("Starting Keep-Alive Server...")
    Thread(target=run_flask, daemon=True).start()
    
    # Run the async loop
    asyncio.run(main())
