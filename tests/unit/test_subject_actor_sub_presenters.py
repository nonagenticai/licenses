"""Migration 048: subject.actor_sub for 12 new service SAs + 11 presenter-verified licences.

Owner rule 2026-10-07: when no human acted, the actor is the Keycloak SA of the service
that PRESENTS the licence. 048 states it per subject (each holds one licence). Subjects
with several presenters (``tenant-merqi``) or no observed traffic stay NULL.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest

from scripts.apply_migrations import _run_file, split_statements
from tests.conftest import TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "048_subject_actor_sub_presenters.sql"
M047 = REPO / "migrations" / "047_subject_actor_sub.sql"

_ROW = re.compile(r"\(\s*'([^']+)'\s*,\s*'([0-9a-f-]{36})'::uuid\s*\)")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

SALES = "105ca2a2-7501-4f18-93e4-35386138efd6"
WORKER_MGMT = "f2e2957c-7bfb-4836-9230-648a068a571e"
RMT_639C = "svc:639c333a-1613-4fbc-aa14-a7b1430dc0db:raise-my-tickets"
OTHER_SA = "0e4c0bde-3f1c-4d6b-9a52-7c1d2e3f4a5b"

NO_SA_SERVICES = {
    *(
        f"svc-coding-{s}"
        for s in (
            "ai",
            "analysis",
            "containers",
            "gateway",
            "github",
            "realtime",
            "repo-jobs",
            "security",
            "testing",
        )
    ),
    "svc-discovery-engine",
    "svc-image-gateway",
    "svc-modality-gateway",
}


def _rows(path: Path) -> list[tuple[str, str]]:
    return _ROW.findall(path.read_text(encoding="utf-8"))


class TestMigrationText:
    def test_the_mapping_is_the_reviewed_set(self):
        """Positive control: 12 + 11 rows, each subject once, each value a real UUID."""
        rows = _rows(UP)
        assert len(rows) == 23
        assert len({k for k, _ in rows}) == 23, "a subject is mapped twice"
        assert all(_UUID.fullmatch(v) for _, v in rows)
        assert "__SUB_" not in UP.read_text(encoding="utf-8"), "unfilled placeholder"

    def test_every_no_sa_service_is_mapped(self):
        assert NO_SA_SERVICES <= {k for k, _ in _rows(UP)}

    def test_multi_presenter_tenant_subjects_are_not_mapped(self):
        names = {k for k, _ in _rows(UP)}
        assert not {n for n in names if n.startswith("tenant-")}
        assert "n8n-test-perpetual" not in names

    def test_no_subject_is_mapped_twice_across_047_and_048(self):
        assert not {k for k, _ in _rows(UP)} & {k for k, _ in _rows(M047)}

    def test_new_service_subs_are_distinct(self):
        """Each new SA is its own principal: no two no-SA services share a sub."""
        subs = [v for k, v in _rows(UP) if k in NO_SA_SERVICES]
        assert len(subs) == len(set(subs)) == 12

    def test_keycloak_sub_is_never_written_and_one_statement(self):
        text = UP.read_text(encoding="utf-8")
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
        assert not re.search(r"SET\s+keycloak_sub", body, re.IGNORECASE)
        assert len(split_statements(text)) == 1


@pytest.fixture
def conn(db_manager):
    c = psycopg.connect(TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1))
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _insert(conn, keycloak_sub: str, actor_sub: str | None = None) -> None:
    conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm, actor_sub) VALUES (%s, 'service', %s)",
        (keycloak_sub, actor_sub),
    )


def _actor(conn, keycloak_sub: str) -> str | None:
    row = conn.execute(
        "SELECT actor_sub FROM subject WHERE keycloak_sub = %s", (keycloak_sub,)
    ).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


class TestMigrationRunForReal:
    def test_fills_mapped_subjects_only_and_never_overwrites(self, conn):
        expected = dict(_rows(UP))
        _insert(conn, RMT_639C)
        _insert(conn, "svc-workers-aa-agents")
        _insert(conn, "svc-coding-ai")
        _insert(conn, "tenant-merqi")  # several presenters: must stay NULL
        _insert(conn, "svc-kag-merqi")  # no observed traffic: must stay NULL
        _insert(conn, "svc-modality-gateway", OTHER_SA)  # set by hand: must survive
        assert _actor(conn, RMT_639C) is None, "positive control"

        _run_file(conn, UP)

        assert _actor(conn, RMT_639C) == SALES
        assert _actor(conn, "svc-workers-aa-agents") == WORKER_MGMT
        assert _actor(conn, "svc-coding-ai") == expected["svc-coding-ai"]
        assert _actor(conn, "tenant-merqi") is None
        assert _actor(conn, "svc-kag-merqi") is None
        assert _actor(conn, "svc-modality-gateway") == OTHER_SA
        keys = {r[0] for r in conn.execute("SELECT keycloak_sub FROM subject").fetchall()}
        assert RMT_639C in keys, "keycloak_sub must be untouched"

    def test_rerun_is_a_noop(self, conn):
        _insert(conn, RMT_639C)
        _run_file(conn, UP)
        _run_file(conn, UP)
        assert _actor(conn, RMT_639C) == SALES
