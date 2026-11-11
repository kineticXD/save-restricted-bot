
import asyncio
import logging

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from plugins.helpers import (
    parse_telegram_message_link,
    message_belongs_to_topic,
    get_message_topic_id,
    get_destination,
    prepare_chats,
    guard,
)
from plugins.transfer import (
    copy_or_reupload,
    reupload_media_group,
    UnsupportedMedia,
)

logger = logging.getLogger(__name__)

MAX_BATCH = 500
FETCH_CHUNK = 100
active_jobs = {}


def get_userbot(client: Client) -> Client:
    ub = getattr(client, "userbot", None)
    if ub is None:
        raise RuntimeError("Source reader session is not attached to the bot client.")
    return ub


def get_poster(client: Client) -> Client:
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
            "regular member, then run /settarget again."
        )
    if name in {"SlowmodeWait", "FloodWait"}:
        return f"Telegram rate-limited the upload ({exc}). Wait, then try again."
    return str(exc)


async def _destination(client, message):
    return await get_destination(message, OWNER_ID)


async def _edit_status(status_message: Message, text: str):
    """Make status edits resilient to duplicate, stale, or rate-limited updates."""
    try:
        await status_message.edit_text(text, parse_mode=enums.ParseMode.DISABLED)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name in {"MessageNotModified", "MessageIdInvalid", "MessageToEditNotFound"}:
            return
        logger.debug("Could not update transfer status: %s", exc)


async def _fetch_messages_ordered(reader, chat_id, message_ids):
    """Fetch IDs in chunks, then explicitly restore ascending message-ID order."""
    by_id = {}
    ids = list(message_ids)
    for offset in range(0, len(ids), FETCH_CHUNK):
        chunk = ids[offset:offset + FETCH_CHUNK]
        try:
            try:
                result = await reader.get_messages(chat_id, message_ids=chunk, replies=0)
            except TypeError:
                result = await reader.get_messages(chat_id, chunk)
            if not isinstance(result, (list, tuple)):
                result = [result]
            for item in result:
                if item is not None and getattr(item, "id", None) is not None:
                    by_id[int(item.id)] = item
        except Exception as exc:  # noqa: BLE001
            # A chunk failure should not silently reorder or abort the whole range.
            logger.warning("Chunk fetch failed for %s (%s); retrying IDs individually", chunk, exc)
            for message_id in chunk:
                try:
                    item = await reader.get_messages(chat_id, message_id)
                    if item is not None and getattr(item, "id", None) is not None:
                        by_id[int(item.id)] = item
                except Exception:
                    logger.exception("Could not fetch source message %s", message_id)
    return [by_id.get(int(message_id)) for message_id in ids]


