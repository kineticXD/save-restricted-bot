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
    # Check if SESSION_STRING is valid before starting
    if not SESSION_STRING:
        logger.error("❌ CRITICAL ERROR: SESSION_STRING is missing in Railway Variables!")
        logger.error("Please generate a new Pyrogram String and add it to your environment variables.")
        return
        
    logger.info("Starting Bot Client...")
    await bot.start()
    
    logger.info("Starting User Client...")
    await user.start()
    
    logger.info("🚀 Both clients started successfully. Bot is running!")
    
    # Keep the event loop alive
    await idle()
    
    logger.info("Stopping clients...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    logger.info("Starting Keep-Alive Server...")
    Thread(target=run_flask, daemon=True).start()
    
    # Run the async loop
    asyncio.run(main())
