"""scripts/migrate_member_licences_to_tenant.py — member licences -> tenant-level per-app.

Ruling 2026-09-28: bundles and licences are TENANT-level. Pinned here: the default dry
run writes nothing (every transaction READ ONLY, and that guard is shown to bite);
--apply creates/adopts one svc:<tenant>:<app> licence per member app under the tenant's
bundle with the SAME apps and plan, marks (never revokes) the old licences, and is
idempotent; an ambiguous bundle choice refuses the tenant; --retire-confirmed revokes only
confirmed subjects; no token value or hash is ever printed or written.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from models import BundleApp, DatabaseManager
from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "migrate_member_licences_to_tenant", REPO / "scripts" / "migrate_member_licences_to_tenant.py"
)
assert _spec is not None and _spec.loader is not None
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

T1, T2 = SEED_TENANT_ID, SEED_OTHER_TENANT_ID


@pytest.fixture(autouse=True)
def _pinned_token(pinned_service_token):
    """--apply provisions metering through ``main``; never reach a real token endpoint."""


ALICE, BOB = "11111111-aaaa-4aaa-8aaa-111111111111", "22222222-bbbb-4bbb-8bbb-222222222222"


def _bundle(db: DatabaseManager, key: str, app_ids: list[UUID]) -> UUID:
    with db.engine.begin() as conn:
        bid = conn.execute(
            text("INSERT INTO bundle (key, name) VALUES (:k, :k) RETURNING id"), {"k": key}
        ).scalar_one()
    s = db.get_session()
    try:
        for a in app_ids:
            s.add(BundleApp(bundle_id=bid, app_id=a))
        s.commit()
    finally:
        s.close()
    return UUID(str(bid))


@pytest.fixture
def world(db_manager):
    """Apps fr/kag/extra (plans standard+perpetual); bundle 'main' = all three."""
    apps: dict[str, UUID] = {}
    plans: dict[tuple[str, str], UUID] = {}
    for key in ("fr", "kag", "extra"):
        apps[key] = UUID(str(db_manager.create_app(key=key, name=key, issuers=("platform",)).id))
        for code in ("standard", "perpetual"):
            plans[(key, code)] = UUID(
                str(db_manager.create_plan(app_id=apps[key], code=code, name=code).id)
            )
    bundles = {"main": _bundle(db_manager, "main", list(apps.values()))}
    return db_manager, apps, plans, bundles


def _member(db, apps, plans, sub, tenant, app, code="standard"):
    subj = db.create_or_get_subject(keycloak_sub=sub, keycloak_realm="portal")
    lic = db.create_license(
        subject_id=UUID(str(subj.id)),
        app_id=apps[app],
        plan_id=plans[(app, code)],
        tenant_id=tenant,
    )
    _tok, raw = db.create_token(license_id=UUID(str(lic.id)))
    return lic, raw


def _snapshot(db) -> dict[str, object]:
    with db.engine.connect() as conn:
        out: dict[str, object] = {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("tenant_bundle", "license", "token", "subject")
        }
        out["license_rows"] = conn.execute(
            text(
                "SELECT id, status, bundle_id, metadata::text, updated_at FROM license ORDER BY id"
            )
        ).all()
        out["token_rows"] = conn.execute(text("SELECT id, revoked FROM token ORDER BY id")).all()
    return out


def _active(db, tenant):
    with db.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT a.key, s.keycloak_sub, s.keycloak_realm, l.bundle_id, l.plan_id, l.id "
                "FROM license l JOIN app a ON a.id=l.app_id JOIN subject s ON s.id=l.subject_id "
                "WHERE l.tenant_id=:t AND l.status='active' ORDER BY a.key, s.keycloak_sub"
            ),
            {"t": tenant},
        ).all()


class TestDryRun:
    def test_writes_nothing_and_prints_the_per_subject_plan(self, world, capsys):
        db, apps, plans, _ = world
        _member(db, apps, plans, ALICE, T1, "fr")
        _member(db, apps, plans, ALICE, T1, "kag", "perpetual")
        before = _snapshot(db)
        assert mig.main([], db=db) == 0
        assert _snapshot(db) == before
        out = capsys.readouterr().out
        assert "DRY-RUN" in out and "nothing written" in out
        assert f"(new) svc:{T1}:fr" in out and f"(new) svc:{T1}:kag" in out
        assert ALICE in out and "PROVISION tenant_bundle" in out
        assert "create=2" in out

    def test_the_read_only_guard_actually_refuses_a_write(self, world):
        db, *_ = world
        with mig._ReadOnly(db), pytest.raises(DBAPIError, match="read-only"):
            db.ensure_tenant_bundle(T1, world[3]["main"])
        # and it is removed afterwards
        assert db.ensure_tenant_bundle(T1, world[3]["main"]) is True


class TestApply:
    def test_creates_tenant_licences_with_the_same_apps_and_plans(self, world, tmp_path):
        db, apps, plans, bundles = world
        old_fr, raw_fr = _member(db, apps, plans, ALICE, T1, "fr")
        old_kag, _ = _member(db, apps, plans, ALICE, T1, "kag", "perpetual")
        # Live rows hold metadata as JSON null (create_license's default, as old_fr here),
        # SQL NULL, or an object; cover SQL NULL too.
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET metadata = NULL WHERE id = :i"), {"i": old_kag.id}
            )
        out = tmp_path / "changes.json"
        assert mig.main(["--apply", "--changes-out", str(out)], db=db) == 0

        rows = _active(db, T1)
        svc = {r[0]: r for r in rows if r[1].startswith("svc:")}
        assert set(svc) == {"fr", "kag"}, "same apps — 'extra' is in the bundle but NOT provisioned"
        assert svc["fr"][1] == f"svc:{T1}:fr" and svc["fr"][2] == "service"
        assert all(r[3] == bundles["main"] for r in svc.values())
        assert svc["fr"][4] == plans[("fr", "standard")]
        assert svc["kag"][4] == plans[("kag", "perpetual")], "member's plan is carried over"
        assert db.tenant_holds_bundle(T1, bundles["main"])

        # old licences: still active, token still valid, marked pending_retirement
        assert db.introspect_token(raw_fr)["active"] is True
        for old, app in ((old_fr, "fr"), (old_kag, "kag")):
            meta = db.get_license(UUID(str(old.id))).license_metadata[mig.MARK]
            assert meta["state"] == "pending_retirement"
            assert meta["replaced_by"] == str(svc[app][5])

        changes = json.loads(out.read_text())
        entries = {e["app"]: e for e in changes["subjects"][ALICE]}
        assert entries["fr"]["old_license_ids"] == [str(old_fr.id)]
        assert entries["fr"]["new_license_id"] == str(svc["fr"][5])

    def test_is_idempotent(self, world, tmp_path, capsys):
        db, apps, plans, _ = world
        _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "a.json")], db=db) == 0
        first = json.loads((tmp_path / "a.json").read_text())
        capsys.readouterr()
        before = _snapshot(db)
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "b.json")], db=db) == 0
        assert _snapshot(db) == before, "second apply wrote something"
        assert "MIGRATION_AUDIT" not in capsys.readouterr().out
        second = json.loads((tmp_path / "b.json").read_text())
        assert first["subjects"] == second["subjects"]

    def test_adopts_an_unbundled_service_licence(self, world, tmp_path):
        db, apps, plans, bundles = world
        _member(db, apps, plans, ALICE, T1, "fr")
        subj = db.create_or_get_subject(keycloak_sub=f"svc:{T1}:fr", keycloak_realm="service")
        pre = db.create_license(
            subject_id=UUID(str(subj.id)),
            app_id=apps["fr"],
            plan_id=plans[("fr", "standard")],
            tenant_id=T1,
        )
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        assert db.get_license(UUID(str(pre.id))).bundle_id == bundles["main"]
        assert sum(1 for r in _active(db, T1) if r[1].startswith("svc:")) == 1


class TestRefusal:
    def test_ambiguous_bundle_refuses_the_tenant_and_writes_nothing(self, world, capsys):
        db, apps, plans, _ = world
        _bundle(db, "second", [apps["fr"], apps["kag"]])
        _member(db, apps, plans, ALICE, T1, "fr")
        before = _snapshot(db)
        assert mig.main(["--apply", "--changes-out", "-"], db=db) == mig.EXIT_REFUSED
        out = capsys.readouterr().out
        assert "AMBIGUOUS" in out and "main" in out and "second" in out
        assert _snapshot(db) == before
        # an explicit choice resolves it
        assert (
            mig.main(["--apply", "--changes-out", "-", "--bundle-for", f"{T1}=second"], db=db) == 0
        )

    def test_no_bundle_covering_every_app_refuses(self, world, capsys):
        db, apps, plans, _ = world
        orphan = UUID(str(db.create_app(key="orphan", name="orphan", issuers=("platform",)).id))
        db.create_plan(app_id=orphan, code="standard", name="s")
        apps["orphan"] = orphan
        plans[("orphan", "standard")] = db.list_plans(orphan)[0].id
        _member(db, apps, plans, ALICE, T1, "fr")
        _member(db, apps, plans, ALICE, T1, "orphan")
        assert mig.main([], db=db) == mig.EXIT_REFUSED
        assert "NO bundle contains all apps" in capsys.readouterr().out

    def test_a_competing_active_licence_refuses(self, world, capsys):
        db, apps, plans, _ = world
        _member(db, apps, plans, ALICE, T1, "fr")
        other = db.create_or_get_subject(keycloak_sub="some-machine", keycloak_realm="other-realm")
        db.create_license(
            subject_id=UUID(str(other.id)),
            app_id=apps["fr"],
            plan_id=plans[("fr", "standard")],
            tenant_id=T1,
        )
        assert mig.main([], db=db) == mig.EXIT_REFUSED
        assert "left competing" in capsys.readouterr().out


class TestRetire:
    def _migrate(self, world, tmp_path):
        db, apps, plans, _ = world
        a, raw_a = _member(db, apps, plans, ALICE, T1, "fr")
        b, raw_b = _member(db, apps, plans, BOB, T2, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "x.json")], db=db) == 0
        return db, (a, raw_a), (b, raw_b)

    def test_retires_only_confirmed_subjects(self, world, tmp_path):
        db, (a, raw_a), (b, raw_b) = self._migrate(world, tmp_path)
        conf = tmp_path / "confirmed.json"
        conf.write_text(json.dumps({"confirmed": [ALICE]}))
        before = _snapshot(db)
        assert mig.main(["--retire-confirmed", str(conf)], db=db) == 0
        assert _snapshot(db) == before, "retire dry-run wrote"
        # METERING_SERVICE_URL unset in tests -> revoked, teardown reported skipped (exit 5)
        assert (
            mig.main(["--retire-confirmed", str(conf), "--apply"], db=db)
            == mig.EXIT_TEARDOWN_INCOMPLETE
        )
        assert db.get_license(UUID(str(a.id))).status == "revoked"
        assert db.introspect_token(raw_a)["active"] is False
        assert db.get_license(UUID(str(a.id))).license_metadata[mig.MARK]["state"] == "retired"
        assert db.get_license(UUID(str(b.id))).status == "active", "unconfirmed subject touched"
        assert db.introspect_token(raw_b)["active"] is True
        # tenant-level licences untouched
        assert all(r for r in _active(db, T1) if r[1].startswith("svc:"))

    def test_sub_at_tenant_narrows_the_confirmation(self, world, tmp_path):
        db, apps, plans, _ = world
        a1, _ = _member(db, apps, plans, ALICE, T1, "fr")
        a2, _ = _member(db, apps, plans, ALICE, T2, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "x.json")], db=db) == 0
        conf = tmp_path / "c.txt"
        conf.write_text(f"{ALICE}@{T2}\n")
        mig.main(["--retire-confirmed", str(conf), "--apply"], db=db)
        assert db.get_license(UUID(str(a1.id))).status == "active"
        assert db.get_license(UUID(str(a2.id))).status == "revoked"

    def _replacement(self, db, member_id):
        mark = db.get_license(UUID(str(member_id))).license_metadata[mig.MARK]
        return UUID(mark["replaced_by"]), mark["bundle"]

    def test_a_revoked_replacement_is_skipped_and_the_member_stays_active(self, world, tmp_path):
        db, (a, raw_a), _ = self._migrate(world, tmp_path)
        new_id, _bundle_key = self._replacement(db, a.id)
        assert db.revoke_license(new_id) is True
        assert db.get_license(new_id).deleted_at is None, "only is_active() can catch this"
        conf = tmp_path / "c.json"
        conf.write_text(json.dumps([ALICE]))
        assert mig.main(["--retire-confirmed", str(conf), "--apply"], db=db) == 0
        assert db.get_license(UUID(str(a.id))).status == "active"
        assert db.introspect_token(raw_a)["active"] is True
        assert (
            db.get_license(UUID(str(a.id))).license_metadata[mig.MARK]["state"]
            == "pending_retirement"
        )

    def test_a_replacement_moved_to_another_bundle_is_skipped(self, world, tmp_path, capsys):
        db, (a, raw_a), _ = self._migrate(world, tmp_path)
        new_id, recorded = self._replacement(db, a.id)
        assert recorded == "main"
        other = _bundle(db, "other", [world[1]["fr"]])
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET bundle_id = :b WHERE id = :i"), {"b": other, "i": new_id}
            )
        new = db.get_license(new_id)
        assert new.is_active() and new.bundle_id is not None, "still active AND bundled"
        conf = tmp_path / "c.json"
        conf.write_text(json.dumps([ALICE]))
        capsys.readouterr()
        assert mig.main(["--retire-confirmed", str(conf), "--apply"], db=db) == 0
        assert "SKIP" in capsys.readouterr().out
        assert db.get_license(UUID(str(a.id))).status == "active"
        assert db.introspect_token(raw_a)["active"] is True


class _TxnRecorder:
    """Record every statement, grouped per transaction, on ``db.engine``."""

    def __init__(self, engine):
        self.engine = engine
        self.txns: list[list[str]] = []

    def _begin(self, conn):
        self.txns.append([])

    def _stmt(self, conn, cursor, statement, parameters, context, executemany):
        if not self.txns:  # a statement outside any begin() would show up here
            self.txns.append(["<no begin>"])
        self.txns[-1].append(statement.strip())

    def __enter__(self):
        # Registered BEFORE the script's own begin listener, so it opens the record
        # first and the script's SET TRANSACTION lands inside it.
        event.listen(self.engine, "begin", self._begin)
        event.listen(self.engine, "before_cursor_execute", self._stmt)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "begin", self._begin)
        event.remove(self.engine, "before_cursor_execute", self._stmt)


def test_every_dry_run_transaction_is_read_only(world, capsys):
    db, apps, plans, _ = world
    _bundle(db, "non-agentic-ai", [apps["fr"]])
    apps["w2v"] = UUID(
        str(db.create_app(key="w2v", name="w2v", issuers=("platform",)).id)
    )  # in no bundle
    plans[("w2v", "standard")] = UUID(
        str(db.create_plan(app_id=apps["w2v"], code="standard", name="s").id)
    )
    _member(db, apps, plans, ALICE, T1, "fr")
    _member(db, apps, plans, ALICE, T1, "w2v")
    with _TxnRecorder(db.engine) as rec:
        assert mig.main(["--extend-bundle", "non-agentic-ai"], db=db) == 0
    assert "would add 1 app(s)" in capsys.readouterr().out
    assert len(rec.txns) > 3, "positive control: the dry run opened transactions"
    bundle_lookups = [t for t in rec.txns if any("FROM bundle" in s for s in t)]
    assert bundle_lookups, "positive control: the bundle lookups were recorded"
    not_ro = [t for t in rec.txns if not t or t[0] != "SET TRANSACTION READ ONLY"]
    assert not not_ro, f"{len(not_ro)} dry-run transaction(s) not READ ONLY: {not_ro[:2]}"


def test_no_token_value_or_hash_is_printed_or_written(world, tmp_path, capsys):
    db, apps, plans, _ = world
    _, raw1 = _member(db, apps, plans, ALICE, T1, "fr")
    _, raw2 = _member(db, apps, plans, ALICE, T1, "kag")
    mig.main([], db=db)
    changes = tmp_path / "t.json"
    mig.main(["--apply", "--changes-out", str(changes)], db=db)
    conf = tmp_path / "c.json"
    conf.write_text(json.dumps([ALICE]))
    mig.main(["--retire-confirmed", str(conf), "--apply"], db=db)
    cap = capsys.readouterr()
    blob = cap.out + cap.err + changes.read_text()
    with db.engine.connect() as conn:
        hashes = [r[0] for r in conn.execute(text("SELECT token_hash FROM token")).all()]
    assert hashes, "positive control: tokens exist"
    for secret in (raw1, raw2, *hashes):
        assert secret not in blob


# ── ruling 2026-09-28: default bundle, --extend-bundle, realm scope ──────────────


class TestDefaultBundle:
    def test_ambiguity_defaults_to_non_agentic_ai_and_prints_why(self, world, capsys):
        db, apps, plans, bundles = world
        nai = _bundle(db, "non-agentic-ai", [apps["fr"], apps["kag"]])
        _bundle(db, "knowledge", [apps["fr"]])
        _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main([], db=db) == 0
        out = capsys.readouterr().out
        assert "AMBIGUOUS" not in out
        assert "bundle=non-agentic-ai" in out
        assert "bundle reason: default non-agentic-ai: among 3 fitting" in out
        assert " default" in out.split("tenants:")[0], "reason column in the plan table"
        assert mig.main(["--apply", "--changes-out", "-"], db=db) == 0
        assert db.tenant_holds_bundle(T1, nai) and not db.tenant_holds_bundle(T1, bundles["main"])

    def test_bundle_for_still_overrides_the_default(self, world, capsys):
        db, apps, plans, bundles = world
        _bundle(db, "non-agentic-ai", [apps["fr"]])
        _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", "-", "--bundle-for", f"{T1}=main"], db=db) == 0
        assert db.tenant_holds_bundle(T1, bundles["main"])
        assert "bundle reason: --bundle-for" in capsys.readouterr().out


APPLY_EXT = ["--extend-bundle", "non-agentic-ai", "--apply", "--changes-out", "-"]
APPLY_EXT += ["--confirm-bundle-holders"]


class TestExtendBundle:
    @pytest.fixture
    def gap(self, world):
        """non-agentic-ai = {fr}; tenant T1 holds fr + extra + w2v -> no bundle fits."""
        db, apps, plans, _ = world
        nai = _bundle(db, "non-agentic-ai", [apps["fr"]])
        apps["w2v"] = UUID(str(db.create_app(key="w2v", name="w2v", issuers=("platform",)).id))
        plans[("w2v", "standard")] = UUID(
            str(db.create_plan(app_id=apps["w2v"], code="standard", name="s").id)
        )
        for a in ("fr", "w2v"):
            _member(db, apps, plans, ALICE, T1, a)
        _member(db, apps, plans, BOB, T1, "extra")  # 'extra' is in 'main' only
        return db, apps, nai

    def _nai_apps(self, db, nai):
        return {str(a.key) for a in db.list_bundle_apps(nai)}

    def test_dry_run_lists_the_apps_and_writes_nothing(self, gap, capsys):
        db, _apps, nai = gap
        before = (_snapshot(db), self._nai_apps(db, nai))
        assert mig.main(["--extend-bundle", "non-agentic-ai"], db=db) == 0
        out = capsys.readouterr().out
        assert "would add 2 app(s) to its app set: ['extra', 'w2v']" in out
        assert "extended" in out and "bundle=non-agentic-ai" in out
        assert (_snapshot(db), self._nai_apps(db, nai)) == before
        # without the flag the same tenant is refused
        assert mig.main([], db=db) == mig.EXIT_REFUSED

    def test_apply_writes_bundle_app_then_licences_and_is_idempotent(self, gap, tmp_path, capsys):
        db, _apps, nai = gap
        assert mig.main([*APPLY_EXT, "0"], db=db) == 0
        out = capsys.readouterr().out
        assert out.count('"action": "bundle_app_insert"') == 2
        assert self._nai_apps(db, nai) == {"fr", "extra", "w2v"}
        svc = {r[0] for r in _active(db, T1) if r[1].startswith("svc:") and r[3] == nai}
        assert svc == {"fr", "extra", "w2v"}
        assert mig.main([*APPLY_EXT, "0"], db=db) == 0
        assert "bundle_app_insert" not in capsys.readouterr().out

    def test_revoked_app_is_refused_not_added(self, gap, capsys):
        db, apps, nai = gap
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE app SET revoked = true WHERE id = :i"), {"i": apps["w2v"]})
        assert mig.main([*APPLY_EXT, "0"], db=db) == mig.EXIT_REFUSED
        assert "unknown or revoked app(s) ['w2v']" in capsys.readouterr().out
        assert self._nai_apps(db, nai) == {"fr"}, "refused tenant's apps were still added"

    def test_unknown_extend_bundle_is_a_usage_error(self, world):
        db, *_ = world
        assert mig.main(["--extend-bundle", "nope"], db=db) == mig.EXIT_USAGE

    def test_ensure_bundle_app_refuses_an_unknown_app_id(self, world):
        db, _apps, _plans, bundles = world
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            db.ensure_bundle_app(bundles["main"], UUID("00000000-0000-4000-8000-00000000dead"))


class TestRealmScope:
    def _at(self, db, apps, plans, sub, realm, app="fr"):
        subj = db.create_or_get_subject(keycloak_sub=sub, keycloak_realm=realm)
        return db.create_license(
            subject_id=UUID(str(subj.id)),
            app_id=apps[app],
            plan_id=plans[(app, "standard")],
            tenant_id=T1,
        )

    def test_default_is_portal_plus_secure_apps_and_never_svc(self, world, capsys):
        db, apps, plans, _ = world
        self._at(db, apps, plans, ALICE, "portal")
        self._at(db, apps, plans, "subhankar", "secure-apps", "kag")
        self._at(db, apps, plans, "elsewhere", "other-realm", "extra")
        self._at(
            db, apps, plans, f"svc:{T2}:fr", "secure-apps", "fr"
        )  # tenant-level: never a source
        mig.main([], db=db)
        out = capsys.readouterr().out
        assert "realms in scope: portal,secure-apps" in out
        assert ALICE in out and "subhankar" in out
        assert "elsewhere" not in out and f"svc:{T2}:fr" not in out.split("tenants:")[0].replace(
            f"(new) svc:{T1}", ""
        )

    def test_all_and_comma_separated(self, world, capsys):
        db, apps, plans, _ = world
        self._at(db, apps, plans, "elsewhere", "other-realm", "extra")
        self._at(db, apps, plans, "svc:x:y", "other-realm", "kag")  # svc: in any realm
        self._at(db, apps, plans, "svc-machine", "service", "fr")  # service principal
        mig.main(["--realm", "all"], db=db)
        out = capsys.readouterr().out
        assert (
            "realms in scope: ALL except service" in out
            and "elsewhere" in out
            and "svc:x:y" not in out
        )
        assert "svc-machine" not in out
        mig.main(["--realm", "portal,other-realm"], db=db)
        assert "realms in scope: other-realm,portal" in capsys.readouterr().out


# ── metering: a created licence is provisioned like the HTTP create path ─────────


def _stamped(db, license_id) -> bool:
    return db.get_license(UUID(str(license_id))).metering_provisioned_at is not None


@pytest.fixture
def metered_world(world):
    """world, with fr's `standard` plan owing billing a token budget and a quota."""
    db, apps, plans, bundles = world
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE plan SET token_budget_monthly = 1000, quota_monthly = 50 WHERE id = :i"),
            {"i": plans[("fr", "standard")]},
        )
    return world


