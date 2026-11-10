import re
import random
import asyncio
import logging
from datetime import datetime, timedelta
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
    """Wrap API calls with FloodWait compliance and a circuit breaker."""
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


def extract_chat_and_msg_id(link: str):
    """Parse common t.me links, including private, public and topic links."""
    if not link:
        return None, None
    pattern = r"(?:https?://)?(?:www\.)?t\.me/(?:c/)?([^/?#]+)/(?:(\d+)/)?(\d+)"
    match = re.search(pattern, link.strip())
    if not match:
        return None, None

    chat_identifier = match.group(1)
    msg_id = int(match.group(3))
    if chat_identifier.isdigit():
        chat_identifier = int(f"-100{chat_identifier}")
    elif chat_identifier.startswith("+"):
        return None, None  # invite links do not identify an individual message
    return chat_identifier, msg_id


def _chat_kind(chat):
    """Return a normalized chat type across Pyrogram/Kurigram enum variants."""
    value = getattr(chat, "type", None)
    candidates = (
        getattr(value, "value", None),
        getattr(value, "name", None),
        value,
    )
    for candidate in candidates:
        if candidate is None:
            continue
        kind = str(candidate).strip().lower()
        if not kind:
            continue
        kind = kind.rsplit(".", 1)[-1]
        kind = re.sub(r"[^a-z]", "", kind)
        if kind in {"private", "bot", "group", "supergroup", "channel"}:
            return kind
    return ""


def is_group_chat(chat) -> bool:
    """Recognize basic groups and supergroups, including enum/version variants.

    Telegram group chat IDs are negative. Use that as a fallback only when the
    library returns an unknown/missing type; never mistake a known channel for
    a group.
    """
    if chat is None:
        return False

    kind = _chat_kind(chat)
    if kind in {"group", "supergroup"}:
        return True
    if kind in {"private", "bot", "channel"}:
        return False

    try:
        return int(getattr(chat, "id", 0) or 0) < 0
    except (TypeError, ValueError):
        return False


async def get_destination(message: Message, owner_id: int):
    """Use the current group when invoked there; otherwise the saved group."""
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
