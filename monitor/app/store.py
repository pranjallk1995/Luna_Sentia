"""The monitor's tables in its own database, lunamonitor, on Sentia's PostgreSQL.

- ``gpu_samples``: one row per five seconds, with averages and peaks.
- ``model_calls``: one row per model call, with its timings and size; never its content.
- ``model_events``: when Ollama loaded or unloaded a model.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.calls import Call

SCHEMA = """
CREATE TABLE IF NOT EXISTS gpu_samples (
    at timestamptz PRIMARY KEY,
    gpu_util_avg real, gpu_util_max real, mem_util_avg real,
    vram_used_mb_max real, vram_total_mb real, ollama_vram_mb_max real,
    power_w_avg real, temp_c_max real, sm_clock_mhz_avg real, mem_clock_mhz_avg real, fan_pct_avg real,
    in_flight_max integer, waiting_max integer, busy_pct real
);
CREATE TABLE IF NOT EXISTS model_calls (
    id bigserial PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    app text NOT NULL, endpoint text NOT NULL, model text NOT NULL,
    status integer, outcome text NOT NULL, streamed boolean, structured boolean,
    images integer, num_ctx integer, request_bytes integer, response_bytes integer, ahead integer,
    wall_ms real, ttfb_ms real, wait_ms real, load_ms real, prompt_ms real, output_ms real,
    prompt_tokens integer, output_tokens integer, error text
);
CREATE INDEX IF NOT EXISTS model_calls_started ON model_calls (started_at);
CREATE TABLE IF NOT EXISTS model_events (
    at timestamptz NOT NULL, model text NOT NULL, event text NOT NULL, vram_mb real, context integer
);
CREATE INDEX IF NOT EXISTS model_events_at ON model_events (at);
"""

SAMPLE_COLUMNS = (
    "gpu_util_avg", "gpu_util_max", "mem_util_avg", "vram_used_mb_max", "vram_total_mb", "ollama_vram_mb_max",
    "power_w_avg", "temp_c_max", "sm_clock_mhz_avg", "mem_clock_mhz_avg", "fan_pct_avg",
    "in_flight_max", "waiting_max", "busy_pct",
)
CALL_COLUMNS = (
    "app", "endpoint", "model", "status", "outcome", "streamed", "structured", "images", "num_ctx",
    "request_bytes", "response_bytes", "ahead", "wall_ms", "ttfb_ms", "wait_ms", "load_ms", "prompt_ms",
    "output_ms", "prompt_tokens", "output_tokens", "error",
)
# How a history range is bucketed: about 360 points whatever its length.
BUCKETS = ((3600, 10), (6 * 3600, 60), (24 * 3600, 240), (7 * 86400, 1800), (31 * 86400, 7200))
CALL_ROWS_LIMIT = 5000


def plain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows the page can read as JSON: PostgreSQL gives avg() of an integer column as numeric."""
    return [{key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()} for row in rows]


def bucket_seconds(span: int) -> int:
    for limit, size in BUCKETS:
        if span <= limit:
            return size
    return 21600


class MonitorStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute(SCHEMA)

    def write(self, calls: list[Call], samples: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        with self.connect() as db, db.cursor() as cursor:
            if samples:
                cursor.executemany(
                    f"INSERT INTO gpu_samples (at, {', '.join(SAMPLE_COLUMNS)}) "
                    f"VALUES (to_timestamp(%s), {', '.join(['%s'] * len(SAMPLE_COLUMNS))}) ON CONFLICT (at) DO NOTHING",
                    [(row["at"], *(row.get(key) for key in SAMPLE_COLUMNS)) for row in samples],
                )
            if calls:
                cursor.executemany(
                    f"INSERT INTO model_calls (started_at, finished_at, {', '.join(CALL_COLUMNS)}) "
                    f"VALUES (to_timestamp(%s), to_timestamp(%s), {', '.join(['%s'] * len(CALL_COLUMNS))})",
                    [(call.started_at, call.finished_at, *(getattr(call, key) for key in CALL_COLUMNS)) for call in calls],
                )
            if events:
                cursor.executemany(
                    "INSERT INTO model_events (at, model, event, vram_mb, context) VALUES (to_timestamp(%s), %s, %s, %s, %s)",
                    [(e["at"], e["model"], e["event"], e.get("vram_mb"), e.get("context")) for e in events],
                )

    def purge(self, sample_days: int, call_days: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM gpu_samples WHERE at < now() - make_interval(days => %s)", (sample_days,))
            db.execute("DELETE FROM model_calls WHERE started_at < now() - make_interval(days => %s)", (call_days,))
            db.execute("DELETE FROM model_events WHERE at < now() - make_interval(days => %s)", (call_days,))

    def history(self, start: float, end: float) -> dict[str, Any]:
        size = bucket_seconds(int(end - start))
        window = (start, end)
        with self.connect() as db:
            samples = db.execute(
                """SELECT extract(epoch FROM date_bin(make_interval(secs => %s), at, timestamptz 'epoch'))::float AS at,
                          avg(gpu_util_avg) AS gpu_util, max(gpu_util_max) AS gpu_util_max, avg(mem_util_avg) AS mem_util,
                          max(vram_used_mb_max) AS vram_used_mb, max(vram_total_mb) AS vram_total_mb,
                          max(ollama_vram_mb_max) AS ollama_vram_mb, avg(power_w_avg) AS power_w, max(temp_c_max) AS temp_c,
                          avg(sm_clock_mhz_avg) AS sm_clock_mhz, max(in_flight_max) AS in_flight,
                          max(waiting_max) AS waiting, avg(busy_pct) AS busy_pct
                   FROM gpu_samples WHERE at >= to_timestamp(%s) AND at < to_timestamp(%s)
                   GROUP BY 1 ORDER BY 1""",
                (size, *window),
            ).fetchall()
            calls = db.execute(
                f"""SELECT extract(epoch FROM started_at)::float AS started_at, extract(epoch FROM finished_at)::float AS finished_at,
                           {', '.join(CALL_COLUMNS)}
                    FROM model_calls WHERE started_at >= to_timestamp(%s) AND started_at < to_timestamp(%s)
                    ORDER BY started_at DESC LIMIT %s""",
                (*window, CALL_ROWS_LIMIT),
            ).fetchall()
            total = db.execute(
                "SELECT count(*) AS n FROM model_calls WHERE started_at >= to_timestamp(%s) AND started_at < to_timestamp(%s)",
                window,
            ).fetchone()["n"]
            groups = db.execute(
                """SELECT app, model, count(*) AS calls,
                          count(*) FILTER (WHERE outcome <> 'ok') AS problems,
                          avg(wall_ms) AS wall_avg,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY wall_ms) AS wall_p50,
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY wall_ms) AS wall_p95,
                          sum(wall_ms) AS wall_sum, sum(wait_ms) AS wait_sum, sum(load_ms) AS load_sum,
                          sum(prompt_ms) AS prompt_ms, sum(prompt_tokens) AS prompt_tokens,
                          sum(output_ms) AS output_ms, sum(output_tokens) AS output_tokens,
                          avg(images) AS images_avg, max(num_ctx) AS num_ctx_max
                   FROM model_calls WHERE started_at >= to_timestamp(%s) AND started_at < to_timestamp(%s)
                   GROUP BY app, model ORDER BY count(*) DESC""",
                window,
            ).fetchall()
            intervals = db.execute(
                """SELECT extract(epoch FROM started_at)::float AS started_at, extract(epoch FROM finished_at)::float AS finished_at, model
                   FROM model_calls WHERE finished_at >= to_timestamp(%s) AND started_at < to_timestamp(%s)""",
                window,
            ).fetchall()
            events = db.execute(
                """SELECT extract(epoch FROM at)::float AS at, model, event, vram_mb, context FROM model_events
                   WHERE at >= to_timestamp(%s) AND at < to_timestamp(%s) ORDER BY at""",
                window,
            ).fetchall()
        return {"bucket_seconds": size, "samples": plain(samples), "calls": plain(calls), "calls_total": total,
                "groups": plain(groups), "intervals": plain(intervals), "events": plain(events)}
