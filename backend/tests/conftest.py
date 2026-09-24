"""
conftest.py — shared pytest fixtures for the backend test suite.

WHY THIS FILE EXISTS
--------------------
`httpx.ASGITransport` sends HTTP scopes to the app but does NOT run ASGI
*lifespan* events. FastAPI loads the ML model in its `lifespan` context
manager, so under a bare ASGITransport the model is never loaded, `_state`
stays empty, and every real endpoint returns 503 Service Unavailable.

The failure is deceptive: tests that assert 422 still pass (Pydantic rejects
the body before the endpoint body runs), so the suite looks half-healthy while
nothing is actually exercising the model.

The fix is to enter `app.router.lifespan_context(app)` around the client, which
runs startup and shutdown exactly as Uvicorn would — no extra dependency needed.
"""
from __future__ import annotations

from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run the async tests on asyncio only (no trio dependency)."""
    return "asyncio"


@pytest.fixture(scope="module")
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async test client with the application lifespan actually running.

    Module-scoped so the model loads once per test module rather than once per
    test — loading is ~1 s, which would otherwise dominate the suite runtime.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