@pytest.fixture
def pushes(monkeypatch):
    """The billing client: both pushes recorded, both accepted (tests flip the verdict)."""
    from unittest.mock import AsyncMock

    import main

    budget = AsyncMock(return_value=True)
    quota = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "_push_token_budget", budget)
    monkeypatch.setattr(main, "_push_quota_config", quota)
    return budget, quota


class TestMetering:
    def test_apply_provisions_the_created_licence_and_stamps_it(
        self, metered_world, pushes, tmp_path
    ):
        db, apps, plans, _ = metered_world
        _member(db, apps, plans, ALICE, T1, "fr")
        out = tmp_path / "c.json"
        assert mig.main(["--apply", "--changes-out", str(out)], db=db) == 0
        (new,) = [r for r in _active(db, T1) if r[1] == f"svc:{T1}:fr"]
        budget, quota = pushes
        budget.assert_awaited_once()
        assert budget.await_args.kwargs["license_id"] == str(new[5])
        assert budget.await_args.kwargs["tenant_id"] == T1
        assert budget.await_args.kwargs["budget_tokens"] == 1000
        assert quota.await_args.kwargs["quota_monthly"] == 50
        assert quota.await_args.kwargs["app_id"] == str(apps["fr"])
        assert _stamped(db, new[5])
        (entry,) = json.loads(out.read_text())["subjects"][ALICE]
        assert entry["metering_provisioned"] is True

    def test_dry_run_makes_no_metering_call_and_reports_what_it_would_push(
        self, metered_world, pushes, capsys
    ):
        db, apps, plans, _ = metered_world
        _member(db, apps, plans, ALICE, T1, "fr")
        before = _snapshot(db)
        assert mig.main([], db=db) == 0
        assert _snapshot(db) == before
        budget, quota = pushes
        budget.assert_not_awaited()
        quota.assert_not_awaited()
        assert "metering fr: would push token_budget=1000, quota=50" in capsys.readouterr().out

    def test_billing_down_creates_the_licence_leaves_the_stamp_null_and_reports(
        self, metered_world, monkeypatch, tmp_path, capsys
    ):
        """The REAL pushes, against a billing that refuses the connection."""
        from unittest.mock import AsyncMock, patch

        import httpx

        import main

        db, apps, plans, _ = metered_world
        old, _ = _member(db, apps, plans, ALICE, T1, "fr")
        monkeypatch.setattr(main, "METERING_SERVICE_URL", "http://billing.invalid")
        monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__ = AsyncMock(return_value=client)
            cls.return_value.__aexit__ = AsyncMock(return_value=False)
            rc = mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db)
        assert rc == mig.EXIT_METERING_PENDING
        assert client.post.await_count >= 1, "billing was never even tried"
        (new,) = [r for r in _active(db, T1) if r[1] == f"svc:{T1}:fr"]
        assert not _stamped(db, new[5]), "stamped a licence billing never accepted"
        err = capsys.readouterr().err
        assert "METERING NOT PROVISIONED" in err and str(new[5]) in err
        # the old licence is still marked (the migration itself went ahead)
        meta = db.get_license(UUID(str(old.id))).license_metadata[mig.MARK]
        assert meta["state"] == "pending_retirement"

    def test_an_adopted_licence_is_never_pushed_to(self, metered_world, pushes, tmp_path):
        db, apps, plans, _ = metered_world
        _member(db, apps, plans, ALICE, T1, "fr")
        svc = db.create_or_get_subject(keycloak_sub=f"svc:{T1}:fr", keycloak_realm="service")
        db.create_license(
            subject_id=UUID(str(svc.id)),
            app_id=apps["fr"],
            plan_id=plans[("fr", "standard")],
            tenant_id=T1,
        )
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        pushes[0].assert_not_awaited()  # a push OVERWRITES; existing rows go to the sweep


