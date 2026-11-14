
import asyncio
import logging
import shlex

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from storage import get_preferences
from plugins.helpers import (
    parse_telegram_message_link,
    message_belongs_to_topic,
    get_message_topic_id,
    get_destination,
    prepare_chats,
    guard,
    safe_execute,
)
from plugins.transfer import (
    copy_or_reupload,
    reupload_media_group,
    UnsupportedMedia,
)

logger = logging.getLogger(__name__)

MAX_BATCH = 500                    # selected-topic messages per range
MAX_SCAN_SPAN = 10_000             # chat-wide message IDs to inspect per range
MAX_BATCH_RANGES = 10              # up to 10 start/end pairs in one command
MAX_MULTI_SCAN_SPAN = 50_000       # total ID span across all ranges
MAX_MULTI_MESSAGES = 2_000        # total selected messages across all ranges
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
    text = str(exc)
    if "FILE_REFERENCE_EXPIRED" in text.upper() or "FILEREFERENCEEXPIRED" in name.upper():
        return (
            "Telegram's media reference expired. The bot attempted to refresh the source message; "
            "try this item again if the refreshed download also failed."
        )
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
    return text


async def _destination(client, message):
    return await get_destination(message, OWNER_ID)


async def _edit_status(status_message: Message, text: str):
    """Status edits are best-effort and must not stop a transfer."""
    try:
        await status_message.edit_text(text, parse_mode=enums.ParseMode.DISABLED)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in {"MessageNotModified", "MessageIdInvalid", "MessageToEditNotFound"}:
            return
        logger.debug("Could not update transfer status: %s", exc)


async def _fetch_messages_ordered(reader, chat_id, message_ids):
    """Fetch in chunks and restore ascending source-message ID order."""
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
    """Resolve and validate the topic shared by both boundary links."""
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
    if is_forum and topic_id is None:
        topic_id = 1  # Telegram General topic

    if topic_id is not None:
        if not message_belongs_to_topic(start_msg, topic_id):
            raise ValueError("The first link does not belong to the selected forum topic.")
        if not message_belongs_to_topic(end_msg, topic_id):
            raise ValueError("The last link does not belong to the selected forum topic.")
    return topic_id


def _is_renamable_media(message) -> bool:
    media = getattr(message, "media", None)
    name = str(getattr(media, "name", media)).lower()
    return any(token in name for token in ("photo", "video", "document", "audio", "animation", "voice", "sticker", "video_note"))


def _format_filename_template(template: str, number: int, message_id: int, title: str | None) -> str:
    values = {"n": number, "index": number, "message_id": message_id, "title": title or ""}
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError(
            'Invalid --name-template. Supported placeholders: {n}, {index}, {message_id}, {title}.'
        ) from exc


