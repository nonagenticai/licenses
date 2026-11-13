"""Migration 045: re-key the `book-fulfillment` tenant (owner ruling 2026-10-06).

`tenants.id` 1d0090ac-… was ALSO the Keycloak `sub` of a live service account —
the violation `LicenseTenantIdIsKeycloakPrincipal` reports. 045 moves the tenant
to 01a1109c-… . It is the single sanctioned exception to "no uuid is ever
re-keyed", so what is pinned here is that it moves EVERYTHING or NOTHING.

WHAT IS PINNED.
* Text-level (no database): the whole re-key is ONE top-level statement (the
  runner is autocommit per statement, so two statements would be two
  transactions); the runner cannot auto-apply the downgrade.
* Run for real (Postgres; every test rolls back): the prod shape — tenant + 2
  licences + 2 subjects (one the bare PRINCIPAL sub, one `svc:<old>:…`) + a
  bundle grant + a per-app byo row — lands on the new id with every tenant
  column and the name preserved; nothing references the old id; a bystander
  tenant is untouched; the principal's own `keycloak_sub` is NOT rewritten; a
  re-run and an absent tenant are byte-for-byte no-ops; the closing assertion
  really aborts; the downgrade restores the exact prior state.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from scripts.apply_migrations import _run_file, split_statements
from tests.conftest import SEED_TENANT_ID, TEST_DATABASE_URL
from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "045_rekey_book_fulfillment_tenant.sql"
DOWN = MIGRATIONS / "downgrades" / "045_rekey_book_fulfillment_tenant.sql"

# Literals on purpose: these two ids ARE the contract (the new one was minted for
# this migration and is already written into scripts/backfill_tenants_byo_mode.py).
OLD = "1d0090ac-adbf-4c07-b704-bc66061b572e"
NEW = "01a1109c-f895-7766-ba28-6ea56e56a5f7"
OLD_SVC_SUB = f"svc:{OLD}:inference-router"
NEW_SVC_SUB = f"svc:{NEW}:inference-router"

# Every table that holds a tenant id, plus `token` (which must NOT change at all).
TABLES = ("tenants", "subject", "license", "tenant_bundle", "tenant_app_byo_mode", "token")


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _one(conn, sql: str, params=None):
    row = conn.execute(sql, params).fetchone()
    assert row is not None, sql
    return row[0]


def _dump(conn) -> dict[str, list]:
    """Every row of every tenant-bearing table, order-stable. The no-op proofs
    compare two of these, so a write to ANY column of ANY row shows up."""
    return {
        t: _one(
            conn,
            f"SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text), '[]'::jsonb) "  # noqa: S608
            f"FROM {t} x",
        )
        for t in TABLES
    }


def _references(conn, tenant_id: str) -> dict[str, int]:
    return {
        "tenants.id": _one(conn, "SELECT count(*) FROM tenants WHERE id = %s", (tenant_id,)),
        "license.tenant_id": _one(
            conn, "SELECT count(*) FROM license WHERE tenant_id = %s", (tenant_id,)
        ),
        "subject.tenant_id": _one(
            conn, "SELECT count(*) FROM subject WHERE tenant_id = %s", (tenant_id,)
        ),
        "tenant_bundle.tenant_id": _one(
            conn, "SELECT count(*) FROM tenant_bundle WHERE tenant_id = %s", (tenant_id,)
        ),
        "tenant_app_byo_mode.tenant_id": _one(
            conn, "SELECT count(*) FROM tenant_app_byo_mode WHERE tenant_id = %s", (tenant_id,)
        ),
        "subject.keycloak_sub svc:": _one(
            conn,
            "SELECT count(*) FROM subject WHERE keycloak_sub LIKE %s",
            (f"svc:{tenant_id}:%",),
        ),
    }


def _tenant_is_principal_rows(conn) -> int:
    """The gauge's own `match="keycloak_sub"` predicate (tenant_subject_invariant)."""
    return _one(
        conn,
        "SELECT count(*) FROM tenants t "
        "JOIN subject s ON lower(s.keycloak_sub) = lower(t.id::text)",
    )


