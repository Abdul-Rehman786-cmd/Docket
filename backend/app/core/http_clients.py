"""Lifespan-owned pools, scoped to the current application/request context.

Clients have no default credentials: every operation supplies its current settings.
This permits credential reloads without retaining an obsolete authenticated client.
"""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import wraps

import httpx


class HTTPClients:
    def __init__(self):
        options = dict(
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            timeout=httpx.Timeout(20, connect=5, pool=5),
            trust_env=False,
            follow_redirects=False,
        )
        self.database = httpx.AsyncClient(**options)
        self.switch = httpx.AsyncClient(**options)

    async def close(self):
        try:
            await self.database.aclose()
        finally:
            await self.switch.aclose()


_current: ContextVar[HTTPClients | None] = ContextVar("http_clients", default=None)


@contextmanager
def use_http_clients(clients):
    token = _current.set(clients)
    try:
        yield
    finally:
        _current.reset(token)


@asynccontextmanager
async def service_client(service: str):
    clients = _current.get()
    if clients is not None:
        yield getattr(clients, service)
    else:
        # Standalone scripts/tests own and close their temporary client.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5, pool=5),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            trust_env=False, follow_redirects=False,
        ) as client:
            yield client


def database_client(function):
    """Inject one pooled client through an entire repository operation."""
    @wraps(function)
    async def wrapped(*args, client=None, **kwargs):
        if client is not None:
            return await function(*args, client=client, **kwargs)
        async with service_client("database") as active:
            return await function(*args, client=active, **kwargs)
    return wrapped