def _retire_file(tmp_path, *subs: str):
    conf = tmp_path / "confirmed.json"
    conf.write_text(json.dumps({"confirmed": list(subs)}))
    return conf


class TestRetireMeteringGate:
    def test_refuses_when_the_replacement_is_unmetered(self, world, tmp_path, capsys):
        """MUTATION PROVEN RED: drop the metering_provisioned_at check in
        ``_metering_refusal`` and the old licence is revoked here."""
        db, apps, plans, _ = world
        old, _ = _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        (new,) = [r for r in _active(db, T1) if r[1] == f"svc:{T1}:fr"]
        assert _stamped(db, new[5])
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET metering_provisioned_at = NULL WHERE id = :i"),
                {"i": new[5]},
            )
        capsys.readouterr()
        assert (
            mig.main(["--retire-confirmed", str(_retire_file(tmp_path, ALICE)), "--apply"], db=db)
            == 0
        )
        out = capsys.readouterr().out
        assert "NOT metered" in out and "revoke=0 skipped=1" in out
        assert db.get_license(UUID(str(old.id))).status == "active"

    def test_budget_owing_replacement_needs_billings_budget_row(
        self, metered_world, pushes, tmp_path, capsys, fake_billing, billing_configured
    ):
        """Same filter the sweep counts as coverage: ACTIVE + source=license_sync."""
        db, apps, plans, _ = metered_world
        old, _ = _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        (new,) = [r for r in _active(db, T1) if r[1] == f"svc:{T1}:fr"]
        conf = _retire_file(tmp_path, ALICE)

        # billing holds only a soft-deleted row and a manual one: NOT coverage
        fake_billing(
            {
                T1: [
                    {
                        "id": "b0",
                        "license_id": str(new[5]),
                        "source": "license_sync",
                        "active": False,
                    },
                    {"id": "b1", "license_id": str(new[5]), "source": "manual", "active": True},
                ]
            }
        )
        capsys.readouterr()
        assert mig.main(["--retire-confirmed", str(conf), "--apply"], db=db) == 0
        assert "no active license_sync budget" in capsys.readouterr().out
        assert db.get_license(UUID(str(old.id))).status == "active"

    def test_billing_unknown_refuses(
        self, metered_world, pushes, tmp_path, capsys, fake_billing, billing_configured
    ):
        db, apps, plans, _ = metered_world
        old, _ = _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        fake_billing({}, list_status=403)
        capsys.readouterr()
        conf = _retire_file(tmp_path, ALICE)
        assert mig.main(["--retire-confirmed", str(conf), "--apply"], db=db) == 0
        assert "UNKNOWN" in capsys.readouterr().out
        assert db.get_license(UUID(str(old.id))).status == "active"

    def test_retires_when_billing_holds_the_budget(
        self, metered_world, pushes, tmp_path, capsys, fake_billing, billing_configured
    ):
        db, apps, plans, _ = metered_world
        old, _ = _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        (new,) = [r for r in _active(db, T1) if r[1] == f"svc:{T1}:fr"]
        fake_billing(
            {
                T1: [
                    {
                        "id": "b9",
                        "license_id": str(new[5]),
                        "source": "license_sync",
                        "active": True,
                    }
                ]
            }
        )
        conf = _retire_file(tmp_path, ALICE)
        assert mig.main(["--retire-confirmed", str(conf), "--apply"], db=db) == 0
        assert db.get_license(UUID(str(old.id))).status == "revoked"


