
"""Reliable Telegram message copy/re-upload helpers.

The source reader and destination poster may be different clients. In that setup
media is downloaded by the reader and uploaded by the bot, in original message
order and into the selected destination forum topic.
"""

import asyncio
import inspect
import logging
import os
import re
import shutil
import tempfile
import time
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

FALLBACK_ERRORS = tuple(
    error for error in (
        getattr(errors, "ChatForwardsRestricted", None),
        getattr(errors, "FileReferenceExpired", None),
        getattr(errors, "FileReferenceEmpty", None),
        getattr(errors, "FileReferenceInvalid", None),
        getattr(errors, "MediaEmpty", None),
        getattr(errors, "MediaInvalid", None),
    ) if isinstance(error, type)
)


def _is_file_reference_error(exc: Exception) -> bool:
    """Handle FILE_REFERENCE_* errors even when a client build lacks a class mapping."""
    name = type(exc).__name__.replace("_", "").lower()
    text = str(exc).upper()
    return (
        "filereference" in name
        or "FILE_REFERENCE_EXPIRED" in text
        or "FILE_REFERENCE_EMPTY" in text
        or "FILE_REFERENCE_INVALID" in text
    )


def _is_copy_fallback_error(exc: Exception) -> bool:
    if FALLBACK_ERRORS and isinstance(exc, FALLBACK_ERRORS):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).upper()
    return (
        _is_file_reference_error(exc)
        or "chatforwardsrestricted" in name
        or "CHAT_FORWARDS_RESTRICTED" in text
        or "MEDIA_EMPTY" in text
        or "MEDIA_INVALID" in text
    )


