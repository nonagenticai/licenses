"""Migration 050: tenants and licences owned per app.

Run for REAL against a database built FROM EMPTY through 049 (init.sql -> the
genesis shim -> 001..049, the same replay the schema-parity corpus arm uses),
seeded with the prod shapes that matter, then 050 is applied:

* owners: every existing tenant -> 'platform' except the curated list, by ID —
  ``netcare`` and ``cp-e2e-2026-10-13`` -> commonpractice (George 2026-10-13:
  netcare is CommonPractice's only real customer), ``ola-armatys`` -> merqi,
  ``merqi`` -> merqi, fr provisioning's tenants -> fr with external_ref = name;
* netcare stays ARCHIVED (un-archiving is a post-deploy API step);
* grants (least privilege): 'platform' on every non-platform tenant; otherwise an
  owner is granted only as the SOLE issuer of an app with a live licence there —
  a multi-issuer app (inference-router, the platform+fr long tail) grants nobody;
  plus commonpractice's registered shared platform tenants; nobody gets one for
  an expired / revoked licence or for its own tenant;
* tenants.owner_key has NO default;
* the seed rows equal the Python constants the boot seed writes on a FRESH
  database (models.TENANT_OWNERS / API_CLIENTS / issuers_for_app_key);
* names: UNIQUE(name) is gone, UNIQUE(owner_key, name) holds;
* re-running 050 is a no-op; the downgrade restores the pre-050 shape and
  refuses while two owners share a name.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS, TENANT_OWNERS, issuers_for_app_key
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_054_bundle_minter_owners import M054_API_CLIENTS
from tests.unit.test_migration_055_provisioning_bundle_minter import M055_API_CLIENTS
from tests.unit.test_migration_056_growth_bundle_minter import M056_API_CLIENTS
from tests.unit.test_migration_057_kag_owner import (
    M057_API_CLIENTS,
    M057_ISSUERS,
    M057_TENANT_OWNERS,
)
from tests.unit.test_migration_059_app_requires import M059_API_CLIENTS
from tests.unit.test_migration_060_kag_frontend_bundle_minter import M060_API_CLIENTS
from tests.unit.test_migration_061_kag_bundle_minter import M061_API_CLIENTS
from tests.unit.test_migration_064_karma_bundle_minter import M064_API_CLIENTS
from tests.unit.test_migration_067_strata_apps import M067_API_CLIENTS, M067_TENANT_OWNERS
from tests.unit.test_migration_068_care_bus_licenses_admin_client import (
    M068_API_CLIENTS,
)
from tests.unit.test_migration_070_agent_app import M070_API_CLIENTS, M070_TENANT_OWNERS
from tests.unit.test_migration_078_care_bus_early_access import M078_API_CLIENTS
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_API_CLIENTS,
    M082_TENANT_OWNERS,
)
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "050_tenant_ownership_per_app.sql"
DOWN = MIGRATIONS / "downgrades" / "050_tenant_ownership_per_app.sql"

NETCARE = "01a0ef72-31af-7561-936a-b8383b5fb6c8"
CP_E2E = "01a135dd-698e-7000-864d-205ec171ad33"
OLA = "019edc48-6060-76e9-8d8f-2e720e12c609"
TENANT_DEV = "3806d6e5-4698-571e-9bb2-22ffd076ac73"
MERQI = "639c333a-1613-4fbc-aa14-a7b1430dc0db"
CLAUDE = "01a03dd9-941a-7300-ad03-f099dcf93864"  # fr provisioning-created
FR_TEST = "019efaa0-512b-733e-9e98-b1ac82a6f7ee"  # fr provisioning-created, archived
RAISEMYTICKETS = "01a0ae64-3703-76a6-8e05-9a4826428d32"  # CP-registered shared tenant
# Service tenants owned by the app whose own code uses them (migration header).
SVC_OWNED = {
    "svc-commonpractice": ("b8e94a81-dbd3-5f75-8a58-45deceaef000", "commonpractice"),
    "svc-w2v-shared": ("4af92cd8-4d13-54f1-aaf9-94ee2caaf823", "w2v"),
    "svc-tts-synth": ("333c001e-03eb-5508-901c-b05409b0df85", "w2v"),
    "svc-fr-backend": ("d4804d48-2761-50d7-9302-fb366fbcad1c", "fr"),
    "svc-fr-backend-kag": ("dfb9ca84-05f9-510b-93ae-b8dda21ebd4a", "fr"),
}
DB = "licenses_m050_test"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 049 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            _run_sql_file(c, INIT_SQL, "genesis")
            _run_sql_file(c, SHIM_SQL, "shim")
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name < UP.name:
                    _run_sql_file(c, path, "corpus")
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _one(conn, sql: str, params=None):
    row = conn.execute(sql, params).fetchone()
    assert row is not None, sql
    return row[0]


def _app(conn, key: str) -> str:
    existing = conn.execute("SELECT id FROM app WHERE key = %s", (key,)).fetchone()
    if existing is not None:
        return str(existing[0])
    return str(_one(conn, "INSERT INTO app (key, name) VALUES (%s, %s) RETURNING id", (key, key)))


def _plan(conn, app_id: str) -> str:
    return str(
        _one(
            conn,
            "INSERT INTO plan (app_id, code, name) VALUES (%s, %s, 'Std') RETURNING id",
            (app_id, f"std-{uuid.uuid4().hex[:6]}"),
        )
    )


def _tenant(conn, name: str, tid: str | None = None, *, archived: bool = False) -> str:
    return str(
        _one(
            conn,
            "INSERT INTO tenants (id, name, archived_at) VALUES (%s, %s, %s) RETURNING id",
            (tid or str(uuid.uuid7()), name, "2026-10-01T00:00:00+00" if archived else None),
        )
    )


def _licence(conn, tenant: str, app_key: str, *, status="active", expired=False) -> None:
    app_id = _app(conn, app_key)
    sub = str(
        _one(
            conn,
            "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') "
            "RETURNING id",
            (f"svc:{tenant}:{app_key}:{uuid.uuid4().hex[:4]}",),
        )
    )
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at, "
        "expires_at) VALUES (%s, %s, %s, %s, %s, now() - interval '2 days', %s)",
        (
            sub,
            app_id,
            _plan(conn, app_id),
            tenant,
            status,
            "2026-01-01T00:00:00+00" if expired else None,
        ),
    )


def _seed(conn) -> dict[str, str]:
    t = {
        "netcare": _tenant(conn, "netcare", NETCARE, archived=True),
        "cp_e2e": _tenant(conn, "cp-e2e-2026-10-13", CP_E2E),
        "ola": _tenant(conn, "ola-armatys", OLA),
        "merqi": _tenant(conn, "merqi", MERQI),
        "claude": _tenant(conn, "claude", CLAUDE),
        "fr_test": _tenant(conn, "fr-test", FR_TEST, archived=True),
        "rmt": _tenant(conn, "raisemytickets", RAISEMYTICKETS, archived=True),
        "plain": _tenant(conn, "some-platform-tenant"),
        "ir_only": _tenant(conn, "ir-only"),
        "tenant_dev": _tenant(conn, "tenant-dev", TENANT_DEV),
        "cp_live": _tenant(conn, "cp-live"),
    }
    # netcare's live licences: fr-, w2v-, merqi- and platform(+fr)-issued apps.
    for app_key in ("fr", "kag", "w2v", "marketing", "workers", "commonpractice"):
        _licence(conn, t["netcare"], app_key)
    # Not live -> must NOT produce a grant.
    _licence(conn, t["netcare"], "book-fulfillment", expired=True)
    _licence(conn, t["cp_e2e"], "w2v-image", status="revoked")
    # A platform tenant with only a platform+fr app: a multi-issuer app grants nobody.
    _licence(conn, t["plain"], "workers")
    # inference-router (fr + commonpractice) alone grants neither of them...
    _licence(conn, t["ir_only"], "inference-router")
    # ...but commonpractice gets in where commonpractice itself is live.
    _licence(conn, t["cp_live"], "inference-router")
    _licence(conn, t["cp_live"], "commonpractice")
    # raisemytickets: CP's own licence revoked, only a bundle inference-router live —
    # CP keeps its grant because it is one of CP's registered shared tenants.
    _licence(conn, t["rmt"], "commonpractice", status="revoked")
    _licence(conn, t["rmt"], "inference-router")
    # ola-armatys (merqi's tenant_lifecycle tenant), its prod licence shape
    # (the primary, read-only 2026-10-14): w2v keeps a grant through its live `w2v`.
    for app_key in ("fr", "kag", "w2v", "raise-my-tickets", "inference-router"):
        _licence(conn, t["ola"], app_key)
    for app_key in ("marketing", "sales"):
        _licence(conn, t["ola"], app_key, status="revoked")
    # tenant-dev stays platform; merqi references it and keeps a grant.
    for app_key in ("inference-router", "kag", "marketing", "raise-my-tickets", "sales", "w2v"):
        _licence(conn, t["tenant_dev"], app_key)
    # cp-e2e with its own app: only the platform grant.
    _licence(conn, t["cp_e2e"], "commonpractice")
    return t


def _owners(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT name, owner_key FROM tenants").fetchall()}


def _grants(conn, tenant: str) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT grantee_owner_key, granted_by FROM tenant_app_grant "
            "WHERE tenant_id = %s AND revoked_at IS NULL",
            (tenant,),
        ).fetchall()
    }


def _apply(conn, path: Path) -> None:
    _run_sql_file(conn, path, "050")


class TestText:
    def test_one_transaction(self):
        for path in (UP, DOWN):
            stmts = [
                "\n".join(ln for ln in st.splitlines() if not ln.lstrip().startswith("--")).strip()
                for st in split_statements(path.read_text(encoding="utf-8"))
            ]
            assert stmts[0] == "BEGIN" and stmts[-1] == "COMMIT", path.name

    def test_the_runner_cannot_auto_apply_the_downgrade(self):
        assert DOWN.parent != MIGRATIONS

    def test_header_names_the_curated_owners(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for name in (
            "netcare",
            "cp-e2e-2026-10-13",
            "ola-armatys",
            NETCARE,
            MERQI,
            CLAUDE,
            FR_TEST,
            RAISEMYTICKETS,
        ):
            assert name in header
        for name, (tid, owner) in SVC_OWNED.items():
            assert name in header and tid in header and f"-> {owner}" in header, name


class TestRunForReal:
    def test_owner_backfill(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        owners = _owners(conn)
        assert owners["netcare"] == "commonpractice"
        assert owners["cp-e2e-2026-10-13"] == "commonpractice"
        assert owners["ola-armatys"] == "merqi"
        assert owners["merqi"] == "merqi"
        assert owners["claude"] == "fr"
        assert owners["fr-test"] == "fr"
        assert owners["some-platform-tenant"] == "platform"
        refs = {
            r[0]: r[1] for r in conn.execute("SELECT name, external_ref FROM tenants").fetchall()
        }
        assert refs["claude"] == "claude" and refs["fr-test"] == "fr-test"
        assert refs["netcare"] is None and refs["merqi"] is None
        rest = {
            n: o
            for n, o in owners.items()
            if n
            not in {"netcare", "cp-e2e-2026-10-13", "ola-armatys", "merqi", "claude", "fr-test"}
        }
        assert rest and set(rest.values()) == {"platform"}, rest
        assert _one(
            conn, "SELECT archived_at IS NOT NULL FROM tenants WHERE id = %s", (t["netcare"],)
        )

    def test_netcare_grants_sole_issuers_with_a_live_licence_and_platform(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        grants = _grants(conn, t["netcare"])
        # fr (fr, kag), w2v (w2v), merqi (marketing): each the SOLE issuer of a live
        # app; platform as infrastructure. `workers` (platform+fr) adds nobody.
        assert grants == {
            "fr": "migration-050",
            "w2v": "migration-050",
            "merqi": "migration-050",
            "platform": "platform-infrastructure",
        }, grants
        assert "commonpractice" not in grants  # the owner needs none
        assert "book-fulfillment" not in grants  # expired licence only

    def test_ola_armatys_is_merqis_and_w2v_keeps_a_grant(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        assert _grants(conn, t["ola"]) == {
            "w2v": "migration-050",
            "fr": "migration-050",
            "platform": "platform-infrastructure",
        }

    def test_tenant_dev_stays_platform_and_merqi_keeps_a_grant(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        assert _owners(conn)["tenant-dev"] == "platform"
        assert _grants(conn, t["tenant_dev"]) == {
            "merqi": "migration-050",
            "w2v": "migration-050",
            "fr": "migration-050",
        }

    def test_no_grant_for_revoked_licences_or_the_owner(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        assert _grants(conn, t["cp_e2e"]) == {"platform": "platform-infrastructure"}

    def test_a_multi_issuer_app_alone_grants_nobody(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        assert _grants(conn, t["plain"]) == {}
        assert _grants(conn, t["ir_only"]) == {}
        assert _grants(conn, t["cp_live"]) == {"commonpractice": "migration-050"}

    def test_commonpractice_keeps_its_registered_shared_tenants(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        assert _grants(conn, t["rmt"]) == {"commonpractice": "migration-050"}

    def test_every_non_platform_tenant_has_a_platform_grant(self, conn):
        _seed(conn)
        _apply(conn, UP)
        missing = conn.execute(
            "SELECT name FROM tenants t WHERE owner_key <> 'platform' AND NOT EXISTS ("
            "SELECT 1 FROM tenant_app_grant g WHERE g.tenant_id = t.id "
            "AND g.grantee_owner_key = 'platform' AND g.revoked_at IS NULL)"
        ).fetchall()
        assert missing == []
        assert not _one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM tenant_app_grant g JOIN tenants t "
            "ON t.id = g.tenant_id WHERE t.owner_key = g.grantee_owner_key)",
        )

    def test_seed_rows_equal_the_boot_seed_constants(self, conn):
        _seed(conn)
        _apply(conn, UP)
        assert {
            (r[0], r[1]) for r in conn.execute("SELECT key, display_name FROM tenant_owner")
        } == (
            set(TENANT_OWNERS)
            - M057_TENANT_OWNERS
            - M067_TENANT_OWNERS
            - M070_TENANT_OWNERS
            - M082_TENANT_OWNERS
        )  # 057 kag, 067 strata, 070 agent
        assert {
            (r[0], r[1], r[2], r[3])
            for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
        } == (
            set(API_CLIENTS)
            - M054_API_CLIENTS
            - M055_API_CLIENTS
            - M056_API_CLIENTS
            - M057_API_CLIENTS
            - M059_API_CLIENTS
            - M060_API_CLIENTS
            - M061_API_CLIENTS
            - M064_API_CLIENTS
            - M067_API_CLIENTS
            - M068_API_CLIENTS
            - M070_API_CLIENTS
            - M078_API_CLIENTS
            - M082_API_CLIENTS
            - M083_API_CLIENTS
            - M085_API_CLIENTS
            - M090_API_CLIENTS
        )  # 054/055/056/059 seed minters, 057 kag's client
        issuers: dict[str, set[str]] = {}
        for key, owner in conn.execute(
            "SELECT a.key, ai.owner_key FROM app_issuer ai JOIN app a ON a.id = ai.app_id"
        ).fetchall():
            issuers.setdefault(key, set()).add(owner)
        all_apps = {r[0] for r in conn.execute("SELECT key FROM app")}
        assert set(issuers) == all_apps, "every existing app must get an issuer"
        for key, owners in issuers.items():
            # 057 adds kag to its four apps' issuers.
            assert owners == set(issuers_for_app_key(key)) - M057_ISSUERS.get(key, set()), key

    def test_names_are_unique_per_owner_only(self, conn):
        _seed(conn)
        _apply(conn, UP)
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'netcare', 'fr')",
            (str(uuid.uuid7()),),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'netcare', 'commonpractice')",
                (str(uuid.uuid7()),),
            )
        # No default owner: a writer that forgets it fails, never lands in platform.
        default = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'tenants' AND column_name = 'owner_key'"
        ).fetchone()
        assert default is not None and default[0] is None, default
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, 'defaulted')", (str(uuid.uuid7()),)
            )
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(
                "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'nul', NULL)",
                (str(uuid.uuid7()),),
            )

    def test_rerun_is_a_no_op(self, conn):
        t = _seed(conn)
        _apply(conn, UP)
        before = (_owners(conn), _grants(conn, t["netcare"]))
        counts = _one(conn, "SELECT count(*) FROM tenant_app_grant")
        _apply(conn, UP)
        assert (_owners(conn), _grants(conn, t["netcare"])) == before
        assert _one(conn, "SELECT count(*) FROM tenant_app_grant") == counts

    def test_downgrade_restores_the_global_namespace(self, conn):
        _seed(conn)
        _apply(conn, UP)
        _apply(conn, DOWN)
        for table in ("tenant_owner", "api_client", "app_issuer", "tenant_app_grant"):
            assert _one(conn, "SELECT to_regclass(%s) IS NULL", (f"public.{table}",)), table
        assert not _one(
            conn,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tenants' AND column_name IN ('owner_key', 'external_ref'))",
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO tenants (id, name) VALUES (%s, 'netcare')", (str(uuid.uuid7()),)
            )

    def test_downgrade_refuses_while_two_owners_share_a_name(self, conn):
        _seed(conn)
        _apply(conn, UP)
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'netcare', 'fr')",
            (str(uuid.uuid7()),),
        )
        with pytest.raises(Exception, match="downgrade 050 REFUSED"):
            _apply(conn, DOWN)
        conn.execute("ROLLBACK")
        assert _one(conn, "SELECT count(*) FROM tenants WHERE name = 'netcare'") == 2


class TestServiceTenants:
    def test_svc_tenants_go_to_the_app_that_uses_them_else_platform(self, conn):
        for name, (tid, _owner) in SVC_OWNED.items():
            _tenant(conn, name, tid)
            conn.execute("UPDATE tenants SET kind = 'service' WHERE id = %s", (tid,))
        _licence(conn, SVC_OWNED["svc-commonpractice"][0], "inference-router")
        _licence(conn, SVC_OWNED["svc-tts-synth"][0], "w2v")
        platform_svc = _tenant(conn, "svc-marketing")
        _licence(conn, platform_svc, "inference-router")
        _apply(conn, UP)
        owners = _owners(conn)
        for name, (_tid, owner) in SVC_OWNED.items():
            assert owners[name] == owner, name
        assert owners["svc-marketing"] == "platform"
        # CP OWNS its inference-router holder now: no grant needed, and it was the
        # gap — inference-router (fr + commonpractice) alone granted CP nothing.
        assert _grants(conn, SVC_OWNED["svc-commonpractice"][0]) == {
            "platform": "platform-infrastructure"
        }
        assert _grants(conn, SVC_OWNED["svc-tts-synth"][0]) == {
            "platform": "platform-infrastructure"
        }
        assert _grants(conn, platform_svc) == {}


class TestRunnerCycle:
    """DELETED 2026-10-31 (owner ruling) — the up -> down -> up cycle.

    It replayed every file in ``migrations/downgrades/`` in LIFO order to prove
    that 050's downgrade still reaches ``tenant_owner`` after 36 later
    migrations exist. Removed because NOTHING IN THE SYSTEM CAN RUN A
    DOWNGRADE: ``scripts/apply_migrations.py`` only applies forward (no ``--down``,
    no script, no operator path), so the only consumer of those 61 files was this
    test. It tested its own artefacts.

    The forcing function it created was worse than useless: every migration had
    to ship a rollback that is never executed and frequently cannot be honest —
    050's own is documented LOSSY and fails if two tenants share a name, and
    087's could only have been written as "re-enable these 32 hardcoded ids",
    because ``tenant_app_grant`` records no revoke marker. A rollback that is
    never run and cannot be precise is fiction, and it costs a file plus a test
    per migration.

    The forward half — that every migration applies cleanly and the corpus
    matches the ORM — is covered by tests/unit/test_schema_parity.py, which is
    the arm that catches real drift. The 61 existing downgrade files are left on
    disk, inert; deleting them is a separate cleanup.
    """
