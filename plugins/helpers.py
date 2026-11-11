
import re
import random
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit, parse_qs, unquote

from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelInvalid, ChannelPrivate
from pyrogram.types import Message

from storage import get_target

logger = logging.getLogger(__name__)

# --- TRANSFER SAFETY SETTINGS ---
MIN_DELAY = 2.0
MAX_DELAY = 5.0
FLOOD_MULTIPLIER = 1.3
MAX_FLOODS_PER_DAY = 3


class SafetyGuard:
    def __init__(self):
        self.flood_count = 0
        self.flood_reset_time = datetime.now()
        self.circuit_open = False
        self.circuit_until = None

    def record_flood(self):
        if datetime.now() > self.flood_reset_time + timedelta(hours=24):
            self.flood_count = 0
            self.flood_reset_time = datetime.now()
        self.flood_count += 1
        if self.flood_count >= MAX_FLOODS_PER_DAY:
            self.circuit_open = True
            self.circuit_until = datetime.now() + timedelta(hours=6)
            logger.critical("Circuit breaker OPEN. Pausing sending for 6 hours.")

    def check_circuit(self):
        if self.circuit_open:
            if self.circuit_until and datetime.now() > self.circuit_until:
                self.circuit_open = False
                self.flood_count = 0
                self.circuit_until = None
                logger.info("Circuit breaker RESET.")
            else:
                return False
        return True


guard = SafetyGuard()


async def safe_delay():
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


async def safe_execute(func, *args, **kwargs):
    """Wrap Telegram API calls with FloodWait compliance and a circuit breaker."""
    if not guard.check_circuit():
        raise RuntimeError("Safety circuit is open. Please wait before sending again.")

    for attempt in range(2):
        try:
            result = await func(*args, **kwargs)
            await safe_delay()
            return result
        except FloodWait as e:
            wait_time = e.value * FLOOD_MULTIPLIER
            logger.warning("FloodWait: sleeping for %ss (attempt %s)", wait_time, attempt + 1)
            await asyncio.sleep(wait_time)
            guard.record_flood()
    raise RuntimeError("Telegram rate-limit retry failed. Try again later.")


@dataclass(frozen=True)
class TelegramMessageRef:
    chat_id: int | str
    message_id: int
    topic_id: int | None = None


def parse_telegram_message_link(link: str) -> TelegramMessageRef | None:
    """Parse public/private Telegram message links, including forum-topic links.

    Supported examples:
      https://t.me/channel_name/123
      https://t.me/channel_name/456/123       (topic 456, message 123)
      https://t.me/c/123456789/123
      https://t.me/c/123456789/456/123        (topic 456, message 123)
      https://t.me/c/123456789/123?thread=456
    """
    if not link:
        return None

    candidate = str(link).strip().strip("<>[](){}.,;\"'")
    if not candidate:
        return None
    if not re.match(r"^https?://", candidate, re.IGNORECASE):
        candidate = "https://" + candidate

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog"}:
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if not parts:
        return None

    is_private_link = parts[0].lower() == "c"
    if is_private_link:
        if len(parts) < 3 or not parts[1].isdigit():
            return None
        internal_id = parts[1]
        chat_id: int | str = int(f"-100{internal_id}")
        tail = parts[2:]
    else:
        # The /s/ path is the public-preview form of a channel link.
        if parts[0].lower() == "s":
            parts = parts[1:]
        if len(parts) < 2:
            return None
        username = parts[0].lstrip("@")
        if not username or username.startswith("+") or username.lower() in {"joinchat", "addstickers"}:
            return None
        chat_id = username
        tail = parts[1:]

    if not tail or not tail[-1].isdigit():
        return None

    message_id = int(tail[-1])
    if message_id <= 0:
        return None

    topic_id = None
    query = parse_qs(parsed.query)

    # Topic links normally have /<topic_id>/<message_id>; Telegram also
    # occasionally shares a message URL with ?thread=<topic_id>.
    if len(tail) >= 2 and tail[-2].isdigit():
        topic_id = int(tail[-2])
    for key in ("thread", "topic", "topic_id", "thread_id"):
        values = query.get(key)
        if values and str(values[0]).isdigit():
            topic_id = int(values[0])
            break

    return TelegramMessageRef(chat_id=chat_id, message_id=message_id, topic_id=topic_id)


def extract_chat_and_msg_id(link: str):
    """Backward-compatible two-value parser used by older plugin code."""
    parsed = parse_telegram_message_link(link)
    if not parsed:
        return None, None
    return parsed.chat_id, parsed.message_id