def _seed(conn, tenant_id: str = OLD) -> dict[str, str]:
    """The prod shape of `book-fulfillment`, plus one bystander on SEED_TENANT_ID.

    Every `tenants` column carries a NON-default value, so a column the migration
    forgot to copy comes back as its default and the comparison goes red.
    """
    ids = {
        k: str(uuid.uuid4())
        for k in (
            "app_site",
            "app_router",
            "plan_site",
            "plan_router",
            "bundle",
            "subj_principal",
            "subj_svc",
            "lic_site",
            "lic_router",
            "token",
            "subj_bystander",
            "lic_bystander",
        )
    }
    conn.execute(
        "INSERT INTO tenants (id, name, keycloak_realm, byo_mode, archived_at, kind, "
        "cost_centre, budget_currency, created_at) VALUES "
        "(%s, 'book-fulfillment', 'secure-apps', 'platform_only', "
        "'2026-03-04T05:06:07+00', 'customer', 'customers', 'EUR', '2026-01-02T03:04:05+00')",
        (tenant_id,),
    )
    for app, key in (("app_site", "book-site"), ("app_router", "inference-router")):
        conn.execute("INSERT INTO app (id, key, name) VALUES (%s, %s, %s)", (ids[app], key, key))
    for plan, app in (("plan_site", "app_site"), ("plan_router", "app_router")):
        conn.execute(
            "INSERT INTO plan (id, app_id, code, name) VALUES (%s, %s, 'full', 'Full')",
            (ids[plan], ids[app]),
        )
    conn.execute(
        "INSERT INTO bundle (id, key, name) VALUES (%s, 'inference-router', 'Inference Router')",
        (ids["bundle"],),
    )
    # Subject 1 is the Keycloak PRINCIPAL: its sub IS the old tenant id. Subject 2
    # is the tenant-level service subject, whose sub EMBEDS the tenant id.
    for subj, sub, tid in (
        ("subj_principal", OLD, tenant_id),
        ("subj_svc", f"svc:{tenant_id}:inference-router", tenant_id),
        ("subj_bystander", f"svc:{SEED_TENANT_ID}:inference-router", SEED_TENANT_ID),
    ):
        conn.execute(
            "INSERT INTO subject (id, keycloak_sub, keycloak_realm, tenant_id) "
            "VALUES (%s, %s, 'secure-apps', %s)",
            (ids[subj], sub, tid),
        )
    for lic, subj, app, plan, tid, bundle in (
        ("lic_site", "subj_principal", "app_site", "plan_site", tenant_id, None),
        ("lic_router", "subj_svc", "app_router", "plan_router", tenant_id, ids["bundle"]),
        ("lic_bystander", "subj_bystander", "app_router", "plan_router", SEED_TENANT_ID, None),
    ):
        conn.execute(
            "INSERT INTO license (id, subject_id, app_id, plan_id, tenant_id, bundle_id, "
            "status, updated_at) VALUES (%s, %s, %s, %s, %s, %s, 'active', now())",
            (ids[lic], ids[subj], ids[app], ids[plan], tid, bundle),
        )
    conn.execute(
        "INSERT INTO token (id, license_id, token_hash, token_type) "
        "VALUES (%s, %s, 'deadbeef', 'api')",
        (ids["token"], ids["lic_site"]),
    )
    conn.execute(
        "INSERT INTO tenant_bundle (tenant_id, bundle_id) VALUES (%s, %s)",
        (tenant_id, ids["bundle"]),
    )
    conn.execute(
        "INSERT INTO tenant_app_byo_mode (tenant_id, app_id, byo_mode) "
        "VALUES (%s, %s, 'platform_only')",
        (tenant_id, ids["app_site"]),
    )
    return ids


def _tenant_without_id(conn, tenant_id: str):
    return _one(conn, "SELECT to_jsonb(t) - 'id' FROM tenants t WHERE id = %s", (tenant_id,))


@pytest.fixture
def conn(db_manager):
    """One transaction on the freshly truncated test database; always rolled back."""
    c = psycopg.connect(_libpq_dsn())
    try:
        yield c
    finally:
        c.rollback()
        c.close()


class TestTheTextKeepsTheContract:
    def test_the_whole_rekey_is_one_statement_and_so_one_transaction(self):
        """`apply_migrations` runs each top-level statement autocommit. Split the
        DO block in two and a failure between them leaves a half-moved tenant."""
        for path in (UP, DOWN):
            statements = split_statements(path.read_text(encoding="utf-8"))
            assert len(statements) == 1, path.name
            body = "\n".join(
                ln for ln in statements[0].splitlines() if not ln.lstrip().startswith("--")
            )
            assert body.lstrip().startswith("DO $$"), path.name

    def test_the_runner_cannot_auto_apply_the_downgrade(self):
        assert DOWN.exists()
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}

    def test_it_names_the_ruling_and_that_it_is_the_only_exception(self):
        header = UP.read_text(encoding="utf-8").split("DO $$", 1)[0]
        assert "OWNER RULING 2026-10-06" in header
        assert "SINGLE SANCTIONED EXCEPTION" in header