async def _try_album(
    reader, poster, source_msg, chat_id, start_id, end_id, topic_id,
    target_chat, target_thread, on_stage, already_processed=None, preferences=None,
):
    """Preserve a 2–10 item album where possible; return source IDs consumed."""
    group_id = getattr(source_msg, "media_group_id", None)
    if not group_id or not hasattr(reader, "get_media_group"):
        return []

    try:
        group = await reader.get_media_group(chat_id, source_msg.id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Media-group lookup unavailable for %s: %s", source_msg.id, exc)
        return []

    already_processed = already_processed or set()
    in_range = [
        item for item in (group or [])
        if item
        and start_id <= int(getattr(item, "id", 0) or 0) <= end_id
        and (chat_id, int(getattr(item, "id", 0) or 0)) not in already_processed
        and message_belongs_to_topic(item, topic_id)
        and not getattr(item, "empty", False)
    ]
    in_range.sort(key=lambda item: int(item.id))
    if len(in_range) < 2:
        return []

    try:
        sent = await reupload_media_group(
            reader,
            poster,
            in_range,
            target_chat,
            thread_id=target_thread,
            on_stage=on_stage,
            source_chat=chat_id,
            preferences=preferences,
        )
        if sent:
            return [int(item.id) for item in in_range]
    except UnsupportedMedia:
        logger.info("Album %s is not supported as a media group; sending items individually", group_id)
    except Exception:
        logger.exception("Album upload failed for media_group_id=%s", group_id)
        raise
    return []


async def _prepare_range(reader, start_ref, end_ref, range_number):
    """Fetch and topic-filter one pair before any uploads begin."""
    if start_ref.chat_id != end_ref.chat_id:
        raise ValueError(f"Range {range_number}: both links must belong to the same Telegram chat.")

    start_id, end_id = start_ref.message_id, end_ref.message_id
    if start_id > end_id:
        raise ValueError(f"Range {range_number}: the first link must be older than the second link.")

    scan_span = end_id - start_id + 1
    if scan_span > MAX_SCAN_SPAN:
        raise ValueError(
            f"Range {range_number}: this ID span is {scan_span:,}; per-range scan limit is "
            f"{MAX_SCAN_SPAN:,}. This is a scan-safety limit, not the topic message limit."
        )

    source_chat = await _safe_get_chat(reader, start_ref.chat_id)
    boundaries = await _fetch_messages_ordered(reader, start_ref.chat_id, [start_id, end_id])
    first_msg, last_msg = boundaries[0], boundaries[-1]

    if not first_msg or getattr(first_msg, "empty", False):
        raise ValueError(f"Range {range_number}: first link points to a missing or inaccessible message.")
    if not last_msg or getattr(last_msg, "empty", False):
        raise ValueError(f"Range {range_number}: last link points to a missing or inaccessible message.")

    topic_id = _resolve_batch_topic(start_ref, end_ref, first_msg, last_msg, source_chat)
    ordered_messages = await _fetch_messages_ordered(
        reader, start_ref.chat_id, range(start_id, end_id + 1)
    )

    topic_messages = []
    ignored_other_topics = 0
    unavailable_ids = 0

    for message_id, source_msg in zip(range(start_id, end_id + 1), ordered_messages):
        if not source_msg or getattr(source_msg, "empty", False):
            unavailable_ids += 1
            continue
        if not message_belongs_to_topic(source_msg, topic_id):
            ignored_other_topics += 1
            continue
        topic_messages.append((message_id, source_msg))

    if len(topic_messages) > MAX_BATCH:
        raise ValueError(
            f"Range {range_number}: selected topic contains {len(topic_messages):,} messages; "
            f"per-range maximum is {MAX_BATCH:,}."
        )
    if not topic_messages:
        raise ValueError(f"Range {range_number}: no accessible messages found in the selected topic.")

    return {
        "number": range_number,
        "chat_id": start_ref.chat_id,
        "start_id": start_id,
        "end_id": end_id,
        "topic_id": topic_id,
        "messages": topic_messages,
        "ignored": ignored_other_topics,
        "unavailable": unavailable_ids,
        "scan_span": scan_span,
    }


def _command_tokens(message: Message) -> list[str]:
    try:
        tokens = shlex.split(message.text or "")
    except ValueError as exc:
        raise ValueError(f"Could not parse quoted arguments: {exc}") from exc
    return tokens[1:]


def _parse_single_options(tokens: list[str], rename_command: bool = False):
    if not tokens:
        raise ValueError("A Telegram message link is required.")
    link = tokens[0]
    custom_name = None
    custom_caption = None
    index = 1

    if rename_command and index < len(tokens) and not tokens[index].startswith("--"):
        custom_name = " ".join(tokens[index:])
        index = len(tokens)

    while index < len(tokens):
        flag = tokens[index]
        index += 1
        if flag not in {"--name", "--caption"}:
            raise ValueError(f"Unknown option: {flag}. Supported: --name, --caption.")
        if index >= len(tokens):
            raise ValueError(f"{flag} needs a value.")
        value = tokens[index]
        index += 1
        if flag == "--name":
            custom_name = value
        else:
            custom_caption = value

    if rename_command and not custom_name:
        raise ValueError('Usage: /rename <telegram_link> "New filename.ext"')
    return link, custom_name, custom_caption


@Client.on_message(filters.command(["save", "rename"]) & filters.user(OWNER_ID))
async def save_single(client: Client, message: Message):
    command_name = str((message.command or ["save"])[0]).split("@", 1)[0].lstrip("/").lower()
    try:
        link, custom_name, custom_caption = _parse_single_options(
            _command_tokens(message), rename_command=command_name == "rename"
        )
    except ValueError as exc:
        await message.reply_text(
            f"❌ {exc}\n\n"
            'Examples:\n/save <link> --name "Lecture 01.mp4"\n'
            '/save <link> --caption "Week 1 notes"\n'
            '/rename <link> "Renamed lecture.mp4"'
        )
        return

    reference = parse_telegram_message_link(link)
    if not reference:
        await message.reply_text("❌ Invalid Telegram message link. Please copy the direct message link.")
        return

    try:
        target_chat, target_thread = await _destination(client, message)
    except LookupError as exc:
        await message.reply_text(f"🎯 {exc}")
        return

    status_msg = await message.reply_text(
        f"⏳ Preparing upload{f' as {custom_name}' if custom_name else ''}..."
    )

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
            reader, reference.chat_id, source_msg, target_chat,
            thread_id=target_thread,
            on_stage=on_stage,
            sender=poster,
            custom_filename=custom_name,
            custom_caption=custom_caption,
            preferences=get_preferences(OWNER_ID),
        )
        await _edit_status(status_msg, "✅ Uploaded successfully to your destination group.")

    except UnsupportedMedia as exc:
        await _edit_status(status_msg, f"⚠️ Skipped: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("/%s failed", command_name)
        await _edit_status(status_msg, f"❌ Upload failed: {_telegram_error_text(exc)}")


@Client.on_message(filters.command(["batch", "multibatch"]) & filters.user(OWNER_ID))
async def save_batch(client: Client, message: Message):
    """Accept /batch start end or titled /multibatch range pairs.

    Syntax:
      /multibatch <start1> <end1> --title "Percentages" --names "File 1.mp4|File 2.pdf" \
                   <start2> <end2> --title "Profit & Loss" --name-template "P&L {n}"
    """
    try:
        tokens = _command_tokens(message)
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}")
        return
    command_name = str((message.command or ["batch"])[0]).split("@", 1)[0].lower()

    parsed_specs = []
    cursor = 0
    while cursor < len(tokens):
        if cursor + 1 >= len(tokens) or tokens[cursor].startswith("--"):
            await message.reply_text(
                "❌ Each range must begin with two links: start link, then end link."
            )
            return
        start_link, end_link = tokens[cursor], tokens[cursor + 1]
        cursor += 2
        metadata = {"title": None, "names": None, "name_template": None, "caption": None}
        while cursor < len(tokens) and tokens[cursor].startswith("--"):
            flag = tokens[cursor]
            cursor += 1
            if flag not in {"--title", "--names", "--name-template", "--caption"}:
                await message.reply_text(
                    f"❌ Unknown option {flag}. Use --title, --names, --name-template or --caption."
                )
                return
            if cursor >= len(tokens) or tokens[cursor].startswith("--"):
                await message.reply_text(f"❌ {flag} needs a value in quotation marks.")
                return
            value = tokens[cursor]
            cursor += 1
            key = flag[2:].replace("-", "_")
            if key == "names":
                names = [part.strip() for part in value.split("|") if part.strip()]
                if not names:
                    await message.reply_text("❌ --names requires filenames separated by |.")
                    return
                metadata[key] = names
            else:
                metadata[key] = value
        parsed_specs.append((start_link, end_link, metadata))

    if not parsed_specs:
        await message.reply_text(
            "Usage:\n/batch <first_link> <last_link> [--title \"Section title\"]\n\n"
            "/multibatch <start1> <end1> --title \"Topic 1\" "
            "<start2> <end2> --title \"Topic 2\"\n\n"
            "Optional per-range options:\n"
            '--names "File 01.mp4|Notes.pdf"\n'
            '--name-template "Lecture {n}"\n'
            '--caption "Custom caption"'
        )
        return

    pairs_count = len(parsed_specs)
    if pairs_count > MAX_BATCH_RANGES:
        await message.reply_text(f"❌ Too many ranges. Maximum is {MAX_BATCH_RANGES} start/end pairs.")
        return
    if command_name == "multibatch" and pairs_count < 2:
        await message.reply_text("❌ /multibatch needs at least two start/end pairs (four links).")
        return

    parsed_pairs = []
    for index, (start_link, end_link, metadata) in enumerate(parsed_specs, start=1):
        start_ref = parse_telegram_message_link(start_link)
        end_ref = parse_telegram_message_link(end_link)
        if not start_ref or not end_ref:
            await message.reply_text(f"❌ Range {index}: one of its Telegram links is invalid.")
            return
        parsed_pairs.append((start_ref, end_ref, metadata))

    total_scan_span = sum(
        max(0, end_ref.message_id - start_ref.message_id + 1)
        for start_ref, end_ref, _metadata in parsed_pairs
    )
    if total_scan_span > MAX_MULTI_SCAN_SPAN:
        await message.reply_text(
            f"❌ Combined message-ID scan span is {total_scan_span:,}; "
            f"multi-range scan limit is {MAX_MULTI_SCAN_SPAN:,}. "
            "Use narrower boundary links or split the work into two commands."
        )
        return

    # Validate the link pairs before reserving the job or sending anything.
    for index, (start_ref, end_ref, _metadata) in enumerate(parsed_pairs, start=1):
        if start_ref.chat_id != end_ref.chat_id:
            await message.reply_text(f"❌ Range {index}: both links must be from the same Telegram chat.")
            return
        if start_ref.message_id > end_ref.message_id:
            await message.reply_text(f"❌ Range {index}: start link must be older than end link.")
            return
        if end_ref.message_id - start_ref.message_id + 1 > MAX_SCAN_SPAN:
            await message.reply_text(
                f"❌ Range {index} exceeds the per-range scan limit of {MAX_SCAN_SPAN:,} IDs."
            )
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
        f"⏳ Preparing {pairs_count} range(s)...\n"
        f"Combined ID scan span: {total_scan_span:,}\n"
        "No uploads will start until every range has been checked."
    )

    try:
        reader = get_userbot(client)
        poster = get_poster(client)
        preferences = get_preferences(OWNER_ID)

        # Preflight all sources, then upload. This avoids starting range 1 only to
        # discover that range 3 is malformed or inaccessible.
        prepared_ranges = []
        peer_checks = set()

        for index, (start_ref, end_ref, metadata) in enumerate(parsed_pairs, start=1):
            if cancel_event.is_set():
                await _edit_status(status_msg, "🛑 Cancelled during preparation; nothing was uploaded.")
                return

            source_key = str(start_ref.chat_id)
            if source_key not in peer_checks:
                problem = await prepare_chats(reader, start_ref.chat_id, target_chat, sender=poster)
                if problem:
                    raise RuntimeError(f"Range {index}: {problem}")
                peer_checks.add(source_key)

            await _edit_status(
                status_msg,
                f"🔎 Checking range {index}/{pairs_count}...\n"
                f"Source chat: {start_ref.chat_id}\n"
                f"IDs: {start_ref.message_id} → {end_ref.message_id}"
            )
            prepared = await _prepare_range(reader, start_ref, end_ref, index)
            prepared.update(metadata)
            media_messages = [
                (mid, item) for mid, item in prepared["messages"]
                if _is_renamable_media(item)
            ]
            if prepared.get("names") is not None and len(prepared["names"]) != len(media_messages):
                raise ValueError(
                    f"Range {index}: --names contains {len(prepared['names'])} filename(s), "
                    f"but the range contains {len(media_messages)} media file(s). Provide one name per file."
                )
            if prepared.get("names") and prepared.get("name_template"):
                raise ValueError(f"Range {index}: use either --names or --name-template, not both.")
            prepared["filename_by_id"] = {}
            for media_index, (media_id, _media_message) in enumerate(media_messages, start=1):
                if prepared.get("names"):
                    prepared["filename_by_id"][int(media_id)] = prepared["names"][media_index - 1]
                elif prepared.get("name_template"):
                    prepared["filename_by_id"][int(media_id)] = _format_filename_template(
                        prepared["name_template"], media_index, int(media_id), prepared.get("title")
                    )
            prepared_ranges.append(prepared)

        # Remove overlapping duplicates while preserving the order of the
        # supplied ranges and the ascending order within each range.
        seen_messages = set()
        duplicate_count = 0

        for prepared in prepared_ranges:
            unique_messages = []
            for message_id, source_msg in prepared["messages"]:
                key = (str(prepared["chat_id"]), int(message_id))
                if key in seen_messages:
                    duplicate_count += 1
                    continue
                seen_messages.add(key)
                unique_messages.append((message_id, source_msg))
            prepared["messages"] = unique_messages

        selected_total = sum(len(item["messages"]) for item in prepared_ranges)
        if selected_total > MAX_MULTI_MESSAGES:
            raise ValueError(
                f"The selected ranges contain {selected_total:,} unique topic messages; "
                f"the multi-range safety limit is {MAX_MULTI_MESSAGES:,}."
            )
        if selected_total == 0:
            raise ValueError("No accessible messages were found in the supplied ranges.")

        ignored_total = sum(item["ignored"] for item in prepared_ranges)
        unavailable_total = sum(item["unavailable"] for item in prepared_ranges)
        summary = "\n".join(
            f"{item['number']}. {item.get('title') or ('Topic ' + str(item['topic_id'] or '—'))}: {len(item['messages'])} messages"
            for item in prepared_ranges
        )

        await _edit_status(
            status_msg,
            f"✅ All {pairs_count} ranges checked.\n"
            f"Unique selected messages: {selected_total}\n"
            f"Other-topic messages ignored: {ignored_total}\n"
            f"Unavailable message IDs: {unavailable_total}\n"
            f"Overlapping duplicates removed: {duplicate_count}\n\n"
            f"Upload order:\n{summary}\n\nStarting uploads in the supplied range order..."
        )

        success_count = 0
        errors = []
        cancelled = False
        processed_ids = set()
        processed_albums = set()
        attempted_count = 0

        async def on_stage(text: str):
            await _edit_status(status_msg, text)

        for prepared in prepared_ranges:
            if cancel_event.is_set():
                cancelled = True
                break

            range_number = prepared["number"]
            chat_id = prepared["chat_id"]
            start_id = prepared["start_id"]
            end_id = prepared["end_id"]
            topic_id = prepared["topic_id"]
            messages = prepared["messages"]
            section_title = (prepared.get("title") or "").strip()
            if section_title:
                await safe_execute(
                    poster.send_message,
                    chat_id=target_chat,
                    text=f"📚 {section_title}",
                    parse_mode=enums.ParseMode.DISABLED,
                    message_thread_id=target_thread,
                )

            await _edit_status(
                status_msg,
                f"📤 Starting range {range_number}/{pairs_count}\n"
                f"Section: {section_title or ('Topic ' + str(topic_id or '—'))}\n"
                f"Messages in this range: {len(messages)}\n"
                f"Overall uploaded: {success_count}/{selected_total}"
            )

            for position, (message_id, source_msg) in enumerate(messages, start=1):
                if cancel_event.is_set():
                    cancelled = True
                    break

                global_key = (str(chat_id), int(message_id))
                if global_key in processed_ids:
                    continue
                if not guard.check_circuit():
                    errors.append("Safety circuit opened; batch stopped early to protect the account.")
                    cancelled = True
                    break

                try:
                    media_group_id = getattr(source_msg, "media_group_id", None)
                    album_key = (str(chat_id), str(media_group_id)) if media_group_id else None

                    has_per_file_overrides = bool(
                        prepared.get("names") or prepared.get("name_template") or prepared.get("caption")
                        or preferences.get("caption_mode") != "keep" or preferences.get("replace_rules")
                    )
                    if album_key and album_key not in processed_albums and not has_per_file_overrides:
                        consumed = await _try_album(
                            reader, poster, source_msg, chat_id,
                            start_id, end_id, topic_id,
                            target_chat, target_thread, on_stage,
                            already_processed=processed_ids,
                            preferences=preferences,
                        )
                        processed_albums.add(album_key)

                        if consumed:
                            for consumed_id in consumed:
                                processed_ids.add((str(chat_id), int(consumed_id)))
                            success_count += len(consumed)
                            attempted_count += len(consumed)

                            if position % 5 == 0 or position == len(messages):
                                await _edit_status(
                                    status_msg,
                                    f"📤 Uploading range {range_number}/{pairs_count}\n"
                                    f"Range progress: {position}/{len(messages)}\n"
                                    f"Overall uploaded: {success_count}/{selected_total}\n"
                                    f"Issues: {len(errors)}\nUse /cancel to stop."
                                )
                            continue

                    custom_filename = prepared.get("filename_by_id", {}).get(int(message_id))

                    await copy_or_reupload(
                        reader, chat_id, source_msg, target_chat,
                        thread_id=target_thread,
                        on_stage=on_stage,
                        sender=poster,
                        custom_filename=custom_filename,
                        custom_caption=prepared.get("caption"),
                        preferences=preferences,
                    )
                    success_count += 1
                    attempted_count += 1
                    processed_ids.add(global_key)

                except UnsupportedMedia as exc:
                    errors.append(f"Range {range_number}, message {message_id}: skipped — {exc}")
                    processed_ids.add(global_key)

                except Exception as exc:  # noqa: BLE001
                    logger.exception("Range %s message %s failed", range_number, message_id)
                    errors.append(f"Range {range_number}, message {message_id}: {_telegram_error_text(exc)}")
                    processed_ids.add(global_key)

                    if type(exc).__name__ in {
                        "ChatWriteForbidden", "ChatAdminRequired", "ChatRestricted", "UserNotParticipant"
                    }:
                        cancelled = True
                        break

                if position % 5 == 0 or position == len(messages):
                    await _edit_status(
                        status_msg,
                        f"📤 Uploading range {range_number}/{pairs_count}\n"
                        f"Range progress: {position}/{len(messages)}\n"
                        f"Overall uploaded: {success_count}/{selected_total}\n"
                        f"Issues: {len(errors)}\nUse /cancel to stop."
                    )

            if cancelled:
                break

        final_status = "🛑 Multi-batch cancelled." if cancelled else "✅ Multi-batch finished."
        result = (
            f"{final_status}\n"
            f"Ranges processed: {pairs_count}\n"
            f"Uploaded: {success_count}\n"
            f"Skipped/unavailable source IDs: {unavailable_total}\n"
            f"Ignored messages from other topics: {ignored_total}\n"
            f"Overlapping duplicates removed: {duplicate_count}\n"
            f"Issues: {len(errors)}"
        )

        if errors:
            result += "\n\nFirst issues:\n" + "\n".join(f"• {item[:220]}" for item in errors[:5])
            if len(errors) > 5:
                result += f"\n• ...and {len(errors) - 5} more."

        await _edit_status(status_msg, result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Batch preparation/upload failed")
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
    await message.reply_text(
        "🛑 Cancellation requested. The current upload will finish, then the batch will stop."
    )
