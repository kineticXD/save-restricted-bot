"""
Copy a message with copy_message (fast, server-side) and fall back to
download + re-upload when that isn't possible (protected / "restricted" chats,
expired file references, ...).
"""
import os
import re
import time
import shutil
import asyncio
import logging
import tempfile
from typing import Awaitable, Callable, Optional

from pyrogram import enums, errors, types
from pyrogram.errors import FloodWait

from config import FORCE_REUPLOAD, MAX_FILE_MB
from plugins.helpers import safe_execute

logger = logging.getLogger(__name__)

MB = 1024 * 1024
THUMB_MAX_BYTES = 200 * 1024
CAPTION_LIMIT_FREE = 1024
CAPTION_LIMIT_PREMIUM = 4096
FILE_LIMIT_FREE_MB = 2000
FILE_LIMIT_PREMIUM_MB = 4000

StageCallback = Optional[Callable[[str], Awaitable[None]]]

# Errors from copy_message that mean "copying isn't possible, but re-uploading might be".
# Anything else (target chat forbids you, slowmode, flood, ...) is NOT retried via
# download/upload because it would fail the same way after a wasted download.
FALLBACK_ERRORS = (
    errors.ChatForwardsRestricted,
    errors.FileReferenceExpired,
    errors.FileReferenceEmpty,
    errors.FileReferenceInvalid,
    errors.MediaEmpty,
    errors.MediaInvalid,
)

# One download/upload at a time: protects disk, RAM and the account from bursts.
_transfer_lock = asyncio.Lock()
_premium_cache: dict = {}


class UnsupportedMedia(Exception):
    """This message can't be copied (shown to the user as a skip, not a crash)."""


# media type -> (message attribute, send method, name of the file parameter,
#                supports caption, supports thumb)
_MEDIA_SPECS = {
    enums.MessageMediaType.PHOTO: ("photo", "send_photo", "photo", True, False),
    enums.MessageMediaType.VIDEO: ("video", "send_video", "video", True, True),
    enums.MessageMediaType.DOCUMENT: ("document", "send_document", "document", True, True),
    enums.MessageMediaType.AUDIO: ("audio", "send_audio", "audio", True, True),
    enums.MessageMediaType.VOICE: ("voice", "send_voice", "voice", True, False),
    enums.MessageMediaType.ANIMATION: ("animation", "send_animation", "animation", True, True),
    enums.MessageMediaType.STICKER: ("sticker", "send_sticker", "sticker", False, False),
    enums.MessageMediaType.VIDEO_NOTE: ("video_note", "send_video_note", "video_note", False, True),
}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def is_protected(msg) -> bool:
    """True if the message or its chat has 'restrict saving content' enabled."""
    if getattr(msg, "has_protected_content", None):
        return True
    chat = getattr(msg, "chat", None)
    return bool(getattr(chat, "has_protected_content", None))


def _fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _safe_local_name(name, max_bytes: int = 120) -> str:
    """A filesystem-safe local file name (the original name is still sent to Telegram)."""
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .")
    if not name:
        return ""
    stem, ext = os.path.splitext(name)
    ext = ext[:16]
    while stem and len((stem + ext).encode("utf-8")) > max_bytes:
        stem = stem[:-1]
    return (stem + ext) if stem else ""


def _pick_thumb(media):
    """Largest thumbnail that is <= 320px wide (Telegram's thumb limit), else the smallest."""
    thumbs = [t for t in (getattr(media, "thumbs", None) or []) if getattr(t, "file_id", None)]
    if not thumbs:
        return None
    small = [t for t in thumbs if (getattr(t, "width", 0) or 0) <= 320]
    if small:
        return max(small, key=lambda t: getattr(t, "width", 0) or 0)
    return min(thumbs, key=lambda t: getattr(t, "width", 0) or 0)


async def _is_premium(user) -> bool:
    key = id(user)
    if key not in _premium_cache:
        try:
            _premium_cache[key] = bool((await user.get_me()).is_premium)
        except Exception:  # noqa: BLE001
            _premium_cache[key] = False
    return _premium_cache[key]


async def _stage(on_stage: StageCallback, text: str):
    """Best-effort status update; never lets a failed edit break a transfer."""
    if on_stage:
        try:
            await on_stage(text)
        except Exception:  # noqa: BLE001
            pass


def _progress_cb(on_stage: StageCallback, label: str):
    """Throttled progress callback (one status edit every >= 8 s)."""
    if not on_stage:
        return None
    last = {"t": 0.0}

    async def cb(current, total):
        now = time.monotonic()
        if current != total and now - last["t"] < 8:
            return
        last["t"] = now
        pct = f" {current * 100 // total}%" if total else ""
        await _stage(on_stage, f"{label}{pct} ({_fmt_size(current)} / {_fmt_size(total)})")

    return cb


