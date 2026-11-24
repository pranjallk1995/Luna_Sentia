"""The monitor's database stays under its size limit: past it, the oldest rows of each table go.

Needs a PostgreSQL it may write to, named by MONITOR_TEST_DATABASE_URL; skipped without one."""

import os
import unittest

from app.store import SHRINK_TO, TIMED_TABLES, MonitorStore

URL = os.getenv("MONITOR_TEST_DATABASE_URL", "")


@unittest.skipUnless(URL, "MONITOR_TEST_DATABASE_URL is not set")
class SizeLimitTests(unittest.TestCase):
    def setUp(self):
        self.store = MonitorStore(URL)
        with self.store.connect() as db:
            db.execute("DROP TABLE IF EXISTS gpu_samples, model_calls, model_events")
        self.store.initialize()
        with self.store.connect() as db:
            db.execute("""INSERT INTO gpu_samples (at, gpu_util_avg)
                          SELECT now() - make_interval(secs => n * 5), n FROM generate_series(1, 200000) n""")
            db.execute("""INSERT INTO model_calls (started_at, finished_at, app, endpoint, model, outcome, error)
                          SELECT now() - make_interval(secs => n * 60), now(), 'socia', '/api/chat', 'm', 'ok',
                                 repeat('x', 500) FROM generate_series(1, 20000) n""")
            db.execute("""INSERT INTO model_events (at, model, event)
                          SELECT now() - make_interval(secs => n * 600), 'm', 'load' FROM generate_series(1, 2000) n""")

    def newest_and_count(self):
        with self.store.connect() as db:
            return {table: db.execute(f"SELECT max({column}) AS newest, count(*) AS n FROM {table}").fetchone()
                    for table, column in TIMED_TABLES}

    def test_under_the_limit_nothing_goes(self):
        before = self.newest_and_count()
        self.assertEqual(self.store.purge(3650, 3650, self.store.size() * 2), 0)
        self.assertEqual(self.newest_and_count(), before)

    def test_over_the_limit_the_oldest_go_and_the_disk_gets_it_back(self):
        before, size = self.newest_and_count(), self.store.size()
        limit = size // 2
        removed = self.store.purge(3650, 3650, limit)
        after = self.newest_and_count()
        self.assertGreater(removed, 0)
        self.assertLess(self.store.size(), limit)
        for table, _ in TIMED_TABLES:
            self.assertEqual(after[table]["newest"], before[table]["newest"])
            self.assertLess(after[table]["n"], before[table]["n"] * SHRINK_TO * limit / size + 1)
        self.assertEqual(self.store.purge(3650, 3650, limit), 0)


if __name__ == "__main__":
    unittest.main()
