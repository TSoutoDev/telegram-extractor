from fastapi import FastAPI, Header, HTTPException
from telethon import TelegramClient
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import ChannelParticipantsSearch
import os

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PHONE = os.environ["PHONE"]
SECRET_KEY = os.environ.get("API_KEY", "chave-secreta")

app = FastAPI()
client = TelegramClient("session", API_ID, API_HASH)

@app.on_event("startup")
async def startup():
    await client.start(phone=PHONE)

def check_key(x_api_key: str = Header(...)):
    if x_api_key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/groups")
async def get_groups(x_api_key: str = Header(...)):
    check_key(x_api_key)
    dialogs = await client.get_dialogs()
    groups = [
        {"id": str(d.entity.id), "name": d.name, "members": getattr(d.entity, "participants_count", 0)}
        for d in dialogs if d.is_group or d.is_channel
    ]
    return groups

@app.get("/members")
async def get_members(group_id: str, x_api_key: str = Header(...)):
    check_key(x_api_key)
    entity = await client.get_entity(int(group_id))
    all_participants = []
    offset = 0
    limit = 200
    while True:
        participants = await client(GetParticipantsRequest(
            entity, ChannelParticipantsSearch(""), offset, limit, hash=0
        ))
        if not participants.users:
            break
        all_participants.extend(participants.users)
        offset += len(participants.users)
        if offset >= participants.count:
            break
    members = [
        {"id": str(u.id), "name": (u.first_name or "") + " " + (u.last_name or ""), "username": u.username or "", "phone": u.phone or ""}
        for u in all_participants if not u.bot
    ]
    return members
