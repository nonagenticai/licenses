"""Migration 046: re-key the `portal-render-e2e` tenant (owner ruling 2026-10-06).

`tenants.id` e148e8af-… was ALSO the `keycloak_sub` of subject `8382f0b3-…` (a
deleted `portal`-realm user) — the violation `LicenseTenantIdIsKeycloakPrincipal`
reports. 046 moves the tenant to 01a1113b-… . Same structure and guarantees as
045 (this file mirrors that test): it moves EVERYTHING or NOTHING.

WHAT IS PINNED.
* Text-level (no database): the whole re-key is ONE top-level statement (the
  runner is autocommit per statement, so two statements would be two
  transactions); the runner cannot auto-apply the downgrade; no statement names
  `license.metadata`.
* Run for real (Postgres; every test rolls back): the prod shape — tenant + 3
  licences (2 active, 1 revoked; one carrying the old id in `metadata` as the
  provisioning ACTOR) + the service-account subject whose BARE `keycloak_sub` is
  the old id and whose `tenant_id` is NULL + the name-based service subject + a
  bundle grant — lands on the new id with every tenant column and the name
  preserved; nothing references the old id; a bystander tenant is untouched; the
  bare `keycloak_sub` and `license.metadata` STILL hold the old id; a
  `svc:<old>:…` subject (none on prod; the generic rewrite kept from 045) and a
  per-app byo row move too; a re-run and an absent tenant are byte-for-byte
  no-ops; the closing assertion really aborts; the downgrade restores the exact
  prior state.
* Bootstrap, through the REAL runner and the REAL boot seed (committed, not
  rolled back): a prod-shaped database stamped through 045 ends with exactly one
  `portal-render-e2e` row, on the new id, and the seed does not re-create the old
  one; a fresh database (every file baselined, none run) ends the same way; so
  does a replayed corpus (013 seeds the old id, 046 moves it).
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from models import PLATFORM_TENANTS, ClientSafeError
from scripts.apply_migrations import _run_file, _stamp, apply_all, split_statements
from tests.conftest import SEED_TENANT_ID, TEST_DATABASE_URL
from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "046_rekey_portal_render_e2e_tenant.sql"
DOWN = MIGRATIONS / "downgrades" / "046_rekey_portal_render_e2e_tenant.sql"

# Literals on purpose: these two ids ARE the contract (the new one was minted for
# this migration and is written into models.PLATFORM_TENANTS and
# scripts/backfill_tenants_byo_mode.py).
OLD = "e148e8af-3751-49bf-a0fa-56ab46d31ddc"
NEW = "01a1113b-674f-72b0-a84b-dc746d39afca"
NAME = "portal-render-e2e"
OLD_SVC_SUB = f"svc:{OLD}:inference-router"
NEW_SVC_SUB = f"svc:{NEW}:inference-router"

# What prod's two `license.metadata` rows look like in the sense that matters: the
# old id as the ACTOR that provisioned the licence. Must survive byte-for-byte.
ACTOR_METADATA = (
    '{"provisioned_by": "e148e8af-3751-49bf-a0fa-56ab46d31ddc", "source": "portal-bff"}'
)

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


def _seed(conn, tenant_id: str = OLD, *, with_svc_subject: bool = False) -> dict[str, str]:
    """The prod shape of `portal-render-e2e`, plus one bystander on SEED_TENANT_ID.

    Every `tenants` column carries a NON-default value, so a column the migration
    forgot to copy comes back as its default and the comparison goes red.

    `with_svc_subject` adds what prod does NOT have for this tenant: a
    `svc:<tenant>:<app>` subject and a per-app byo row, to exercise the generic
    repoints kept from 045.
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
            "subj_named",
            "subj_svc",
            "lic_site",
            "lic_router",
            "lic_revoked",
            "token",
            "subj_bystander",
            "lic_bystander",
        )
    }
    conn.execute(
        "INSERT INTO tenants (id, name, keycloak_realm, byo_mode, archived_at, kind, "
        "cost_centre, budget_currency, created_at) VALUES "
        "(%s, %s, 'portal', 'platform_only', "
        "'2026-03-04T05:06:07+00', 'internal', 'ci', 'EUR', '2026-07-20T21:08:00+00')",
        (tenant_id, NAME),
    )
    for app, key in (("app_site", "w2v"), ("app_router", "inference-router")):
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
    # Subject 1 is the PRINCIPAL (prod: `8382f0b3-…`): its sub IS the old tenant id
    # and its tenant_id is NULL. Subject 2 is the name-based service subject, the
    # one prod row with subject.tenant_id = the tenant.
    subjects: list[tuple[str, str, str | None]] = [
        ("subj_principal", OLD, None),
        ("subj_named", "svc-inference-router-portal-render-e2e", tenant_id),
        ("subj_bystander", f"svc:{SEED_TENANT_ID}:inference-router", SEED_TENANT_ID),
    ]
    if with_svc_subject:
        subjects.append(("subj_svc", f"svc:{tenant_id}:inference-router", tenant_id))
    for subj, sub, tid in subjects:
        conn.execute(
            "INSERT INTO subject (id, keycloak_sub, keycloak_realm, tenant_id) "
            "VALUES (%s, %s, 'portal', %s)",
            (ids[subj], sub, tid),
        )
    # `metadata` names the old id as the provisioning ACTOR — the user sense.
    for lic, subj, app, plan, tid, bundle, status_, meta in (
        (
            "lic_site",
            "subj_principal",
            "app_site",
            "plan_site",
            tenant_id,
            None,
            "active",
            ACTOR_METADATA,
        ),
        (
            "lic_router",
            "subj_named",
            "app_router",
            "plan_router",
            tenant_id,
            ids["bundle"],
            "active",
            None,
        ),
        (
            "lic_revoked",
            "subj_principal",
            "app_site",
            "plan_site",
            tenant_id,
            None,
            "revoked",
            ACTOR_METADATA,
        ),
        (
            "lic_bystander",
            "subj_bystander",
            "app_router",
            "plan_router",
            SEED_TENANT_ID,
            None,
            "active",
            None,
        ),
    ):
        conn.execute(
            "INSERT INTO license (id, subject_id, app_id, plan_id, tenant_id, bundle_id, "
            "status, metadata, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())",
            (ids[lic], ids[subj], ids[app], ids[plan], tid, bundle, status_, meta),
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
    if with_svc_subject:
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
        assert "SECOND SANCTIONED EXCEPTION" in header

    def test_no_statement_touches_license_metadata(self):
        """`license.metadata` names the old id as the provisioning ACTOR (the user
        sense). The run-for-real test proves the seeded rows survive; this proves
        no statement could reach the column at all."""
        for path in (UP, DOWN):
            body = "\n".join(
                ln
                for ln in path.read_text(encoding="utf-8").split("DO $$", 1)[1].splitlines()
                if not ln.lstrip().startswith("--")
            )
            assert "metadata" not in body.lower(), path.name


class TestRunForReal:
    def test_everything_moves_to_the_new_id_and_nothing_is_left_on_the_old(self, conn):
        ids = _seed(conn)
        before = _tenant_without_id(conn, OLD)
        assert _references(conn, OLD) == {
            "tenants.id": 1,
            "license.tenant_id": 3,
            "subject.tenant_id": 1,
            "tenant_bundle.tenant_id": 1,
            "tenant_app_byo_mode.tenant_id": 0,
            "subject.keycloak_sub svc:": 0,
        }, "positive control: the seed is the prod shape"
        assert _tenant_is_principal_rows(conn) == 1, "positive control: it violates before"
        token_before = _dump(conn)["token"]

        _run_file(conn, UP)

        assert set(_references(conn, OLD).values()) == {0}
        assert _references(conn, NEW) == {
            "tenants.id": 1,
            "license.tenant_id": 3,
            "subject.tenant_id": 1,
            "tenant_bundle.tenant_id": 1,
            "tenant_app_byo_mode.tenant_id": 0,
            "subject.keycloak_sub svc:": 0,
        }
        # Name preserved, and EVERY other tenant column copied — not defaulted.
        after = _tenant_without_id(conn, NEW)
        assert after == before
        assert after["name"] == NAME
        assert (
            after["kind"],
            after["cost_centre"],
            after["byo_mode"],
            after["budget_currency"],
        ) == (
            "internal",
            "ci",
            "platform_only",
            "EUR",
        )
        assert _one(conn, "SELECT count(*) FROM tenants WHERE name LIKE 'rekey-046-%'") == 0
        # The SAME rows moved: no licence / subject was re-minted under a new pk.
        assert {
            str(r[0])
            for r in conn.execute("SELECT id FROM license WHERE tenant_id = %s", (NEW,)).fetchall()
        } == {ids["lic_site"], ids["lic_router"], ids["lic_revoked"]}
        # Statuses rode along: the revoked licence is still revoked.
        assert _one(conn, "SELECT status FROM license WHERE id = %s", (ids["lic_revoked"],)) == (
            "revoked"
        )
        # The name-based service subject moved by tenant_id; its sub has no id in it.
        assert _one(
            conn,
            "SELECT tenant_id::text || '|' || keycloak_sub FROM subject WHERE id = %s",
            (ids["subj_named"],),
        ) == (f"{NEW}|svc-inference-router-portal-render-e2e")
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
        """The bare old id in `subject.keycloak_sub` is the principal's `sub`, not
        a reference to the tenant: `get_subject_by_keycloak_sub` and every
        provisioning call look the service account up by it. Rewriting it would
        orphan the account; leaving it is what makes the collision disappear.

        ⚠️ THE ASSERTION THAT CATCHES A WIDENED REWRITE. Change step 3's predicate
        from the `svc:<old>:` prefix to "keycloak_sub mentions the old id" and the
        first assert below goes red (and, with it, the violation count: the
        subject would then collide with the NEW tenant instead)."""
        ids = _seed(conn, with_svc_subject=True)
        assert _tenant_is_principal_rows(conn) == 1, "positive control: it violates before"

        _run_file(conn, UP)

        assert _one(
            conn,
            "SELECT keycloak_sub || '|' || coalesce(tenant_id::text, 'NULL') "
            "FROM subject WHERE id = %s",
            (ids["subj_principal"],),
        ) == (f"{OLD}|NULL")
        assert _one(conn, "SELECT count(*) FROM subject WHERE keycloak_sub = %s", (OLD,)) == 1
        assert _one(conn, "SELECT count(*) FROM subject WHERE keycloak_sub = %s", (NEW,)) == 0
        assert _tenant_is_principal_rows(conn) == 0

    def test_license_metadata_still_names_the_old_id_as_the_actor(self, conn):
        """Two prod licences name the old id in `metadata` as the provisioning
        ACTOR — the user sense. They move tenant; their metadata does not change."""
        ids = _seed(conn)
        before = {
            str(r[0]): r[1]
            for r in conn.execute("SELECT id, metadata FROM license ORDER BY id").fetchall()
        }
        assert before[ids["lic_site"]]["provisioned_by"] == OLD, "control: the seed carries it"

        _run_file(conn, UP)

        after = {
            str(r[0]): r[1]
            for r in conn.execute("SELECT id, metadata FROM license ORDER BY id").fetchall()
        }
        assert after == before
        assert (
            _one(conn, "SELECT count(*) FROM license WHERE metadata::text LIKE %s", (f"%{OLD}%",))
            == 2
        )
        assert (
            _one(conn, "SELECT count(*) FROM license WHERE metadata::text LIKE %s", (f"%{NEW}%",))
            == 0
        )

    def test_a_svc_prefixed_subject_and_a_per_app_byo_row_move_too(self, conn):
        """Prod has neither for this tenant. Kept from 045 so one created between
        the census and the apply cannot be left behind."""
        ids = _seed(conn, with_svc_subject=True)
        assert _references(conn, OLD)["subject.keycloak_sub svc:"] == 1
        assert _references(conn, OLD)["tenant_app_byo_mode.tenant_id"] == 1

        _run_file(conn, UP)

        assert set(_references(conn, OLD).values()) == {0}
        assert _references(conn, NEW)["tenant_app_byo_mode.tenant_id"] == 1
        assert _one(conn, "SELECT keycloak_sub FROM subject WHERE id = %s", (ids["subj_svc"],)) == (
            NEW_SVC_SUB
        )

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
        assert len(before["license"]) == 4, "control: the dump sees the seeded rows"

        _run_file(conn, UP)

        assert _dump(conn) == before
        assert _references(conn, NEW)["tenants.id"] == 0

    def test_both_ids_present_is_refused_not_guessed_at(self, conn):
        _seed(conn)
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, 'someone-else')", (NEW,))

        with pytest.raises(RuntimeError, match="046 REFUSED"):
            _run_file(conn, UP)

    def test_the_closing_assertion_really_aborts(self, conn, tmp_path):
        """Prove the RAISE is reachable, by running a copy of 046 with ONE repoint
        removed. The `svc:` rewrite is the one no FK backstops: drop a `tenant_id`
        UPDATE and the DELETE fails on RESTRICT first, but drop this and only the
        assertion stands between prod and a silently stale `svc:<old>:` subject."""
        sql = UP.read_text(encoding="utf-8")
        needle = "SET keycloak_sub = new_svc || substr(keycloak_sub, length(old_svc) + 1)"
        assert sql.count(needle) == 1
        mutant = tmp_path / "046_mutant.sql"
        mutant.write_text(sql.replace(needle, "SET keycloak_sub = keycloak_sub"), encoding="utf-8")
        _seed(conn, with_svc_subject=True)

        with pytest.raises(RuntimeError, match="046 FAILED: 1 row"):
            _run_file(conn, mutant)

    def test_the_downgrade_restores_the_exact_prior_state(self, conn):
        _seed(conn, with_svc_subject=True)
        before = _dump(conn)

        _run_file(conn, UP)
        assert _dump(conn) != before, "control: the upgrade changed something"
        _run_file(conn, DOWN)

        assert _dump(conn) == before


