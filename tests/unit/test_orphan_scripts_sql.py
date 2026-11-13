"""Regression tests for the FK-campaign orphan-tenant scripts after migration 016.

Migration 016 retyped ``license.tenant_id`` from ``VARCHAR(255)`` to ``uuid``.
Both orphan scripts pre-dated that retype and compared ``tenants.id::text =
license.tenant_id`` (``text = uuid``), which Postgres rejects outright
("operator does not exist: text = uuid") — so *running* either script post-016
crashed on its first query instead of reporting zero orphans. The resolver's
Python classifier also compared a ``str`` against ``rec["tenant_id"]`` which the
driver now hands back as a ``uuid.UUID``, so the equality was always False and
every service-derivable orphan silently misclassified.

These tests pin: (1) both aggregation queries execute against the real,
post-016 schema without an operator error, and (2) the classifier matches a
``uuid.UUID`` tenant_id.
"""

from __future__ import annotations

import uuid

from scripts import audit_orphan_tenants, resolve_orphan_tenants


def test_audit_orphan_sql_executes_post_uuid_retype(db_manager):
    """audit ``_ORPHAN_SQL`` runs (no ``text = uuid`` operator error) and, with
    the FK in force (no orphans possible), returns an empty result."""
    session = db_manager.get_session()
    try:
        rows = session.execute(audit_orphan_tenants._ORPHAN_SQL).mappings().all()
    finally:
        session.close()
    assert rows == []


def test_resolve_orphan_sql_executes_post_uuid_retype(db_manager):
    """resolver ``_ORPHAN_SQL`` runs against the post-016 schema and returns no
    orphans (the FK guarantees zero)."""
    orphans = resolve_orphan_tenants._load_orphans(db_manager)
    assert orphans == []


def test_resolve_classify_matches_uuid_object_tenant_id():
    """``_classify`` must recognise a service-derivable orphan even though the
    driver now yields ``rec['tenant_id']`` as a ``uuid.UUID``.

    Pre-fix ``str(uuid5(...)) == rec['tenant_id']`` compared str-to-UUID and was
    always False, misclassifying every derivable orphan as ``needs-name``.
    """
    sub = "w2v-backend"
    derived = uuid.uuid5(resolve_orphan_tenants._SERVICE_TENANT_NS, sub)
    rec = {
        "tenant_id": derived,  # uuid.UUID, as the driver returns post-016
        "subs": [sub],
        "miskeyed": False,
        "active": 3,
    }
    disposition, name = resolve_orphan_tenants._classify(rec)
    assert disposition == "service-derivable"
    assert name == sub
