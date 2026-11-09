from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import OWNER_ID
import logging

logger = logging.getLogger(__name__)

# The OWNER_ID restriction is back in place
@Client.on_message(filters.command("start") & filters.user(OWNER_ID))
async def start_handler(client: Client, message: Message):
    logger.info(f"🔥 Received /start command from User ID: {message.from_user.id}")
    
    text = (
        "🚀 **Personal Save Bot**\n\n"
        "I can extract restricted content and post it directly here.\n\n"
        "**Commands:**\n"
        "`/save <link>` – Save a single post.\n"
        "`/batch <start_link> <end_link>` – Save a range of posts.\n"
        "`/stats` – Show safety status.\n\n"
        "**Note:** Use this bot only in your own group."
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)
