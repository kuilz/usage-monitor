import aiosqlite
from datetime import datetime, timedelta, timezone


ALLOWED_BUCKET_MINUTES = {5, 30, 1440}


def _normalize_bucket_minutes(bucket_minutes: int) -> int:
    return bucket_minutes if bucket_minutes in ALLOWED_BUCKET_MINUTES else 60


def _normalize_timestamp(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_time_slot(slot: str, bucket_minutes: int) -> datetime:
    if bucket_minutes >= 1440:
        return datetime.fromisoformat(slot).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(slot).replace(tzinfo=timezone.utc)


async def get_summary(db: aiosqlite.Connection, hours: int | None = None, since: str | None = None, until: str | None = None) -> dict:
    """Get overall and period stats."""
    where = ""
    params: list = []
    if since:
        where = "WHERE created_at >= ?"
        params = [_normalize_timestamp(since)]
        if until:
            where += " AND created_at < ?"
            params.append(_normalize_timestamp(until))
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
    bucket_minutes = _normalize_bucket_minutes(bucket_minutes)
    where = ""
    params: list = []
    if since:
        where = "WHERE created_at >= ?"
        params = [_normalize_timestamp(since)]
        if until:
            where += " AND created_at < ?"
            params.append(_normalize_timestamp(until))
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
        start = _parse_time_slot(first_key, bucket_minutes)
    else:
        start = now

    # Reference time for end calculation: use `until` if provided, otherwise `now`
    ref = datetime.fromisoformat(until).replace(tzinfo=timezone.utc) if until else now

    if bucket_minutes >= 1440:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        if until:
            end -= timedelta(days=1)
    elif bucket_minutes < 60:
        bucket = max(1, bucket_minutes)
        start = start.replace(minute=(start.minute // bucket) * bucket, second=0, microsecond=0)
        end = ref.replace(minute=(ref.minute // bucket) * bucket, second=0, microsecond=0)
        if until:
            end -= timedelta(minutes=bucket)
    else:
        start = start.replace(minute=0, second=0, microsecond=0)
        end = ref.replace(minute=0, second=0, microsecond=0)
        if until:
            end -= timedelta(hours=1)

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
        params = [_normalize_timestamp(since)]
        if until:
            where += " AND created_at < ?"
            params.append(_normalize_timestamp(until))
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
