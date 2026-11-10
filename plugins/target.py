
"""Destination-group commands for the Save Restricted Bot.

Replace this file at: plugins/target.py
This module contains its own chat-type detection.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import Message

from config import OWNER_ID
from storage import set_target, get_target, clear_target

logger = logging.getLogger(__name__)


def _normalized_chat_kind(chat: Any) -> str:
    """Normalize Pyrogram/Kurigram enum and string representations."""
    value = getattr(chat, "type", None)

    candidates = (
        getattr(value, "value", None),
        getattr(value, "name", None),
        value,
    )

    for candidate in candidates:
        if candidate is None:
            continue

        text = str(candidate).strip().lower()
        text = text.rsplit(".", 1)[-1]
        text = re.sub(r"[^a-z]", "", text)

        if "supergroup" in text:
            return "supergroup"
        if "channel" in text:
            return "channel"
        if text == "group" or text.endswith("group"):
            return "group"
        if "private" in text:
            return "private"
        if text == "bot" or text.endswith("bot"):
            return "bot"

    return "unknown"


def _is_group_or_supergroup(chat: Any) -> bool:
    """Identify Telegram groups and supergroups."""
    if chat is None:
        return False

    kind = _normalized_chat_kind(chat)

    if kind in {"group", "supergroup"}:
        return True

    if kind in {"channel", "private", "bot"}:
        return False

    try:
        return int(getattr(chat, "id", 0) or 0) < 0
    except (TypeError, ValueError):
        return False


def _command_args(message: Message) -> list[str]:
    command = getattr(message, "command", None) or []

    if not isinstance(command, (list, tuple)):
        return []

    return [
        str(item).strip()
        for item in command[1:]
        if str(item).strip()
    ]


async def _own_bot_username(client: Client) -> str | None:
    me = getattr(client, "me", None)
    username = getattr(me, "username", None) if me else None

    if not username:
        try:
            me = await client.get_me()
            username = getattr(me, "username", None)
        except Exception:
            logger.exception("Could not read bot username")

    return str(username).lstrip("@") if username else None


@Client.on_message(
    filters.command("settarget") & filters.user(OWNER_ID)
)
async def set_target_handler(client: Client, message: Message):
    args = _command_args(message)

    current_chat = getattr(message, "chat", None)
    current_is_group = _is_group_or_supergroup(current_chat)

    own_username = await _own_bot_username(client)

    # If the owner's command includes this bot's username
    # while inside the group, use the current group.
    if current_is_group and args and own_username:
        if args[0].lstrip("@").casefold() == own_username.casefold():
            args = args[1:]

    logger.info(
        "settarget request: chat_id=%r raw_chat_type=%r "
        "normalized_type=%s is_group=%s args=%r",
        getattr(current_chat, "id", None),
        getattr(current_chat, "type", None),
        _normalized_chat_kind(current_chat),
        current_is_group,
        args,
    )

    # OPTION 1: Select the current group.
    if current_is_group and not args:
        chat = current_chat
        thread_id = getattr(message, "message_thread_id", None)

        # General forum topic.
        if thread_id == 1:
            thread_id = None

    # OPTION 2: Select a group by ID or username.
    elif args:
        identifier = args[0]

        if identifier.lstrip("-").isdigit():
            identifier = int(identifier)

        elif (
            own_username
            and identifier.lstrip("@").casefold()
            == own_username.casefold()
        ):
            await message.reply_text(
                "❌ That is this bot's username, not a group username.\n\n"
                "Add this bot to the destination group and send "
                "/settarget@Wagur1_Restricted_Bot "
                "inside that group, without any extra argument."
            )
            return

        else:
            identifier = identifier.lstrip("@")

        try:
            chat = await client.get_chat(identifier)

        except Exception as exc:
            logger.warning(
                "Could not resolve destination %r: %s",
                identifier,
                exc,
            )

            await message.reply_text(
                "❌ I couldn't open that destination.\n\n"
                "For a private group, add this bot to the group "
                "and run /settarget@Wagur1_Restricted_Bot "
                "inside that group.\n\n"
                "For a public group, use its actual "
                "@GroupUsername in this private chat."
            )
            return

        thread_id = None

    # OPTION 3: No group or argument provided.
    else:
        await message.reply_text(
            "🎯 SET DESTINATION GROUP\n\n"
            "1. Add this bot to your destination group "
            "as a regular member.\n"
            "2. Open the group's General topic.\n"
            "3. Send /settarget@Wagur1_Restricted_Bot "
            "without an extra argument.\n\n"
            "Do not pass the bot's own username "
            "as the group username."
        )
        return

    # Validate the resolved destination.
    if not _is_group_or_supergroup(chat):
        raw_type = getattr(chat, "type", None)
        chat_id = getattr(chat, "id", None)

        logger.error(
            "Rejected destination: chat_id=%r "
            "raw_chat_type=%r normalized_type=%s",
            chat_id,
            raw_type,
            _normalized_chat_kind(chat),
        )

        await message.reply_text(
            "❌ Telegram did not identify this destination "
            "as a group.\n\n"
            f"Detected chat ID: {chat_id}\n"
            f"Detected chat type: {raw_type!s}\n\n"
            "Send /settarget@Wagur1_Restricted_Bot directly "
            "inside the group's General topic.\n\n"
            "If this happens again, send me the Railway log "
            "starting with 'settarget request'."
        )
        return

    chat_id = int(chat.id)

    title = (
        getattr(chat, "title", None)
        or getattr(chat, "first_name", None)
        or "Telegram group"
    )

    username = getattr(chat, "username", None)

    # Save destination.
    set_target(
        OWNER_ID,
        chat_id,
        thread_id,
        title,
        username,
    )

    logger.info(
        "Destination group saved: title=%r "
        "chat_id=%s thread_id=%r",
        title,
        chat_id,
        thread_id,
    )

    await message.reply_text(
        "✅ DESTINATION SAVED\n\n"
        f"Group: {title}\n"
        f"Chat ID: {chat_id}\n"
        + (
            f"Topic ID: {thread_id}\n"
            if thread_id else ""
        )
        + "\nThe bot can post as a regular member "
        "when group permissions allow members to send "
        "messages and media. Admin rights are not required."
    )


@Client.on_message(
    filters.command("target") & filters.user(OWNER_ID)
)
async def show_target_handler(client: Client, message: Message):

    target = get_target(OWNER_ID)

    if not target:
        await message.reply_text(
            "No destination is configured.\n\n"
            "Add the bot to your group and send "
            "/settarget@Wagur1_Restricted_Bot there."
        )
        return

    text = (
        "📍 SAVED DESTINATION\n\n"
        f"Group: {target.get('target_title') or 'Telegram group'}\n"
        f"Chat ID: {target['target_chat_id']}"
    )

    if target.get("target_username"):
        text += f"\nUsername: @{target['target_username']}"

    if target.get("target_thread_id"):
        text += f"\nTopic ID: {target['target_thread_id']}"

    await message.reply_text(text)


@Client.on_message(
    filters.command("cleartarget") & filters.user(OWNER_ID)
)
async def clear_target_handler(client: Client, message: Message):

    removed = clear_target(OWNER_ID)

    await message.reply_text(
        "✅ Destination removed."
        if removed
        else "There was no saved destination."
    )
