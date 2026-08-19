import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

api_id = int(os.environ["API_ID"])
api_hash = os.environ["API_HASH"]

client = TelegramClient(
    "telethon_session",
    api_id,
    api_hash,
)

session_string = StringSession.save(client.session)

print(session_string)