"""API keys: issued once, stored as a hash, and counted."""

from __future__ import annotations

import pytest

from app.security import (
    KeyLimiter,
    create_key,
    hash_key,
    list_keys,
    revoke_key,
    verify_key,
)


@pytest.mark.asyncio
async def test_a_key_is_returned_once_and_stored_as_a_hash(client):
    """A leaked database must not be a leaked set of keys."""
    key, record = await create_key(client, "partner one")

    assert key and key.startswith(record.prefix)
    stored = await client.execute("SELECT key_hash FROM api_keys WHERE key_id = ?", (record.key_id,))
    assert stored.scalar(0) == hash_key(key)
    assert key != stored.scalar(0)


@pytest.mark.asyncio
async def test_a_valid_key_is_recognised_and_an_unknown_one_is_not(client):
    key, _record = await create_key(client, "partner two")

    assert (await verify_key(client, key)) is not None
    assert (await verify_key(client, "not-a-key")) is None
    assert (await verify_key(client, "")) is None


@pytest.mark.asyncio
async def test_a_revoked_key_stops_working_but_stays_on_record(client):
    """Revoking keeps the history of what the key spent."""
    key, record = await create_key(client, "partner three")

    assert await revoke_key(client, record.key_id) is True
    found = await verify_key(client, key)
    assert found is not None and found.revoked is True
    assert await revoke_key(client, record.key_id) is False
    assert [entry.key_id for entry in await list_keys(client)] == [record.key_id]


@pytest.mark.asyncio
async def test_calls_are_counted_per_key(client):
    from app.security import record_call

    _key, record = await create_key(client, "partner four")
    await record_call(client, record.key_id)
    await record_call(client, record.key_id)

    found = await verify_key(client, _key)
    assert found.calls_total == 2
    assert found.calls_today == 2


def test_the_per_minute_limit_refuses_the_request_after_the_allowance():
    clock = [0.0]
    limiter = KeyLimiter(now=lambda: clock[0])

    for _ in range(3):
        allowed, _remaining = limiter.allow("k1", 3)
        assert allowed is True

    allowed, remaining = limiter.allow("k1", 3)
    assert allowed is False and remaining == 0

    clock[0] = 61.0
    allowed, _remaining = limiter.allow("k1", 3)
    assert allowed is True


def test_one_key_spending_does_not_affect_another():
    limiter = KeyLimiter(now=lambda: 0.0)
    for _ in range(3):
        limiter.allow("busy", 3)

    allowed, _remaining = limiter.allow("quiet", 3)
    assert allowed is True
