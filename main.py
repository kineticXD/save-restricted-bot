# main.py
import os
import logging
import asyncio
from threading import Thread
from flask import Flask

# --- Apply the peer ID fix BEFORE anything else from pyrogram is imported ---
from plugins.peer_fix import apply_fix
apply_fix()
# --------------------------------------------------------------------------

from pyrogram import Client, idle
from config import API_ID, API_HASH, BOT_TOKEN, SESSION_STRING

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ... rest of the file remains the same
