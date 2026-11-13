"""End-to-end tests for ``scripts/provision_demo_tenant.py`` against the Postgres test DB.

Every case drives the real ``main(argv)`` and then measures the database, because the
claims under test are about what is (and is NOT) written: the refusal paths must write
nothing, the fingerprint abort must be a real rollback, dry-run must be read-only, a
re-run must not mint duplicates, and no path may touch a token outside the demo tenant.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from models import DatabaseManager, Token
from tests.conftest import SEED_TENANT_ID

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "provision_demo_tenant.py",
)
_spec = importlib.util.spec_from_file_location("provision_demo_tenant", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
pdt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pdt  # @dataclass resolves its module via sys.modules
_spec.loader.exec_module(pdt)

DEMO_ID = pdt.DEMO_TENANT_ID
N8N_DEV_ID = UUID("019de842-c92a-7000-8000-00000000abcd")
# Deliberately NOT the column default ('byo_required'), so a copy is distinguishable
# from a default that merely happens to match.
N8N_DEV_BYO = "local_allowed"
OWNER_SUB = "demo-owner-keycloak-sub"
APPS = ("kanban", "kag")


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def seeded(db_manager: DatabaseManager, monkeypatch) -> dict[str, str]:
    """n8n-dev tenant, two apps with `standard` plans, and one PRE-EXISTING outside
    licence + token (the credential every test proves is left untouched)."""
    url = db_manager.engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")

    db_manager.ensure_tenant(N8N_DEV_ID, name="n8n-dev", byo_mode=N8N_DEV_BYO, owner_key="platform")
    plans = {}
    for key in APPS:
        app = db_manager.create_app(key=key, name=key, description=key, issuers=("platform",))
        plans[key] = db_manager.create_plan(
            app_id=UUID(str(app.id)), code="standard", name="Standard"
        )
    outside_subject = db_manager.create_or_get_subject("outside-sub", "portal")
    outside_lic = db_manager.create_license(
        subject_id=UUID(str(outside_subject.id)),
        app_id=UUID(str(plans["kanban"].app_id)),
        plan_id=UUID(str(plans["kanban"].id)),
        tenant_id=SEED_TENANT_ID,
    )
    outside_tok, outside_raw = db_manager.create_token(license_id=UUID(str(outside_lic.id)))
    return {
        "outside_license_id": str(outside_lic.id),
        "outside_token_id": str(outside_tok.id),
        "outside_raw": outside_raw,
    }


def _argv(*extra: str, apps: str = ",".join(APPS), apply: bool = True) -> list[str]:
    """Most tests exercise the write path, so they pass --apply explicitly."""
    return [
        "--owner-sub",
        OWNER_SUB,
        "--tenant-owner",
        "platform",
        "--apps",
        apps,
        *(["--apply"] if apply else []),
        *extra,
    ]


def _rc(argv: list[str]) -> int:
    """Exit code of main(), whether returned or raised by argparse."""
    try:
        return pdt.main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


def _snapshot(db_manager: DatabaseManager) -> dict[str, object]:
    """Everything a guard promises not to change, read straight from the DB."""
    with db_manager.engine.connect() as conn:
        return {
            "tenants": conn.execute(
                text("SELECT id, name, kind, byo_mode FROM tenants ORDER BY id")
            ).all(),
            "licences": conn.execute(
                text("SELECT id, tenant_id, status FROM license ORDER BY id")
            ).all(),
            "tokens": conn.execute(
                text("SELECT id, license_id, revoked FROM token ORDER BY id")
            ).all(),
            "plans": conn.execute(text("SELECT id, code FROM plan ORDER BY id")).all(),
        }


def _demo_tokens(db_manager: DatabaseManager) -> Sequence[Any]:
    with db_manager.engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT t.id, t.token_hash, t.revoked, a.key FROM token t "
                "JOIN license l ON l.id = t.license_id JOIN app a ON a.id = l.app_id "
                "WHERE l.tenant_id = :tid ORDER BY t.created_at"
            ),
            {"tid": DEMO_ID},
        ).all()


def _assert_outside_untouched(db_manager: DatabaseManager, seeded: dict[str, str]) -> None:
    with db_manager.engine.connect() as conn:
        row = conn.execute(
            text("SELECT revoked, revoked_at, license_id FROM token WHERE id = :id"),
            {"id": seeded["outside_token_id"]},
        ).one()
    assert row == (False, None, UUID(seeded["outside_license_id"]))


@pytest.fixture
def forbid_token_mutators(monkeypatch) -> None:
    """Any call to a token-mutating DatabaseManager method fails the test loudly."""

    def _boom(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"provision_demo_tenant called DatabaseManager.{name}")

        return _raise

    for name in dir(DatabaseManager):
        if name.startswith(("regenerate", "revoke", "update")) or name in {
            "set_tenant_byo_mode",
            "set_tenant_archived",
        }:
            monkeypatch.setattr(DatabaseManager, name, _boom(name))


# --- happy path ------------------------------------------------------------------


def test_first_run_provisions_tenant_licences_and_tokens(
    db_manager, seeded, capsys, caplog, forbid_token_mutators
) -> None:
    rc = pdt.main(_argv())
    out, err = capsys.readouterr()

    assert rc == 0
    tenant = db_manager.get_tenant(str(DEMO_ID))
    assert tenant is not None
    assert (tenant.name, tenant.kind, tenant.cost_centre, tenant.keycloak_realm) == (
        "demo",
        "test",
        "tests",
        "portal",
    )
    assert tenant.byo_mode == N8N_DEV_BYO  # copied from n8n-dev at runtime

    payload = json.loads(out)
    assert payload["tenant_id"] == str(DEMO_ID)
    assert set(payload["tokens"]) == set(APPS)
    rows = _demo_tokens(db_manager)
    assert {r[3] for r in rows} == set(APPS)
    for raw_key, raw in payload["tokens"].items():
        match = [r for r in rows if r[3] == raw_key]
        assert match[0][1] == Token.hash_token(raw)
        assert raw not in err and raw not in caplog.text  # printed once, never logged
        assert match[0][1] not in err and match[0][1] not in caplog.text

    with db_manager.engine.connect() as conn:
        subs = conn.execute(
            text(
                "SELECT DISTINCT s.keycloak_sub FROM license l JOIN subject s "
                "ON s.id = l.subject_id WHERE l.tenant_id = :tid"
            ),
            {"tid": DEMO_ID},
        ).all()
    assert subs == [(OWNER_SUB,)]
    assert "fingerprint before" in err and "fingerprint after" in err
    _assert_outside_untouched(db_manager, seeded)


@pytest.mark.parametrize(
    "extra",
    [
        ("--tenant-id", str(N8N_DEV_ID)),
        ("--tenant-name", "n8n-dev"),
        ("--tenant-id", pdt.DEMO_TENANT_ID.hex),
        ("--tenant-name", "demo"),
    ],
)
def test_the_tenant_is_not_an_argument(db_manager, seeded, extra, capsys) -> None:
    before = _snapshot(db_manager)
    assert _rc(_argv(*extra)) == 2
    assert _snapshot(db_manager) == before


def _victim(db_manager) -> tuple[str, str]:
    """A live n8n-dev kanban licence + token: the auditor's victim."""
    app = db_manager.get_app_by_key("kanban")
    plan = [p for p in db_manager.list_plans(UUID(str(app.id))) if p.code == "standard"][0]
    sub = db_manager.create_or_get_subject("n8n-dev-user", "portal")
    lic = db_manager.create_license(
        subject_id=UUID(str(sub.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=str(N8N_DEV_ID),
    )
    tok, _ = db_manager.create_token(license_id=UUID(str(lic.id)))
    return str(lic.id), str(tok.id)


def test_auditor_repro_cannot_touch_n8n_dev(db_manager, seeded, capsys) -> None:
    """#219 audit: this exact argv used to exit 0 and REVOKE n8n-dev's token."""
    _, victim_token = _victim(db_manager)
    before = _snapshot(db_manager)
    argv = [
        "--owner-sub", OWNER_SUB, "--tenant-owner", "platform", "--apps", "kanban",
        "--tenant-id", str(N8N_DEV_ID), "--tenant-name", "n8n-dev", "--rotate-demo",
    ]  # fmt: skip
    assert _rc(argv) == 2
    assert _snapshot(db_manager) == before
    with db_manager.engine.connect() as conn:
        revoked = conn.execute(
            text("SELECT revoked FROM token WHERE id = :id"), {"id": victim_token}
        ).scalar_one()
    assert revoked is False


def test_monthly_budget_uses_a_demo_plan(db_manager, seeded, capsys) -> None:
    assert pdt.main(_argv("--monthly-token-budget", "50000")) == 0
    with db_manager.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.code, p.token_budget_monthly FROM license l JOIN plan p "
                "ON p.id = l.plan_id WHERE l.tenant_id = :tid"
            ),
            {"tid": DEMO_ID},
        ).all()
    assert sorted(rows) == [("demo", 50000), ("demo", 50000)]


