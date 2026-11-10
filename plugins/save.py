import logging
import asyncio

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from plugins.helpers import (
    extract_chat_and_msg_id,
    get_destination,
    prepare_chats,
    guard,
)
from plugins.transfer import copy_or_reupload, UnsupportedMedia

logger = logging.getLogger(__name__)

MAX_BATCH = 500
active_jobs = {}


def get_userbot(client: Client) -> Client:
    ub = getattr(client, "userbot", None)
    if ub is None:
        raise RuntimeError("Source reader session is not attached to the bot client.")
    return ub


def get_poster(client: Client) -> Client:
    # main.py attaches the bot itself as `poster`. The fallback keeps old local
    # test harnesses and one-client deployments compatible.
    return getattr(client, "poster", None) or get_userbot(client)


def _telegram_error_text(exc: Exception) -> str:
    name = type(exc).__name__
    if name in {"ChatWriteForbidden", "ChatAdminRequired", "ChatRestricted"}:
        return (
            "The bot cannot post in the destination group. Check that regular members "
            "are allowed to send messages and media. If the group is admin-only, Telegram "
            "requires admin permission; this bot will not bypass that restriction."
        )
    if name in {"UserNotParticipant", "PeerIdInvalid", "ChannelPrivate"}:
        return (
            "The bot cannot access the destination group. Add the bot to that group as a "
            "regular member, then run `/settarget` again."
        )
    if name in {"SlowmodeWait", "FloodWait"}:
        return f"Telegram rate-limited the upload ({exc}). Wait, then try again."
    return str(exc)


async def _destination(client, message):
    return await get_destination(message, OWNER_ID)