async def _download(user, target, dest: str, progress=None, expected_size: int = 0) -> str:
    """download_media with FloodWait retry + integrity checks. Returns the file path."""
    path = None
    for attempt in range(2):
        try:
            path = await user.download_media(target, file_name=dest, progress=progress)
            break
        except FloodWait as e:
            if attempt == 1 or e.value > 300:
                raise
            logger.warning("FloodWait %ss during download", e.value)
            await asyncio.sleep(e.value + 1)

    if isinstance(path, list):
        path = path[0] if path else None
    if not path or not os.path.isfile(path):
        raise RuntimeError("Download failed: no file was produced.")

    size = os.path.getsize(path)
    if size == 0:
        raise RuntimeError("Download failed: file is empty.")
    if expected_size and size != expected_size:
        raise RuntimeError(
            f"Download incomplete: got {_fmt_size(size)}, expected {_fmt_size(expected_size)}."
        )
    return path


async def _download_thumb(user, media, tmpdir: str) -> Optional[str]:
    """Best-effort thumbnail download; returns None if anything goes wrong."""
    thumb = _pick_thumb(media)
    if thumb is None:
        return None
    try:
        path = await _download(user, thumb, os.path.join(tmpdir, "thumb.jpg"))
        if os.path.getsize(path) > THUMB_MAX_BYTES:
            return None
        return path
    except Exception as e:  # noqa: BLE001
        logger.debug("Thumbnail skipped: %s", e)
        return None


# ----------------------------------------------------------------------------
# re-upload
# ----------------------------------------------------------------------------
async def _send_text(sender, msg, target_chat, thread_id):
    await safe_execute(
        sender.send_message,
        chat_id=target_chat,
        text=msg.text,
        entities=getattr(msg, "entities", None),
        parse_mode=enums.ParseMode.DISABLED,
        link_preview_options=types.LinkPreviewOptions(is_disabled=not getattr(msg, "web_page", None)),
        message_thread_id=thread_id,
    )


async def _send_simple_media(sender, msg, media_type, target_chat, thread_id):
    """Contact / location / venue: nothing to download."""
    if media_type == enums.MessageMediaType.CONTACT:
        c = msg.contact
        kw = dict(
            chat_id=target_chat,
            phone_number=c.phone_number,
            first_name=c.first_name or c.phone_number,
            message_thread_id=thread_id,
        )
        if c.last_name:
            kw["last_name"] = c.last_name
        if c.vcard:
            kw["vcard"] = c.vcard
        await safe_execute(sender.send_contact, **kw)
    elif media_type == enums.MessageMediaType.LOCATION:
        loc = msg.location
        await safe_execute(
            sender.send_location,
            chat_id=target_chat,
            latitude=loc.latitude,
            longitude=loc.longitude,
            message_thread_id=thread_id,
        )
    else:  # VENUE
        v = msg.venue
        kw = dict(
            chat_id=target_chat,
            latitude=v.location.latitude,
            longitude=v.location.longitude,
            title=v.title,
            address=v.address,
            message_thread_id=thread_id,
        )
        if v.foursquare_id:
            kw["foursquare_id"] = v.foursquare_id
        if v.foursquare_type:
            kw["foursquare_type"] = v.foursquare_type
        await safe_execute(sender.send_venue, **kw)