# --- refusals: nothing written -------------------------------------------------------


def test_refuses_when_tenant_id_exists_under_another_name(db_manager, seeded, capsys) -> None:
    db_manager.ensure_tenant(
        DEMO_ID, name="not-demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    before = _snapshot(db_manager)
    assert pdt.main(_argv()) == 2
    assert "not the demo tenant" in capsys.readouterr().err
    assert _snapshot(db_manager) == before


def test_refuses_when_name_exists_under_another_id(db_manager, seeded, capsys) -> None:
    db_manager.ensure_tenant(
        "0195f48b-d000-7000-8000-0000000009aa",
        name="demo",
        kind="test",
        cost_centre="tests",
        owner_key="platform",
    )
    before = _snapshot(db_manager)
    assert pdt.main(_argv()) == 2
    assert "not the demo tenant" in capsys.readouterr().err
    assert _snapshot(db_manager) == before


@pytest.mark.parametrize(
    ("kind", "cost_centre"),
    [("customer", "tests"), ("unclassified", "tests"), ("test", "customers"), ("test", None)],
)
def test_refuses_an_existing_demo_tenant_that_is_not_a_test_tenant(
    db_manager, seeded, kind, cost_centre, capsys
) -> None:
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind=kind, cost_centre=cost_centre, owner_key="platform"
    )
    before = _snapshot(db_manager)
    assert pdt.main(_argv()) == 2
    assert _snapshot(db_manager) == before


