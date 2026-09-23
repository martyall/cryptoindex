from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

Pool = AsyncConnectionPool[AsyncConnection]


async def open_pool(dsn: str, max_size: int) -> Pool:
    """Open a pool and wait until it holds a live connection, so a bad DSN or a
    missing database fails at startup rather than on first use. Connections
    exchange pgvector values as numpy arrays."""
    pool: Pool = AsyncConnectionPool(
        dsn, min_size=1, max_size=max_size, open=False, configure=register_vector_async
    )
    await pool.open(wait=True, timeout=10)
    return pool
