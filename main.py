print("BOOTING APP...", flush=True)

import os
import sys
import asyncio
import logging
from threading import Thread

from flask import Flask, jsonify
from pyrogram import Client, idle

from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID
from storage import init_storage

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
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Save Bot</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#e5e7eb;background:radial-gradient(circle at 15% 15%,#1e3a8a 0,transparent 36%),#0b1020}
main{width:min(560px,calc(100% - 32px));padding:32px;border:1px solid #334155;border-radius:24px;background:#111827dd;
box-shadow:0 24px 80px #0008} .tag{display:inline-block;padding:6px 11px;border:1px solid #166534;border-radius:999px;color:#86efac;font-size:13px}
h1{font-size:30px;margin:18px 0 6px}p{color:#aab6c8}.line{height:1px;background:#263449;margin:22px 0}.small{font-size:13px;color:#8290a5}
</style></head><body><main><span class="tag">● SERVICE ONLINE</span><h1>Save Bot</h1>
<p>Telegram transfer service is running. Open your Telegram bot and use <b>/menu</b> to manage uploads.</p>
<div class="line"></div><div class="small">This endpoint is for service health only. Transfers follow Telegram chat permissions.</div>
</main></body></html>"""


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "save-bot"})


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

    # Plugins use the bot account to post and the signed-in account only to
    # read source chats. This lets the bot post as a normal group member when
    # that group's permissions allow it; no admin role is required.
    bot.userbot = userbot
    bot.poster = bot

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
    init_storage()
    print("STARTING THREADS...", flush=True)
    Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Fatal error in main()")
        sys.exit(1)