class TestRunForReal:
    def test_everything_moves_to_the_new_id_and_nothing_is_left_on_the_old(self, conn):
        ids = _seed(conn)
        before = _tenant_without_id(conn, OLD)
        assert _references(conn, OLD) == {
            "tenants.id": 1,
            "license.tenant_id": 2,
            "subject.tenant_id": 2,
            "tenant_bundle.tenant_id": 1,
            "tenant_app_byo_mode.tenant_id": 1,
            "subject.keycloak_sub svc:": 1,
        }, "positive control: the seed is the prod shape"
        assert _tenant_is_principal_rows(conn) == 1, "positive control: it violates before"
        token_before = _dump(conn)["token"]

        _run_file(conn, UP)

        assert set(_references(conn, OLD).values()) == {0}
        assert _references(conn, NEW) == {
            "tenants.id": 1,
            "license.tenant_id": 2,
            "subject.tenant_id": 2,
            "tenant_bundle.tenant_id": 1,
            "tenant_app_byo_mode.tenant_id": 1,
            "subject.keycloak_sub svc:": 1,
        }
        # Name preserved, and EVERY other tenant column copied — not defaulted.
        after = _tenant_without_id(conn, NEW)
        assert after == before
        assert after["name"] == "book-fulfillment"
        assert (after["kind"], after["byo_mode"], after["budget_currency"]) == (
            "customer",
            "platform_only",
            "EUR",
        )
        assert _one(conn, "SELECT count(*) FROM tenants WHERE name LIKE 'rekey-045-%'") == 0
        # The SAME rows moved: no licence / subject was re-minted under a new pk.
        assert {
            str(r[0])
            for r in conn.execute("SELECT id FROM license WHERE tenant_id = %s", (NEW,)).fetchall()
        } == {ids["lic_site"], ids["lic_router"]}
        assert _one(conn, "SELECT keycloak_sub FROM subject WHERE id = %s", (ids["subj_svc"],)) == (
            NEW_SVC_SUB
        )
        # A token names its tenant only through its licence, so it is untouched
        # and now resolves to the new id.
        assert _dump(conn)["token"] == token_before
        assert (
            str(
                _one(
                    conn,
                    "SELECT l.tenant_id FROM token k JOIN license l ON l.id = k.license_id "
                    "WHERE k.id = %s",
                    (ids["token"],),
                )
            )
            == NEW
        )

    def test_the_principal_keeps_its_sub_and_the_violation_is_gone(self, conn):
        """The bare old id in `subject.keycloak_sub` is the Keycloak principal's
        `sub` claim, not a reference to the tenant. Rewriting it would lock the
        service account out; leaving it is what makes the collision disappear."""
        ids = _seed(conn)

        _run_file(conn, UP)

        assert (
            _one(conn, "SELECT keycloak_sub FROM subject WHERE id = %s", (ids["subj_principal"],))
            == OLD
        )
        assert _tenant_is_principal_rows(conn) == 0

    def test_a_bystander_tenant_is_untouched(self, conn):
        ids = _seed(conn)
        bystander_sub = f"svc:{SEED_TENANT_ID}:inference-router"

        _run_file(conn, UP)

        assert _one(
            conn, "SELECT tenant_id::text FROM license WHERE id = %s", (ids["lic_bystander"],)
        ) == (SEED_TENANT_ID)
        assert _one(
            conn,
            "SELECT tenant_id::text || '|' || keycloak_sub FROM subject WHERE id = %s",
            (ids["subj_bystander"],),
        ) == (f"{SEED_TENANT_ID}|{bystander_sub}")

    def test_reapplying_is_a_complete_noop(self, conn):
        _seed(conn)
        _run_file(conn, UP)
        once = _dump(conn)
        assert any(r["id"] == NEW for r in once["tenants"]), "control: the first run did the work"

        _run_file(conn, UP)

        assert _dump(conn) == once

    def test_a_database_without_the_tenant_is_a_complete_noop(self, conn):
        """Fresh / CI databases. Seeded under an UNRELATED id so there is real
        data the migration could wrongly touch."""
        _seed(conn, tenant_id="5b0c9d3e-2f41-4a6b-9c7d-8e1f2a3b4c5d")
        before = _dump(conn)
        assert len(before["license"]) == 3, "control: the dump sees the seeded rows"

        _run_file(conn, UP)

        assert _dump(conn) == before
        assert _references(conn, NEW)["tenants.id"] == 0

    def test_both_ids_present_is_refused_not_guessed_at(self, conn):
        _seed(conn)
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, 'someone-else')", (NEW,))

        with pytest.raises(RuntimeError, match="045 REFUSED"):
            _run_file(conn, UP)

    def test_the_closing_assertion_really_aborts(self, conn, tmp_path):
        """Prove the RAISE is reachable, by running a copy of 045 with ONE repoint
        removed. The `svc:` rewrite is the one no FK backstops: drop a `tenant_id`
        UPDATE and the DELETE fails on RESTRICT first, but drop this and only the
        assertion stands between prod and a silently stale `svc:<old>:` subject."""
        sql = UP.read_text(encoding="utf-8")
        needle = "SET keycloak_sub = new_svc || substr(keycloak_sub, length(old_svc) + 1)"
        assert sql.count(needle) == 1
        mutant = tmp_path / "045_mutant.sql"
        mutant.write_text(sql.replace(needle, "SET keycloak_sub = keycloak_sub"), encoding="utf-8")
        _seed(conn)

        with pytest.raises(RuntimeError, match="045 FAILED: 1 row"):
            _run_file(conn, mutant)

    def test_the_downgrade_restores_the_exact_prior_state(self, conn):
        _seed(conn)
        before = _dump(conn)

        _run_file(conn, UP)
        assert _dump(conn) != before, "control: the upgrade changed something"
        _run_file(conn, DOWN)

        assert _dump(conn) == before


@pytest.fixture(autouse=True)
def _pre_050_writer_default(legacy_owner_default):  # noqa: F811
    """This file replays a pre-050 writer; see tests/support/legacy_owner_default.py."""