def test_reuses_an_exact_demo_tenant(db_manager, seeded, capsys) -> None:
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    assert pdt.main(_argv()) == 0
    assert json.loads(capsys.readouterr().out)["tokens"].keys() == set(APPS)


def test_refuses_without_the_n8n_dev_reference_tenant(db_manager, seeded, capsys) -> None:
    with db_manager.engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE name = 'n8n-dev'"))
    before = _snapshot(db_manager)
    assert pdt.main(_argv()) == 1
    assert "n8n-dev" in capsys.readouterr().err
    assert _snapshot(db_manager) == before


def test_unknown_app_refuses_before_any_write(db_manager, seeded, capsys) -> None:
    before = _snapshot(db_manager)
    assert pdt.main(_argv(apps="kanban,nope")) == 1
    assert _snapshot(db_manager) == before
    assert db_manager.get_tenant(str(DEMO_ID)) is None


def test_refuses_an_archived_demo_tenant(db_manager, seeded, capsys) -> None:
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    with db_manager.engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET archived_at = now() WHERE id = :t"), {"t": DEMO_ID})
    before = _snapshot(db_manager)
    assert pdt.main(_argv()) == 2
    assert "archived" in capsys.readouterr().err
    assert _snapshot(db_manager) == before


def test_refuses_to_change_an_existing_demo_plan_budget(db_manager, seeded, capsys) -> None:
    assert pdt.main(_argv("--monthly-token-budget", "100", apps="kanban")) == 0
    capsys.readouterr()
    before = _snapshot(db_manager)
    assert pdt.main(_argv("--monthly-token-budget", "200", apps="kag,kanban")) == 1
    assert "refusing to change it" in capsys.readouterr().err
    assert _snapshot(db_manager) == before  # kag was not licensed either: all or nothing


