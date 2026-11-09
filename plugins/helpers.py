import re
import random
import asyncio
import logging
from datetime import datetime, timedelta
from pyrogram.errors import FloodWait
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
    """Parse a t.me link to get chat ID and message ID."""
    patterns = [
        r'(?:https?://)?t\.me/(?:c/)?([^/]+)/(\d+)',
        r'(?:https?://)?t\.me/([^/]+)/(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, link)
        if match:
            chat_identifier = match.group(1)
            msg_id = int(match.group(2))
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
