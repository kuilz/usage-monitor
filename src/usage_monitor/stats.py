import aiosqlite


async def get_summary(db: aiosqlite.Connection, days: int | None = None) -> dict:
    """Get overall and period stats."""
    where = ""
    if days:
        where = f"WHERE created_at >= datetime('now', '-{days} days', 'utc')"

    async with db.execute(
        f"""SELECT
            COUNT(*) as total_requests,
            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
            COALESCE(SUM(cache_creation_input_tokens), 0) as total_cache_creation,
            COALESCE(SUM(cache_read_input_tokens), 0) as total_cache_read
        FROM requests {where}"""
    ) as cursor:
        row = await cursor.fetchone()

    return {
        "total_requests": row["total_requests"],
        "total_input_tokens": row["total_input_tokens"],
        "total_output_tokens": row["total_output_tokens"],
        "total_cache_creation": row["total_cache_creation"],
        "total_cache_read": row["total_cache_read"],
    }


async def get_daily(db: aiosqlite.Connection, days: int = 30) -> list[dict]:
    async with db.execute(
        f"""SELECT
            DATE(created_at) as date,
            COUNT(*) as requests,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens
        FROM requests
        WHERE created_at >= datetime('now', '-{days} days', 'utc')
        GROUP BY DATE(created_at)
        ORDER BY date"""
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "date": row["date"],
            "requests": row["requests"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
        }
        for row in rows
    ]


async def get_by_model(db: aiosqlite.Connection, days: int | None = None) -> list[dict]:
    where = ""
    if days:
        where = f"WHERE created_at >= datetime('now', '-{days} days', 'utc')"

    async with db.execute(
        f"""SELECT
            model,
            COUNT(*) as requests,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens
        FROM requests {where}
        GROUP BY model
        ORDER BY requests DESC"""
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "model": row["model"],
            "requests": row["requests"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
        }
        for row in rows
    ]


async def get_recent(db: aiosqlite.Connection, limit: int = 50) -> list[dict]:
    async with db.execute(
        """SELECT * FROM requests ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ) as cursor:
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]