@pytest.mark.parametrize(
    "argv",
    [
        ["--owner-sub", OWNER_SUB, "--apps", " , "],
        ["--owner-sub", " ", "--apps", "kanban"],
        ["--owner-sub", OWNER_SUB, "--apps", "kanban", "--monthly-token-budget", "-1"],
    ],
)
def test_bad_arguments_exit_2_without_touching_the_db(db_manager, seeded, argv, capsys) -> None:
    before = _snapshot(db_manager)
    assert _rc(argv) == 2
    assert _snapshot(db_manager) == before


def test_missing_database_url_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pdt.main(_argv()) == 2


# --- fingerprint abort ------------------------------------------------------------


def _sabotage_outside_revoke(conn, seeded):
    conn.execute(
        text("UPDATE token SET revoked = true, revoked_at = now() WHERE id = :id"),
        {"id": seeded["outside_token_id"]},
    )


def _sabotage_outside_insert(conn, seeded):
    conn.execute(
        text(
            "INSERT INTO token (id, license_id, token_hash, token_type, created_at, revoked) "
            "VALUES (gen_random_uuid(), :lid, 'x', 'api', :ts, false)"
        ),
        {"lid": seeded["outside_license_id"], "ts": datetime.now(UTC)},
    )


def _sabotage_untracked_demo_insert(conn, seeded):
    """A token on a DEMO licence the run did not track: demo rows are fingerprinted too."""
    conn.execute(
        text(
            "INSERT INTO token (id, license_id, token_hash, token_type, created_at, revoked) "
            "SELECT gen_random_uuid(), l.id, 'y', 'api', now(), false FROM license l "
            "WHERE l.tenant_id = :demo LIMIT 1"
        ),
        {"demo": DEMO_ID},
    )


@pytest.mark.parametrize(
    "sabotage",
    [_sabotage_outside_revoke, _sabotage_outside_insert, _sabotage_untracked_demo_insert],
    ids=[
        "outside-update-caught-by-state-digest",
        "outside-insert-caught-by-count",
        "untracked-demo-row-caught",
    ],
)
def test_fingerprint_change_aborts_and_rolls_everything_back(
    db_manager, seeded, capsys, monkeypatch, sabotage
) -> None:
    """A write outside the demo tenant made INSIDE the script's transaction (a bug
    simulated by wrapping create_token) must roll back the demo writes too."""
    real_execute = pdt._execute

    def _buggy(db, conn, plan):
        tokens = real_execute(db, conn, plan)
        sabotage(conn, seeded)
        return tokens

    monkeypatch.setattr(pdt, "_execute", _buggy)
    before = _snapshot(db_manager)

    rc = pdt.main(_argv())
    out, err = capsys.readouterr()

    assert rc == 3
    assert "ABORTED, rolled back" in err
    assert out == ""  # no token of a rolled-back transaction is ever printed
    assert _snapshot(db_manager) == before
    assert db_manager.get_tenant(str(DEMO_ID)) is None
    _assert_outside_untouched(db_manager, seeded)


def test_concurrent_outside_writes_do_not_fake_a_change(db_manager, seeded, monkeypatch) -> None:
    """Live traffic minting a token for ANOTHER tenant mid-run is invisible to the
    REPEATABLE READ snapshot, so it neither aborts the run nor is rolled back."""
    real_execute = pdt._execute

    def _with_concurrent_mint(db, conn, plan):
        tokens = real_execute(db, conn, plan)
        db_manager.create_token(license_id=UUID(seeded["outside_license_id"]))  # own conn
        return tokens

    monkeypatch.setattr(pdt, "_execute", _with_concurrent_mint)
    assert pdt.main(_argv()) == 0
    with db_manager.engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM token WHERE license_id = :l"),
            {"l": seeded["outside_license_id"]},
        ).scalar_one()
    assert n == 2


