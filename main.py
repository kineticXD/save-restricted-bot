print("BOOTING APP...", flush=True)

import os
import sys
import asyncio
import logging
from threading import Thread

from flask import Flask, jsonify
from pyrogram import Client, idle
from pyrogram.errors import FloodWait

from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING, OWNER_ID
from storage import init_storage

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("main")
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# Updated by the Telegram startup loop; the HTTP process can remain healthy
# while Telegram is waiting out a server-imposed FloodWait.
BOT_STATE = "starting"

# ---------------- tiny web server (Railway health / keep-alive) ----------------
app = Flask(__name__)


@app.route("/")
def home():
    state = BOT_STATE
    ready = state == "running"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Save Bot</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;
font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#e5e7eb;background:radial-gradient(circle at 15% 15%,#1e3a8a 0,transparent 36%),#0b1020}}
main{{width:min(560px,calc(100% - 32px));padding:32px;border:1px solid #334155;border-radius:24px;background:#111827dd;
box-shadow:0 24px 80px #0008}} .tag{{display:inline-block;padding:6px 11px;border:1px solid {'#166534' if ready else '#92400e'};border-radius:999px;color:{'#86efac' if ready else '#fcd34d'};font-size:13px}}
h1{{font-size:30px;margin:18px 0 6px}}p{{color:#aab6c8}}.line{{height:1px;background:#263449;margin:22px 0}}.small{{font-size:13px;color:#8290a5}}
</style></head><body><main><span class="tag">● {'TELEGRAM BOT READY' if ready else 'PROCESS ONLINE · TELEGRAM STARTING'}</span><h1>Save Bot</h1>
<p>Telegram state: <b>{state}</b>. Open your Telegram bot and use <b>/menu</b> when the bot is ready.</p>
<div class="line"></div><div class="small">Transfers follow Telegram chat permissions. During a FloodWait, this service waits for Telegram's retry window instead of repeatedly restarting.</div>
</main></body></html>"""


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "save-bot",
        "telegram_state": BOT_STATE,
        "bot_ready": BOT_STATE == "running",
    })


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


# ---------------- client lifecycle ----------------
def create_clients():
    # The bot's auth key is persisted as SaveBot.session. Mount DATA_DIR (or
    # SESSION_DIR) as a Railway volume so normal restarts reuse this login and
    # do not repeatedly call auth.ImportBotAuthorization.
    session_dir = os.environ.get("SESSION_DIR") or os.environ.get("DATA_DIR", "data")
    os.makedirs(session_dir, exist_ok=True)

    bot = Client(
        "SaveBot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        plugins=dict(root="plugins"),
        workdir=session_dir,
    )
    userbot = Client(
        "SaveUser",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )

    # The bot account posts; the signed-in user session only reads source chats.
    bot.userbot = userbot
    bot.poster = bot
    return bot, userbot


async def stop_client(client, name):
    try:
        if client.is_connected:
            await client.stop()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error while stopping %s client: %s", name, exc)


# ---------------- main ----------------
async def main():
    global BOT_STATE

    while True:
        bot, userbot = create_clients()
        flood_wait_seconds = None
        try:
            BOT_STATE = "starting_user_client"
            logger.info("Starting User Client...")
            await asyncio.wait_for(userbot.start(), timeout=90)
            me = await userbot.get_me()
            logger.info("User Client started as %s (id=%s)", me.first_name, me.id)

            BOT_STATE = "starting_bot_client"
            logger.info("Starting Bot Client...")
            await asyncio.wait_for(bot.start(), timeout=90)
            bme = await bot.get_me()
            logger.info("Bot Client started as @%s", bme.username)

            BOT_STATE = "running"
            logger.info("Both clients started successfully. Bot is running!")
            await idle()
            break

        except FloodWait as exc:
            # Telegram explicitly tells clients how many seconds to wait.
            # Keep this process alive and wait once rather than exiting into a
            # Railway restart loop that repeatedly attempts bot authorization.
            flood_wait_seconds = max(1, int(getattr(exc, "value", 1)))
            BOT_STATE = "waiting_for_floodwait"
            logger.error(
                "Telegram rate-limited client startup. Required wait: %s seconds (%.1f minutes).",
                flood_wait_seconds,
                flood_wait_seconds / 60,
            )
            logger.error(
                "No new login attempt will be made until the full wait has elapsed. "
                "Do not manually restart or scale this service during the wait."
            )

        finally:
            if flood_wait_seconds is None:
                BOT_STATE = "stopping"
            await stop_client(bot, "bot")
            await stop_client(userbot, "user")

        if flood_wait_seconds is None:
            break

        wait_seconds = flood_wait_seconds + 10  # small safety margin
        logger.warning("Waiting %s seconds before one controlled startup retry...", wait_seconds)
        await asyncio.sleep(wait_seconds)


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
