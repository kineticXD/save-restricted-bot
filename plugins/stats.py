from datetime import timedelta

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from plugins.helpers import guard
from plugins.save import active_jobs
from storage import get_target


@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_handler(client: Client, message: Message):
    status = "🔴 PAUSED (Safety circuit open)" if guard.circuit_open else "🟢 READY"
    target = get_target(OWNER_ID)
    target_label = target["target_title"] if target else "Not configured"
    reset = guard.flood_reset_time + timedelta(hours=24)
    text = (
        "📊 **BOT STATUS**\n\n"
        f"**Transfer state:** `{status}`\n"
        f"**Flood waits (24h):** `{guard.flood_count}`\n"
        f"**Next counter reset:** `{reset:%Y-%m-%d %H:%M}`\n"
        f"**Destination:** `{target_label}`\n"
        f"**Active batch:** `{'Yes' if int(OWNER_ID) in active_jobs else 'No'}`\n"
        "**Uploader:** `Bot account`"
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)
