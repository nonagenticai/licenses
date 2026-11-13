"""Migration 043: ``tenants.budget_currency`` (tenant-inference-provisioning PLAN §2.2).

A tenant whose budget is held as MONEY in billing (``budget_currency='EUR'``) must not
also be given a TOKEN budget by this service. This column is the switch; it is NULL
for every existing tenant, so nothing changes for them.

WHAT IS PINNED.
* The column: nullable ``char(3)``, CHECK allows ``'EUR'``/``'USD'``/``'CAD'`` (PLAN
  REV F), existing rows NULL; the API Literal equals ``BUDGET_CURRENCIES``.
* 043 is metadata-only, idempotent, and its downgrade removes the column.
* ``Tenant.to_dict`` carries it; ``ensure_tenant`` / ``create_tenant`` set it on INSERT.
* Routes: ``POST /tenants`` and ``PUT /tenants/{id}`` accept it on insert;
  ``PUT /tenants/{id}/budget-currency`` moves it NULL -> a currency, idempotently;
  switching to a DIFFERENT currency is ``platform-admin`` only and audited by a
  ``budget_currency_changed`` log line (a plain license-admin gets 409
  ``budget_currency_immutable``); nothing can return a money tenant to tokens.
  ``PATCH /tenants/{id}`` (the rename) is unchanged.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Generator
from pathlib import Path
from typing import get_args
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

import main
from auth.models import KeycloakUser
from models import BUDGET_CURRENCIES, BudgetCurrencyImmutableError, ClientSafeError, Tenant
from tests.conftest import TEST_DATABASE_URL, _make_authenticated_client
from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "043_tenants_budget_currency.sql"
DOWN = REPO / "migrations" / "downgrades" / "043_tenants_budget_currency.sql"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _run_file():
    sys.path.insert(0, str(REPO / "scripts"))
    from apply_migrations import _run_file as run  # type: ignore[import-not-found]

    return run


def _column(cur) -> tuple | None:
    return cur.execute(
        "SELECT data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name = 'tenants' AND column_name = 'budget_currency'"
    ).fetchone()


class TestColumn:
    def test_shape(self, db_manager):
        conn = psycopg.connect(_libpq_dsn())
        try:
            assert _column(conn) == ("character", 3, "YES", None)
        finally:
            conn.close()

    @pytest.mark.parametrize("ccy", ["EUR", "USD", "CAD"])
    def test_check_allows_eur_usd_cad(self, db_manager, ccy):
        t = db_manager.ensure_tenant(
            uuid4(), name=f"ck-{ccy.lower()}", budget_currency=ccy, owner_key="platform"
        )
        assert t.budget_currency == ccy
        # And the DB CHECK admits it on a raw write too.
        conn = psycopg.connect(_libpq_dsn())
        try:
            conn.execute(
                "INSERT INTO tenants (id, name, budget_currency) VALUES (%s, %s, %s)",
                (uuid4(), f"raw-{ccy.lower()}", ccy),
            )
        finally:
            conn.rollback()
            conn.close()

    def test_check_refuses_other_currencies(self, db_manager):
        with pytest.raises(ClientSafeError):
            db_manager.ensure_tenant(
                uuid4(), name="ck-gbp", budget_currency="GBP", owner_key="platform"
            )
        # The DB CHECK is the backstop for a write that bypasses the primitive.
        conn = psycopg.connect(_libpq_dsn())
        try:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO tenants (id, name, budget_currency) VALUES (%s, 'raw-gbp', 'GBP')",
                    (uuid4(),),
                )
        finally:
            conn.rollback()
            conn.close()

    def test_api_literal_equals_model_vocabulary(self):
        assert get_args(main.BudgetCurrency) == BUDGET_CURRENCIES == ("EUR", "USD", "CAD")

    def test_default_is_null_and_to_dict_carries_it(self, db_manager):
        t = db_manager.create_tenant(name="no-currency", owner_key="platform")
        assert t.budget_currency is None
        assert t.to_dict()["budget_currency"] is None
        e = db_manager.create_tenant(
            name="eur-currency", budget_currency="EUR", owner_key="platform"
        )
        assert e.to_dict()["budget_currency"] == "EUR"
        assert "budget_currency" in Tenant.__table__.c


class TestMigrationFile:
    def test_downgrade_removes_upgrade_adds_idempotently_and_keeps_rows(self, db_manager):
        run = _run_file()
        kept = db_manager.ensure_tenant(uuid4(), name="pre-043", owner_key="platform")
        conn = psycopg.connect(_libpq_dsn())  # not autocommit: rolled back below
        try:
            assert _column(conn) is not None, "positive control: create_all built it"
            run(conn, DOWN)
            assert _column(conn) is None, "the downgrade did not remove the column"
            run(conn, UP)
            assert _column(conn) == ("character", 3, "YES", None)
            run(conn, UP)  # idempotent
            row = conn.execute(
                "SELECT budget_currency FROM tenants WHERE id = %s", (kept.id,)
            ).fetchone()
            assert row == (None,), "043 must leave every existing tenant NULL"
            con = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_tenants_budget_currency'"
            ).fetchone()
            assert con is not None
            for ccy in ("'EUR'", "'USD'", "'CAD'"):
                assert ccy in con[0], (ccy, con[0])
        finally:
            conn.rollback()
            conn.close()

    def test_sql_has_no_dml(self):
        for path in (UP, DOWN):
            body = "\n".join(
                ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("--")
            ).upper()
            for verb in ("UPDATE ", "INSERT ", "DELETE "):
                assert verb not in body, f"{path.name}: found {verb!r}"


class TestRoutes:
    def test_post_tenants_accepts_eur(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post(
            "/tenants", json={"name": "post-eur", "budget_currency": "EUR"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["budget_currency"] == "EUR"

    def test_post_tenants_default_null(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post("/tenants", json={"name": "post-null"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["budget_currency"] is None

    def test_put_register_accepts_eur(self, auth_client_admin: TestClient):
        resp = auth_client_admin.put(
            f"/tenants/{uuid4()}", json={"name": "put-eur", "budget_currency": "EUR"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["budget_currency"] == "EUR"

    @pytest.mark.parametrize("ccy", ["USD", "CAD"])
    def test_post_and_put_register_accept_usd_and_cad(
        self, auth_client_admin: TestClient, ccy: str
    ):
        resp = auth_client_admin.post(
            "/tenants", json={"name": f"post-{ccy.lower()}", "budget_currency": ccy}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["budget_currency"] == ccy
        resp = auth_client_admin.put(
            f"/tenants/{uuid4()}", json={"name": f"put-{ccy.lower()}", "budget_currency": ccy}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["budget_currency"] == ccy

    def test_create_refuses_other_currencies(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post(
            "/tenants", json={"name": "post-gbp", "budget_currency": "GBP"}
        )
        assert resp.status_code == 422, resp.text

    @staticmethod
    def _url(tenant) -> str:
        return f"/tenants/{tenant.id}/budget-currency"

    def test_put_null_to_eur_then_idempotent(self, auth_client_admin: TestClient, db_manager):
        t = db_manager.create_tenant(name="put-me", owner_key="platform")
        resp = auth_client_admin.put(self._url(t), json={"budget_currency": "EUR"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["budget_currency"] == "EUR"
        assert resp.json()["name"] == "put-me"
        again = auth_client_admin.put(self._url(t), json={"budget_currency": "EUR"})
        assert again.status_code == 200, again.text
        assert db_manager.get_tenant(str(t.id)).budget_currency == "EUR"

    def test_put_by_name_works(self, auth_client_admin: TestClient, db_manager):
        db_manager.create_tenant(name="by-name", owner_key="platform")
        resp = auth_client_admin.put(
            "/tenants/by-name/budget-currency", json={"budget_currency": "EUR"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["budget_currency"] == "EUR"

    def test_put_cannot_return_an_eur_tenant_to_tokens(
        self, auth_client_admin: TestClient, db_manager
    ):
        t = db_manager.create_tenant(name="eur-locked", budget_currency="EUR", owner_key="platform")
        for body in ({"budget_currency": None}, {}, {"budget_currency": "GBP"}):
            resp = auth_client_admin.put(self._url(t), json=body)
            assert resp.status_code == 422, (body, resp.text)
        assert db_manager.get_tenant(str(t.id)).budget_currency == "EUR"

    @pytest.mark.parametrize("ccy", ["USD", "CAD"])
    def test_put_null_to_usd_or_cad(self, auth_client_admin: TestClient, db_manager, ccy):
        t = db_manager.create_tenant(name=f"put-{ccy.lower()}-ccy", owner_key="platform")
        resp = auth_client_admin.put(self._url(t), json={"budget_currency": ccy})
        assert resp.status_code == 200, resp.text
        assert db_manager.get_tenant(str(t.id)).budget_currency == ccy

    @pytest.mark.parametrize(("held", "to"), [("EUR", "USD"), ("EUR", "CAD"), ("CAD", "EUR")])
    def test_license_admin_cannot_switch_currency_409(
        self, auth_client_admin: TestClient, db_manager, held, to, caplog
    ):
        t = db_manager.create_tenant(
            name=f"la-{held}-{to}".lower(), budget_currency=held, owner_key="platform"
        )
        with caplog.at_level(logging.WARNING):
            resp = auth_client_admin.put(self._url(t), json={"budget_currency": to})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "budget_currency_immutable"
        assert db_manager.get_tenant(str(t.id)).budget_currency == held
        assert not any("budget_currency_changed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(("held", "to"), [("EUR", "USD"), ("USD", "CAD"), ("CAD", "EUR")])
    def test_platform_admin_switches_currency_and_is_audited(
        self, platform_admin_client: TestClient, db_manager, held, to, caplog
    ):
        t = db_manager.create_tenant(
            name=f"pa-{held}-{to}".lower(), budget_currency=held, owner_key="platform"
        )
        with caplog.at_level(logging.WARNING):
            resp = platform_admin_client.put(self._url(t), json={"budget_currency": to})
        assert resp.status_code == 200, resp.text
        assert resp.json()["budget_currency"] == to
        assert db_manager.get_tenant(str(t.id)).budget_currency == to
        audit = [
            r.getMessage() for r in caplog.records if "budget_currency_changed" in r.getMessage()
        ]
        assert len(audit) == 1, audit
        line = audit[0]
        for part in (f"tenant={t.id}", f"from={held}", f"to={to}", "actor=owner-user-uuid", "at="):
            assert part in line, (part, line)

    def test_platform_admin_can_never_clear_to_null(
        self, platform_admin_client: TestClient, db_manager
    ):
        t = db_manager.create_tenant(name="pa-no-null", budget_currency="USD", owner_key="platform")
        resp = platform_admin_client.put(self._url(t), json={"budget_currency": None})
        assert resp.status_code == 422, resp.text
        with pytest.raises(BudgetCurrencyImmutableError):
            db_manager.set_tenant_budget_currency(str(t.id), None, allow_change=True)
        assert db_manager.get_tenant(str(t.id)).budget_currency == "USD"

    def test_platform_admin_cannot_change_an_archived_tenant(
        self, platform_admin_client: TestClient, db_manager
    ):
        t = db_manager.create_tenant(
            name="pa-archived", budget_currency="EUR", owner_key="platform"
        )
        db_manager.set_tenant_archived(str(t.id), True)
        resp = platform_admin_client.put(self._url(t), json={"budget_currency": "CAD"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "tenant_archived"
        assert db_manager.get_tenant(str(t.id)).budget_currency == "EUR"

    def test_null_to_currency_is_audited_too(
        self, auth_client_admin: TestClient, db_manager, caplog
    ):
        t = db_manager.create_tenant(name="audit-first-set", owner_key="platform")
        with caplog.at_level(logging.WARNING):
            resp = auth_client_admin.put(self._url(t), json={"budget_currency": "CAD"})
        assert resp.status_code == 200, resp.text
        audit = [
            r.getMessage() for r in caplog.records if "budget_currency_changed" in r.getMessage()
        ]
        assert len(audit) == 1 and "from=None" in audit[0] and "to=CAD" in audit[0], audit

    def test_a_different_held_currency_is_409_immutable(
        self, auth_client_admin: TestClient, db_manager, monkeypatch
    ):
        """The route's 409 mapping and its no-echo rule, pinned by making the model
        raise (the real refusal is ``test_license_admin_cannot_switch_currency_409``)."""
        t = db_manager.create_tenant(name="other-ccy", owner_key="platform")

        def _raise(*_a, **_k):
            raise BudgetCurrencyImmutableError("held 'XXX'")

        monkeypatch.setattr(main.db_manager, "set_tenant_budget_currency", _raise)
        resp = auth_client_admin.put(self._url(t), json={"budget_currency": "EUR"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "budget_currency_immutable"
        assert "XXX" not in resp.text, "the caught exception's text must not be echoed"

    def test_model_refuses_any_change_but_null_to_currency(self, db_manager):
        t = db_manager.create_tenant(
            name="model-locked", budget_currency="EUR", owner_key="platform"
        )
        with pytest.raises(BudgetCurrencyImmutableError):
            db_manager.set_tenant_budget_currency(str(t.id), None)
        assert db_manager.get_tenant(str(t.id)).budget_currency == "EUR"
        assert db_manager.set_tenant_budget_currency(str(t.id), "EUR").budget_currency == "EUR"

    def test_generic_patch_is_unchanged(self, auth_client_admin: TestClient, db_manager):
        t = db_manager.create_tenant(name="patch-unchanged", owner_key="platform")
        resp = auth_client_admin.patch(f"/tenants/{t.id}", json={"budget_currency": "EUR"})
        assert resp.status_code == 422, "PATCH /tenants/{id} is the rename; name is required"
        assert db_manager.get_tenant(str(t.id)).budget_currency is None

    def test_put_unknown_tenant_404(self, auth_client_admin: TestClient):
        resp = auth_client_admin.put(
            f"/tenants/{uuid4()}/budget-currency", json={"budget_currency": "EUR"}
        )
        assert resp.status_code == 404, resp.text

    def test_put_requires_license_admin(self, auth_client_reader: TestClient, db_manager):
        t = db_manager.create_tenant(name="reader-cannot", owner_key="platform")
        resp = auth_client_reader.put(self._url(t), json={"budget_currency": "EUR"})
        assert resp.status_code == 403, resp.text
        assert db_manager.get_tenant(str(t.id)).budget_currency is None


@pytest.fixture
def platform_admin_client(db_manager) -> Generator[TestClient]:
    """The owner capability: realm role ``platform-admin`` (no license-admin)."""
    owner = KeycloakUser(
        sub="owner-user-uuid",
        preferred_username="owner",
        email="owner@test.com",
        realm_access={"roles": ["platform-admin"]},
        resource_access={},
        iss="https://keycloak.example.com/realms/secure-apps",
    )
    yield from _make_authenticated_client(db_manager, owner)


@pytest.fixture(autouse=True)
def _pre_050_writer_default(legacy_owner_default):  # noqa: F811
    """This file replays a pre-050 writer; see tests/support/legacy_owner_default.py."""
