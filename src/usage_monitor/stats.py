import aiosqlite
from datetime import datetime, timedelta, timezone


async def get_summary(db: aiosqlite.Connection, hours: int | None = None, since: str | None = None, until: str | None = None) -> dict:
    """Get overall and period stats."""
    where = ""
    params: list = []
    if since:
        where = "WHERE created_at >= ?"
        params = [since]
        if until:
            where += " AND created_at < ?"
            params.append(until)
    elif hours:
        where = f"WHERE created_at >= datetime('now', '-{hours} hours', 'utc')"

    async with db.execute(
        f"""SELECT
            COUNT(*) as total_requests,
            COALESCE(SUM(input_tokens), 0) as total_input_tokens,
            COALESCE(SUM(output_tokens), 0) as total_output_tokens,
            COALESCE(SUM(cache_creation_input_tokens), 0) as total_cache_creation,
            COALESCE(SUM(cache_read_input_tokens), 0) as total_cache_read
        FROM requests {where}""",
        params,
    ) as cursor:
        row = await cursor.fetchone()

    return {
        "total_requests": row["total_requests"],
        "total_input_tokens": row["total_input_tokens"],
        "total_output_tokens": row["total_output_tokens"],
        "total_cache_creation": row["total_cache_creation"],
        "total_cache_read": row["total_cache_read"],
    }


async def get_usage_trend(
    db: aiosqlite.Connection,
    hours: int | None = None,
    bucket_minutes: int = 60,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    where = ""
    params: list = []
    if since:
        where = "WHERE created_at >= ?"
        params = [since]
        if until:
            where += " AND created_at < ?"
            params.append(until)
    elif hours:
        where = f"WHERE created_at >= datetime('now', '-{hours} hours', 'utc')"

    if bucket_minutes >= 1440:
        time_expr = "DATE(created_at)"
    elif bucket_minutes < 60:
        bucket = max(1, bucket_minutes)
        time_expr = (
            f"strftime('%Y-%m-%dT%H:', created_at) || "
            f"printf('%02i', (CAST(strftime('%M', created_at) AS INTEGER) / {bucket}) * {bucket}) || ':00'"
        )
    else:
        time_expr = "strftime('%Y-%m-%dT%H:00:00', created_at)"

    async with db.execute(
        f"""SELECT
            {time_expr} as time_slot,
            COUNT(*) as requests,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens
        FROM requests {where}
        GROUP BY time_slot
        ORDER BY time_slot""",
        params,
    ) as cursor:
        rows = await cursor.fetchall()

    data = {}
    for row in rows:
        data[row["time_slot"]] = {
            "time": row["time_slot"],
            "requests": row["requests"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
        }

    # Generate full time range with zero-filled gaps
    now = datetime.now(timezone.utc)
    if since:
        start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    elif hours:
        start = now - timedelta(hours=hours)
    elif data:
        first_key = min(data.keys())
        fmt = "%Y-%m-%d" if bucket_minutes >= 1440 else "%Y-%m-%dT%H:%M:%S"
        start = datetime.strptime(first_key[: len(first_key.rstrip("0:"))], fmt).replace(
            tzinfo=timezone.utc
        )
    else:
        start = now

    # Reference time for end calculation: use `until` if provided, otherwise `now`
    ref = datetime.fromisoformat(until).replace(tzinfo=timezone.utc) if until else now

    if bucket_minutes >= 1440:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        if until:
            end -= timedelta(days=1)
        fmt_slot = "%Y-%m-%d"
    elif bucket_minutes < 60:
        bucket = max(1, bucket_minutes)
        start = start.replace(minute=(start.minute // bucket) * bucket, second=0, microsecond=0)
        end = ref.replace(minute=(ref.minute // bucket) * bucket, second=0, microsecond=0)
        if until:
            end -= timedelta(minutes=bucket)
        fmt_slot = "%Y-%m-%dT%H:" + f"{0:02d}:00"  # placeholder
    else:
        start = start.replace(minute=0, second=0, microsecond=0)
        end = ref.replace(minute=0, second=0, microsecond=0)
        if until:
            end -= timedelta(hours=1)
        fmt_slot = "%Y-%m-%dT%H:00:00"

    result = []
    current = start
    delta = timedelta(minutes=bucket_minutes)
    while current <= end:
        if bucket_minutes >= 1440:
            slot_str = current.strftime("%Y-%m-%d")
        elif bucket_minutes < 60:
            slot_str = f"{current.strftime('%Y-%m-%dT%H:')}{current.minute:02d}:00"
        else:
            slot_str = current.strftime("%Y-%m-%dT%H:00:00")

        result.append(data.get(slot_str, {
            "time": slot_str,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }))
        current += delta

    return result


async def get_by_model(db: aiosqlite.Connection, hours: int | None = None, since: str | None = None, until: str | None = None) -> list[dict]:
    where = ""
    params: list = []
    if since:
        where = "WHERE created_at >= ?"
        params = [since]
        if until:
            where += " AND created_at < ?"
            params.append(until)
    elif hours:
        where = f"WHERE created_at >= datetime('now', '-{hours} hours', 'utc')"

    async with db.execute(
        f"""SELECT
            model,
            COUNT(*) as requests,
            COALESCE(SUM(input_tokens), 0) as input_tokens,
            COALESCE(SUM(output_tokens), 0) as output_tokens
        FROM requests {where}
        GROUP BY model
        ORDER BY requests DESC""",
        params,
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
