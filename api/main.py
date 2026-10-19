"""Una consulta real y una conexión nueva por solicitud; sin caché ni reintentos."""

import asyncio
import os
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

load_dotenv()


def bounded_float(name: str, default: str, low: float, high: float) -> float:
    value = float(os.getenv(name, default))
    if not low <= value <= high:
        raise ValueError(f"{name} debe estar entre {low} y {high}")
    return value


DB_HOST = os.getenv("DB_HOST", "10.77.0.2")
if DB_HOST != "10.77.0.2":
    raise ValueError("En esta topología DB_HOST debe ser la IP del túnel 10.77.0.2")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
if DB_PORT != 5432:
    raise ValueError("En esta topología DB_PORT debe ser 5432")
CONNECT_TIMEOUT = bounded_float("DB_CONNECT_TIMEOUT_S", "2", 0.1, 3)
QUERY_TIMEOUT = bounded_float("DB_QUERY_TIMEOUT_S", "1", 0.1, 3)
TOTAL_TIMEOUT = bounded_float("DB_TOTAL_TIMEOUT_S", "3", 0.2, 3.5)

app = FastAPI(title="Cloud Hybrid VPN PoC", version="1.0.0")


class Product(BaseModel):
    id: int
    name: str
    price: Decimal
    stock: int


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/products", response_model=list[Product])
async def products(limit: int = Query(default=10, ge=1, le=100)):
    connection = None
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            connection = await asyncpg.connect(
                host=DB_HOST,
                port=DB_PORT,
                database=os.getenv("DB_NAME", "hybrid_poc"),
                user=os.getenv("DB_USER", "poc_reader"),
                password=os.environ.get("DB_PASSWORD", ""),
                timeout=CONNECT_TIMEOUT,
                command_timeout=QUERY_TIMEOUT,
                ssl=False,  # El transporte se cifra mediante WireGuard.
                statement_cache_size=0,
                server_settings={
                    "application_name": "cloud-hybrid-vpn-poc",
                    "statement_timeout": str(int(QUERY_TIMEOUT * 1000)),
                },
            )
            rows = await connection.fetch(
                "SELECT id, name, price, stock FROM public.products ORDER BY id LIMIT $1",
                limit,
            )
            return [dict(row) for row in rows]
    except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
        raise HTTPException(status_code=503, detail="Base de datos no disponible") from None
    finally:
        if connection is not None:
            # No esperamos un intercambio adicional por una conexión interrumpida.
            connection.terminate()
