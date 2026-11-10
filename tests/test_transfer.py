"""
Offline tests for plugins/transfer.py and the /save, /batch handlers.

The fake userbot validates EVERY call against the real Kurigram method signatures,
so a misspelled / unsupported keyword argument fails here instead of on Railway.

Run:  python tests/test_transfer.py
"""
import os, sys, glob, asyncio, inspect, tempfile, warnings
from types import SimpleNamespace as NS

warnings.simplefilter("ignore")
for k, v in dict(API_ID="1", API_HASH="a", BOT_TOKEN="1:a", SESSION_STRING="s", OWNER_ID="42").items():
    os.environ.setdefault(k, v)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyrogram import Client, enums, errors
from plugins import helpers, transfer
from plugins.transfer import copy_or_reupload, UnsupportedMedia

helpers.MIN_DELAY = helpers.MAX_DELAY = 0  # no sleeping in tests
T = enums.MessageMediaType
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def leftover_dirs():
    return glob.glob(os.path.join(tempfile.gettempdir(), "save_*"))


class FakeUser:
    """Mimics the parts of pyrogram.Client the code uses; validates kwargs vs the real signatures."""

    def __init__(self, premium=False, copy_error=None, send_errors=None, download_truncate=False):
        self.premium, self.copy_error = premium, copy_error
        self.send_errors = list(send_errors or [])
        self.download_truncate = download_truncate
        self.calls = []          # (method, kwargs, file_existed_at_call_time)
        self.downloads = []

    def _validate(self, name, kw):
        inspect.signature(getattr(Client, name)).bind(None, **kw)  # raises TypeError on a bad kwarg

    async def get_me(self):
        return NS(is_premium=self.premium)

    async def copy_message(self, **kw):
        self._validate("copy_message", kw)
        self.calls.append(("copy_message", kw, None))
        if self.copy_error:
            raise self.copy_error

    async def download_media(self, message, file_name="", progress=None, **_):
        directory, name = os.path.split(file_name)
        os.makedirs(directory, exist_ok=True)
        if not name:                                  # pyrogram would generate one
            name = "generated.bin"
        path = os.path.join(directory, name)
        size = getattr(message, "file_size", None)
        if size is None:                              # message object
            m = getattr(message, getattr(getattr(message, "media", None), "value", ""), None)
            size = getattr(m, "file_size", 10)
        size = size - 1 if self.download_truncate else size
        with open(path, "wb") as f:
            f.write(b"x" * max(size, 1))
        if progress:
            await progress(size // 2, size)
            await progress(size, size)
        self.downloads.append(path)
        return path

    def _sender(name):
        async def send(self, **kw):
            self._validate(name, kw)
            existed = None
            for k in ("photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note"):
                if k in kw:
                    existed = os.path.isfile(kw[k])
            if self.send_errors:
                raise self.send_errors.pop(0)
            self.calls.append((name, kw, existed))
        return send

    send_message = _sender("send_message")
    send_photo = _sender("send_photo")
    send_video = _sender("send_video")
    send_document = _sender("send_document")
    send_audio = _sender("send_audio")
    send_voice = _sender("send_voice")
    send_animation = _sender("send_animation")
    send_sticker = _sender("send_sticker")
    send_video_note = _sender("send_video_note")
    send_contact = _sender("send_contact")
    send_location = _sender("send_location")
    send_venue = _sender("send_venue")

    def methods(self):
        return [c[0] for c in self.calls]


def thumbs():
    return [NS(file_id="t1", width=90, height=90, file_size=2000), NS(file_id="t2", width=320, height=180, file_size=9000)]


def msg(media=None, protected=False, **fields):
    base = dict(id=7, chat=NS(id=-1003761240705, has_protected_content=protected), has_protected_content=None,
                empty=False, service=False, text=None, caption=None, caption_entities=None, entities=None,
                has_media_spoiler=None, show_caption_above_media=None, web_page=None, media=media)
    base.update(fields)
    return NS(**base)


TARGET, SRC = -1002194149025, -1003761240705


async def run(user, m, **kw):
    return await copy_or_reupload(user, SRC, m, TARGET, **kw)


async def main():
    # 1 ------------------------------------------------------------ normal chat -> server-side copy
    u = FakeUser()
    r = await run(u, msg(T.VIDEO, video=NS(file_size=100)))
    check("1 normal chat uses copy_message", r == "copied" and u.methods() == ["copy_message"] and not u.downloads)

    # 2 ------------------------------------------------------------ protected chat -> no copy attempt
    u = FakeUser()
    r = await run(u, msg(T.PHOTO, protected=True, photo=NS(file_size=500, thumbs=None), caption="hi"))
    check("2 protected chat skips copy_message", r == "reuploaded" and "copy_message" not in u.methods(), u.methods())
    check("2b photo uploaded with caption + file existed", u.calls[0][0] == "send_photo" and u.calls[0][1]["caption"] == "hi" and u.calls[0][2] is True)
    check("2c caption parse mode disabled (no markdown mangling)", u.calls[0][1]["parse_mode"] == enums.ParseMode.DISABLED)

    # 3 ------------------------------------------------------------ copy fails with restricted error -> fallback
    for err in (errors.ChatForwardsRestricted(), errors.FileReferenceExpired(), errors.MediaEmpty()):
        u = FakeUser(copy_error=err)
        r = await run(u, msg(T.DOCUMENT, document=NS(file_size=300, file_name="a.pdf", thumbs=None)))
        check(f"3 fallback on {type(err).__name__}", r == "reuploaded" and u.methods() == ["copy_message", "send_document"], u.methods())

    # 4 ------------------------------------------------------------ non-restriction errors are NOT retried via download
    u = FakeUser(copy_error=errors.ChatWriteForbidden())
    try:
        await run(u, msg(T.VIDEO, video=NS(file_size=100)))
        check("4 target-side error propagates", False)
    except errors.ChatWriteForbidden:
        check("4 target-side error propagates (no wasted download)", not u.downloads)

    # 5 ------------------------------------------------------------ every media type, kwargs validated vs real signatures
    cases = {
        "video": (T.VIDEO, "send_video", NS(file_size=900, width=640, height=360, duration=12, file_name="clip.mp4", thumbs=thumbs())),
        "document": (T.DOCUMENT, "send_document", NS(file_size=900, file_name="notes.pdf", thumbs=thumbs())),
        "audio": (T.AUDIO, "send_audio", NS(file_size=900, duration=200, performer="P", title="T", file_name="s.mp3", thumbs=None)),
        "voice": (T.VOICE, "send_voice", NS(file_size=900, duration=5, waveform=b"\x01\x02")),
        "animation": (T.ANIMATION, "send_animation", NS(file_size=900, width=320, height=240, duration=3, file_name="g.mp4", thumbs=None)),
        "sticker": (T.STICKER, "send_sticker", NS(file_size=900, emoji="😀")),
        "video_note": (T.VIDEO_NOTE, "send_video_note", NS(file_size=900, duration=9, length=240, thumbs=thumbs())),
        "photo": (T.PHOTO, "send_photo", NS(file_size=900, thumbs=None)),
    }
    for name, (mt, sender, obj) in cases.items():
        u = FakeUser()
        r = await run(u, msg(mt, protected=True, **{name: obj}))
        ok = r == "reuploaded" and u.methods() == [sender] and u.calls[0][2] is True
        check(f"5 {name}: uploaded via {sender}, file present at send time", ok, u.methods())
    u = FakeUser(); await run(u, msg(T.VIDEO, protected=True, video=cases["video"][2]))
    kw = u.calls[0][1]
    check("5b video keeps duration/size/name/streaming", (kw["duration"], kw["width"], kw["height"], kw["file_name"], kw["supports_streaming"]) == (12, 640, 360, "clip.mp4", True))
    check("5c thumbnail downloaded and attached (largest <=320px)", os.path.basename(kw.get("thumb", "")) == "thumb.jpg")
    u = FakeUser(); await run(u, msg(T.AUDIO, protected=True, audio=cases["audio"][2]))
    check("5d audio keeps performer/title", (u.calls[0][1]["performer"], u.calls[0][1]["title"]) == ("P", "T"))

    # 6 ------------------------------------------------------------ spoiler + entities preserved
    ents = [NS(type="bold")]
    u = FakeUser(); await run(u, msg(T.PHOTO, protected=True, photo=NS(file_size=50, thumbs=None), caption="*hey*", caption_entities=ents, has_media_spoiler=True))
    kw = u.calls[0][1]
    check("6 spoiler + caption entities forwarded", kw.get("has_spoiler") is True and kw["caption_entities"] is ents)

    # 7 ------------------------------------------------------------ text
    u = FakeUser(); r = await run(u, msg(None, protected=True, text="hello *world*", entities=ents))
    kw = u.calls[0][1]
    check("7 protected text sent with entities, parse disabled", u.methods() == ["send_message"] and kw["entities"] is ents and kw["parse_mode"] == enums.ParseMode.DISABLED)
    check("7b link preview disabled when original had none", kw["link_preview_options"].is_disabled is True)
    u = FakeUser(); await run(u, msg(T.WEB_PAGE, protected=True, text="see http://x", web_page=NS(url="http://x")))
    check("7c web page message -> text with preview enabled", u.calls[0][1]["link_preview_options"].is_disabled is False)

    # 8 ------------------------------------------------------------ contact / location / venue
    u = FakeUser(); await run(u, msg(T.CONTACT, protected=True, contact=NS(phone_number="+1", first_name="A", last_name=None, vcard=None)))
    check("8 contact", u.methods() == ["send_contact"])
    u = FakeUser(); await run(u, msg(T.LOCATION, protected=True, location=NS(latitude=1.0, longitude=2.0)))
    check("8b location", u.methods() == ["send_location"])
    u = FakeUser(); await run(u, msg(T.VENUE, protected=True, venue=NS(location=NS(latitude=1.0, longitude=2.0), title="V", address="A", foursquare_id=None, foursquare_type=None)))
    check("8c venue", u.methods() == ["send_venue"])

    # 9 ------------------------------------------------------------ unsupported types
    for mt in (T.POLL, T.DICE, T.GAME):
        u = FakeUser()
        try:
            await run(u, msg(mt, protected=True)); check(f"9 {mt.name} unsupported", False)
        except UnsupportedMedia as e:
            check(f"9 {mt.name} -> clean UnsupportedMedia", "restricted chat" in str(e))
    u = FakeUser()
    try:
        await run(u, msg(None, protected=True)); check("9b empty message unsupported", False)
    except UnsupportedMedia:
        check("9b message with no content -> UnsupportedMedia", True)

    # 10 ----------------------------------------------------------- long caption (non-premium 1024)
    long_cap = "a" * 1500
    u = FakeUser(premium=False); await run(u, msg(T.VIDEO, protected=True, video=NS(file_size=10, width=1, height=1, duration=1, file_name="v.mp4", thumbs=None), caption=long_cap))
    check("10 long caption: media without caption + separate text", u.methods() == ["send_video", "send_message"] and "caption" not in u.calls[0][1] and u.calls[1][1]["text"] == long_cap)
    u = FakeUser(premium=True); await run(u, msg(T.VIDEO, protected=True, video=NS(file_size=10, width=1, height=1, duration=1, file_name="v.mp4", thumbs=None), caption=long_cap))
    check("10b premium: long caption stays attached", u.methods() == ["send_video"] and u.calls[0][1]["caption"] == long_cap)

    # 11 ----------------------------------------------------------- size limits
    u = FakeUser()
    try:
        await run(u, msg(T.VIDEO, protected=True, video=NS(file_size=2100 * 1024 * 1024, width=1, height=1, duration=1, file_name="big.mp4", thumbs=None)))
        check("11 >2GB rejected for non-premium", False)
    except UnsupportedMedia:
        check("11 >2GB rejected for non-premium before downloading", not u.downloads)
    u = FakeUser(premium=True)
    big = NS(file_size=2100 * 1024 * 1024, width=1, height=1, duration=1, file_name="big.mp4", thumbs=None)
    orig = transfer.shutil.disk_usage
    transfer.shutil.disk_usage = lambda p: NS(free=10 * 1024 ** 4)
    class Huge(FakeUser):
        async def download_media(self, message, file_name="", progress=None, **_):
            self.downloads.append("stub"); p = os.path.join(os.path.split(file_name)[0], "big.mp4"); open(p, "wb").close(); return p
    # premium allows it past the guard; (download stub returns empty file -> integrity error proves we got past the guard)
    try:
        await run(Huge(premium=True), msg(T.VIDEO, protected=True, video=big))
    except RuntimeError:
        check("11b premium passes the size guard", True)
    except UnsupportedMedia:
        check("11b premium passes the size guard", False)
    transfer.shutil.disk_usage = lambda p: NS(free=1024)
    u = FakeUser()
    try:
        await run(u, msg(T.VIDEO, protected=True, video=NS(file_size=5 * 1024 * 1024, width=1, height=1, duration=1, file_name="v.mp4", thumbs=None)))
        check("11c low disk rejected", False)
    except UnsupportedMedia as e:
        check("11c insufficient disk space -> clean skip, nothing downloaded", "disk space" in str(e) and not u.downloads)
    transfer.shutil.disk_usage = orig

    # 12 ----------------------------------------------------------- integrity + cleanup on every failure path
    u = FakeUser(download_truncate=True)
    try:
        await run(u, msg(T.DOCUMENT, protected=True, document=NS(file_size=400, file_name="a.zip", thumbs=None))); check("12 truncated download detected", False)
    except RuntimeError as e:
        check("12 truncated download detected (never uploaded)", "incomplete" in str(e) and "send_document" not in u.methods())
    u = FakeUser(send_errors=[errors.ChatWriteForbidden()])
    try:
        await run(u, msg(T.DOCUMENT, protected=True, document=NS(file_size=400, file_name="a.zip", thumbs=None)))
    except errors.ChatWriteForbidden:
        pass
    check("12b temp dirs removed after failure", leftover_dirs() == [], leftover_dirs())
    check("12c temp dirs removed after all earlier runs", leftover_dirs() == [])

    # 13 ----------------------------------------------------------- FloodWait on upload is retried once
    u = FakeUser(send_errors=[errors.FloodWait(value=0)])
    r = await run(u, msg(T.PHOTO, protected=True, photo=NS(file_size=60, thumbs=None)))
    check("13 FloodWait on send -> retried and succeeded", r == "reuploaded" and u.methods() == ["send_photo"])
    helpers.guard.flood_count = 0

    # 14 ----------------------------------------------------------- FORCE_REUPLOAD flag
    transfer.FORCE_REUPLOAD = True
    u = FakeUser(); r = await run(u, msg(T.PHOTO, photo=NS(file_size=60, thumbs=None)))
    transfer.FORCE_REUPLOAD = False
    check("14 FORCE_REUPLOAD bypasses copy_message", r == "reuploaded" and "copy_message" not in u.methods())

    # 15 ----------------------------------------------------------- weird file names
    check("15 name sanitising", transfer._safe_local_name("../../etc/pa:ss?.txt") == "pa_ss_.txt" and transfer._safe_local_name("a" * 300 + ".mp4").endswith(".mp4") and len(transfer._safe_local_name("a" * 300 + ".mp4").encode()) <= 120 and transfer._safe_local_name("   ") == "")
    u = FakeUser(); await run(u, msg(T.DOCUMENT, protected=True, document=NS(file_size=40, file_name="../../evil/x.sh", thumbs=None)))
    p = u.downloads[0]
    check("15b path traversal impossible", os.path.basename(p) == "x.sh" and os.path.dirname(p).startswith(tempfile.gettempdir()))

    # 16 ----------------------------------------------------------- progress / stage callbacks never break the transfer
    seen = []
    async def stage(t): seen.append(t)
    u = FakeUser(); await run(u, msg(T.VIDEO, protected=True, video=NS(file_size=800, width=1, height=1, duration=1, file_name="v.mp4", thumbs=None)), on_stage=stage)
    check("16 stage callbacks fired (download + upload)", any("Downloading" in s for s in seen) and any("Uploading" in s for s in seen), seen)
    async def bad_stage(t): raise RuntimeError("edit failed")
    u = FakeUser(); r = await run(u, msg(T.PHOTO, protected=True, photo=NS(file_size=50, thumbs=None)), on_stage=bad_stage)
    check("16b a failing status edit does not break the transfer", r == "reuploaded")

    # 17 ----------------------------------------------------------- full handler flow (/save and /batch)
    from plugins import save as S
    class Status:
        def __init__(s): s.texts = []
        async def edit_text(s, t, **_): s.texts.append(t)
    class Cmd:
        def __init__(s, args):
            s.command = ["save"] + args; s.chat = NS(id=TARGET); s.message_thread_id = None; s.status = Status()
        async def reply_text(s, t, **_): s.last = t; return s.status
    class HUser(FakeUser):
        def __init__(s, msgs, **kw): super().__init__(**kw); s.msgs = msgs
        async def get_chat(s, cid): return NS(id=cid)
        async def get_messages(s, chat, mid): return s.msgs[mid]
    helpers._resolved_peers.clear()
    client = NS(userbot=HUser({5: msg(T.VIDEO, protected=True, video=NS(file_size=70, width=1, height=1, duration=1, file_name="v.mp4", thumbs=None))}))
    c = Cmd(["https://t.me/c/3761240705/5"]); c.command[0] = "save"
    await S.save_single(client, c)
    check("17 /save handler completes an upload", "Uploaded successfully" in c.status.texts[-1], c.status.texts)
    msgs = {5: msg(T.PHOTO, photo=NS(file_size=50, thumbs=None)),
            6: msg(T.POLL, protected=True),
            7: msg(T.PHOTO, protected=True, photo=NS(file_size=50, thumbs=None))}
    msgs[5].id, msgs[6].id, msgs[7].id = 5, 6, 7
    client = NS(userbot=HUser(msgs))
    c = Cmd(["https://t.me/c/3761240705/5", "https://t.me/c/3761240705/7"]); c.command[0] = "batch"
    await S.save_batch(client, c)
    rep = c.status.texts[-1]
    check("17b /batch mixed: two uploaded and poll skipped with reason", "Uploaded: `2` / `3`" in rep and "skipped" in rep, rep)
    check("17c no temp files leaked by handlers", leftover_dirs() == [])

    # 18 ----------------------------------------------------------- separate source-reader and destination-bot clients
    reader, poster = FakeUser(), FakeUser()
    dual_msg = msg(T.DOCUMENT, protected=True, document=NS(file_size=120, file_name="group.pdf", thumbs=None))
    result = await copy_or_reupload(reader, SRC, dual_msg, TARGET, sender=poster)
    check("18 dual-client delivery downloads with reader and uploads with poster",
          result == "reuploaded" and bool(reader.downloads) and poster.methods() == ["send_document"] and not reader.methods(),
          (reader.methods(), poster.methods()))
    check("18b dual-client transfer cleans temp files", leftover_dirs() == [])

    print()
    print("ALL TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)

asyncio.run(main())
