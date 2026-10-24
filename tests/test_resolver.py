import asyncio

from citecheck.resolver import ResilientResolver

ADDR = "agent1qg70hvphw3e3x3excsqtasn0mjsz2hmqu8q7eeqcmu8pa68rhuu2kpwue43"


class Flaky:
    """Inner resolver that fails for the first `fail` calls."""

    def __init__(self, fail):
        self.fail, self.calls = fail, 0

    async def resolve(self, destination):
        self.calls += 1
        if self.calls <= self.fail:
            return None, []
        return ADDR, ["https://agentverse.ai/v2/agents/mailbox/submit"]


def run(r, dest=ADDR):
    return asyncio.run(r.resolve(dest))


def test_retries_transient_failure():
    inner = Flaky(fail=2)
    assert run(ResilientResolver(inner=inner, attempts=3))[1]
    assert inner.calls == 3


def test_falls_back_to_cached_endpoints():
    inner = Flaky(fail=0)
    r = ResilientResolver(inner=inner, attempts=2)
    good = run(r)
    inner.fail, inner.calls = 99, 0
    assert run(r) == good
    assert inner.calls == 0


def test_invalidated_cache_is_refreshed():
    inner = Flaky(fail=0)
    r = ResilientResolver(inner=inner, attempts=1)
    assert run(r)[1]
    r.invalidate(ADDR)
    assert run(r)[1]
    assert inner.calls == 2


def test_gives_up_without_cache():
    assert run(ResilientResolver(inner=Flaky(fail=99), attempts=2)) == (None, [])


def test_static_endpoint_skips_network():
    inner = Flaky(fail=99)
    r = ResilientResolver(static={ADDR: "http://127.0.0.1:8002/submit"}, inner=inner)
    assert run(r) == (ADDR, ["http://127.0.0.1:8002/submit"]) and inner.calls == 0
