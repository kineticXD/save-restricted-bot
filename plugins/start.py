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
            [InlineKeyboardButton("🧩 Multi-batch sections", callback_data="menu:multibatch")],
            [InlineKeyboardButton("✏️ Rename a file", callback_data="menu:rename")],
            [InlineKeyboardButton("📤 Upload replied file", callback_data="menu:upload")],
            [
                InlineKeyboardButton("🎯 Set destination", callback_data="menu:settarget"),
                InlineKeyboardButton("📍 Current target", callback_data="menu:target"),
            ],
            [
                InlineKeyboardButton("⚙️ Transfer settings", callback_data="menu:settings"),
                InlineKeyboardButton("📊 Stats", callback_data="menu:stats"),
            ],
            [InlineKeyboardButton("❔ Help", callback_data="menu:help")],
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
        "✨ <b>WAGURI — CONTROL CENTER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Choose an action below. The bot account posts to the destination; "
        "your signed-in session reads source links.\n\n"
        f"{target_text}\n\n"
        "<i>Admin rights are not required when the group allows regular members "
        "to send messages and media.</i>\n\n"
        "<b>New tools:</b> titled multi-batches, custom filenames, captions, "
        "thumbnails, word replacement and auto-pin. Use /settings to configure them."
    )


@Client.on_message(filters.command(["start", "menu"]) & filters.user(OWNER_ID))
async def start_handler(client: Client, message: Message):
    logger.info("Dashboard opened by owner id=%s", message.from_user.id)
    await message.reply_text(
        dashboard_text(OWNER_ID),
        reply_markup=dashboard_keyboard(),
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("help") & filters.user(OWNER_ID))
async def help_handler(client: Client, message: Message):
    text = (
        "<b>WAGURI COMMANDS</b>\n\n"
        "<code>/save LINK</code> — save one post\n"
        '<code>/save LINK --name "Lecture 01.mp4"</code> — rename a file\n'
        '<code>/save LINK --caption "My caption"</code> — custom one-off caption\n'
        '<code>/rename LINK "New name.mp4"</code> — upload with a new filename\n'
        "<code>/batch START END --title &quot;Section title&quot;</code> — one titled range\n"
        "<code>/multibatch</code> — several titled ranges in order\n"
        "<code>/upload</code> — upload a replied-to message\n"
        "<code>/settarget</code>, <code>/target</code>, <code>/cleartarget</code> — destination\n"
        "<code>/settings</code> — view transfer settings\n"
        "<code>/setcaption append|replace TEXT</code> — default caption rule\n"
        "<code>/clearcaption</code> — clear default caption\n"
        "<code>/setthumb</code> — reply to a photo to set custom thumbnail\n"
        "<code>/clearthumb</code> — clear custom thumbnail\n"
        "<code>/setthumbmode source|custom|none</code> — choose thumbnail mode\n"
        '<code>/replaceword "old" =&gt; "new"</code> — text replacement\n'
        "<code>/clearreplace</code> — clear replacement rules\n"
        "<code>/setpin on|off</code> — auto-pin uploaded posts\n"
        "<code>/stats</code> — transfer status\n"
        "<code>/cancel</code> — cancel the active batch\n\n"
        "Use double quotes around titles or filenames that contain spaces."
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)