async def _refresh_source_message(reader, source_chat, message_id):
    """Fetch a message again so Telegram supplies a current media file_reference."""
    if source_chat is None or message_id is None:
        return None
    try:
        refreshed = await reader.get_messages(source_chat, int(message_id))
    except TypeError:
        try:
            refreshed = await reader.get_messages(source_chat, message_ids=[int(message_id)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not refresh source message %s: %s", message_id, exc)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not refresh source message %s: %s", message_id, exc)
        return None

    if isinstance(refreshed, (list, tuple)):
        refreshed = refreshed[0] if refreshed else None
    if refreshed is None or getattr(refreshed, "empty", False):
        return None
    return refreshed


# Keep only one large transfer active at a time to protect Railway disk/RAM.
_transfer_lock = asyncio.Lock()
_premium_cache: dict[int, bool] = {}


class UnsupportedMedia(Exception):
    """A Telegram message type cannot be recreated by the available send API."""


# media type -> (message attribute, sender method, file argument, caption, thumbnail)
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


async def _send_result(sender, method_name: str, target_chat, preferences=None, **kwargs):
    """Send once and optionally pin the returned message without failing the transfer."""
    result = await safe_execute(getattr(sender, method_name), **kwargs)
    if preferences and preferences.get("auto_pin"):
        messages = result if isinstance(result, (list, tuple)) else [result]
        first = next((item for item in messages if getattr(item, "id", None)), None)
        if first is not None and hasattr(sender, "pin_chat_message"):
            try:
                await safe_execute(
                    sender.pin_chat_message,
                    chat_id=target_chat,
                    message_id=first.id,
                    disable_notification=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Upload succeeded but pinning message %s failed: %s", first.id, exc)
    return result


def _replace_words(value: str | None, preferences=None) -> str | None:
    if value is None:
        return None
    result = str(value)
    for rule in (preferences or {}).get("replace_rules", []) or []:
        if isinstance(rule, dict):
            old, new = rule.get("old", ""), rule.get("new", "")
        elif isinstance(rule, (list, tuple)) and len(rule) >= 2:
            old, new = rule[0], rule[1]
        else:
            continue
        if old:
            result = result.replace(str(old), str(new))
    return result


def _choose_filename(original_name: str | None, custom_filename: str | None, preferences=None) -> str | None:
    original_name = _replace_words(original_name, preferences)
    if custom_filename:
        chosen = _safe_local_name(_replace_words(custom_filename, preferences))
        if not chosen:
            raise UnsupportedMedia("The custom filename is empty or contains only invalid characters.")
        if not os.path.splitext(chosen)[1] and original_name:
            chosen += os.path.splitext(original_name)[1]
        return chosen
    return _safe_local_name(original_name) or None


def is_protected(msg) -> bool:
    """True when Telegram marks the message or source chat as protected."""
    if getattr(msg, "has_protected_content", None):
        return True
    chat = getattr(msg, "chat", None)
    return bool(getattr(chat, "has_protected_content", None))


def _fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _safe_local_name(name, max_bytes: int = 120) -> str:
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
    thumbs = [
        t for t in (getattr(media, "thumbs", None) or [])
        if getattr(t, "file_id", None)
    ]
    if not thumbs:
        return None
    small = [t for t in thumbs if (getattr(t, "width", 0) or 0) <= 320]
    if small:
        return max(small, key=lambda t: getattr(t, "width", 0) or 0)
    return min(thumbs, key=lambda t: getattr(t, "width", 0) or 0)


async def _is_premium(client) -> bool:
    key = id(client)
    if key not in _premium_cache:
        try:
            me = await client.get_me()
            _premium_cache[key] = bool(getattr(me, "is_premium", False))
        except Exception:  # noqa: BLE001
            _premium_cache[key] = False
    return _premium_cache[key]


async def _stage(on_stage: StageCallback, text: str):
    """Progress edits are best-effort and must never abort a media transfer."""
    if not on_stage:
        return
    try:
        await on_stage(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Progress/status update skipped: %s", exc)


def _progress_cb(on_stage: StageCallback, label: str, interval: float = 2.5):
    """Create a throttled, concurrency-safe progress callback with a stable bar."""
    if not on_stage:
        return None

    state = {"last": 0.0, "pct": -1}
    lock = asyncio.Lock()

    async def callback(current, total, *_extra):
        try:
            current = max(0, int(current or 0))
            total = max(0, int(total or 0))
        except (TypeError, ValueError):
            return

        if total <= 0:
            return

        current = min(current, total)
        percent = min(100, int(current * 100 / total))
        now = time.monotonic()
        finished = current >= total

        if not finished and percent == state["pct"] and now - state["last"] < interval:
            return
        if not finished and now - state["last"] < interval:
            return

        async with lock:
            now = time.monotonic()
            if not finished and now - state["last"] < interval:
                return

            filled = max(0, min(10, percent // 10))
            bar = "█" * filled + "░" * (10 - filled)
            state["last"] = now
            state["pct"] = percent

            await _stage(
                on_stage,
                f"{label}\n[{bar}] {percent}%\n{_fmt_size(current)} / {_fmt_size(total)}",
            )

    return callback


def _supports_parameter(func, name: str) -> bool:
    """Check a method signature before passing optional kwargs."""
    try:
        signature = inspect.signature(func)
        return name in signature.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
    except (TypeError, ValueError):
        # Kurigram's public upload methods support progress; use it if the
        # extension hides its Python signature.
        return True


def _optional_kwargs(func, values: dict) -> dict:
    """Keep optional media metadata compatible with the installed client build."""
    return {
        key: value
        for key, value in values.items()
        if _supports_parameter(func, key)
    }


async def _download(
    reader,
    target,
    dest: str,
    progress=None,
    expected_size: int = 0,
    source_chat=None,
    source_message_id=None,
) -> str:
    """Download media, refreshing its source message once on FILE_REFERENCE_EXPIRED."""
    candidate = target
    path = None
    flood_retries = 0
    refreshed_once = False

    while True:
        try:
            path = await reader.download_media(
                candidate,
                file_name=dest,
                progress=progress,
            )
            break

        except FloodWait as exc:
            if flood_retries >= 2 or exc.value > 300:
                raise
            flood_retries += 1
            logger.warning(
                "FloodWait %ss during download; retry %s",
                exc.value,
                flood_retries,
            )
            await asyncio.sleep(exc.value + 1)

        except Exception as exc:  # noqa: BLE001
            if not refreshed_once and _is_file_reference_error(exc):
                refreshed_once = True
                refreshed = await _refresh_source_message(
                    reader,
                    source_chat,
                    source_message_id or getattr(target, "id", None),
                )
                if refreshed is not None:
                    logger.info(
                        "Refreshed source message %s after expired file reference",
                        getattr(refreshed, "id", source_message_id),
                    )
                    candidate = refreshed
                    continue
            raise

    if isinstance(path, list):
        path = path[0] if path else None

    if not path or not isinstance(path, (str, os.PathLike)) or not os.path.isfile(path):
        raise RuntimeError("Download failed: Telegram did not produce a local file.")

    size = os.path.getsize(path)
    if size <= 0:
        raise RuntimeError("Download failed: the downloaded file is empty.")

    if expected_size and size != expected_size:
        raise RuntimeError(
            f"Download incomplete: got {_fmt_size(size)}, expected {_fmt_size(expected_size)}."
        )

    return str(path)


async def _download_thumb(reader, media, tmpdir: str) -> Optional[str]:
    thumb = _pick_thumb(media)
    if thumb is None:
        return None

    try:
        path = await _download(
            reader,
            thumb,
            os.path.join(tmpdir, "thumb.jpg")
        )
        if os.path.getsize(path) > THUMB_MAX_BYTES:
            return None
        return path
    except Exception as exc:  # noqa: BLE001
        logger.debug("Thumbnail skipped: %s", exc)
        return None


async def _send_text(sender, msg, target_chat, thread_id, preferences=None):
    link_options = None
    if hasattr(types, "LinkPreviewOptions"):
        link_options = types.LinkPreviewOptions(
            is_disabled=not bool(getattr(msg, "web_page", None))
        )

    kwargs = {
        "chat_id": target_chat,
        "text": msg.text,
        "entities": getattr(msg, "entities", None),
        "parse_mode": enums.ParseMode.DISABLED,
        "message_thread_id": thread_id,
    }

    if link_options is not None:
        kwargs["link_preview_options"] = link_options

    kwargs["text"] = _replace_words(kwargs["text"], preferences)
    await _send_result(sender, "send_message", target_chat, preferences, **kwargs)


async def _send_simple_media(sender, msg, media_type, target_chat, thread_id, preferences=None):
    if media_type == enums.MessageMediaType.CONTACT:
        contact = msg.contact
        kwargs = {
            "chat_id": target_chat,
            "phone_number": contact.phone_number,
            "first_name": contact.first_name or contact.phone_number,
            "message_thread_id": thread_id,
        }

        if contact.last_name:
            kwargs["last_name"] = contact.last_name
        if contact.vcard:
            kwargs["vcard"] = contact.vcard

        await _send_result(sender, "send_contact", target_chat, preferences, **kwargs)
        return

    if media_type == enums.MessageMediaType.LOCATION:
        loc = msg.location
        kwargs = {
            "chat_id": target_chat,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "message_thread_id": thread_id,
        }

        for name in (
            "horizontal_accuracy",
            "live_period",
            "heading",
            "proximity_alert_radius",
        ):
            value = getattr(loc, name, None)
            if value is not None and _supports_parameter(sender.send_location, name):
                kwargs[name] = value

        await _send_result(sender, "send_location", target_chat, preferences, **kwargs)
        return

    venue = msg.venue
    kwargs = {
        "chat_id": target_chat,
        "latitude": venue.location.latitude,
        "longitude": venue.location.longitude,
        "title": venue.title,
        "address": venue.address,
        "message_thread_id": thread_id,
    }

    if getattr(venue, "foursquare_id", None):
        kwargs["foursquare_id"] = venue.foursquare_id
    if getattr(venue, "foursquare_type", None):
        kwargs["foursquare_type"] = venue.foursquare_type

    await _send_result(sender, "send_venue", target_chat, preferences, **kwargs)


def _plain_text(value) -> str:
    if value is None:
        return ""
    inner = getattr(value, "text", None)
    return str(inner if inner is not None else value)


async def _send_poll(sender, msg, target_chat, thread_id, preferences=None):
    poll = getattr(msg, "poll", None)
    if poll is None:
        raise UnsupportedMedia("The poll data is unavailable in this message.")

    option_objects = []
    for option in (getattr(poll, "options", None) or []):
        text = _plain_text(getattr(option, "text", option)).strip()
        if not text:
            continue
        if hasattr(types, "InputPollOption"):
            option_objects.append(types.InputPollOption(text=text))
        else:
            option_objects.append(text)

    if not 2 <= len(option_objects) <= 12:
        raise UnsupportedMedia("This poll has an unsupported number of answer options.")

    poll_kind = getattr(poll, "type", None)
    kwargs = {
        "chat_id": target_chat,
        "question": getattr(poll, "question", "") or "Poll",
        "options": option_objects,
        "is_anonymous": getattr(poll, "is_anonymous", True),
        "allows_multiple_answers": getattr(poll, "allows_multiple_answers", False),
        "message_thread_id": thread_id,
    }

    if poll_kind is not None:
        kwargs["type"] = poll_kind

    correct_ids = getattr(poll, "correct_option_ids", None)
    if correct_ids is None:
        single_correct = getattr(poll, "correct_option_id", None)
        correct_ids = [single_correct] if single_correct is not None else None

    is_quiz = "quiz" in str(getattr(poll_kind, "value", poll_kind)).lower()

    if correct_ids and is_quiz:
        kwargs["correct_option_ids"] = list(correct_ids)
    elif is_quiz:
        # Some forwarded quiz polls hide their answer key from the source
        # account. Keep the question usable without inventing a correct answer.
        regular_type = getattr(getattr(enums, "PollType", None), "REGULAR", None)
        if regular_type is not None:
            kwargs["type"] = regular_type

        description = _plain_text(getattr(poll, "description", None)).strip()
        notice = "Original quiz answer key is hidden; recreated as a regular poll."
        kwargs["description"] = (description + "\n\n" + notice).strip()[:1024]

    explanation = getattr(poll, "explanation", None)
    if explanation:
        kwargs["explanation"] = explanation

    description = getattr(poll, "description", None)
    if description and "description" not in kwargs:
        kwargs["description"] = description

    # Preserve the settings that Kurigram's installed send_poll supports.
    optional_poll_fields = (
        "allows_revoting",
        "shuffle_options",
        "allow_adding_options",
        "hide_results_until_closes",
        "members_only",
        "country_codes",
    )

    for name in optional_poll_fields:
        value = getattr(poll, name, None)
        if value is not None and _supports_parameter(sender.send_poll, name):
            kwargs[name] = value

    # Telegram does not allow open_period and close_date together. Preserve
    # one active timer, but never copy a stale/expired close date.
    if not bool(getattr(poll, "is_closed", False)):
        open_period = getattr(poll, "open_period", None)
        close_date = getattr(poll, "close_date", None)

        if open_period and _supports_parameter(sender.send_poll, "open_period"):
            kwargs["open_period"] = open_period
        elif close_date and _supports_parameter(sender.send_poll, "close_date"):
            try:
                from datetime import datetime, timezone
                now = datetime.now(close_date.tzinfo or timezone.utc)
                if close_date > now:
                    kwargs["close_date"] = close_date
            except (TypeError, ValueError):
                logger.debug("Skipping invalid poll close_date: %r", close_date)

    if bool(getattr(poll, "is_closed", False)) and _supports_parameter(sender.send_poll, "is_closed"):
        kwargs["is_closed"] = True

    await _send_result(sender, "send_poll", target_chat, preferences, **kwargs)


async def _send_dice(sender, msg, target_chat, thread_id, preferences=None):
    dice = getattr(msg, "dice", None)
    emoji = getattr(dice, "emoji", None) or "🎲"

    await _send_result(
        sender, "send_dice", target_chat, preferences,
        chat_id=target_chat, emoji=emoji, message_thread_id=thread_id,
    )


async def _reupload_file(
    reader,
    sender,
    msg,
    media_type,
    target_chat,
    thread_id,
    on_stage,
    source_chat=None,
    custom_filename=None,
    custom_caption=None,
    preferences=None,
):
    attr, sender_name, param, has_caption, has_thumb = _MEDIA_SPECS[media_type]
    media = getattr(msg, attr)
    size = int(getattr(media, "file_size", 0) or 0)
    kind = getattr(media_type, "name", str(media_type)).lower().replace("_", " ")
    premium = await _is_premium(sender)

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
        upload_name = _choose_filename(original_name, custom_filename, preferences)
        local_name = upload_name
        dest = os.path.join(tmpdir, local_name) if local_name else tmpdir + os.sep

        async with _transfer_lock:
            await _stage(on_stage, f"⬇️ Downloading {kind} ({_fmt_size(size)})...")

            path = await _download(
                reader,
                msg,
                dest,
                progress=_progress_cb(on_stage, f"⬇️ Downloading {kind}"),
                expected_size=size,
                source_chat=source_chat,
                source_message_id=getattr(msg, "id", None),
            )

            thumb_mode = (preferences or {}).get("thumb_mode", "source")
            custom_thumb = (preferences or {}).get("thumb_path")
            if thumb_mode == "none":
                thumb_path = None
            elif thumb_mode == "custom":
                thumb_path = (
                    custom_thumb
                    if custom_thumb and os.path.isfile(custom_thumb)
                    and os.path.getsize(custom_thumb) <= THUMB_MAX_BYTES
                    else None
                )
            else:
                thumb_path = await _download_thumb(reader, media, tmpdir) if has_thumb else None

            kwargs = {
                "chat_id": target_chat,
                param: path,
                "message_thread_id": thread_id,
            }

            source_caption = getattr(msg, "caption", None) or None
            caption = source_caption
            if custom_caption is not None:
                caption = custom_caption or None
            elif preferences:
                mode = preferences.get("caption_mode", "keep")
                pref_caption = (preferences.get("caption_text") or "").strip()
                if mode == "replace" and pref_caption:
                    caption = pref_caption
                elif mode == "append" and pref_caption:
                    caption = f"{source_caption or ''}\n\n{pref_caption}".strip()
            unmodified_caption = caption
            caption = _replace_words(caption, preferences)
            caption_entities = getattr(msg, "caption_entities", None) if caption == source_caption and caption == unmodified_caption else None
            long_caption = None

            if has_caption and caption:
                caption_limit = CAPTION_LIMIT_PREMIUM if premium else CAPTION_LIMIT_FREE
                if len(caption) <= caption_limit:
                    kwargs.update(
                        caption=caption,
                        parse_mode=enums.ParseMode.DISABLED,
                        caption_entities=caption_entities,
                    )
                else:
                    long_caption = caption

            sender_method = getattr(sender, sender_name)
            renamed_as_document = False
            if custom_filename:
                if media_type in (enums.MessageMediaType.PHOTO, enums.MessageMediaType.STICKER,
                                  enums.MessageMediaType.VOICE, enums.MessageMediaType.VIDEO_NOTE):
                    # These send methods do not consistently expose filename metadata.
                    # Upload as a document when the owner explicitly requests a filename.
                    sender_name, param = "send_document", "document"
                    sender_method = sender.send_document
                    renamed_as_document = True
                    kwargs.pop("photo", None)
                    kwargs.pop("sticker", None)
                    kwargs.pop("voice", None)
                    kwargs.pop("video_note", None)
                    kwargs["document"] = path
                    kwargs["file_name"] = upload_name
                    kwargs["force_document"] = True

            if has_caption and getattr(msg, "has_media_spoiler", None) and media_type in (
                enums.MessageMediaType.PHOTO,
                enums.MessageMediaType.VIDEO,
                enums.MessageMediaType.ANIMATION,
            ) and _supports_parameter(sender_method, "has_spoiler"):
                kwargs["has_spoiler"] = True

            if has_caption and caption and long_caption is None and getattr(
                msg, "show_caption_above_media", None
            ) and media_type in (
                enums.MessageMediaType.PHOTO,
                enums.MessageMediaType.VIDEO,
                enums.MessageMediaType.ANIMATION,
            ) and _supports_parameter(sender_method, "show_caption_above_media"):
                kwargs["show_caption_above_media"] = True

            if thumb_path and _supports_parameter(sender_method, "thumb"):
                kwargs["thumb"] = thumb_path

            if media_type == enums.MessageMediaType.VIDEO:
                kwargs.update(
                    duration=getattr(media, "duration", 0) or 0,
                    width=getattr(media, "width", 0) or 0,
                    height=getattr(media, "height", 0) or 0,
                    supports_streaming=True,
                )
                if upload_name:
                    kwargs["file_name"] = upload_name

            elif media_type == enums.MessageMediaType.ANIMATION:
                kwargs.update(
                    duration=getattr(media, "duration", 0) or 0,
                    width=getattr(media, "width", 0) or 0,
                    height=getattr(media, "height", 0) or 0,
                )
                if upload_name:
                    kwargs["file_name"] = upload_name

            elif media_type == enums.MessageMediaType.DOCUMENT:
                kwargs["force_document"] = True
                if upload_name:
                    kwargs["file_name"] = upload_name

            elif media_type == enums.MessageMediaType.AUDIO:
                kwargs["duration"] = getattr(media, "duration", 0) or 0
                if getattr(media, "performer", None):
                    kwargs["performer"] = media.performer
                if getattr(media, "title", None):
                    kwargs["title"] = media.title
                if upload_name:
                    kwargs["file_name"] = upload_name

            elif media_type == enums.MessageMediaType.VOICE and not renamed_as_document:
                kwargs["duration"] = getattr(media, "duration", 0) or 0
                if getattr(media, "waveform", None):
                    kwargs["waveform"] = media.waveform

            elif media_type == enums.MessageMediaType.VIDEO_NOTE and not renamed_as_document:
                kwargs.update(
                    duration=getattr(media, "duration", 0) or 0,
                    length=getattr(media, "length", 0) or 240,
                )

            elif media_type == enums.MessageMediaType.STICKER and not renamed_as_document:
                if getattr(media, "emoji", None):
                    kwargs["emoji"] = media.emoji

            progress = _progress_cb(on_stage, f"⬆️ Uploading {kind}")
            if progress and _supports_parameter(sender_method, "progress"):
                kwargs["progress"] = progress

            await _stage(on_stage, f"⬆️ Uploading {kind} ({_fmt_size(size)})...")
            await _send_result(sender, sender_name, target_chat, preferences, **kwargs)

            if long_caption:
                await _send_result(
                    sender, "send_message", target_chat, preferences,
                    chat_id=target_chat,
                    text=long_caption,
                    entities=caption_entities,
                    parse_mode=enums.ParseMode.DISABLED,
                    message_thread_id=thread_id,
                )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_ALBUM_INPUT_TYPES = {
    enums.MessageMediaType.PHOTO: ("photo", "InputMediaPhoto"),
    enums.MessageMediaType.VIDEO: ("video", "InputMediaVideo"),
    enums.MessageMediaType.DOCUMENT: ("document", "InputMediaDocument"),
    enums.MessageMediaType.AUDIO: ("audio", "InputMediaAudio"),
}


async def reupload_media_group(
    reader, sender, messages, target_chat, thread_id=None, on_stage=None, source_chat=None,
    preferences=None,
) -> bool:
    """Re-upload a Telegram album as an album. Returns False if it is not supported."""
    ordered = sorted(
        (m for m in (messages or []) if m and not getattr(m, "empty", False)),
        key=lambda item: int(getattr(item, "id", 0) or 0),
    )

    if not 2 <= len(ordered) <= 10:
        return False

    media_types = [getattr(m, "media", None) for m in ordered]
    if any(mt not in _ALBUM_INPUT_TYPES for mt in media_types):
        return False

    # Check the installed Kurigram build before downloading anything. Telegram
    # accepts mixed photo/video albums, but audio and document albums must remain
    # homogeneous.
    if any(not hasattr(types, _ALBUM_INPUT_TYPES[mt][1]) for mt in set(media_types)):
        return False

    names = {_ALBUM_INPUT_TYPES[mt][0] for mt in media_types}
    if ("audio" in names or "document" in names) and len(names) != 1:
        return False

    premium = await _is_premium(sender)
    max_size = (FILE_LIMIT_PREMIUM_MB if premium else FILE_LIMIT_FREE_MB) * MB
    if MAX_FILE_MB:
        max_size = min(max_size, MAX_FILE_MB * MB)

    item_sizes = [
        int(getattr(getattr(m, _ALBUM_INPUT_TYPES[m.media][0]), "file_size", 0) or 0)
        for m in ordered
    ]

    too_large = max(item_sizes, default=0)
    if too_large > max_size:
        raise UnsupportedMedia(
            f"An album item is {_fmt_size(too_large)}; per-file limit is {_fmt_size(max_size)}."
        )

    total_size = sum(item_sizes)

    tmpdir = tempfile.mkdtemp(prefix="save_album_")
    long_captions = []

    try:
        free = shutil.disk_usage(tmpdir).free
        if total_size and free < total_size * 1.05 + 50 * MB:
            raise UnsupportedMedia(
                f"Not enough disk space for this {_fmt_size(total_size)} album ({_fmt_size(free)} free)."
            )

        input_media = []

        async with _transfer_lock:
            for index, item in enumerate(ordered, start=1):
                attr, class_name = _ALBUM_INPUT_TYPES[item.media]
                media = getattr(item, attr)
                size = int(getattr(media, "file_size", 0) or 0)
                filename = _safe_local_name(getattr(media, "file_name", None))
                dest = os.path.join(tmpdir, f"{index}_{filename}") if filename else tmpdir + os.sep

                await _stage(
                    on_stage,
                    f"⬇️ Downloading album item {index}/{len(ordered)} ({_fmt_size(size)})..."
                )

                path = await _download(
                    reader,
                    item,
                    dest,
                    progress=_progress_cb(on_stage, f"⬇️ Album item {index}/{len(ordered)}"),
                    expected_size=size,
                    source_chat=source_chat,
                    source_message_id=getattr(item, "id", None),
                )

                kwargs = {
                    "media": path,
                    "parse_mode": enums.ParseMode.DISABLED,
                }

                source_caption = getattr(item, "caption", None) or None
                caption = source_caption
                if preferences:
                    mode = preferences.get("caption_mode", "keep")
                    pref_caption = (preferences.get("caption_text") or "").strip()
                    if mode == "replace" and pref_caption:
                        caption = pref_caption
                    elif mode == "append" and pref_caption:
                        caption = f"{source_caption or ''}\n\n{pref_caption}".strip()
                caption = _replace_words(caption, preferences)
                if caption:
                    caption_limit = CAPTION_LIMIT_PREMIUM if premium else CAPTION_LIMIT_FREE
                    entities = getattr(item, "caption_entities", None) if caption == source_caption else None
                    if len(caption) <= caption_limit:
                        kwargs["caption"] = caption
                        kwargs["caption_entities"] = entities
                    else:
                        long_captions.append((caption, entities))

                if item.media == enums.MessageMediaType.VIDEO:
                    kwargs.update(
                        duration=getattr(media, "duration", 0) or 0,
                        width=getattr(media, "width", 0) or 0,
                        height=getattr(media, "height", 0) or 0,
                        supports_streaming=True,
                    )
                    if getattr(media, "file_name", None):
                        kwargs["file_name"] = media.file_name
                    if getattr(item, "has_media_spoiler", None):
                        kwargs["has_spoiler"] = True
                    if getattr(item, "show_caption_above_media", None):
                        kwargs["show_caption_above_media"] = True

                elif item.media == enums.MessageMediaType.PHOTO:
                    if getattr(item, "has_media_spoiler", None):
                        kwargs["has_spoiler"] = True
                    if getattr(item, "show_caption_above_media", None):
                        kwargs["show_caption_above_media"] = True

                elif item.media == enums.MessageMediaType.AUDIO:
                    kwargs.update(
                        duration=getattr(media, "duration", 0) or 0,
                        title=getattr(media, "title", None),
                        performer=getattr(media, "performer", None),
                    )

                elif item.media == enums.MessageMediaType.DOCUMENT:
                    kwargs["file_name"] = (
                        getattr(media, "file_name", None) or os.path.basename(path)
                    )

                thumb_mode = (preferences or {}).get("thumb_mode", "source")
                custom_thumb = (preferences or {}).get("thumb_path")
                if thumb_mode == "custom" and custom_thumb and item.media != enums.MessageMediaType.PHOTO and os.path.isfile(custom_thumb) and os.path.getsize(custom_thumb) <= THUMB_MAX_BYTES:
                    input_cls = getattr(types, class_name)
                    try:
                        if "thumb" in inspect.signature(input_cls).parameters:
                            kwargs["thumb"] = custom_thumb
                    except (TypeError, ValueError):
                        pass
                input_cls = getattr(types, class_name)
                input_media.append(input_cls(**kwargs))

            await _stage(
                on_stage,
                f"⬆️ Uploading album ({len(input_media)} items)..."
            )

            await _send_result(
                sender, "send_media_group", target_chat, preferences,
                chat_id=target_chat,
                media=input_media,
                message_thread_id=thread_id,
            )

            for caption, caption_entities in long_captions:
                await _send_result(
                    sender, "send_message", target_chat, preferences,
                    chat_id=target_chat,
                    text=_replace_words(caption, preferences),
                    entities=caption_entities,
                    parse_mode=enums.ParseMode.DISABLED,
                    message_thread_id=thread_id,
                )

        return True

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def reupload(
    reader, msg, target_chat, thread_id=None, on_stage: StageCallback = None,
    sender=None, source_chat=None, custom_filename=None, custom_caption=None,
    preferences=None,
):
    """Re-create a message using reader to download and sender to post."""
    sender = sender or reader

    if getattr(msg, "empty", None) or getattr(msg, "service", None):
        raise UnsupportedMedia("Empty or Telegram service message.")

    media_type = getattr(msg, "media", None)

    if getattr(msg, "text", None) and media_type in (
        None,
        getattr(enums.MessageMediaType, "WEB_PAGE", None),
    ):
        await _send_text(sender, msg, target_chat, thread_id, preferences)
        return

    if media_type in _MEDIA_SPECS:
        await _reupload_file(
            reader,
            sender,
            msg,
            media_type,
            target_chat,
            thread_id,
            on_stage,
            source_chat=source_chat,
            custom_filename=custom_filename,
            custom_caption=custom_caption,
            preferences=preferences,
        )
        return

    simple_types = {
        getattr(enums.MessageMediaType, "CONTACT", None),
        getattr(enums.MessageMediaType, "LOCATION", None),
        getattr(enums.MessageMediaType, "VENUE", None),
    }

    if media_type in simple_types:
        await _send_simple_media(sender, msg, media_type, target_chat, thread_id, preferences)
        return

    if media_type == getattr(enums.MessageMediaType, "POLL", None):
        await _send_poll(sender, msg, target_chat, thread_id, preferences)
        return

    if media_type == getattr(enums.MessageMediaType, "DICE", None):
        await _send_dice(sender, msg, target_chat, thread_id, preferences)
        return

    name = getattr(media_type, "name", str(media_type) if media_type else "unknown")
    name = str(name).lower().replace("_", " ")

    explanations = {
        "game": "Interactive Telegram games require their original game bot and cannot be recreated as a normal upload.",
        "invoice": "Payment invoices cannot be copied as normal media; the original payment provider setup is required.",
        "paid media": "Paid media is not re-created as a free upload. Open/purchase it through Telegram and use an authorized sharing method.",
        "story": "Stories are not regular chat messages and cannot be imported with this message-link command.",
        "giveaway": "Telegram giveaway service posts cannot be recreated as ordinary media.",
    }

    raise UnsupportedMedia(
        explanations.get(
            name,
            f"Telegram message type '{name}' cannot be re-created by the available send API."
        )
    )


async def copy_or_reupload(
    reader, source_chat, source_msg, target_chat, thread_id=None,
    on_stage: StageCallback = None, sender=None, custom_filename=None,
    custom_caption=None, preferences=None,
) -> str:
    """Copy when possible; refresh stale source references and re-upload on fallback."""
    sender = sender or reader
    current_msg = source_msg

    if sender is reader and not (FORCE_REUPLOAD or is_protected(source_msg) or custom_filename or custom_caption is not None or preferences and preferences.get("caption_mode") != "keep" or preferences and preferences.get("replace_rules") or preferences and preferences.get("thumb_path") or preferences and preferences.get("auto_pin")):
        try:
            await _send_result(
                reader, "copy_message", target_chat, preferences,
                chat_id=target_chat,
                from_chat_id=source_chat,
                message_id=source_msg.id,
                message_thread_id=thread_id,
            )
            return "copied"

        except Exception as exc:  # noqa: BLE001
            if not _is_copy_fallback_error(exc):
                raise

            logger.info(
                "copy_message fallback for source %s (%s); refreshing before re-upload",
                getattr(source_msg, "id", None),
                type(exc).__name__,
            )

            refreshed = await _refresh_source_message(
                reader,
                source_chat,
                getattr(source_msg, "id", None)
            )

            if refreshed is not None:
                current_msg = refreshed

            # For an expired file reference, try copy once more with the newly
            # fetched message. If Telegram still rejects it, download and re-upload.
            if _is_file_reference_error(exc) and refreshed is not None:
                try:
                    await _send_result(
                        reader, "copy_message", target_chat, preferences,
                        chat_id=target_chat,
                        from_chat_id=source_chat,
                        message_id=current_msg.id,
                        message_thread_id=thread_id,
                    )
                    return "copied_after_refresh"

                except Exception as retry_exc:  # noqa: BLE001
                    if not _is_copy_fallback_error(retry_exc):
                        raise

                    logger.info(
                        "copy still unavailable after refresh for message %s (%s); re-uploading",
                        current_msg.id,
                        type(retry_exc).__name__,
                    )

    await reupload(
        reader,
        current_msg,
        target_chat,
        thread_id,
        on_stage,
        sender=sender,
        source_chat=source_chat,
        custom_filename=custom_filename,
        custom_caption=custom_caption,
        preferences=preferences,
    )

    return "reuploaded"
