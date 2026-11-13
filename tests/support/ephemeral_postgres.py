"""Test-only: give the suite a database WITHOUT ever touching a host Postgres.

Resolution order (``resolve_database_url``):

1. ``TEST_DATABASE_URL`` or ``DATABASE_URL`` set -> used as-is (CI's per-job
   ``postgres:18`` sidecar exports ``DATABASE_URL``; locally, your own container).
2. Neither set, docker reachable -> start a throwaway ``postgres:18`` (tmpfs data
   dir, RANDOM ``127.0.0.1`` host port), wait for a real TCP ``SELECT 1``, create
   the database, export ``DATABASE_URL`` to it. The container is removed by its own
   id at session end (``pytest_unconfigure`` in the conftest + ``atexit`` + SIGTERM),
   pass or fail.
3. Neither set, no docker -> ``pytest.exit`` with a clear message.

There is deliberately NO ``localhost:5432`` fallback: on a dev box that can be a
REAL database (fr incident 2026-10-20 wiped local tables), and mini3's bare-metal
Postgres was removed 2026-10-21 (George ruling: no test may default to a host DB).
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time

import pytest

IMAGE = "postgres:18"
_LABEL_KEY = "nonagentic.ephemeral-test-pg"
_container_id: str | None = None


def _docker(*args: str, timeout: float = 60.0) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def remove_ephemeral_postgres() -> None:
    """Remove the container THIS process started (by id). No-op otherwise. Idempotent."""
    global _container_id
    cid, _container_id = _container_id, None
    if cid:
        _docker("rm", "-f", "-v", cid, timeout=60)


def _on_signal(signum, _frame):  # pragma: no cover - exercised by hand
    remove_ephemeral_postgres()
    sys.exit(128 + signum)


def _unavailable(why: str) -> None:
    pytest.exit(
        "[tests] TEST_DATABASE_URL / DATABASE_URL are unset and docker is unavailable "
        f"({why}): refusing to fall back to a host database. Start docker, or export "
        "DATABASE_URL to a throwaway postgres:18, e.g.\n"
        "  docker run --rm -d --tmpfs /var/lib/postgresql -p 127.0.0.1::5432 "
        "-e POSTGRES_PASSWORD=postgres postgres:18",
        returncode=4,
    )


def resolve_database_url(db_name: str, label: str) -> str:
    """Return the DSN the suite must use; see the module docstring for the order."""
    global _container_id
    explicit = (os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if explicit:
        return explicit
    if _container_id is not None:  # already started in this process
        return os.environ["DATABASE_URL"]

    ok, out = _docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    if not ok:
        _unavailable(out.splitlines()[0] if out else "docker info failed")

    ok, out = _docker(
        "run", "--rm", "-d",
        "--tmpfs", "/var/lib/postgresql",
        "-p", "127.0.0.1::5432",  # RANDOM host port - never 5432 / 8000
        "--label", f"{_LABEL_KEY}={label}",
        "-e", "POSTGRES_PASSWORD=postgres",
        IMAGE,
        timeout=300,
    )  # fmt: skip
    if not ok:
        _unavailable(f"docker run failed: {out.splitlines()[-1] if out else ''}")
    _container_id = out.splitlines()[-1].strip()
    atexit.register(remove_ephemeral_postgres)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    try:
        ok, out = _docker("port", _container_id, "5432/tcp")
        port = out.splitlines()[0].rsplit(":", 1)[-1] if ok and out else ""
        if not port.isdigit() or port in ("5432", "8000"):
            raise RuntimeError(f"unusable mapped port {port!r} for {_container_id}")
        base = f"postgresql://postgres:postgres@127.0.0.1:{port}"  # pragma: allowlist secret

        import psycopg
        from psycopg import sql

        # TCP readiness: the image's init-phase server listens on the unix socket
        # only, so a TCP SELECT 1 succeeding means the FINAL server is up.
        deadline = time.monotonic() + 90
        while True:
            try:
                with psycopg.connect(f"{base}/postgres", connect_timeout=2, autocommit=True) as c:
                    c.execute("SELECT 1")
                    c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.3)
    except BaseException as exc:
        remove_ephemeral_postgres()
        _unavailable(f"ephemeral postgres did not come up: {exc}")

    url = f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/{db_name}"  # pragma: allowlist secret
    os.environ["DATABASE_URL"] = url
    print(
        f"[tests] DATABASE_URL unset: using ephemeral {IMAGE} {_container_id[:12]} on "
        f"127.0.0.1:{port} (removed after the run)",
        file=sys.stderr,
    )
    return url
