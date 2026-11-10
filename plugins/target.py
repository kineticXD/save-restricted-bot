import logging

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from plugins.helpers import is_group_chat
from storage import set_target, get_target, clear_target

logger = logging.getLogger(__name__)


@Client.on_message(filters.command("settarget") & filters.user(OWNER_ID))
async def set_target_handler(client: Client, message: Message):
    args = getattr(message, "command", [])[1:]
    if args:
        identifier = args[0]
        if identifier.lstrip("-").isdigit():
            identifier = int(identifier)
        try:
            chat = await client.get_chat(identifier)
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not resolve destination %s: %s", identifier, exc)
            await message.reply_text(
                "❌ I couldn't open that group. Add the bot to the destination first, "
                "then run `/settarget@YourBotUsername` inside it.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            return
        thread_id = None
    else:
        chat = message.chat
        if not is_group_chat(chat):
            await message.reply_text(
                "🎯 To set a private group, add the bot to the group as a regular member "
                "and send `/settarget@YourBotUsername` inside that group.\n\n"
                "For a public group, add the bot to the group first, then use `/settarget @groupusername` here in private.",
                parse_mode=enums.ParseMode.MARKDOWN,
            )
            return
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id == 1:
            thread_id = None

    chat_type = getattr(getattr(chat, "type", None), "value", str(getattr(chat, "type", "")))
    kind = str(chat_type).lower().split(".")[-1]
    if kind not in {"group", "supergroup"} and not (kind == "" and int(chat.id) < 0):
        await message.reply_text("❌ The destination must be a Telegram group or supergroup.")
        return

    username = getattr(chat, "username", None)
    title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or "Telegram group"
    set_target(OWNER_ID, chat.id, thread_id, title, username)
    logger.info("Destination group set: %s (%s)", title, chat.id)

    await message.reply_text(
        "✅ Destination saved\n\n"
        f"Group: {title}\n"
        f"Chat ID: {chat.id}\n\n"
        "The bot will post as a regular member. It does not need admin rights, but "
        "the group's permissions must allow members to send messages/media.",
    )


@Client.on_message(filters.command("target") & filters.user(OWNER_ID))
async def show_target_handler(client: Client, message: Message):
    target = get_target(OWNER_ID)
    if not target:
        await message.reply_text("No destination is configured. Use `/settarget@YourBotUsername` in your group.", parse_mode=enums.ParseMode.MARKDOWN)
        return
    text = (
        "📍 SAVED DESTINATION\n\n"
        f"Group: {target['target_title']}\n"
        f"Chat ID: {target['target_chat_id']}"
    )
    if target.get("target_username"):
        text += f"\nUsername: @{target['target_username']}"
    if target.get("target_thread_id"):
        text += f"\nTopic ID: {target['target_thread_id']}"
    await message.reply_text(text)


@Client.on_message(filters.command("cleartarget") & filters.user(OWNER_ID))
async def clear_target_handler(client: Client, message: Message):
    removed = clear_target(OWNER_ID)
    text = "✅ Destination removed." if removed else "There was no saved destination."
    await message.reply_text(text)