class TestRetireGateJudgesTheEffectiveBudget:
    """#241 audit nit: ``_metering_refusal`` reads the replacement's EFFECTIVE budget
    (``models.effective_metering``: its own token_budget_override, else the plan's),
    not the plan's alone. An override on an UNLIMITED plan owes billing a budget, so
    the gate must demand billing's budget row before the old licence is retired."""

    def _replacement(self, world, tmp_path):
        db, apps, plans, _ = world
        _member(db, apps, plans, ALICE, T1, "fr")
        assert mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db) == 0
        (new,) = [r for r in _active(db, T1) if r[1] == f"svc:{T1}:fr"]
        assert _stamped(db, new[5])
        return db, UUID(str(new[5]))

    def test_unlimited_plan_without_override_owes_nothing(self, world, tmp_path, monkeypatch):
        db, new_id = self._replacement(world, tmp_path)
        calls: list[str] = []
        monkeypatch.setattr(mig, "_billing_budget_ids", lambda lid, tid: calls.append(lid))
        assert mig._metering_refusal(db, db.get_license(new_id)) == ""
        assert calls == [], "an unlimited plan with no override must not consult billing"

    def test_override_on_unlimited_plan_needs_billings_budget_row(
        self, world, tmp_path, monkeypatch
    ):
        db, new_id = self._replacement(world, tmp_path)
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET token_budget_override = 500 WHERE id = :i"),
                {"i": new_id},
            )
        monkeypatch.setattr(mig, "_billing_budget_ids", lambda lid, tid: set())
        refusal = mig._metering_refusal(db, db.get_license(new_id))
        assert "no active license_sync budget" in refusal
        monkeypatch.setattr(mig, "_billing_budget_ids", lambda lid, tid: {"b1"})
        assert mig._metering_refusal(db, db.get_license(new_id)) == ""


