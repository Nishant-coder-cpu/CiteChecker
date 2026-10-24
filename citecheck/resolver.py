"""Address resolution that survives transient Almanac API stalls.

The SDK's AlmanacApiResolver gives up after a single 5 s request, so one network
hiccup makes a reply to ASI:One fail with "Unable to resolve destination
endpoint". This wrapper retries, falls back to the last good resolution, and
can pin known local agents (the Verifier) to their endpoint.
"""

import asyncio
import logging
import time

from uagents.resolver import GlobalResolver, Resolver
from uagents_core.identity import parse_identifier

LOGGER = logging.getLogger("citecheck.resolver")


class ResilientResolver(Resolver):
    def __init__(self, static: dict[str, str] | None = None, attempts: int = 3,
                 cache_ttl: float = 3600.0, prefer_cache_ttl: float = 300.0,
                 inner: Resolver | None = None):
        self._inner = inner or GlobalResolver()
        self._static = static or {}
        self._attempts = attempts
        self._cache_ttl = cache_ttl
        self._prefer_cache_ttl = prefer_cache_ttl
        self._cache: dict[str, tuple[float, str, list[str]]] = {}

    def invalidate(self, destination: str) -> None:
        """Forget a cached endpoint after a definite delivery failure."""
        _, _, address = parse_identifier(destination)
        self._cache.pop(address, None)

    async def resolve(self, destination: str) -> tuple[str | None, list[str]]:
        _, _, address = parse_identifier(destination)
        if address in self._static:
            return address, [self._static[address]]

        cached = self._cache.get(address)
        if cached and time.monotonic() - cached[0] < self._prefer_cache_ttl:
            return cached[1], cached[2]

        for attempt in range(self._attempts):
            try:
                resolved, endpoints = await self._inner.resolve(destination)
            except Exception as exc:  # noqa: BLE001 - treat like an empty resolution
                LOGGER.warning(f"resolve attempt {attempt + 1} raised {type(exc).__name__}")
                resolved, endpoints = None, []
            if resolved and endpoints:
                self._cache[address] = (time.monotonic(), resolved, endpoints)
                return resolved, endpoints
            if attempt < self._attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))

        cached = self._cache.get(address)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            LOGGER.warning(f"Almanac lookup failed for {address[:16]}…; using cached endpoints")
            return cached[1], cached[2]
        return None, []