async def _reupload_file(reader, sender, msg, media_type, target_chat, thread_id, on_stage):
    attr, sender_name, param, has_caption, has_thumb = _MEDIA_SPECS[media_type]
    media = getattr(msg, attr)
    size = int(getattr(media, "file_size", 0) or 0)
    kind = media_type.name.lower().replace("_", " ")

    premium = await _is_premium(sender)

    # ---- size / disk guards (fail early, before downloading anything) ----
    limit_mb = FILE_LIMIT_PREMIUM_MB if premium else FILE_LIMIT_FREE_MB
    if MAX_FILE_MB:
        limit_mb = min(limit_mb, MAX_FILE_MB)
    if size and size > limit_mb * MB:
        raise UnsupportedMedia(f"{kind} is {_fmt_size(size)}; limit is {limit_mb} MB.")

    tmpdir = tempfile.mkdtemp(prefix="save_")
    try:
        free = shutil.disk_usage(tmpdir).free
        if size and free < size * 1.05 + 50 * MB:
            raise UnsupportedMedia(
                f"Not enough disk space for a {_fmt_size(size)} {kind} ({_fmt_size(free)} free)."
            )

        original_name = getattr(media, "file_name", None)
        local_name = _safe_local_name(original_name)
        dest = os.path.join(tmpdir, local_name) if local_name else tmpdir + os.sep

        async with _transfer_lock:
            await _stage(on_stage, f"⬇️ Downloading {kind} ({_fmt_size(size)})...")
            path = await _download(
                reader, msg, dest, progress=_progress_cb(on_stage, f"⬇️ Downloading {kind}"), expected_size=size
            )
            thumb_path = await _download_thumb(reader, media, tmpdir) if has_thumb else None

            # ---- build send arguments ----
            kw = {"chat_id": target_chat, param: path, "message_thread_id": thread_id}

            caption = getattr(msg, "caption", None) or None
            extra_caption_text = None
            if has_caption and caption:
                limit = CAPTION_LIMIT_PREMIUM if premium else CAPTION_LIMIT_FREE
                if len(caption) <= limit:
                    kw.update(
                        caption=caption,
                        parse_mode=enums.ParseMode.DISABLED,
                        caption_entities=getattr(msg, "caption_entities", None),
                    )
                else:
                    extra_caption_text = caption  # sent as a separate text message afterwards

            if has_caption and getattr(msg, "has_media_spoiler", None) and media_type in (
                enums.MessageMediaType.PHOTO, enums.MessageMediaType.VIDEO, enums.MessageMediaType.ANIMATION
            ):
                kw["has_spoiler"] = True
            if has_caption and caption and extra_caption_text is None and getattr(
                msg, "show_caption_above_media", None
            ) and media_type in (
                enums.MessageMediaType.PHOTO, enums.MessageMediaType.VIDEO, enums.MessageMediaType.ANIMATION
            ):
                kw["show_caption_above_media"] = True

            if thumb_path:
                kw["thumb"] = thumb_path

            if media_type == enums.MessageMediaType.VIDEO:
                kw.update(duration=media.duration or 0, width=media.width or 0, height=media.height or 0,
                          supports_streaming=True)
                if original_name:
                    kw["file_name"] = original_name
            elif media_type == enums.MessageMediaType.ANIMATION:
                kw.update(duration=media.duration or 0, width=media.width or 0, height=media.height or 0)
                if original_name:
                    kw["file_name"] = original_name
            elif media_type == enums.MessageMediaType.DOCUMENT:
                kw["force_document"] = True
                if original_name:
                    kw["file_name"] = original_name
            elif media_type == enums.MessageMediaType.AUDIO:
                kw.update(duration=media.duration or 0)
                if media.performer:
                    kw["performer"] = media.performer
                if media.title:
                    kw["title"] = media.title
                if original_name:
                    kw["file_name"] = original_name
            elif media_type == enums.MessageMediaType.VOICE:
                kw["duration"] = media.duration or 0
                if getattr(media, "waveform", None):
                    kw["waveform"] = media.waveform
            elif media_type == enums.MessageMediaType.VIDEO_NOTE:
                kw.update(duration=media.duration or 0, length=media.length or 240)
            elif media_type == enums.MessageMediaType.STICKER:
                if getattr(media, "emoji", None):
                    kw["emoji"] = media.emoji

            progress = _progress_cb(on_stage, f"⬆️ Uploading {kind}")
            if progress:
                kw["progress"] = progress

            await _stage(on_stage, f"⬆️ Uploading {kind} ({_fmt_size(size)})...")
            await safe_execute(getattr(sender, sender_name), **kw)

            if extra_caption_text:
                await safe_execute(
                    sender.send_message,
                    chat_id=target_chat,
                    text=extra_caption_text,
                    entities=getattr(msg, "caption_entities", None),
                    parse_mode=enums.ParseMode.DISABLED,
                    message_thread_id=thread_id,
                )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def reupload(user, msg, target_chat, thread_id=None, on_stage: StageCallback = None, sender=None):
    """Re-create a message using `user` to read it and `sender` to post it.

    `sender` defaults to `user` to preserve the original user-session behaviour.
    """
    sender = sender or user
    if getattr(msg, "empty", None) or getattr(msg, "service", None):
        raise UnsupportedMedia("Empty or service message.")

    media_type = getattr(msg, "media", None)

    # Plain text (also messages whose only "media" is a link preview)
    if getattr(msg, "text", None) and media_type in (None, enums.MessageMediaType.WEB_PAGE):
        await _send_text(sender, msg, target_chat, thread_id)
        return

    if media_type in _MEDIA_SPECS:
        await _reupload_file(user, sender, msg, media_type, target_chat, thread_id, on_stage)
        return

    if media_type in (
        enums.MessageMediaType.CONTACT, enums.MessageMediaType.LOCATION, enums.MessageMediaType.VENUE
    ):
        await _send_simple_media(sender, msg, media_type, target_chat, thread_id)
        return

    name = media_type.name.lower().replace("_", " ") if media_type else "unknown"
    raise UnsupportedMedia(f"Can't copy '{name}' messages from a restricted chat.")


# ----------------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------------
async def copy_or_reupload(
    user, source_chat, source_msg, target_chat, thread_id=None,
    on_stage: StageCallback = None, sender=None,
) -> str:
    """Copy/re-upload a source message to a destination.

    `user` is the account that can read the source. `sender` is the account that
    posts to the destination. When both clients differ (the normal bot setup),
    the reader downloads the media and the bot account uploads it. Telegram
    group permissions still apply to that bot account.
    """
    sender = sender or user

    # Server-side copy only works through one Telegram account that can access
    # both chats. With separate reader/sender clients, use a controlled
    # download-and-upload transfer so the destination post is authored by sender.
    if sender is user and not (FORCE_REUPLOAD or is_protected(source_msg)):
        try:
            await safe_execute(
                user.copy_message,
                chat_id=target_chat,
                from_chat_id=source_chat,
                message_id=source_msg.id,
                message_thread_id=thread_id,
            )
            return "copied"
        except FALLBACK_ERRORS as e:
            logger.info("copy_message not possible (%s) - falling back to download/upload", type(e).__name__)

    await reupload(user, source_msg, target_chat, thread_id, on_stage, sender=sender)
    return "reuploaded"
