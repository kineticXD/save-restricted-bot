import asyncio
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import OWNER_ID
from plugins.helpers import extract_chat_and_msg_id, get_target_topic, safe_execute

logger = logging.getLogger(__name__)

@Client.on_message(filters.command("save") & filters.user(OWNER_ID))
async def save_single(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: `/save <link>`", parse_mode=enums.ParseMode.MARKDOWN)
        return

    link = message.command[1]
    chat_id, msg_id = extract_chat_and_msg_id(link)
    if not chat_id:
        await message.reply_text("❌ Invalid Telegram link.")
        return

    target_chat, thread_id = await get_target_topic(message)
    status_msg = await message.reply_text("⏳ **Fetching...**")

    try:
        from main import user
        
        # Safely resolve the peer ID so Pyrogram caches it
        await user.resolve_peer(target_chat)
        
        source_msg = await user.get_messages(chat_id, msg_id)
        if not source_msg or source_msg.empty:
            await status_msg.edit_text("❌ Message not found. It might be a system message or deleted.")
            return

        await safe_execute(
            user.copy_message,
            chat_id=target_chat,
            from_chat_id=chat_id,
            message_id=msg_id,
            message_thread_id=thread_id
        )
        await status_msg.edit_text("✅ **Saved successfully.**")

    except Exception as e:
        await status_msg.edit_text(f"❌ **Error:** `{str(e)}`")

@Client.on_message(filters.command("batch") & filters.user(OWNER_ID))
async def save_batch(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text("Usage: `/batch <start_link> <end_link>`", parse_mode=enums.ParseMode.MARKDOWN)
        return

    start_link = message.command[1]
    end_link = message.command[2]
    chat_id, start_id = extract_chat_and_msg_id(start_link)
    _, end_id = extract_chat_and_msg_id(end_link)

    if not chat_id or not start_id or not end_id or start_id > end_id:
        await message.reply_text("❌ Invalid start or end link.")
        return

    target_chat, thread_id = await get_target_topic(message)
    status_msg = await message.reply_text(f"⏳ **Processing batch from {start_id} to {end_id}...**")

    try:
        from main import user
        
        # Safely resolve the peer ID so Pyrogram caches it
        await user.resolve_peer(target_chat)
        
        success_count = 0
        semaphore = asyncio.Semaphore(1) 
        errors = []

        async def process_one(msg_id):
            async with semaphore:
                try:
                    source_msg = await user.get_messages(chat_id, msg_id)
                    if source_msg and not source_msg.empty:
                        await safe_execute(
                            user.copy_message,
                            chat_id=target_chat,
                            from_chat_id=chat_id,
                            message_id=msg_id,
                            message_thread_id=thread_id
                        )
                        return True
                    else:
                        errors.append(f"Msg {msg_id}: Empty/System")
                except Exception as e:
                    errors.append(f"Msg {msg_id}: {str(e)}")
                return False

        tasks = [process_one(mid) for mid in range(start_id, end_id + 1)]
        results = await asyncio.gather(*tasks)
        success_count = sum(results)

        report = f"✅ **Batch complete.**\nSent {success_count} / {end_id - start_id + 1} posts.\n"
        
        if errors:
            report += "\n**Errors Encountered:**\n"
            for err in errors[:3]: 
                report += f"• `{err}`\n"
            if len(errors) > 3:
                report += f"• ...and {len(errors) - 3} more."
                
        await status_msg.edit_text(report)

    except Exception as e:
        await status_msg.edit_text(f"❌ **Batch error:** `{str(e)}`")
