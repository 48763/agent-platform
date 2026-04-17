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
        return await client.get_entity(chat_id)
    except (ValueError, TypeError):
        pass

    # Plain username
    return await client.get_entity(identifier)