# --- dry-run ---------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_writes_nothing(db_manager, seeded, capsys) -> None:
    before = _snapshot(db_manager)
    rc = pdt.main(_argv("--dry-run", "--monthly-token-budget", "10", apply=False))
    out, _ = capsys.readouterr()

    assert rc == 0
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert "tokens" not in payload
    assert payload["plan"]["tenant"]["action"] == "create"
    assert payload["plan"]["tenant"]["byo_mode"] == N8N_DEV_BYO
    assert [a["action"] for a in payload["plan"]["apps"]] == ["create", "create"]
    assert [a["plan_action"] for a in payload["plan"]["apps"]] == ["create", "create"]
    assert _snapshot(db_manager) == before


# --- idempotence and rotation ------------------------------------------------------


def test_rerun_reuses_tenant_and_skips_licensed_apps(db_manager, seeded, capsys) -> None:
    assert pdt.main(_argv()) == 0
    first = json.loads(capsys.readouterr().out)["tokens"]
    after_first = _snapshot(db_manager)

    assert pdt.main(_argv()) == 0
    out, err = capsys.readouterr()

    assert json.loads(out)["tokens"] == {}
    assert "no tokens minted" in err
    assert _snapshot(db_manager) == after_first
    for raw in first.values():  # the first run's demo tokens still valid
        tok = db_manager.get_token_by_hash(Token.hash_token(raw))
        assert tok is not None and tok.revoked is False


def test_rerun_licenses_only_the_new_app(db_manager, seeded, capsys) -> None:
    assert pdt.main(_argv(apps="kanban")) == 0
    capsys.readouterr()
    assert pdt.main(_argv()) == 0
    assert set(json.loads(capsys.readouterr().out)["tokens"]) == {"kag"}


def test_rotate_demo_touches_only_demo_tokens(
    db_manager, seeded, capsys, forbid_token_mutators
) -> None:
    assert pdt.main(_argv()) == 0
    first = json.loads(capsys.readouterr().out)["tokens"]
    licences_before = _snapshot(db_manager)["licences"]

    assert pdt.main(_argv("--rotate-demo", apps="kanban")) == 0
    second = json.loads(capsys.readouterr().out)["tokens"]

    assert set(second) == {"kanban"} and second["kanban"] != first["kanban"]
    old = db_manager.get_token_by_hash(Token.hash_token(first["kanban"]))
    new = db_manager.get_token_by_hash(Token.hash_token(second["kanban"]))
    kag = db_manager.get_token_by_hash(Token.hash_token(first["kag"]))
    assert old is not None and old.revoked is True
    assert new is not None and new.revoked is False and new.license_id == old.license_id
    assert kag is not None and kag.revoked is False  # not rotated
    assert _snapshot(db_manager)["licences"] == licences_before  # no new licence
    _assert_outside_untouched(db_manager, seeded)


def test_rotate_revokes_only_tokens_this_script_minted(db_manager, seeded, capsys) -> None:
    """A token minted on the demo licence by another path (API, operator) is not ours."""
    assert pdt.main(_argv(apps="kanban")) == 0
    first = json.loads(capsys.readouterr().out)["tokens"]["kanban"]
    old = db_manager.get_token_by_hash(Token.hash_token(first))
    assert old is not None
    foreign, _ = db_manager.create_token(license_id=UUID(str(old.license_id)))

    assert pdt.main(_argv("--rotate-demo", apps="kanban")) == 0
    capsys.readouterr()
    with db_manager.engine.connect() as conn:
        state = dict(
            conn.execute(
                text("SELECT id, revoked FROM token WHERE id IN (:a, :b)"),
                {"a": old.id, "b": foreign.id},
            ).all()
        )
    assert state == {old.id: True, foreign.id: False}


