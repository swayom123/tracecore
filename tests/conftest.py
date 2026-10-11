from collections.abc import AsyncGenerator, Generator

import anyio
import httpx
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tracecore import api as api_module
from tracecore import models  # noqa: F401
from tracecore.api import app
from tracecore.database import Base, get_session


class ASGITestClient:
    def __init__(self) -> None:
        self.transport = ASGITransport(app=app)

    def request(self, method: str, url: str, **kwargs: object) -> Response:
        async def send() -> Response:
            async with AsyncClient(transport=self.transport, base_url="http://test") as client:
                return await client.request(method, url, **kwargs)

        return anyio.run(send)

    def get(self, url: str, **kwargs: object) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> Response:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: object) -> Response:
        return self.request("PATCH", url, **kwargs)


class SyncASGITransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request_body = request.read()

        async def send() -> httpx.Response:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.request(
                    request.method,
                    str(request.url),
                    headers=dict(request.headers),
                    content=request_body,
                )
                return httpx.Response(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=response.content,
                    request=request,
                )

        return anyio.run(send)


@pytest.fixture
def client() -> Generator[ASGITestClient, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    TestingSession = sessionmaker(bind=test_engine, expire_on_commit=False)
    original_session_factory = api_module.SessionLocal
    api_module.SessionLocal = TestingSession

    async def override_session(request: Request) -> AsyncGenerator[Session, None]:
        session = TestingSession()
        principal = getattr(request.state, "principal", None)
        if principal is not None:
            session.info["principal"] = principal
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override_session
    yield ASGITestClient()
    app.dependency_overrides.clear()
    api_module.SessionLocal = original_session_factory
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()