def get_message_topic_id(message) -> int | None:
    """Return the Telegram forum topic root ID for a message when available."""
    if message is None:
        return None

    for attribute in ("message_thread_id", "reply_to_top_message_id", "topic_id", "top_msg_id"):
        value = getattr(message, attribute, None)
        try:
            if value is not None and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            continue

    # Some client versions expose the MTProto reply header instead of the
    # flattened reply_to_top_message_id convenience property.
    reply_header = getattr(message, "reply_to", None)
    if reply_header is not None:
        for attribute in ("reply_to_top_id", "top_msg_id", "reply_to_top_message_id"):
            value = getattr(reply_header, attribute, None)
            try:
                if value is not None and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue

    topic = getattr(message, "topic", None)
    topic_id = getattr(topic, "id", None) if topic else None
    try:
        if topic_id is not None and int(topic_id) > 0:
            return int(topic_id)
    except (TypeError, ValueError):
        pass

    # The topic creation service message is itself the topic root.
    if getattr(message, "forum_topic_created", None) is not None:
        try:
            return int(message.id)
        except (TypeError, ValueError, AttributeError):
            pass

    # Do not infer General merely from is_topic_message: that boolean is true
    # for named topics too. The caller may classify marker-free messages as
    # General, but named topics must always match an actual root/thread ID.
    return None


def message_belongs_to_topic(message, topic_id: int | None) -> bool:
    """Fail closed for named topics so messages from adjacent topics never leak in."""
    if topic_id is None:
        return True

    try:
        if int(getattr(message, "id", 0) or 0) == int(topic_id):
            return True  # topic-root service message
    except (TypeError, ValueError):
        pass

    actual_topic = get_message_topic_id(message)
    if actual_topic is not None:
        return int(actual_topic) == int(topic_id)

    # Telegram's General topic is topic ID 1. In a forum, messages without a
    # top-message marker are General-topic messages; named topics have a root ID.
    return int(topic_id) == 1


def _chat_kind(chat):
    value = getattr(chat, "type", None)
    candidates = (
        getattr(value, "value", None),
        getattr(value, "name", None),
        value,
    )
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).lower().split(".")[-1]
        text = re.sub(r"[^a-z]", "", text)
        if "supergroup" in text:
            return "supergroup"
        if text == "group" or text.endswith("group"):
            return "group"
        if "channel" in text:
            return "channel"
        if "private" in text:
            return "private"
    return "unknown"


def is_group_chat(chat) -> bool:
    kind = _chat_kind(chat)
    if kind in {"group", "supergroup"}:
        return True
    # Compatibility for test doubles and old Message objects with no .type.
    return getattr(chat, "type", None) is None and int(getattr(chat, "id", 0) or 0) < 0


async def get_destination(message: Message, owner_id: int):
    """Use the current group when invoked there; otherwise use the saved group."""
    chat = getattr(message, "chat", None)
    if chat and is_group_chat(chat):
        thread_id = getattr(message, "message_thread_id", None)
        return chat.id, (None if thread_id == 1 else thread_id)

    saved = get_target(owner_id)
    if not saved:
        raise LookupError(
            "No destination group is set yet. Add the bot to your group as a regular member, "
            "then run /settarget@YourBotUsername in that group."
        )
    return saved["target_chat_id"], saved.get("target_thread_id")


# --- PEER CACHE HELPERS ---
# Railway can restart with an empty peer cache. Resolve chats lazily.
_resolved_peers = set()


async def ensure_peer(client, chat_id, scan_limit: int = 300, timeout: int = 60) -> bool:
    """Ensure the client can resolve a chat, with a bounded dialog-scan fallback."""
    cache_key = (id(client), chat_id)
    if isinstance(chat_id, int) and cache_key in _resolved_peers:
        return True
    try:
        await client.get_chat(chat_id)
        if isinstance(chat_id, int):
            _resolved_peers.add(cache_key)
        return True
    except (KeyError, ValueError, PeerIdInvalid, ChannelInvalid, ChannelPrivate):
        pass

    if not isinstance(chat_id, int):
        return False

    async def _scan():
        async for dialog in client.get_dialogs(limit=scan_limit):
            if dialog.chat.id == chat_id:
                return True
        return False

    try:
        if await asyncio.wait_for(_scan(), timeout=timeout):
            _resolved_peers.add(cache_key)
            return True
    except asyncio.TimeoutError:
        logger.warning("Dialog scan timed out while looking for %s", chat_id)
    except FloodWait as e:
        logger.warning("FloodWait %ss during dialog scan", e.value)
    return False


async def prepare_chats(reader, source_chat, target_chat, sender=None):
    """Check source access for the reader and destination access for the sender."""
    sender = sender or reader
    if not await ensure_peer(reader, source_chat):
        return (
            "The signed-in source account can't access the source chat. "
            "Join that source chat with the account used for SESSION_STRING."
        )
    if not await ensure_peer(sender, target_chat):
        return (
            "The bot account can't access the destination group. Add the bot to that group "
            "as a regular member; administrator privileges are not required."
        )
    return None