async def _safe_get_chat(reader, chat_id):
    try:
        return await reader.get_chat(chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not inspect source chat %s: %s", chat_id, exc)
        return None


def _resolve_batch_topic(start_ref, end_ref, start_msg, end_msg, source_chat):
    """Resolve one topic for the entire range; never guess a named topic."""
    explicit = [value for value in (start_ref.topic_id, end_ref.topic_id) if value is not None]
    if len(set(explicit)) > 1:
        raise ValueError("The two links point to different forum topics. Use two links from the same topic.")

    start_topic = get_message_topic_id(start_msg)
    end_topic = get_message_topic_id(end_msg)
    inferred = [value for value in (start_topic, end_topic) if value is not None]
    if len(set(inferred)) > 1:
        raise ValueError("The first and last messages belong to different forum topics.")

    topic_id = explicit[0] if explicit else (inferred[0] if inferred else None)
    is_forum = bool(getattr(source_chat, "is_forum", False)) if source_chat else False

    # In Telegram forums, unthreaded messages belong to General (topic 1).
    # Named topics have a reply_to_top_message_id; the explicit link path is
    # preferred because some client versions omit thread metadata on service posts.
    if is_forum and topic_id is None:
        topic_id = 1

    if topic_id is not None:
        if not message_belongs_to_topic(start_msg, topic_id):
            raise ValueError("The first link does not belong to the selected forum topic.")
        if not message_belongs_to_topic(end_msg, topic_id):
            raise ValueError("The second link does not belong to the selected forum topic.")

    return topic_id


async def _try_album(reader, poster, source_msg, chat_id, start_id, end_id,
                     topic_id, target_chat, target_thread, on_stage):
    """Try to preserve a 2–10 item album; return the source IDs consumed."""
    group_id = getattr(source_msg, "media_group_id", None)
    if not group_id or not hasattr(reader, "get_media_group"):
        return []

    try:
        group = await reader.get_media_group(chat_id, source_msg.id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Media-group lookup unavailable for %s: %s", source_msg.id, exc)
        return []

    in_range = [
        item for item in (group or [])
        if item
        and start_id <= int(getattr(item, "id", 0) or 0) <= end_id
        and message_belongs_to_topic(item, topic_id)
        and not getattr(item, "empty", False)
    ]
    in_range.sort(key=lambda item: int(item.id))
    if len(in_range) < 2:
        return []

    # Do not cross a topic boundary if metadata on an album item is inconsistent.
    if any(not message_belongs_to_topic(item, topic_id) for item in in_range):
        return []

    try:
        sent = await reupload_media_group(
            reader,
            poster,
            in_range,
            target_chat,
            thread_id=target_thread,
            on_stage=on_stage,
        )
        if sent:
            return [int(item.id) for item in in_range]
    except UnsupportedMedia:
        logger.info("Album %s cannot be sent as one Telegram media group; sending items individually", group_id)
    except Exception:
        logger.exception("Album upload failed for media_group_id=%s", group_id)
        raise
    return []


@Client.on_message(filters.command("save") & filters.user(OWNER_ID))
async def save_single(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(
            "Usage: /save <telegram_message_link>\nOpen /menu for the button dashboard."
        )
        return

    reference = parse_telegram_message_link(message.command[1])
    if not reference:
        await message.reply_text("❌ Invalid Telegram message link. Please copy the direct message link.")
        return

    try:
        target_chat, target_thread = await _destination(client, message)
    except LookupError as exc:
        await message.reply_text(f"🎯 {exc}")
        return

    status_msg = await message.reply_text("⏳ Preparing upload...")

    async def on_stage(text: str):
        await _edit_status(status_msg, text)

    try:
        reader = get_userbot(client)
        poster = get_poster(client)
        problem = await prepare_chats(reader, reference.chat_id, target_chat, sender=poster)
        if problem:
            await _edit_status(status_msg, f"❌ {problem}")
            return

        source_msg = await reader.get_messages(reference.chat_id, reference.message_id)
        if not source_msg or getattr(source_msg, "empty", False):
            await _edit_status(status_msg, "❌ Message not found. It may have been deleted or is inaccessible.")
            return

        if reference.topic_id is not None and not message_belongs_to_topic(source_msg, reference.topic_id):
            await _edit_status(status_msg, "❌ The linked message does not match the topic shown in the Telegram link.")
            return

        await copy_or_reupload(
            reader,
            reference.chat_id,
            source_msg,
            target_chat,
            thread_id=target_thread,
            on_stage=on_stage,
            sender=poster,
        )
        await _edit_status(status_msg, "✅ Uploaded successfully to your destination group.")

    except UnsupportedMedia as exc:
        await _edit_status(status_msg, f"⚠️ Skipped: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("/save failed")
        await _edit_status(status_msg, f"❌ Upload failed: {_telegram_error_text(exc)}")


@Client.on_message(filters.command("batch") & filters.user(OWNER_ID))
async def save_batch(client: Client, message: Message):
    if len(message.command) < 3:
        await message.reply_text(
            "Usage: /batch <first_link> <last_link>\n"
            "Both links must be from the same chat and the same forum topic."
        )
        return

    start_ref = parse_telegram_message_link(message.command[1])
    end_ref = parse_telegram_message_link(message.command[2])
    if not start_ref or not end_ref:
        await message.reply_text("❌ One of the Telegram message links is invalid.")
        return

    if start_ref.chat_id != end_ref.chat_id:
        await message.reply_text("❌ Both links must belong to the same Telegram chat.")
        return

    start_id, end_id = start_ref.message_id, end_ref.message_id
    if start_id > end_id:
        await message.reply_text("❌ The first link must be older than or equal to the second link.")
        return

    total = end_id - start_id + 1
    if total > MAX_BATCH:
        await message.reply_text(f"❌ Range too large ({total} messages). Maximum is {MAX_BATCH}.")
        return

    try:
        target_chat, target_thread = await _destination(client, message)
    except LookupError as exc:
        await message.reply_text(f"🎯 {exc}")
        return

    owner_key = int(OWNER_ID)
    if owner_key in active_jobs:
        await message.reply_text("⚠️ A batch is already running. Use /cancel to stop it first.")
        return

    cancel_event = asyncio.Event()
    active_jobs[owner_key] = cancel_event
    status_msg = await message.reply_text(
        f"⏳ Starting topic-aware batch\nRange: {start_id} → {end_id}\nMessages in range: {total}\nUploaded: 0"
    )

    try:
        reader = get_userbot(client)
        poster = get_poster(client)
        problem = await prepare_chats(reader, start_ref.chat_id, target_chat, sender=poster)
        if problem:
            await _edit_status(status_msg, f"❌ {problem}")
            return

        source_chat = await _safe_get_chat(reader, start_ref.chat_id)
        boundary_messages = await _fetch_messages_ordered(
            reader, start_ref.chat_id, [start_id, end_id]
        )
        first_msg, last_msg = boundary_messages[0], boundary_messages[-1]
        if not first_msg or getattr(first_msg, "empty", False):
            await _edit_status(status_msg, "❌ The first link points to a missing or inaccessible message.")
            return
        if not last_msg or getattr(last_msg, "empty", False):
            await _edit_status(status_msg, "❌ The second link points to a missing or inaccessible message.")
            return

        try:
            topic_id = _resolve_batch_topic(start_ref, end_ref, first_msg, last_msg, source_chat)
        except ValueError as exc:
            await _edit_status(status_msg, f"❌ {exc}")
            return

        # Load a stable, ordered snapshot of the message IDs. The message IDs in
        # a forum are chat-wide, so we explicitly discard every other topic.
        await _edit_status(
            status_msg,
            f"🔎 Checking messages in order...\nRange: {start_id} → {end_id}\n"
            f"Source topic: {topic_id if topic_id is not None else 'not a forum'}",
        )
        ordered_messages = await _fetch_messages_ordered(
            reader, start_ref.chat_id, range(start_id, end_id + 1)
        )

        success_count = 0
        ignored_other_topics = 0
        missing_count = 0
        errors = []
        cancelled = False
        processed_ids = set()
        processed_albums = set()

        async def on_stage(text: str):
            await _edit_status(status_msg, text)

        for done, (message_id, source_msg) in enumerate(
            zip(range(start_id, end_id + 1), ordered_messages), start=1
        ):
            if cancel_event.is_set():
                cancelled = True
                break
            if message_id in processed_ids:
                continue
            if not guard.check_circuit():
                errors.append("Safety circuit opened; batch stopped early to protect the account.")
                break

            if not source_msg or getattr(source_msg, "empty", False):
                missing_count += 1
                continue

            if not message_belongs_to_topic(source_msg, topic_id):
                ignored_other_topics += 1
                continue

            try:
                media_group_id = getattr(source_msg, "media_group_id", None)
                if media_group_id and media_group_id not in processed_albums:
                    consumed = await _try_album(
                        reader,
                        poster,
                        source_msg,
                        start_ref.chat_id,
                        start_id,
                        end_id,
                        topic_id,
                        target_chat,
                        target_thread,
                        on_stage,
                    )
                    if consumed:
                        success_count += len(consumed)
                        processed_ids.update(consumed)
                        processed_albums.add(media_group_id)
                        if done % 5 == 0 or done == total:
                            await _edit_status(
                                status_msg,
                                "📤 Batch in progress\n"
                                f"Checked: {done}/{total}\n"
                                f"Uploaded: {success_count}\n"
                                f"Ignored other topics: {ignored_other_topics}\n"
                                f"Missing: {missing_count}\n"
                                f"Issues: {len(errors)}\n\n"
                                "Use /cancel to stop.",
                            )
                        continue
                    processed_albums.add(media_group_id)

                await copy_or_reupload(
                    reader,
                    start_ref.chat_id,
                    source_msg,
                    target_chat,
                    thread_id=target_thread,
                    on_stage=on_stage,
                    sender=poster,
                )
                success_count += 1

            except UnsupportedMedia as exc:
                errors.append(f"Message {message_id}: skipped — {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Batch item %s failed", message_id)
                errors.append(f"Message {message_id}: {_telegram_error_text(exc)}")
                if type(exc).__name__ in {
                    "ChatWriteForbidden", "ChatAdminRequired", "ChatRestricted", "UserNotParticipant"
                }:
                    break

            if done % 5 == 0 or done == total:
                await _edit_status(
                    status_msg,
                    "📤 Batch in progress\n"
                    f"Checked: {done}/{total}\n"
                    f"Uploaded: {success_count}\n"
                    f"Ignored other topics: {ignored_other_topics}\n"
                    f"Missing: {missing_count}\n"
                    f"Issues: {len(errors)}\n\n"
                    "Use /cancel to stop.",
                )

        result = (
            ("🛑 Batch cancelled.\n" if cancelled else "✅ Batch finished.\n")
            + f"Uploaded: {success_count}\n"
            + f"Ignored messages from other topics: {ignored_other_topics}\n"
            + f"Missing messages: {missing_count}\n"
            + f"Issues: {len(errors)}"
        )
        if errors:
            result += "\n\nFirst issues:\n" + "\n".join(f"• {item[:220]}" for item in errors[:5])
            if len(errors) > 5:
                result += f"\n• ...and {len(errors) - 5} more."
        await _edit_status(status_msg, result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("/batch failed")
        await _edit_status(status_msg, f"❌ Batch failed: {_telegram_error_text(exc)}")
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
