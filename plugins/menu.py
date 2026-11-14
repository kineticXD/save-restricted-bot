from pyrogram import Client, filters, enums
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import OWNER_ID
from plugins.start import dashboard_text, dashboard_keyboard
from plugins.helpers import guard, is_group_chat
from plugins.save import active_jobs
from storage import get_target, set_target, clear_target


def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to dashboard", callback_data="menu:home")]])


def target_keyboard(has_target=True):
    rows = []
    if has_target:
        rows.append([InlineKeyboardButton("🗑 Clear destination", callback_data="menu:cleartarget")])
    rows.append([InlineKeyboardButton("⬅️ Back to dashboard", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


@Client.on_callback_query(filters.user(OWNER_ID) & filters.regex(r"^menu:"))
async def menu_callback(client: Client, query: CallbackQuery):
    action = query.data.split(":", 1)[1]
    await query.answer()

    if action == "home":
        await query.message.edit_text(
            dashboard_text(OWNER_ID),
            reply_markup=dashboard_keyboard(),
            parse_mode=enums.ParseMode.HTML,
        )
        return

    if action == "settarget":
        chat = query.message.chat
        if is_group_chat(chat):
            thread_id = getattr(query.message, "message_thread_id", None)
            if thread_id == 1:
                thread_id = None
            title = getattr(chat, "title", None) or "Telegram group"
            set_target(OWNER_ID, chat.id, thread_id, title, getattr(chat, "username", None))
            text = (
                "✅ DESTINATION SAVED\n\n"
                f"Group: {title}\nChat ID: {chat.id}"
                + (f"\nTopic ID: {thread_id}" if thread_id else "")
                + "\n\nThe bot will post as a regular member when group permissions allow it."
            )
            await query.message.edit_text(text, reply_markup=target_keyboard(), parse_mode=enums.ParseMode.DISABLED)
        else:
            text = (
                "🎯 SET DESTINATION GROUP\n\n"
                "1. Add this bot to your destination group as a regular member.\n"
                "2. Allow regular members to send messages and media.\n"
                "3. In the group, send `/settarget@YourBotUsername` (replace with the bot username).\n\n"
                "For a public group, add the bot first, then use `/settarget @groupusername` here in private."
            )
            await query.message.edit_text(text, reply_markup=back_keyboard(), parse_mode=enums.ParseMode.MARKDOWN)
        return

    if action == "cleartarget":
        removed = clear_target(OWNER_ID)
        text = "✅ Destination cleared." if removed else "No saved destination was found."
        await query.message.edit_text(text, reply_markup=target_keyboard(False))
        return

    if action == "cancel":
        event = active_jobs.get(int(OWNER_ID))
        if event:
            event.set()
            text = "🛑 Cancellation requested. The current upload will finish, then the batch will stop."
        else:
            text = "There is no active batch to cancel."
        await query.message.edit_text(text, reply_markup=back_keyboard())
        return

    pages = {
        "save": (
            "💾 **SAVE ONE POST**\n\n"
            "Send this command with a Telegram message link:\n"
            "`/save https://t.me/channel/123`\n\n"
            "Private links such as `https://t.me/c/123456/789` are supported.\n"
            "The source account must already have access to the source chat.",
        ),
        "batch": (
            "📚 **BATCH UPLOAD**\n\n"
            "Use two links from the same chat:\n"
            "`/batch <first_link> <last_link>`\n\n"
            "Maximum: 500 messages per range. Add `--title \"Percentages\"` after a link pair to post a section heading. "
            "Use `/cancel` to stop an active batch. Skipped/unsupported items are included in the final report.",
        ),
        "multibatch": (
            "🧩 MULTI-BATCH WITH SECTION TITLES\n\n"
            'Use this format (keep each title in quotes):\n'
            '<code>/multibatch START1 END1 --title "Percentages" START2 END2 --title "Profit &amp; Loss"</code>\n\n'
            "Each title is posted automatically before its range.\n\n"
            'You can also add per-file names: <code>--names "File 1.mp4|File 2.pdf"</code>\n'
            'Or use an automatic pattern: <code>--name-template "Lecture {n}"</code>.\n'
            "Use one name for each media item in that range.\n\n"
            "Optional per-range caption: <code>--caption &quot;Your caption&quot;</code>.",
        ),
        "rename": (
            "✏️ RENAME A FILE\n\n"
            'Use <code>/rename TELEGRAM_LINK "New filename.mp4"</code>.\n'
            'Or use <code>/save TELEGRAM_LINK --name "New filename.mp4"</code>.\n\n'
            "For multi-batch, use <code>--names</code> with filenames separated by |, "
            'or <code>--name-template "Lecture {n}"</code>. Supported placeholders: {n}, {index}, {message_id}, {title}.'
        ),
        "settings": (
            "⚙️ TRANSFER SETTINGS\n\n"
            "<code>/settings</code> — view current preferences\n"
            '<code>/setcaption append "Your caption"</code> — append to source captions\n'
            '<code>/setcaption replace "Your caption"</code> — replace source captions\n'
            "<code>/clearcaption</code> — disable custom caption\n"
            "Reply to a photo with <code>/setthumb</code> to set a custom thumbnail.\n"
            "<code>/clearthumb</code> clears it.\n"
            "<code>/setthumbmode source|custom|none</code> selects original, custom or no thumbnail.\n"
            '<code>/replaceword "old" =&gt; "new"</code> replaces text in filenames/captions.\n'
            "<code>/clearreplace</code> clears rules.\n"
            "<code>/setpin on</code> or <code>/setpin off</code> toggles auto-pin."
        ),
        "upload": (
            "📤 **UPLOAD A REPLIED MESSAGE**\n\n"
            "Send a photo, video, document or text to this bot, reply to that message "
            "with `/upload`, and it will be posted to your configured group.\n\n"
            "You can also reply to a message in a group and use `/upload` there.",
        ),
        "settarget": (
            "🎯 **SET DESTINATION GROUP**\n\n"
            "1. Add this bot to your destination group as a regular member.\n"
            "2. Make sure regular members may send messages and media.\n"
            "3. In that group, send `/settarget@YourBotUsername` (replace with the bot username).\n\n"
            "For a public group, you can also use `/settarget @groupusername` in private.\n"
            "Administrator permissions are not needed and cannot be used to bypass group restrictions.",
        ),
        "target": (target_page(),),
        "help": (
            "❔ **QUICK HELP**\n\n"
            "• The bot account uploads to the destination group.\n"
            "• The signed-in source account reads source links.\n"
            "• The bot does not need admin rights if normal members can post.\n"
            "• The source account must be able to access source content.\n"
            "• Batch uploads run sequentially with rate-limit protection.\n\n"
            "Open `/help` for the complete command list.",
        ),
    }

    if action == "stats":
        target = get_target(OWNER_ID)
        target_label = (str(target["target_title"]).replace("`", "'") if target else "Not set")
        state = "PAUSED — flood safety limit reached" if guard.circuit_open else "READY"
        text = (
            "📊 **BOT STATUS**\n\n"
            f"**Transfer status:** `{state}`\n"
            f"**Flood waits (24h):** `{guard.flood_count}`\n"
            f"**Destination:** `{target_label}`\n"
            "**Delivery identity:** Bot account\n"
            "**Source reader:** Signed-in session"
        )
        await query.message.edit_text(text, reply_markup=back_keyboard(), parse_mode=enums.ParseMode.MARKDOWN)
        return

    page = pages.get(action)
    if not page:
        await query.answer("Unknown menu action", show_alert=True)
        return
    parse_mode = enums.ParseMode.HTML if action in {"target", "multibatch", "rename", "settings"} else enums.ParseMode.MARKDOWN
    keyboard = target_keyboard(bool(get_target(OWNER_ID))) if action == "target" else back_keyboard()
    await query.message.edit_text(page[0], reply_markup=keyboard, parse_mode=parse_mode)


def target_page():
    target = get_target(OWNER_ID)
    if not target:
        return "📍 DESTINATION\n\nNo group is configured. Use /settarget in your destination group."
    lines = [
        "📍 SAVED DESTINATION",
        "",
        f"Group: {target['target_title']}",
        f"Chat ID: {target['target_chat_id']}",
    ]
    if target.get("target_username"):
        lines.append(f"Username: @{target['target_username']}")
    if target.get("target_thread_id"):
        lines.append(f"Topic ID: {target['target_thread_id']}")
    lines.extend(["", "Use /cleartarget to remove it."])
    return "\n".join(lines)