# ── validator 2026-09-28: --extend-bundle mutates a SHARED catalogue bundle ─────────


class TestExtendBundleHolders:
    """``bundle_app`` is shared by every holder of the bundle: list them, and make
    ``--apply`` name their count, re-read inside the write transaction."""

    @pytest.fixture
    def gap(self, world):
        db, apps, plans, _ = world
        nai = _bundle(db, "non-agentic-ai", [apps["fr"]])
        apps["w2v"] = UUID(str(db.create_app(key="w2v", name="w2v", issuers=("platform",)).id))
        plans[("w2v", "standard")] = UUID(
            str(db.create_plan(app_id=apps["w2v"], code="standard", name="s").id)
        )
        for a in ("fr", "w2v"):
            _member(db, apps, plans, ALICE, T1, a)
        # T2 already HOLDS non-agentic-ai, with one licence carrying it.
        assert db.ensure_tenant_bundle(T2, nai)
        svc = db.create_or_get_subject(keycloak_sub=f"svc:{T2}:fr", keycloak_realm="service")
        db.create_license(
            subject_id=UUID(str(svc.id)),
            app_id=apps["fr"],
            plan_id=plans[("fr", "standard")],
            tenant_id=T2,
            bundle_id=nai,
        )
        return db, apps, nai

    def _nai_apps(self, db, nai):
        return {str(a.key) for a in db.list_bundle_apps(nai)}

    def test_dry_run_lists_every_holder_and_what_it_gains(self, gap, capsys):
        db, _apps, nai = gap
        assert mig.main(["--extend-bundle", "non-agentic-ai"], db=db) == 0
        out = capsys.readouterr().out
        t2 = db.get_tenant(T2)
        assert "current holders: 1" in out
        assert f"holder {T2} ({t2.name}) licences=1 gains ['w2v']" in out
        assert "--apply requires --confirm-bundle-holders 1" in out
        assert self._nai_apps(db, nai) == {"fr"}

    def test_apply_without_the_confirmation_refuses_and_writes_nothing(self, gap, capsys):
        db, _apps, nai = gap
        before = _snapshot(db)
        rc = mig.main(["--extend-bundle", "non-agentic-ai", "--apply", "--changes-out", "-"], db=db)
        assert rc == mig.EXIT_USAGE
        assert "--confirm-bundle-holders 1" in capsys.readouterr().err
        assert self._nai_apps(db, nai) == {"fr"}
        assert _snapshot(db) == before

    def test_a_wrong_count_refuses_and_writes_nothing(self, gap, capsys):
        db, _apps, nai = gap
        before = _snapshot(db)
        assert mig.main([*APPLY_EXT, "0"], db=db) == mig.EXIT_REFUSED
        assert "has 1 holder(s) now" in capsys.readouterr().err
        assert self._nai_apps(db, nai) == {"fr"}
        assert _snapshot(db) == before

    def test_the_right_count_applies(self, gap):
        db, _apps, nai = gap
        assert mig.main([*APPLY_EXT, "1"], db=db) == 0
        assert self._nai_apps(db, nai) == {"fr", "w2v"}

    def test_the_count_is_re_read_inside_the_write_transaction(self, gap, monkeypatch, capsys):
        """A holder that appears AFTER the plan was read still refuses the write."""
        db, _apps, nai = gap
        real = db.list_bundle_holders

        def stale_then_new_holder(bid):
            rows = real(bid)  # what the operator reviewed: 1 holder
            db.ensure_tenant(
                "01000000-0000-7000-8000-0000000000c3",
                name="late-holder",
                kind="customer",
                owner_key="platform",
            )
            db.ensure_tenant_bundle("01000000-0000-7000-8000-0000000000c3", nai)
            return rows

        monkeypatch.setattr(db, "list_bundle_holders", stale_then_new_holder)
        assert mig.main([*APPLY_EXT, "1"], db=db) == mig.EXIT_REFUSED
        assert "has 2 holder(s) now" in capsys.readouterr().err
        assert self._nai_apps(db, nai) == {"fr"}