def _render_e2e_rows(db_engine) -> list[tuple[str, str]]:
    with db_engine.connect() as c:
        return [
            (str(r[0]), str(r[1]))
            for r in c.execute(
                text(
                    "SELECT id, name FROM tenants WHERE id = ANY(:ids) OR name LIKE :n ORDER BY 1"
                ),
                {"ids": [OLD, NEW], "n": f"{NAME}%"},
            ).fetchall()
        ]


class TestBootstrapEndsOnTheNewId:
    """The seed must not undo the migration. Boot order is create -> migrate ->
    seed (`main._run_db_bootstrap`); these run the REAL runner (`apply_all`) and
    the REAL seed against committed rows, in that order.

    ⚠️ THE ASSERTION THAT CATCHES `PLATFORM_TENANTS` LEFT ON THE OLD ID is
    `_render_e2e_rows(...) == [(NEW, NAME)]` in the fresh-database test: `subject`
    is empty there, so the seed's principal guard has nothing to refuse and the old
    id is simply born as the tenant. (On a prod-shaped database the guard refuses it
    — that is the second test's last assertion — so only the fresh one can see it.)
    """

    def test_a_fresh_database_is_born_on_the_new_id(self, db_manager, db_engine):
        """What a DR rebuild gets: create_all built the schema, the runner
        BASELINES every file (013 and 046 are stamped, neither runs), and the seed
        alone decides which id `portal-render-e2e` has."""
        assert _render_e2e_rows(db_engine) == [], "control: db_engine starts clean"

        assert apply_all(_libpq_dsn()) == 0
        db_manager.seed_platform_tenants()

        with db_engine.connect() as c:
            stamped = {r[0] for r in c.execute(text("SELECT filename FROM schema_migrations"))}
        assert {UP.name, "013_seed_render_e2e_tenant.sql"} <= stamped
        assert _render_e2e_rows(db_engine) == [(NEW, NAME)]
        assert db_manager.seed_platform_tenants() == 0, "a second boot inserts nothing"
        assert _render_e2e_rows(db_engine) == [(NEW, NAME)]

    def test_a_prod_shaped_database_is_moved_by_the_runner_and_not_reseeded(
        self, db_manager, db_engine
    ):
        """Prod today: every file through 045 stamped, the tenant on the OLD id,
        the service-account subject carrying the old id as `keycloak_sub`. One boot
        of the new image: the runner applies exactly 046, then the seed runs."""
        with psycopg.connect(_libpq_dsn(), autocommit=True) as c:
            ids = _seed(c)
            c.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name != UP.name:
                    _stamp(c, path.name)
        assert _render_e2e_rows(db_engine) == [(OLD, NAME)], "control: prod shape"

        assert apply_all(_libpq_dsn()) == 0
        inserted = db_manager.seed_platform_tenants()

        assert _render_e2e_rows(db_engine) == [(NEW, NAME)]
        with db_engine.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM schema_migrations WHERE filename = :f"),
                    {"f": UP.name},
                ).scalar_one()
                == 1
            )
            # The token under a moved licence now introspects to the NEW tenant.
            assert (
                str(
                    c.execute(
                        text(
                            "SELECT l.tenant_id FROM token k JOIN license l "
                            "ON l.id = k.license_id WHERE k.id = :t"
                        ),
                        {"t": ids["token"]},
                    ).scalar_one()
                )
                == NEW
            )
        # The seed inserted the OTHER platform tenants and nothing for this one.
        assert inserted == len(PLATFORM_TENANTS) - 1
        # A second boot (the other replica, or a restart) changes nothing.
        assert apply_all(_libpq_dsn()) == 0
        assert db_manager.seed_platform_tenants() == 0
        assert _render_e2e_rows(db_engine) == [(NEW, NAME)]

    def test_a_replayed_corpus_ends_on_the_new_id_with_013_untouched(self, db_manager, db_engine):
        """013 is left byte-identical and still INSERTs the old id. Wherever the
        corpus IS run in order (the schema-parity replay arm, a hand-applied
        database), 046 runs after it — and the seed then finds the new row."""
        with psycopg.connect(_libpq_dsn(), autocommit=True) as c:
            _run_file(c, MIGRATIONS / "013_seed_render_e2e_tenant.sql")
            assert _references(c, OLD)["tenants.id"] == 1, "control: 013 still seeds the old id"
            _run_file(c, UP)
        db_manager.seed_platform_tenants()

        assert _render_e2e_rows(db_engine) == [(NEW, NAME)]

    def test_the_seed_names_the_new_id_and_never_the_old(self):
        by_name = {name: tid for tid, name, _byo in PLATFORM_TENANTS}
        assert by_name[NAME] == NEW
        assert OLD not in {tid for tid, _n, _b in PLATFORM_TENANTS}


