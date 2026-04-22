"""Tests for chat_resolver, focused on the deleted-user fallback path.

A user can paste a numeric ID like `8476404382` for someone who has since
deleted their TG account — `get_entity(int)` blows up because the user is
no longer resolvable by username and isn't in cache. The resolver now
cascades to session-DB lookup and finally a dialog scan, which is where
deleted accounts still show up as "Deleted Account" dialogs.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.tg_transfer.chat_resolver import resolve_chat


def _async_iter(items):
    """Wrap a list as an async iterator (Telethon's iter_dialogs returns
    one). MagicMock doesn't supply __aiter__ by default."""
    async def gen():
        for it in items:
            yield it
    return gen()


@pytest.mark.asyncio
async def test_int_id_cache_hit():
    """Common case: ID is in Telethon's entity cache → first call wins,
    nothing else is consulted."""
    client = MagicMock()
    expected = MagicMock(name="entity")
    client.get_entity = AsyncMock(return_value=expected)
    client.get_input_entity = AsyncMock()
    client.iter_dialogs = MagicMock()

    result = await resolve_chat(client, "8476404382")

    assert result is expected
    client.get_entity.assert_awaited_once_with(8476404382)
    client.get_input_entity.assert_not_awaited()
    client.iter_dialogs.assert_not_called()


@pytest.mark.asyncio
async def test_int_id_falls_back_to_session_db():
    """get_entity blows up but session DB still knows access_hash → second
    pass via get_input_entity succeeds."""
    client = MagicMock()
    expected = MagicMock(name="entity")
    input_entity = MagicMock(name="input_entity")
    client.get_entity = AsyncMock(side_effect=[
        ValueError("not in cache"),
        expected,
    ])
    client.get_input_entity = AsyncMock(return_value=input_entity)
    client.iter_dialogs = MagicMock()

    result = await resolve_chat(client, "8476404382")

    assert result is expected
    # First get_entity tried with the int, second with the input wrapper.
    assert client.get_entity.await_args_list[0].args == (8476404382,)
    assert client.get_entity.await_args_list[1].args == (input_entity,)
    client.iter_dialogs.assert_not_called()


@pytest.mark.asyncio
async def test_int_id_falls_back_to_dialog_scan():
    """Both get_entity and get_input_entity fail (user is fully gone from
    cache and session) — last resort scans dialogs and matches by dialog.id.
    Deleted accounts still appear in dialogs with the original peer ID."""
    client = MagicMock()
    target = MagicMock(name="deleted_user_dialog")
    target.id = 8476404382
    target.entity = MagicMock(id=8476404382)
    other = MagicMock(name="other")
    other.id = 1111
    other.entity = MagicMock(id=1111)

    client.get_entity = AsyncMock(side_effect=ValueError("nope"))
    client.get_input_entity = AsyncMock(side_effect=ValueError("nope"))
    client.iter_dialogs = MagicMock(return_value=_async_iter([other, target]))

    result = await resolve_chat(client, "8476404382")

    assert result is target.entity


@pytest.mark.asyncio
async def test_int_id_dialog_scan_matches_unmarked_id():
    """User pastes the unmarked channel ID (e.g. `1234567890`) but Telethon's
    dialog.id stores the marked form (`-1001234567890`). entity.id holds the
    unmarked one — match against both so either input form works."""
    client = MagicMock()
    target = MagicMock(name="channel_dialog")
    target.id = -1001234567890
    target.entity = MagicMock(id=1234567890)
    client.get_entity = AsyncMock(side_effect=ValueError("nope"))
    client.get_input_entity = AsyncMock(side_effect=ValueError("nope"))
    client.iter_dialogs = MagicMock(return_value=_async_iter([target]))

    result = await resolve_chat(client, "1234567890")

    assert result is target.entity


@pytest.mark.asyncio
async def test_int_id_all_fallbacks_exhausted_raises():
    """If even the dialog scan finds nothing, raise — caller surfaces the
    error to the user instead of silently dropping the request."""
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("nope"))
    client.get_input_entity = AsyncMock(side_effect=ValueError("nope"))
    client.iter_dialogs = MagicMock(return_value=_async_iter([]))

    with pytest.raises(ValueError, match="8476404382"):
        await resolve_chat(client, "8476404382")
