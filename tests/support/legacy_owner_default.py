"""Fixture for tests that REPLAY a pre-050 migration (or insert tenants the way a
pre-050 writer did) against the CURRENT schema.

Since migration 050, ``tenants.owner_key`` is NOT NULL with NO default: every live
writer names the owner. A migration that ran on prod BEFORE 050 (e.g. the 045 /
046 / 049 re-keys, which copy a tenants row by an explicit pre-050 column list)
never had to — the column did not exist yet. Replaying such a file against the
post-050 schema the test database is built from would fail NOT NULL for a reason
that has nothing to do with what the test measures.

So THIS fixture, and only in those tests, gives the column the 'platform' default
for the duration of one test and drops it again afterwards. Production code never
sees it (George 2026-10-14: no silent owner default).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text


@pytest.fixture
def legacy_owner_default(db_engine) -> Iterator[None]:
    with db_engine.begin() as conn:
        conn.execute(text("ALTER TABLE tenants ALTER COLUMN owner_key SET DEFAULT 'platform'"))
    try:
        yield
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("ALTER TABLE tenants ALTER COLUMN owner_key DROP DEFAULT"))