def test_rotate_refuses_a_demo_licence_this_script_did_not_create(
    db_manager, seeded, capsys
) -> None:
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    app = db_manager.get_app_by_key("kanban")
    plan = [p for p in db_manager.list_plans(UUID(str(app.id))) if p.code == "standard"][0]
    sub = db_manager.create_or_get_subject("someone-else", "portal")
    lic = db_manager.create_license(
        subject_id=UUID(str(sub.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=str(DEMO_ID),
    )
    db_manager.create_token(license_id=UUID(str(lic.id)))
    before = _snapshot(db_manager)
    assert pdt.main(_argv("--rotate-demo", apps="kanban")) == 1
    assert "not created by this script" in capsys.readouterr().err
    assert _snapshot(db_manager) == before


# --- dry run is the default ----------------------------------------------------------


def _full_fingerprint(db_manager: DatabaseManager) -> dict:
    with db_manager.engine.connect() as conn:
        return pdt.fingerprint(conn, exclude_tokens=set(), exclude_licences=set())


def test_no_flags_writes_nothing(db_manager, seeded, capsys) -> None:
    fp, snap = _full_fingerprint(db_manager), _snapshot(db_manager)
    assert (
        pdt.main(["--owner-sub", OWNER_SUB, "--tenant-owner", "platform", "--apps", "kanban,kag"])
        == 0
    )
    out, err = capsys.readouterr()
    assert json.loads(out)["dry_run"] is True
    assert "DRY RUN (default)" in err
    assert _full_fingerprint(db_manager) == fp
    assert _snapshot(db_manager) == snap
    assert db_manager.get_tenant(str(DEMO_ID)) is None


def test_rotate_without_apply_revokes_nothing(db_manager, seeded, capsys) -> None:
    assert pdt.main(_argv(apps="kanban")) == 0
    raw = json.loads(capsys.readouterr().out)["tokens"]["kanban"]
    fp, snap = _full_fingerprint(db_manager), _snapshot(db_manager)

    assert pdt.main(_argv("--rotate-demo", apps="kanban", apply=False)) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["apps"][0]["action"] == "rotate" and len(plan["apps"][0]["tokens_to_revoke"]) == 1
    tok = db_manager.get_token_by_hash(Token.hash_token(raw))
    assert tok is not None and tok.revoked is False
    assert _full_fingerprint(db_manager) == fp
    assert _snapshot(db_manager) == snap


def test_apply_and_dry_run_together_is_refused(db_manager, seeded, capsys) -> None:
    snap = _snapshot(db_manager)
    assert pdt.main(_argv("--dry-run")) == 2
    assert _snapshot(db_manager) == snap


# --- token-ownership barriers (plan check + revoke scope) ----------------------------


def _demo_licence_with_victim_recorded(db_manager, seeded, capsys) -> tuple[str, str, str]:
    """Provision kanban, then POISON its metadata with n8n-dev's live token id."""
    assert pdt.main(_argv(apps="kanban")) == 0
    raw = json.loads(capsys.readouterr().out)["tokens"]["kanban"]
    demo_tok = db_manager.get_token_by_hash(Token.hash_token(raw))
    assert demo_tok is not None
    _, victim = _victim(db_manager)
    with db_manager.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE license SET metadata = jsonb_set(metadata, ARRAY[:k], "
                "(metadata -> :k) || jsonb_build_array(CAST(:v AS text))) WHERE id = :lid"
            ),
            {"k": pdt.TOKEN_IDS_KEY, "v": victim, "lid": demo_tok.license_id},
        )
    return str(demo_tok.license_id), str(demo_tok.id), victim


def _revoked(db_manager, *ids: str) -> dict[str, bool]:
    with db_manager.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id::text, revoked FROM token WHERE id::text = ANY(:ids)"),
            {"ids": list(ids)},
        ).all()
    return dict(rows)


def test_poisoned_record_is_refused_and_the_victim_stays_active(db_manager, seeded, capsys) -> None:
    """Barrier 1 (plan). A recorded id that lives on another licence refuses the run."""
    _, demo_tok, victim = _demo_licence_with_victim_recorded(db_manager, seeded, capsys)
    snap = _snapshot(db_manager)

    rc = pdt.main(_argv("--rotate-demo", apps="kanban"))
    err = capsys.readouterr().err

    assert rc == 1
    assert "belong to another licence" in err and victim in err
    assert _revoked(db_manager, victim, demo_tok) == {victim: False, demo_tok: False}
    assert _snapshot(db_manager) == snap


