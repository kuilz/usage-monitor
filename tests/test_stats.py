from datetime import datetime, timezone

import pytest

from usage_monitor import stats


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)


@pytest.mark.asyncio
class TestStats:
    async def test_all_trend_starts_from_actual_first_day(self, db, monkeypatch):
        monkeypatch.setattr(stats, "datetime", FrozenDateTime)

        await db.execute(
            """INSERT INTO requests
               (message_id, model, is_streaming, input_tokens, output_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_1", "claude-sonnet-4-20250514", 0, 10, 20, "2026-04-20 10:00:00"),
        )
        await db.execute(
            """INSERT INTO requests
               (message_id, model, is_streaming, input_tokens, output_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_2", "claude-sonnet-4-20250514", 0, 30, 40, "2026-04-26 10:00:00"),
        )
        await db.commit()

        trend = await stats.get_usage_trend(db, bucket_minutes=1440)

        assert trend[0]["time"] == "2026-04-20"
        assert trend[0]["requests"] == 1
        assert trend[0]["input_tokens"] == 10
        assert trend[-1]["time"] == "2026-04-26"

    async def test_30_minute_bucket_aggregates_correctly(self, db):
        await db.execute(
            """INSERT INTO requests
               (message_id, model, is_streaming, input_tokens, output_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_1", "claude-sonnet-4-20250514", 0, 10, 20, "2026-04-26 00:10:00"),
        )
        await db.execute(
            """INSERT INTO requests
               (message_id, model, is_streaming, input_tokens, output_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("msg_2", "claude-sonnet-4-20250514", 0, 30, 40, "2026-04-26 00:35:00"),
        )
        await db.commit()

        trend = await stats.get_usage_trend(
            db,
            since="2026-04-26T00:00:00",
            until="2026-04-26T01:00:00",
            bucket_minutes=30,
        )

        assert trend == [
            {
                "time": "2026-04-26T00:00:00",
                "requests": 1,
                "input_tokens": 10,
                "output_tokens": 20,
            },
            {
                "time": "2026-04-26T00:30:00",
                "requests": 1,
                "input_tokens": 30,
                "output_tokens": 40,
            },
        ]
