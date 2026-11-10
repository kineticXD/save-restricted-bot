print("BOOTING APP...", flush=True)

import os
import logging
import asyncio
from threading import Thread
from flask import Flask

# --- APPLY MONKEY PATCH BEFORE PYROGRAM IMPORTS ---
import pyrogram.utils
def get_peer_type_new(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"
pyrogram.utils.get_peer_type = get_peer_type_new
# -------------------------------------------------

from pyrogram import Client, idle
from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route('/')
def home():
    return "Personal Save Bot is alive."

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

bot = Client("SaveBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, plugins=dict(root="plugins"))
user = Client("SaveUser", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)

async def main():
    if not SESSION_STRING:
        logger.error("❌ CRITICAL ERROR: SESSION_STRING is missing!")
        return
        
    logger.info("Starting Bot Client...")
    await bot.start()
    
    logger.info("Starting User Client...")
    await user.start()
    
    logger.info("🚀 Both clients started successfully. Bot is running!")
    
    await idle()
    
    logger.info("Stopping clients...")
    await bot.stop()
    await user.stop()

if __name__ == "__main__":
    print("STARTING THREADS...", flush=True)
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