class TestExitCodesDoNotMask:
    def test_refused_plus_unmetered_is_7(self, metered_world, pushes, tmp_path, capsys):
        db, apps, plans, _ = metered_world
        budget, _quota = pushes
        budget.return_value = False
        _member(db, apps, plans, ALICE, T1, "fr")  # created, billing refuses -> unmetered
        _member(db, apps, plans, BOB, T2, "fr")  # refused: a competing licence
        other = db.create_or_get_subject(keycloak_sub="some-machine", keycloak_realm="other-realm")
        db.create_license(
            subject_id=UUID(str(other.id)),
            app_id=apps["fr"],
            plan_id=plans[("fr", "standard")],
            tenant_id=T2,
        )
        rc = mig.main(["--apply", "--changes-out", str(tmp_path / "c.json")], db=db)
        assert rc == mig.EXIT_REFUSED_AND_METERING_PENDING == 7
        assert "METERING NOT PROVISIONED" in capsys.readouterr().err

    def test_final_exit_table(self):
        assert mig.final_exit(False, False) == 0
        assert mig.final_exit(True, False) == 3
        assert mig.final_exit(False, True) == 6
        assert mig.final_exit(True, True) == 7

    def test_usage_text_documents_7(self, capsys):
        with pytest.raises(SystemExit):
            mig._parse_args(["--help"])
        assert "7  both 3 and 6" in capsys.readouterr().out


