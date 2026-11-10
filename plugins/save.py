import logging
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import OWNER_ID
from plugins.helpers import (
    extract_chat_and_msg_id,
    get_target_topic,
    prepare_chats,
    guard,
)
from plugins.transfer import copy_or_reupload, UnsupportedMedia

logger = logging.getLogger(__name__)

MAX_BATCH = 500  # sanity cap so a typo can't queue 100k messages


def get_userbot(client: Client) -> Client:
    ub = getattr(client, "userbot", None)
    if ub is None:
        raise RuntimeError("Userbot client is not attached to the bot client.")
    return ub


@Client.on_message(filters.command("save") & filters.user(OWNER_ID))
async def save_single(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/save <link>`", parse_mode=enums.ParseMode.MARKDOWN)
        return

    chat_id, msg_id = extract_chat_and_msg_id(message.command[1])
    if not chat_id:
        await message.reply_text("❌ Invalid Telegram link.")
        return

    target_chat, thread_id = await get_target_topic(message)
    status_msg = await message.reply_text("⏳ **Fetching...**")

    async def on_stage(text: str):
        await status_msg.edit_text(text)

    try:
        user = get_userbot(client)

        problem = await prepare_chats(user, chat_id, target_chat)
        if problem:
            await status_msg.edit_text(f"❌ {problem}")
            return

        source_msg = await user.get_messages(chat_id, msg_id)
        if not source_msg or source_msg.empty:
            await status_msg.edit_text("❌ Message not found. It might be a system message or deleted.")
            return

        how = await copy_or_reupload(
            user, chat_id, source_msg, target_chat, thread_id=thread_id, on_stage=on_stage
        )
        suffix = " (re-uploaded)" if how == "reuploaded" else ""
        await status_msg.edit_text(f"✅ **Saved successfully.**{suffix}")

    except UnsupportedMedia as e:
        await status_msg.edit_text(f"⚠️ **Skipped:** {e}")
    except Exception as e:
        logger.exception("/save failed")
        await status_msg.edit_text(f"❌ **Error:** `{e}`")


@Client.on_message(filters.command("batch") & filters.user(OWNER_ID))
async def save_batch(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text(
            "Usage: `/batch <start_link> <end_link>`", parse_mode=enums.ParseMode.MARKDOWN
        )
        return

    chat_id, start_id = extract_chat_and_msg_id(message.command[1])
    _, end_id = extract_chat_and_msg_id(message.command[2])

    if not chat_id or not start_id or not end_id or start_id > end_id:
        await message.reply_text("❌ Invalid start or end link.")
        return

    total = end_id - start_id + 1
    if total > MAX_BATCH:
        await message.reply_text(f"❌ Range too large ({total}). Max is {MAX_BATCH} per batch.")
        return

    target_chat, thread_id = await get_target_topic(message)
    status_msg = await message.reply_text(f"⏳ **Processing batch {start_id} → {end_id} ({total} posts)...**")

    try:
        user = get_userbot(client)

        problem = await prepare_chats(user, chat_id, target_chat)
        if problem:
            await status_msg.edit_text(f"❌ {problem}")
            return

        success_count = 0
        reuploaded = 0
        errors = []

        for done, msg_id in enumerate(range(start_id, end_id + 1), start=1):
            if not guard.check_circuit():
                errors.append("Circuit breaker opened - batch stopped early.")
                break
            try:
                source_msg = await user.get_messages(chat_id, msg_id)
                if source_msg and not source_msg.empty:
                    how = await copy_or_reupload(
                        user, chat_id, source_msg, target_chat, thread_id=thread_id
                    )
                    success_count += 1
                    if how == "reuploaded":
                        reuploaded += 1
                else:
                    errors.append(f"Msg {msg_id}: Empty/System")
            except UnsupportedMedia as e:
                errors.append(f"Msg {msg_id}: skipped - {e}")
            except Exception as e:
                logger.exception("Batch item %s failed", msg_id)
                errors.append(f"Msg {msg_id}: {e}")

            if done % 10 == 0:
                try:
                    await status_msg.edit_text(f"⏳ **Progress:** {done}/{total} (saved {success_count})")
                except Exception:
                    pass  # progress edits are best-effort

        report = f"✅ **Batch complete.**\nSent {success_count} / {total} posts."
        if reuploaded:
            report += f" ({reuploaded} re-uploaded)"
        report += "\n"
        if errors:
            report += "\n**Errors Encountered:**\n"
            for err in errors[:3]:
                report += f"• `{err}`\n"
            if len(errors) > 3:
                report += f"• ...and {len(errors) - 3} more."
        await status_msg.edit_text(report)

    except Exception as e:
        logger.exception("/batch failed")
        await status_msg.edit_text(f"❌ **Batch error:** `{e}`")
