import logging

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from plugins.helpers import get_destination, prepare_chats
from plugins.save import get_poster
from plugins.transfer import copy_or_reupload, UnsupportedMedia

logger = logging.getLogger(__name__)


@Client.on_message(filters.command("upload") & filters.user(OWNER_ID))
async def upload_replied_message(client: Client, message: Message):
    source_msg = getattr(message, "reply_to_message", None)
    if source_msg is None:
        await message.reply_text(
            "Reply to a photo, video, document, audio or text message with `/upload`.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        return

    try:
        target_chat, thread_id = await get_destination(message, OWNER_ID)
    except LookupError as exc:
        await message.reply_text(f"🎯 {exc}")
        return

    source_chat = source_msg.chat.id
    poster = get_poster(client)
    status = await message.reply_text("⏳ **Uploading your replied message...**", parse_mode=enums.ParseMode.MARKDOWN)

    async def on_stage(text: str):
        await status.edit_text(text)

    try:
        problem = await prepare_chats(poster, source_chat, target_chat, sender=poster)
        if problem:
            await status.edit_text(f"❌ {problem}")
            return
        await copy_or_reupload(
            poster, source_chat, source_msg, target_chat,
            thread_id=thread_id, on_stage=on_stage, sender=poster,
        )
        await status.edit_text("✅ **Message uploaded to the destination group.**")
    except UnsupportedMedia as exc:
        await status.edit_text(f"⚠️ **Skipped:** {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("/upload failed")
        name = type(exc).__name__
        if name in {"ChatWriteForbidden", "ChatAdminRequired", "ChatRestricted"}:
            reason = (
                "The bot cannot post there. Allow regular members to send messages and media; "
                "admin-only groups cannot be bypassed."
            )
        elif name in {"UserNotParticipant", "PeerIdInvalid", "ChannelPrivate"}:
            reason = "Add the bot to the destination group as a regular member, then try again."
        else:
            reason = str(exc)
        await status.edit_text(f"❌ **Upload failed:** {reason}")
