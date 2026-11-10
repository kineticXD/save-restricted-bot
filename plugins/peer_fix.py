# plugins/peer_fix.py
from pyrogram import utils

def get_peer_type_new(peer_id: int) -> str:
    """Correctly determines the peer type, fixing a common Pyrogram bug."""
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    elif peer_id_str.startswith("-100"):
        return "channel"
    else:
        return "chat"

def apply_fix():
    """Applies the monkey patch to the pyrogram.utils module."""
    utils.get_peer_type = get_peer_type_new
