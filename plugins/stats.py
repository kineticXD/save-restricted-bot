from datetime import timedelta

from pyrogram import Client, filters, enums
from pyrogram.types import Message

from config import OWNER_ID
from plugins.helpers import guard
from plugins.save import active_jobs, pause_events
from plugins.transfer import _fmt_size
from storage import get_batch_checkpoint, get_target, get_transfer_stats


@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_handler(client: Client, message: Message):
    status = "🔴 PAUSED (Safety circuit open)" if guard.circuit_open else "🟢 READY"
    target = get_target(OWNER_ID)
    target_label = target["target_title"] if target else "Not configured"
    reset = guard.flood_reset_time + timedelta(hours=24)

    owner_key = int(OWNER_ID)
    if owner_key in active_jobs:
        pe = pause_events.get(owner_key)
        batch_state = "Paused (use /resume)" if (pe and not pe.is_set()) else "Running"
    else:
        batch_state = "Idle"

    cp = get_batch_checkpoint(OWNER_ID)
    cp_label = (
        f"{cp['status']} ({cp['success_count']}/{cp['total_count']}, last ID {cp['last_message_id']})"
        if cp else "None"
    )
    tstats = get_transfer_stats(OWNER_ID)
    text = (
        "📊 **WAGURI BOT STATUS**\n\n"
        f"**Transfer state:** `{status}`\n"
        f"**Flood waits (24h):** `{guard.flood_count}`\n"
        f"**Next counter reset:** `{reset:%Y-%m-%d %H:%M}`\n"
        f"**Destination:** `{target_label}`\n"
        f"**Active batch:** `{batch_state}`\n"
        f"**Saved checkpoint:** `{cp_label}`\n\n"
        "📈 **LIFETIME METRICS**\n"
        f"**Files uploaded:** `{tstats['total_files']:,}`\n"
        f"**Data transferred:** `{_fmt_size(tstats['total_bytes'])}`\n"
        f"**Batches finished:** `{tstats['total_batches']:,}`\n"
        f"**Failed/skipped items:** `{tstats['failed_items']:,}`"
    )
    await message.reply_text(text, parse_mode=enums.ParseMode.MARKDOWN)


@Client.on_message(filters.command("resetguard") & filters.user(OWNER_ID))
async def reset_guard_handler(client: Client, message: Message):
    guard.reset()
    await message.reply_text("🟢 Safety circuit reset. Transfers are ready.")
