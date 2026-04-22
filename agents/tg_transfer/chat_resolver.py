import re
import logging
from telethon import TelegramClient
from telethon.tl.functions.messages import CheckChatInviteRequest

logger = logging.getLogger(__name__)

_INVITE_RE = re.compile(r"https?://t\.me/\+([A-Za-z0-9_-]+)")
_USERNAME_RE = re.compile(r"@([A-Za-z_]\w+)")


async def resolve_chat(client: TelegramClient, identifier: str):
    """Resolve a chat identifier to a Telethon entity.

    Supports:
    - @username
    - https://t.me/+INVITE_HASH (invite link)
    - username (plain text)
    - integer chat_id
    """
    identifier = identifier.strip()

    # Invite link
    m = _INVITE_RE.search(identifier)
    if m:
        invite_hash = m.group(1)
        invite = await client(CheckChatInviteRequest(invite_hash))
        if hasattr(invite, "chat"):
            return invite.chat
        if hasattr(invite, "already_joined"):
            updates = await client(
                __import__("telethon.tl.functions.messages", fromlist=["ImportChatInviteRequest"])
                .ImportChatInviteRequest(invite_hash)
            )
            return updates.chats[0]
        raise ValueError(f"Cannot resolve invite link: {identifier}")

    # @username
    m = _USERNAME_RE.match(identifier)
    if m:
        return await client.get_entity(m.group(1))

    # Integer chat_id
    try:
        chat_id = int(identifier)
    except (ValueError, TypeError):
        chat_id = None

    if chat_id is not None:
        return await _resolve_int_id(client, chat_id)

    # Plain username
    return await client.get_entity(identifier)


async def _resolve_int_id(client: TelegramClient, chat_id: int):
    """Resolve a numeric ID into an entity, with fallbacks for cases where
    `get_entity(int)` fails — most commonly a deleted user whose username is
    gone but whose DM dialog still exists.

    Cascade:
      1. `get_entity(int)` — works for entities already in Telethon's cache.
      2. `get_input_entity(int)` then `get_entity(input)` — pulls access_hash
         from the local session DB; covers users we've previously interacted
         with even if they're no longer resolvable by username.
      3. Scan `iter_dialogs()` — last resort. A deleted account's dialog still
         appears as "Deleted Account" with the original peer ID, so we can
         pick it up here. Compare against both dialog.id (TG-marked form,
         e.g. -100… for channels) and entity.id (unmarked) to handle whichever
         form the user typed.
    """
    try:
        return await client.get_entity(chat_id)
    except (ValueError, TypeError):
        pass

    try:
        input_entity = await client.get_input_entity(chat_id)
        return await client.get_entity(input_entity)
    except (ValueError, TypeError):
        pass

    async for dialog in client.iter_dialogs():
        if dialog.id == chat_id or getattr(dialog.entity, "id", None) == chat_id:
            return dialog.entity

    raise ValueError(
        f"Cannot find any entity corresponding to {chat_id} "
        "（不在 cache、session DB 也沒記錄、dialogs 也找不到）"
    )
