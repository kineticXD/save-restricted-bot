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

# --- Background Sync ---
async def sync_dialogs():
    """Downloads the chat list in the background so the bot recognizes group IDs."""
    await asyncio.sleep(5) # Wait for clients to fully start
    try:
        logger.info("Syncing Bot dialogs in background...")
        async for _ in bot.get_dialogs():
            pass
        logger.info("Syncing User dialogs in background...")
        async for _ in user.get_dialogs():
            pass
        logger.info("✅ Dialog sync complete! Bot knows all chats now.")
    except Exception as e:
        logger.error(f"Dialog sync failed: {e}")

# --- Main Async Function ---
async def main():
    if not SESSION_STRING:
        logger.error("❌ CRITICAL ERROR: SESSION_STRING is missing in Railway Variables!")
        return
        
    logger.info("Starting Bot Client...")
    await bot.start()
    
    logger.info("Starting User Client...")
    await user.start()
    
    logger.info("🚀 Both clients started successfully. Bot is running!")
    
    # Start the background sync task
    asyncio.create_task(sync_dialogs())
    
    # Keep the event loop alive for BOTH clients
    await idle()
    
    logger.info("Stopping clients...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    logger.info("Starting Keep-Alive Server...")
    Thread(target=run_flask, daemon=True).start()
    
    # Run both clients in the same event loop
    asyncio.run(main())
