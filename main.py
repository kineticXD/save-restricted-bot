import os
import logging
from threading import Thread
from flask import Flask
from pyrogram import Client
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
# Bot client (talks to you)
bot = Client("SaveBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, plugins=dict(root="plugins"))

# User client (fetches the restricted content)
user = Client("SaveUser", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

if __name__ == "__main__":
    logger.info("Starting Keep-Alive Server...")
    Thread(target=run_flask, daemon=True).start()
    
    logger.info("Starting User Client...")
    user.start()
    
    logger.info("Starting Bot Client...")
    bot.run()