class TestServiceSubjectLength:
    def test_an_app_key_too_long_for_the_subject_refuses_the_tenant(self, world, capsys):
        from models import SERVICE_SUBJECT_APP_KEY_MAX_LEN

        db, apps, plans, bundles = world
        key = "k" * (SERVICE_SUBJECT_APP_KEY_MAX_LEN + 1)
        apps[key] = UUID(str(db.create_app(key=key, name="long", issuers=("platform",)).id))
        plans[(key, "standard")] = UUID(
            str(db.create_plan(app_id=apps[key], code="standard", name="s").id)
        )
        db.ensure_bundle_app(bundles["main"], apps[key])
        _member(db, apps, plans, ALICE, T1, key)
        before = _snapshot(db)
        assert mig.main(["--apply", "--changes-out", "-"], db=db) == mig.EXIT_REFUSED
        assert f"allows at most {SERVICE_SUBJECT_APP_KEY_MAX_LEN}" in capsys.readouterr().out
        assert _snapshot(db) == before

    def test_the_longest_legal_key_fits_varchar_255(self, world):
        from models import SERVICE_SUBJECT_APP_KEY_MAX_LEN

        db, apps, plans, bundles = world
        key = "k" * SERVICE_SUBJECT_APP_KEY_MAX_LEN
        apps[key] = UUID(str(db.create_app(key=key, name="long", issuers=("platform",)).id))
        plans[(key, "standard")] = UUID(
            str(db.create_plan(app_id=apps[key], code="standard", name="s").id)
        )
        db.ensure_bundle_app(bundles["main"], apps[key])
        _member(db, apps, plans, ALICE, T1, key)
        assert mig.main(["--apply", "--changes-out", "-"], db=db) == 0
        (row,) = [r for r in _active(db, T1) if r[1].startswith("svc:")]
        assert len(row[1]) == 255