def test_revoke_scope_holds_even_if_the_plan_is_wrong(
    db_manager, seeded, capsys, monkeypatch
) -> None:
    """Barrier 2 (revoke SQL). Force the victim into the plan; the scoped UPDATE must not
    reach it, the RETURNING mismatch must roll back, and the legit demo token survives."""
    assert pdt.main(_argv(apps="kanban")) == 0
    raw = json.loads(capsys.readouterr().out)["tokens"]["kanban"]
    tok = db_manager.get_token_by_hash(Token.hash_token(raw))
    assert tok is not None
    demo_tok = str(tok.id)
    _, victim = _victim(db_manager)  # NOT recorded anywhere: barrier 1 cannot see it
    real_build = pdt._build_plan

    def _wrong_plan(session, args, apps, tenant_exists):
        plan = real_build(session, args, apps, tenant_exists)
        plan.apps[0].tokens_to_revoke.append(UUID(victim))
        return plan

    monkeypatch.setattr(pdt, "_build_plan", _wrong_plan)
    rc = pdt.main(_argv("--rotate-demo", apps="kanban"))
    assert rc == 3
    assert "revoke touched" in capsys.readouterr().err
    assert _revoked(db_manager, victim, demo_tok) == {victim: False, demo_tok: False}


# --- tenant row lock / pre-commit re-check -------------------------------------------


def test_concurrent_reclassification_cannot_commit_under_a_run(
    db_manager, seeded, capsys, monkeypatch
) -> None:
    """Another writer flipping kind mid-run is blocked by FOR SHARE (lock_timeout)."""
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    real_execute = pdt._execute
    outcome: dict[str, str] = {}

    def _with_writer(db, conn, plan):
        w = real_execute(db, conn, plan)
        with db_manager.engine.connect() as other:
            other.execute(text("SET lock_timeout = '300ms'"))
            try:
                other.execute(
                    text("UPDATE tenants SET kind = 'customer' WHERE id = :t"), {"t": DEMO_ID}
                )
                other.commit()
                outcome["writer"] = "committed"
            except Exception as exc:  # LockNotAvailable
                outcome["writer"] = type(getattr(exc, "orig", exc)).__name__
        return w

    monkeypatch.setattr(pdt, "_execute", _with_writer)
    assert pdt.main(_argv()) == 0
    assert outcome["writer"] == "LockNotAvailable"
    assert db_manager.get_tenant(str(DEMO_ID)).kind == "test"


def test_kind_changed_before_commit_aborts_everything(
    db_manager, seeded, capsys, monkeypatch
) -> None:
    """The pre-commit re-check: a tenant that is no longer kind=test must not commit."""
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    snap = _snapshot(db_manager)
    real_execute = pdt._execute

    def _flip(db, conn, plan):
        w = real_execute(db, conn, plan)
        conn.execute(text("UPDATE tenants SET kind = 'customer' WHERE id = :t"), {"t": DEMO_ID})
        return w

    monkeypatch.setattr(pdt, "_execute", _flip)
    rc = pdt.main(_argv())
    out, err = capsys.readouterr()
    assert rc == 2 and out == ""
    assert "pre-commit re-check" in err
    assert _snapshot(db_manager) == snap


def test_change_committed_after_the_snapshot_is_a_serialization_abort(
    db_manager, seeded, capsys, monkeypatch
) -> None:
    db_manager.ensure_tenant(
        DEMO_ID, name="demo", kind="test", cost_centre="tests", owner_key="platform"
    )
    # Take the snapshot BEFORE the guard: patch the guard to commit a flip from another
    # connection first, which REPEATABLE READ + FOR SHARE must reject.
    real_guard = pdt._check_demo_tenant

    def _guard(conn):
        conn.execute(text("SELECT 1"))  # snapshot taken here
        with db_manager.engine.begin() as other:
            other.execute(
                text("UPDATE tenants SET kind = 'customer' WHERE id = :t"), {"t": DEMO_ID}
            )
        return real_guard(conn)

    monkeypatch.setattr(pdt, "_check_demo_tenant", _guard)
    snap_tokens = _snapshot(db_manager)["tokens"]
    assert pdt.main(_argv()) == 4
    assert "concurrent change" in capsys.readouterr().err
    assert _snapshot(db_manager)["tokens"] == snap_tokens
