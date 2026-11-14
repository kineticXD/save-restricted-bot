"""Persistent transfer customisation commands for Waguri."""
from __future__ import annotations

import logging
import os
import shlex
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from pyrogram import Client, filters
from pyrogram.types import Message

from config import OWNER_ID
from storage import DATA_DIR, get_preferences, update_preference

logger = logging.getLogger(__name__)
MAX_THUMB_BYTES = 200 * 1024


def _help_text() -> str:
    return (
        "⚙️ WAGURI TRANSFER SETTINGS\n\n"
        "/settings — show current preferences\n"
        "/setcaption append <text> — add a caption below the source caption\n"
        "/setcaption replace <text> — replace source captions\n"
        "/clearcaption — stop adding/replacing captions\n"
        "/setthumb — reply to a photo/image with this command\n"
        "/clearthumb — remove the custom thumbnail\n"
        "/setthumbmode source|custom|none — choose source, custom or no thumbnail\n"
        '/replaceword "old" => "new" — replace text in filenames and captions\n'
        "/clearreplace — remove all replacement rules\n"
        "/setpin on|off — automatically pin uploaded messages\n\n"
        "For a one-off filename:\n"
        '/save <link> --name "New filename.mp4"\n'
        '/rename <link> "New filename.mp4"'
    )


@Client.on_message(filters.command("settings") & filters.user(OWNER_ID))
async def settings_command(client: Client, message: Message):
    prefs = get_preferences(OWNER_ID)
    mode = prefs.get("caption_mode", "keep")
    caption = prefs.get("caption_text") or "(none)"
    if len(caption) > 90:
        caption = caption[:87] + "..."
    thumb = "Configured ✅" if prefs.get("thumb_path") and os.path.isfile(prefs["thumb_path"]) else "Not set"
    rules = prefs.get("replace_rules") or []
    text = (
        "⚙️ WAGURI SETTINGS\n\n"
        f"Caption mode: {mode}\n"
        f"Caption text: {caption}\n"
        f"Custom thumbnail: {thumb}\n"
        f"Thumbnail mode: {prefs.get('thumb_mode', 'source')}\n"
        f"Auto-pin uploads: {'ON ✅' if prefs.get('auto_pin') else 'OFF'}\n"
        f"Word replacement rules: {len(rules)}\n\n"
        "Use /help for commands and examples."
    )
    await message.reply_text(text)


@Client.on_message(filters.command("setcaption") & filters.user(OWNER_ID))
async def set_caption_command(client: Client, message: Message):
    raw = (message.text or "").partition(" ")[2].strip()
    if not raw:
        await message.reply_text('Usage: /setcaption append "Class notes"\n/setcaption replace "New caption"')
        return
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        await message.reply_text(f"❌ Quoting error: {exc}")
        return
    mode = "append"
    if parts and parts[0].lower() in {"append", "replace"}:
        mode = parts.pop(0).lower()
    caption = " ".join(parts).strip()
    if not caption:
        await message.reply_text("❌ Caption text cannot be empty. Use /clearcaption to disable captions.")
        return
    if len(caption) > 900:
        await message.reply_text("❌ Keep the custom caption below 900 characters so it fits safely with media captions.")
        return
    update_preference(OWNER_ID, "caption_mode", mode)
    update_preference(OWNER_ID, "caption_text", caption)
    await message.reply_text(f"✅ Custom caption saved. Mode: {mode}.\nIt will apply to future re-uploads.")


@Client.on_message(filters.command("clearcaption") & filters.user(OWNER_ID))
async def clear_caption_command(client: Client, message: Message):
    update_preference(OWNER_ID, "caption_mode", "keep")
    update_preference(OWNER_ID, "caption_text", "")
    await message.reply_text("✅ Custom caption disabled. Original source captions will be preserved.")


@Client.on_message(filters.command("setthumb") & filters.user(OWNER_ID))
async def set_thumbnail_command(client: Client, message: Message):
    source = getattr(message, "reply_to_message", None)
    if source is None or not (getattr(source, "photo", None) or getattr(source, "document", None)):
        await message.reply_text("Reply to a photo or image document with /setthumb. The bot will resize it to Telegram's thumbnail limits.")
        return
    document = getattr(source, "document", None)
    if document is not None and not str(getattr(document, "mime_type", "")).startswith("image/"):
        await message.reply_text("❌ The replied document is not an image.")
        return

    thumb_dir = Path(DATA_DIR) / "custom_thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    raw_path = thumb_dir / f"{int(OWNER_ID)}_source.jpg"
    final_path = thumb_dir / f"{int(OWNER_ID)}_thumbnail.jpg"
    status = await message.reply_text("⏳ Preparing your custom thumbnail...")
    try:
        downloaded = await client.download_media(source, file_name=str(raw_path))
        if not downloaded or not os.path.isfile(downloaded):
            raise RuntimeError("Telegram did not return an image file.")
        raw_path = Path(downloaded)
        with Image.open(downloaded) as image:
            image = image.convert("RGB")
            image.thumbnail((320, 320), Image.Resampling.LANCZOS)
            quality = 88
            image.save(final_path, format="JPEG", quality=quality, optimize=True)
            while final_path.stat().st_size > MAX_THUMB_BYTES and quality > 45:
                quality -= 8
                image.save(final_path, format="JPEG", quality=quality, optimize=True)
            while final_path.stat().st_size > MAX_THUMB_BYTES and min(image.size) > 80:
                image.thumbnail((max(80, int(image.width * 0.85)), max(80, int(image.height * 0.85))), Image.Resampling.LANCZOS)
                image.save(final_path, format="JPEG", quality=70, optimize=True)
        if final_path.stat().st_size > MAX_THUMB_BYTES:
            raise RuntimeError("Could not compress the image below 200 KB.")
        update_preference(OWNER_ID, "thumb_path", str(final_path))
        update_preference(OWNER_ID, "thumb_mode", "custom")
        await status.edit_text(
            f"✅ Custom thumbnail saved.\nSize: {final_path.stat().st_size // 1024} KB\n"
            "It will be used for supported video/document/audio uploads."
        )
    except (UnidentifiedImageError, OSError, RuntimeError) as exc:
        logger.exception("Could not set custom thumbnail")
        await status.edit_text(f"❌ Could not set thumbnail: {exc}")
    finally:
        try:
            if raw_path.exists():
                raw_path.unlink()
        except OSError:
            pass


