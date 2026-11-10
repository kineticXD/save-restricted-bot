print("BOOTING APP...", flush=True)

import os
import sys
import asyncio
import logging
from threading import Thread

from flask import Flask
from pyrogram import Client, idle

from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("main")
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# ---------------- tiny web server (Railway health / keep-alive) ----------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Personal Save Bot is alive."


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------- config check ----------------
def check_config():
    missing = [
        name
        for name, value in {
            "API_ID": API_ID,
            "API_HASH": API_HASH,
            "BOT_TOKEN": BOT_TOKEN,
            "SESSION_STRING": SESSION_STRING,
            "OWNER_ID": OWNER_ID,
        }.items()
        if not value
    ]
    if missing:
        logger.error("Missing environment variables: %s", ", ".join(missing))
        sys.exit(1)


# ---------------- main ----------------
async def main():
    # IMPORTANT: Clients are created INSIDE the running event loop.
    # Pyrogram/Kurigram capture asyncio.get_event_loop() in Client.__init__
    # (and in the Dispatcher). If they are created at module level, they bind
    # to a different loop than the one asyncio.run() creates, and handlers
    # never get scheduled.
    bot = Client(
        "SaveBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        plugins=dict(root="plugins"),
        in_memory=True,
    )
    userbot = Client(
        "SaveUser",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )

    # Plugins reach the userbot through `client.userbot`.
    # (Do NOT `from main import user` inside handlers: when run as
    # `python main.py` that re-executes this file and gives you a second,
    # never-started Client.)
    bot.userbot = userbot

    try:
        logger.info("Starting User Client...")
        await asyncio.wait_for(userbot.start(), timeout=90)
        me = await userbot.get_me()
        logger.info("User Client started as %s (id=%s)", me.first_name, me.id)

        logger.info("Starting Bot Client...")
        await asyncio.wait_for(bot.start(), timeout=90)
        bme = await bot.get_me()
        logger.info("Bot Client started as @%s", bme.username)

        logger.info("🚀 Both clients started successfully. Bot is running!")
        await idle()
    finally:
        logger.info("Stopping clients...")
        for c in (bot, userbot):
            try:
                if c.is_connected:
                    await c.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error while stopping a client: %s", e)


if __name__ == "__main__":
    check_config()
    print("STARTING THREADS...", flush=True)
    Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Fatal error in main()")
        sys.exit(1)
