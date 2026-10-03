from types import SimpleNamespace

from app.bot.main import WhitelistMiddleware


async def test_whitelist_blocks_strangers():
    calls = []

    async def handler(event, data):
        calls.append(data["event_from_user"].id)
        return "handled"

    mw = WhitelistMiddleware({111})
    assert await mw(handler, object(), {"event_from_user": SimpleNamespace(id=222)}) is None
    assert await mw(handler, object(), {}) is None
    assert await mw(handler, object(), {"event_from_user": SimpleNamespace(id=111)}) == "handled"
    assert calls == [111]