@Client.on_message(filters.command("clearthumb") & filters.user(OWNER_ID))
async def clear_thumbnail_command(client: Client, message: Message):
    prefs = get_preferences(OWNER_ID)
    path = prefs.get("thumb_path")
    update_preference(OWNER_ID, "thumb_path", "")
    update_preference(OWNER_ID, "thumb_mode", "source")
    if path:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            logger.warning("Could not remove old custom thumbnail: %s", path)
    await message.reply_text("✅ Custom thumbnail cleared. Source thumbnails will be used where available.")


@Client.on_message(filters.command("replaceword") & filters.user(OWNER_ID))
async def replace_word_command(client: Client, message: Message):
    raw = (message.text or "").partition(" ")[2]
    if "=>" not in raw:
        await message.reply_text('Usage: /replaceword "old text" => "new text"')
        return
    left, _, right = raw.partition("=>")
    try:
        old_parts = shlex.split(left.strip())
        new_parts = shlex.split(right.strip())
    except ValueError as exc:
        await message.reply_text(f"❌ Quoting error: {exc}")
        return
    old = " ".join(old_parts).strip()
    new = " ".join(new_parts).strip()
    if not old:
        await message.reply_text("❌ The old text cannot be empty.")
        return
    prefs = get_preferences(OWNER_ID)
    rules = list(prefs.get("replace_rules") or [])
    if len(rules) >= 20:
        await message.reply_text("❌ Maximum 20 replacement rules. Use /clearreplace to reset them.")
        return
    rules.append({"old": old, "new": new})
    update_preference(OWNER_ID, "replace_rules", rules)
    await message.reply_text(f"✅ Replacement rule added: {old} → {new}\nIt applies to filenames and captions on re-upload.")


@Client.on_message(filters.command("clearreplace") & filters.user(OWNER_ID))
async def clear_replace_command(client: Client, message: Message):
    update_preference(OWNER_ID, "replace_rules", [])
    await message.reply_text("✅ All filename/caption replacement rules cleared.")


@Client.on_message(filters.command("setpin") & filters.user(OWNER_ID))
async def set_pin_command(client: Client, message: Message):
    args = getattr(message, "command", []) or []
    if len(args) < 2 or str(args[1]).lower() not in {"on", "off"}:
        prefs = get_preferences(OWNER_ID)
        await message.reply_text(
            f"Auto-pin is currently {'ON' if prefs.get('auto_pin') else 'OFF'}.\nUsage: /setpin on or /setpin off"
        )
        return
    enabled = str(args[1]).lower() == "on"
    update_preference(OWNER_ID, "auto_pin", enabled)
    await message.reply_text(f"✅ Auto-pin {'enabled' if enabled else 'disabled'}.")


@Client.on_message(filters.command("setthumbmode") & filters.user(OWNER_ID))
async def set_thumbnail_mode_command(client: Client, message: Message):
    args = getattr(message, "command", []) or []
    if len(args) < 2 or str(args[1]).lower() not in {"source", "custom", "none"}:
        prefs = get_preferences(OWNER_ID)
        await message.reply_text(
            f"Thumbnail mode is currently: {prefs.get('thumb_mode', 'source')}\n"
            "Usage: /setthumbmode source|custom|none\n\n"
            "source = use the original media thumbnail\n"
            "custom = use the image saved with /setthumb\n"
            "none = do not attach a downloaded/custom thumbnail"
        )
        return
    mode = str(args[1]).lower()
    prefs = get_preferences(OWNER_ID)
    if mode == "custom" and not (prefs.get("thumb_path") and os.path.isfile(prefs["thumb_path"])):
        await message.reply_text("❌ No custom thumbnail is saved. Reply to an image with /setthumb first.")
        return
    update_preference(OWNER_ID, "thumb_mode", mode)
    await message.reply_text(f"✅ Thumbnail mode set to: {mode}.")
