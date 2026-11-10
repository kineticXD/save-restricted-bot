import re
import random
import asyncio
import logging
from datetime import datetime, timedelta
from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelInvalid, ChannelPrivate
from pyrogram.types import Message

logger = logging.getLogger(__name__)

# --- SAFETY SETTINGS ---
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
            if datetime.now() > self.circuit_until:
                self.circuit_open = False
                self.flood_count = 0
                logger.info("Circuit breaker RESET.")
            else:
                return False
        return True

guard = SafetyGuard()

async def safe_delay():
    await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

async def safe_execute(func, *args, **kwargs):
    """Wraps API calls with FloodWait compliance and circuit breaker."""
    if not guard.check_circuit():
        raise Exception("Safety circuit is open. Please wait.")

    for attempt in range(2):
        try:
            result = await func(*args, **kwargs)
            await safe_delay()
            return result
        except FloodWait as e:
            wait_time = e.value * FLOOD_MULTIPLIER
            logger.warning(f"FloodWait: sleeping for {wait_time}s (attempt {attempt+1})")
            await asyncio.sleep(wait_time)
            guard.record_flood()
        except Exception as e:
            logger.error(f"API Error: {e}")
            raise e
    raise Exception("FloodWait retry failed.")

def extract_chat_and_msg_id(link: str):
    """Parse a t.me link to get chat ID and message ID (handles Topic links)."""
    # This regex handles: t.me/c/123456789/50 AND t.me/c/123456789/123/50
    pattern = r'(?:https?://)?t\.me/(?:c/)?([^/]+)/(?:(\d+)/)?(\d+)'
    
    match = re.search(pattern, link)
    if match:
        chat_identifier = match.group(1)
        msg_id = int(match.group(3)) # The last number is always the message ID
        
        if chat_identifier.isdigit():
            chat_identifier = int(f"-100{chat_identifier}")
            
        return chat_identifier, msg_id
    return None, None

async def get_target_topic(message: Message):
    """Determine the target chat and topic ID from the command message."""
    thread_id = getattr(message, 'message_thread_id', None)
    if thread_id == 1:
        thread_id = None
    return message.chat.id, thread_id


# --- PEER CACHE HELPERS ---
# Railway wipes local state on every restart, so the userbot starts with an empty
# peer cache. Instead of syncing every dialog at startup (slow, can hang), resolve
# each chat lazily the first time it is needed.
_resolved_peers = set()


async def ensure_peer(client, chat_id, scan_limit: int = 300, timeout: int = 60) -> bool:
    """Make sure `client` can resolve `chat_id`. Returns True on success.

    1. Cheap path: get_chat() (cache hit, or ResolveUsername / GetChannels).
    2. Fallback: scan the first `scan_limit` dialogs (bounded by `timeout`).
       Every API response that carries users/chats populates the peer cache,
       so one scan fills it.
    """
    if isinstance(chat_id, int) and chat_id in _resolved_peers:
        return True

    try:
        await client.get_chat(chat_id)
        if isinstance(chat_id, int):
            _resolved_peers.add(chat_id)
        return True
    except (KeyError, ValueError, PeerIdInvalid, ChannelInvalid, ChannelPrivate):
        pass

    if not isinstance(chat_id, int):
        return False  # usernames are resolved server-side; nothing more to try

    async def _scan():
        async for dialog in client.get_dialogs(limit=scan_limit):
            if dialog.chat.id == chat_id:
                return True
        return False

    try:
        if await asyncio.wait_for(_scan(), timeout=timeout):
            _resolved_peers.add(chat_id)
            return True
    except asyncio.TimeoutError:
        logger.warning("Dialog scan timed out while looking for %s", chat_id)
    except FloodWait as e:
        logger.warning("FloodWait %ss during dialog scan", e.value)
    return False


async def prepare_chats(user, source_chat, target_chat):
    """Return None if both chats are usable by the userbot, else a user-facing error."""
    if not await ensure_peer(user, source_chat):
        return ("Userbot can't access the **source** chat. "
                "Make sure your account has joined it.")
    if not await ensure_peer(user, target_chat):
        return ("Userbot can't access **this** chat. "
                "Add your user account to this group first.")
    return None