@Client.on_message(filters.command("save") & filters.user(OWNER_ID))
async def save_single(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(
            "Usage: `/save <telegram_message_link>`\nOpen `/menu` for the button dashboard.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        return

    chat_id, msg_id = extract_chat_and_msg_id(message.command[1])
    if not chat_id:
        await message.reply_text("❌ Invalid Telegram message link.")
        return

    try:
        target_chat, thread_id = await _destination(client, message)
    except LookupError as exc:
        await message.reply_text(f"🎯 {exc}")
        return

    status_msg = await message.reply_text("⏳ **Preparing upload...**", parse_mode=enums.ParseMode.MARKDOWN)

    async def on_stage(text: str):
        await status_msg.edit_text(text)

    try:
        reader = get_userbot(client)
        poster = get_poster(client)
        problem = await prepare_chats(reader, chat_id, target_chat, sender=poster)
        if problem:
            await status_msg.edit_text(f"❌ {problem}")
            return

        source_msg = await reader.get_messages(chat_id, msg_id)
        if not source_msg or source_msg.empty:
            await status_msg.edit_text("❌ Message not found. It may have been deleted or inaccessible.")
            return

        await copy_or_reupload(
            reader, chat_id, source_msg, target_chat,
            thread_id=thread_id, on_stage=on_stage, sender=poster,
        )
        await status_msg.edit_text("✅ **Uploaded successfully to your destination group.**")

    except UnsupportedMedia as exc:
        await status_msg.edit_text(f"⚠️ **Skipped:** {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("/save failed")
        await status_msg.edit_text(f"❌ **Upload failed:** {_telegram_error_text(exc)}")


@Client.on_message(filters.command("batch") & filters.user(OWNER_ID))
async def save_batch(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text(
            "Usage: `/batch <first_link> <last_link>`\nBoth links must be from the same chat.",
            parse_mode=enums.ParseMode.MARKDOWN,
        )
        return

    chat_id, start_id = extract_chat_and_msg_id(message.command[1])
    end_chat, end_id = extract_chat_and_msg_id(message.command[2])

    if (
        not chat_id or not start_id or not end_id or start_id > end_id
        or end_chat != chat_id
    ):
        await message.reply_text("❌ Invalid range. Both links must be from the same chat, in ascending order.")
        return

    total = end_id - start_id + 1
    if total > MAX_BATCH:
        await message.reply_text(f"❌ Range too large ({total}). Maximum is {MAX_BATCH} posts per batch.")
        return

    try:
        target_chat, thread_id = await _destination(client, message)
    except LookupError as exc:
        await message.reply_text(f"🎯 {exc}")
        return

    owner_key = int(OWNER_ID)
    if owner_key in active_jobs:
        await message.reply_text("⚠️ A batch is already running. Use `/cancel` to stop it first.", parse_mode=enums.ParseMode.MARKDOWN)
        return

    cancel_event = asyncio.Event()
    active_jobs[owner_key] = cancel_event
    status_msg = await message.reply_text(
        f"⏳ **Starting batch**\nRange: `{start_id}` → `{end_id}`\nTotal: `{total}`\nCompleted: `0/{total}`",
        parse_mode=enums.ParseMode.MARKDOWN,
    )

    try:
        reader = get_userbot(client)
        poster = get_poster(client)
        problem = await prepare_chats(reader, chat_id, target_chat, sender=poster)
        if problem:
            await status_msg.edit_text(f"❌ {problem}")
            return

        success_count = 0
        errors = []
        cancelled = False

        for done, msg_id in enumerate(range(start_id, end_id + 1), start=1):
            if cancel_event.is_set():
                cancelled = True
                break
            if not guard.check_circuit():
                errors.append("Safety circuit opened — batch stopped early.")
                break

            try:
                source_msg = await reader.get_messages(chat_id, msg_id)
                if not source_msg or source_msg.empty:
                    errors.append(f"Message {msg_id}: not found/empty")
                else:
                    await copy_or_reupload(
                        reader, chat_id, source_msg, target_chat,
                        thread_id=thread_id, sender=poster,
                    )
                    success_count += 1
            except UnsupportedMedia as exc:
                errors.append(f"Message {msg_id}: skipped — {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Batch item %s failed", msg_id)
                errors.append(f"Message {msg_id}: {_telegram_error_text(exc)}")
                # If the destination itself rejects posting, continuing only creates noise.
                if type(exc).__name__ in {"ChatWriteForbidden", "ChatAdminRequired", "ChatRestricted", "UserNotParticipant"}:
                    break

            if done % 5 == 0 or done == total:
                try:
                    await status_msg.edit_text(
                        "📤 **Batch in progress**\n"
                        f"Completed: `{done}/{total}`\n"
                        f"Uploaded: `{success_count}`\n"
                        f"Skipped/errors: `{len(errors)}`\n\n"
                        "Use `/cancel` to stop.",
                        parse_mode=enums.ParseMode.MARKDOWN,
                    )
                except Exception:  # noqa: BLE001
                    pass

        report = (
            ("🛑 **Batch cancelled.**\n" if cancelled else "✅ **Batch finished.**\n")
            + f"Uploaded: `{success_count}` / `{total}`\n"
            + f"Issues: `{len(errors)}`"
        )
        if errors:
            report += "\n\n**First issues:**\n" + "\n".join(f"• {e[:220]}" for e in errors[:5])
            if len(errors) > 5:
                report += f"\n• ...and {len(errors) - 5} more."
        await status_msg.edit_text(report, parse_mode=enums.ParseMode.MARKDOWN)

    except Exception as exc:  # noqa: BLE001
        logger.exception("/batch failed")
        await status_msg.edit_text(f"❌ **Batch failed:** {_telegram_error_text(exc)}")
    finally:
        active_jobs.pop(owner_key, None)


@Client.on_message(filters.command("cancel") & filters.user(OWNER_ID))
async def cancel_batch(client: Client, message: Message):
    event = active_jobs.get(int(OWNER_ID))
    if not event:
        await message.reply_text("There is no active batch to cancel.")
        return
    event.set()
    await message.reply_text("🛑 Cancellation requested. The current upload will finish, then the batch will stop.")
