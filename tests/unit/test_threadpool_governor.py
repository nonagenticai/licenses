"""Both database-facing thread pools must be BOUNDED, and bounded in the loop.

Moving 33 handlers off the event loop replaces one failure mode (head-of-line
blocking) with a different one if the pools are left at their defaults:

  * sync ``def`` handlers -> ``starlette.run_in_threadpool`` -> anyio's default
    ``CapacityLimiter``, **40** tokens;
  * ``asyncio.to_thread`` -> ``loop.run_in_executor(None, …)`` -> the loop's
    default ``ThreadPoolExecutor``, ``min(32, cpu_count + 4)`` threads, which
    anyio's limiter does **not** bound.

The engine uses ``NullPool``, so that is up to 72 new physical connections per
pod against a pgbouncer budget of 40 server connections for db ``licenses``
(``pool_size=20`` × 2 pgbouncer replicas), shared with the other licences pod,
two CronJobs and any ad-hoc script. pgbouncer's ``query_wait_timeout`` is
unset (default 120s), so the overflow does not fail fast — it hangs for two
minutes with no client-side deadline. PR #116's ``connect_timeout`` does not
rescue this: pgbouncer accepts the connection at once and parks the QUERY in its
wait queue, which is past the point a connect timeout can bound.

Both assertions below fail if the governor is removed from ``lifespan``, and the
anyio half additionally proves the SCOPING claim: anyio's limiter is a ``RunVar``
keyed on the running event loop, so it can only be set from inside the loop.
"""

from __future__ import annotations

import asyncio
import threading

import anyio.to_thread
import pytest

import main


@pytest.fixture
def _point_main_at_test_db(db_manager, monkeypatch):
    monkeypatch.setattr(main, "db_manager", db_manager)
    return db_manager


class TestLifespanBoundsBothPools:
    async def test_anyio_handler_limiter_is_bounded(self, _point_main_at_test_db):
        """Sync ``def`` handlers run under anyio's default limiter — bound it."""
        before = anyio.to_thread.current_default_thread_limiter().total_tokens
        assert before == 40, (
            f"anyio's out-of-the-box default is 40 tokens; saw {before}. If this "
            "changed, the arithmetic in main.py needs revisiting."
        )

        async with main.lifespan(main.app):
            after = anyio.to_thread.current_default_thread_limiter().total_tokens

        assert after == main.HANDLER_THREAD_LIMIT, (
            f"lifespan left the anyio threadpool at {after} tokens instead of "
            f"{main.HANDLER_THREAD_LIMIT}. Every sync `def` handler opens a NEW "
            "physical connection (NullPool), so this number IS the pod's share of "
            "the 40-connection pgbouncer budget."
        )

    async def test_to_thread_executor_is_replaced_and_bounded(self, _point_main_at_test_db):
        """``asyncio.to_thread`` must land in OUR executor, not the loop's default.

        Asserted through public API only: the thread NAME proves which executor
        ran the call, and the count of DISTINCT thread names under a burst of
        3× the limit proves the bound is real. The loop's default executor would
        be named ``asyncio_N`` and sized ``min(32, cpu_count + 4)``.
        """

        def _slow_name() -> str:
            # Long enough that the burst is genuinely concurrent, short enough
            # that the whole test stays well inside the 60s pytest timeout.
            import time

            time.sleep(0.05)
            return threading.current_thread().name

        async with main.lifespan(main.app):
            burst = main.TO_THREAD_EXECUTOR_LIMIT * 3
            names = set(
                await asyncio.gather(*(asyncio.to_thread(_slow_name) for _ in range(burst)))
            )

        assert names, "no to_thread calls were observed"
        assert all(n.startswith("licenses-to-thread") for n in names), (
            "asyncio.to_thread did not run in the executor lifespan installs — "
            "`set_default_executor` is missing or was overwritten. Threads seen: "
            f"{sorted(names)}"
        )
        assert len(names) <= main.TO_THREAD_EXECUTOR_LIMIT, (
            f"{burst} concurrent to_thread calls used {len(names)} distinct "
            f"threads, above the {main.TO_THREAD_EXECUTOR_LIMIT} ceiling: "
            f"{sorted(names)}"
        )

    async def test_the_two_pools_together_fit_the_pgbouncer_budget(self):
        """The arithmetic itself, pinned. See the block comment in main.py.

        40 server connections for db ``licenses`` (pgbouncer pool_size=20 × 2
        pgbouncer replicas), minus ~8 reserved for the CronJobs and ad-hoc
        scripts, divided across ``replicas: 2`` licences pods = 16 per pod.
        """
        per_pod = main.HANDLER_THREAD_LIMIT + main.TO_THREAD_EXECUTOR_LIMIT
        assert per_pod == 16, (
            f"the two pools total {per_pod} connections per pod. Two licences "
            "pods then hold "
            f"{per_pod * 2} of the 40-connection pgbouncer budget, leaving "
            f"{40 - per_pod * 2} for the expire-stale-licences and "
            "metering-reconcile CronJobs and any ad-hoc script. Re-derive the "
            "numbers against gitops pgbouncer.ini before changing this."
        )


class TestGovernorIsRetunable:
    """The two knobs must be readable from the environment, and unusable values
    must not take a licensing pod down — a typo in a Deployment env block is a
    far more likely event than a genuine need to retune."""

    def test_values_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("HANDLER_THREAD_LIMIT", "12")
        assert main._env_int("HANDLER_THREAD_LIMIT", 8) == 12

    def test_a_non_integer_falls_back_to_the_shipped_default(self, monkeypatch):
        monkeypatch.setenv("HANDLER_THREAD_LIMIT", "eight")
        assert main._env_int("HANDLER_THREAD_LIMIT", 8) == 8

    def test_zero_or_negative_falls_back(self, monkeypatch):
        monkeypatch.setenv("TO_THREAD_EXECUTOR_LIMIT", "0")
        assert main._env_int("TO_THREAD_EXECUTOR_LIMIT", 8) == 8
        monkeypatch.setenv("TO_THREAD_EXECUTOR_LIMIT", "-3")
        assert main._env_int("TO_THREAD_EXECUTOR_LIMIT", 8) == 8

    def test_an_unset_variable_keeps_the_default(self, monkeypatch):
        monkeypatch.delenv("HANDLER_THREAD_LIMIT", raising=False)
        assert main._env_int("HANDLER_THREAD_LIMIT", 8) == 8
