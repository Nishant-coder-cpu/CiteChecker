"""Network policy for the agent processes.

Measured on the IIT Mandi deployment: IPv6 to Agentverse (Cloudflare) never
connects (no IPv6 route), and about a third of IPv4 connection attempts stall.
uAgents opens its aiohttp sessions (mailbox polling, message delivery) without a
connect timeout, so one stalled attempt delays a reply by up to ~2 minutes.

apply() makes every aiohttp session default to IPv4 and a short connect timeout,
so a stalled attempt fails fast and the mailbox simply polls again. Sessions that
pass their own timeout/connector are left untouched.
"""

import os
import socket

import aiohttp

CONNECT_TIMEOUT = float(os.getenv("NET_CONNECT_TIMEOUT", "4"))
TOTAL_TIMEOUT = float(os.getenv("NET_TOTAL_TIMEOUT", "90"))

_applied = False


def apply() -> None:
    global _applied
    if _applied or os.getenv("NET_POLICY", "on") == "off":
        return
    original_init = aiohttp.ClientSession.__init__

    def patched_init(self, *args, **kwargs):
        if "connector" not in kwargs:
            kwargs["connector"] = aiohttp.TCPConnector(family=socket.AF_INET)
        if kwargs.get("timeout") in (None, aiohttp.helpers.sentinel):
            kwargs["timeout"] = aiohttp.ClientTimeout(
                total=TOTAL_TIMEOUT, sock_connect=CONNECT_TIMEOUT
            )
        original_init(self, *args, **kwargs)

    aiohttp.ClientSession.__init__ = patched_init
    _applied = True
