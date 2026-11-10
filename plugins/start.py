import logging
from html import escape

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

from config import OWNER_ID
from storage import get_target

logger = logging.getLogger(__name__)


def dashboard_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💾 Save one post", callback_data="menu:save"),
                InlineKeyboardButton("📚 Batch upload", callback_data="menu:batch"),
            ],
            [InlineKeyboardButton("📤 Upload replied file", callback_data="menu:upload")],
            [
                InlineKeyboardButton("🎯 Set destination", callback_data="menu:settarget"),
                InlineKeyboardButton("📍 Current target", callback_data="menu:target"),
            ],
            [
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
                InlineKeyboardButton("❔ Help", callback_data="menu:help"),
            ],
            [InlineKeyboardButton("🛑 Cancel batch", callback_data="menu:cancel")],
        ]
    )


def dashboard_text(owner_id: int):
    target = get_target(owner_id)
    if target:
        target_text = f"<b>Destination:</b> {escape(str(target['target_title']))} (<code>{target['target_chat_id']}</code>)"
        if target.get("target_thread_id"):
            target_text += f"\n<b>Topic ID:</b> <code>{target['target_thread_id']}</code>"
    else:
        target_text = "<b>Destination:</b> Not configured yet"
    return (
        "✨ <b>SAVE BOT — CONTROL CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Choose an action below. The bot account posts to the destination; "
        "your signed-in session reads source links.\n\n"
        f"{target_text}\n\n"
        "<i>Admin rights are not required when the group allows regular members "
        "to send messages and media.</i>"
    )


@Client.on_message(filters.command(["start", "menu"]) & filters.user(OWNER_ID))
async def start_handler(client: Client, message: Message):
    logger.info("Dashboard opened by owner id=%s", message.from_user.id)
    await message.reply_text(
        dashboard_text(OWNER_ID),
        reply_markup=dashboard_keyboard(),
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


@Client.on_message(filters.command("help") & filters.user(OWNER_ID))
async def help_handler(client: Client, message: Message):
    text = (
        "**COMMANDS**\n\n"
        "`/save <telegram_link>` — upload one source post\n"
        "`/batch <first_link> <last_link>` — upload a range\n"
        "`/upload` — upload the message you replied to\n"
        "`/settarget@YourBotUsername` — use the current group as destination\n"
        "`/settarget @publicgroup` — set a public destination from private chat\n"
        "`/target` — show the saved destination\n"
        "`/cleartarget` — remove the saved destination\n"
        "`/cancel` — stop the active batch\n"
        "`/stats` — safety and transfer status\n"
        "`/menu` — open the button dashboard\n\n"
        "**Group setup:** add the bot as a normal member and allow members to send "
        "messages/media. The bot cannot override admin-only posting restrictions."
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)