_SCRIPT_PATH = REPO / "scripts" / "provision_service_token.py"
_TOKEN_LINE = re.compile(r"^token\s*:\s*(\S+)$", re.MULTILINE)


def _provision_service_token():
    spec = importlib.util.spec_from_file_location("provision_service_token_046", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheServiceAccountStillMintsAfterTheRekey:
    """The shared w2v service credential (`DOWNSTREAM_LICENSE_TOKEN`) is minted by
    `scripts/provision_service_token.py --subject e148e8af-… --realm portal
    --app-key <app> --rotate` inside the licences pod. It names the service
    account by its `keycloak_sub` — the id 046 deliberately leaves alone — so the
    procedure must work unchanged, and must resolve to the SAME subject row
    (that row's id is the downstream on-behalf-of allowlist key).
    """

    @pytest.fixture
    def rekeyed(self, db_manager, seed_app, seed_plan, monkeypatch):
        """The state 046 leaves, committed: tenant on the new id, the principal
        subject with its bare sub, no tenants row on the old id."""
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
        with psycopg.connect(_libpq_dsn(), autocommit=True) as c:
            ids = _seed(c)
            # `_seed` archives the tenant (a non-default value in every column);
            # prod's is live, and an archived tenant's tokens do not introspect.
            c.execute("UPDATE tenants SET archived_at = NULL WHERE id = %s", (OLD,))
            _run_file(c, UP)
        return ids

    def _mint(self, capsys, *extra: str) -> tuple[int, str]:
        rc = _provision_service_token().main(
            [
                "--subject",
                OLD,
                "--realm",
                "portal",
                "--app-key",
                "test-app",
                "--plan",
                "trial",
                "--tenant-owner",
                "platform",
            ]
            + list(extra)
        )
        return rc, capsys.readouterr().out

    def test_the_canonical_mint_resolves_the_same_subject(self, db_manager, rekeyed, capsys):
        rc, out = self._mint(capsys, "--rotate")
        assert rc == 0, out
        match = _TOKEN_LINE.search(out)
        assert match is not None, out
        seen = db_manager.introspect_token(match.group(1))
        assert seen["active"] is True
        assert str(seen["subject_id"]) == rekeyed["subj_principal"]
        # The script's own deterministic service tenant — never the old id.
        assert str(seen["tenant_id"]) not in (OLD, NEW)
        assert db_manager.get_tenant(OLD) is None

    def test_minting_into_the_rekeyed_tenant_introspects_the_new_id(
        self, db_manager, rekeyed, capsys
    ):
        rc, out = self._mint(capsys, "--tenant-id", NEW, "--rotate")
        assert rc == 0, out
        match = _TOKEN_LINE.search(out)
        assert match is not None, out
        seen = db_manager.introspect_token(match.group(1))
        assert seen["active"] is True
        assert str(seen["tenant_id"]) == NEW
        assert str(seen["subject_id"]) == rekeyed["subj_principal"]
        tenant = db_manager.get_tenant(NEW)
        assert tenant is not None and str(tenant.name) == NAME, "the tenant row was not renamed"

    def test_the_old_id_cannot_be_named_as_the_tenant_again(self, db_manager, rekeyed, capsys):
        """`--tenant-id <old>` is the one command that could re-create the
        violation by hand. `ensure_tenant` refuses it: the id is a subject's sub."""
        with pytest.raises(ClientSafeError):
            self._mint(capsys, "--tenant-id", OLD, "--rotate")
        assert db_manager.get_tenant(OLD) is None


@pytest.fixture(autouse=True)
def _pre_050_writer_default(legacy_owner_default):  # noqa: F811
    """This file replays a pre-050 writer; see tests/support/legacy_owner_default.py."""
