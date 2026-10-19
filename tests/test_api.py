import asyncio

import asyncpg
import httpx

from api import main


def get(path):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://test") as client:
            return await client.get(path)
    return asyncio.run(request())


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False
        self.arguments = None

    async def fetch(self, query, *parameters):
        self.arguments = (query, parameters)
        return self.rows

    def terminate(self):
        self.closed = True


def test_health_does_not_connect(monkeypatch):
    async def forbidden(**kwargs):
        raise AssertionError("healthz no debe conectarse a PostgreSQL")
    monkeypatch.setattr(main.asyncpg, "connect", forbidden)
    assert get("/healthz").json() == {"status": "ok"}
    assert get("/products?limit=0").status_code == 422
    assert get("/products?limit=1;DROP TABLE products").status_code == 422


def test_failure_then_recovery_and_no_cache(monkeypatch):
    connections = []
    attempts = 0

    async def connect(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncpg.CannotConnectNowError("secret that must not leak")
        conn = FakeConnection([{"id": 1, "name": f"value {attempts}", "price": "12.50", "stock": 3}])
        connections.append(conn)
        return conn

    monkeypatch.setattr(main.asyncpg, "connect", connect)
    failure = get("/products")
    assert failure.status_code == 503
    assert "secret" not in failure.text
    assert get("/products?limit=1").json()[0]["name"] == "value 2"
    assert get("/products?limit=1").json()[0]["name"] == "value 3"
    assert attempts == 3
    assert all(c.closed for c in connections)
    assert connections[0].arguments[1] == (1,)
    assert "$1" in connections[0].arguments[0]


def test_total_timeout_and_health_during_wait(monkeypatch):
    async def slow_connect(**kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(main.asyncpg, "connect", slow_connect)
    monkeypatch.setattr(main, "TOTAL_TIMEOUT", 0.05)
    async def concurrent_requests():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://test") as client:
            pending = asyncio.create_task(client.get("/products"))
            await asyncio.sleep(0.01)
            assert (await client.get("/healthz")).status_code == 200
            assert not pending.done()
            assert (await pending).status_code == 503
    asyncio.run(concurrent_requests())


def test_query_timeout_closes_connection(monkeypatch):
    conn = FakeConnection([])

    async def slow_query(*args):
        await asyncio.sleep(10)

    async def connect(**kwargs):
        return conn

    conn.fetch = slow_query
    monkeypatch.setattr(main.asyncpg, "connect", connect)
    monkeypatch.setattr(main, "TOTAL_TIMEOUT", 0.05)
    assert get("/products").status_code == 503
    assert conn.closed
