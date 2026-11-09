from pyrogram import Client, filters
from pyrogram.types import Message, enums
from config import OWNER_ID
from plugins.helpers import guard

@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_handler(client: Client, message: Message):
    status = "🔴 OPEN (Paused)" if guard.circuit_open else "🟢 CLOSED (Healthy)"
    text = (
        f"**Safety Status**\n\n"
        f"Floods in last 24h: `{guard.flood_count}`\n"
        f"Circuit Breaker: `{status}`\n"
        f"Next Reset: `{guard.flood_reset_time + __import__('datetime').timedelta(hours=24)}`"
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)
