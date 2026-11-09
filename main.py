import os
import logging
import asyncio
from threading import Thread
from flask import Flask
from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Keep Alive Server ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Alive"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Clients ---
bot = Client("SaveBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, plugins=dict(root="plugins"))
user = Client("SaveUser", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

# --- Userbot Runner ---
def start_userbot():
    logger.info("Starting User Client...")
    user.start()
    logger.info("User Client started successfully.")
    # Keep the userbot alive forever
    asyncio.get_event_loop().run_forever()

# --- Main ---
if __name__ == "__main__":
    if not SESSION_STRING:
        logger.error("❌ CRITICAL ERROR: SESSION_STRING is missing!")
        exit(1)

    # Start Flask
    Thread(target=run_flask, daemon=True).start()
    
    # Start Userbot in a background thread
    Thread(target=start_userbot, daemon=True).start()
    
    # Start the Bot (bot.run() handles the event loop and polling automatically)
    logger.info("Starting Bot Client...")
    bot.run()
