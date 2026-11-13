"""`effective_status` on the wire: the CONSUMER contract, not just the field.

Emitting this key is not a passive addition. `provisioning`
(`src/entitlements.py::is_active_license`) already reads it FIRST and falls back
to `status` only when it is absent:

    if row.get("revoked_at"):            return False      # floor 1
    if _has_lapsed(row.get("expires_at")): return False    # floor 2
    effective = row.get("effective_status")
    if effective:                                          # present AND non-empty
        return effective in ("active", "pending")          # <-- NEW path
    return row.get("status") in (None, "", "active")       # <-- path used TODAY

So the day licenses starts emitting the key, provisioning silently switches
branch on every licence in the estate. What must be true is not "the field is
present" but "the two branches return the same verdict", and that is what these
tests assert. A test that only checked presence would have passed even if the
switch flipped real verdicts.

Consequence if they ever disagree, from the consumer's own docstring: a row that
reads REUSABLE today but NOT reusable after would make provisioning mint a new
licence for a key that already owns the `(tenant, app, subject)` unique-index
slot — a create → 409 → retry loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from models import License

# --- provisioning's two branches, transcribed verbatim ----------------------
# Deliberately duplicated rather than imported: provisioning is a different
# repo/service and is not importable here. The point of the test is that THIS
# service's output keeps THAT service's verdict stable, so the copy is the
# contract under test. Keep in sync with provisioning src/entitlements.py.
_ACTIVE_STATUSES: tuple[str | None, ...] = (None, "", "active")
_REUSABLE_EFFECTIVE_STATUSES: tuple[str, ...] = ("active", "pending")


def _has_lapsed(value) -> bool:
    """`expires_at` present AND already past. None/absent is PERPETUAL."""
    if not value:
        return False
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < datetime.now(UTC)


def _verdict_today(row: dict) -> bool:
    """What provisioning decides while `effective_status` is ABSENT."""
    if row.get("revoked_at"):
        return False
    if _has_lapsed(row.get("expires_at")):
        return False
    return row.get("status") in _ACTIVE_STATUSES


def _verdict_after(row: dict) -> bool:
    """What provisioning decides once `effective_status` is present."""
    if row.get("revoked_at"):
        return False
    if _has_lapsed(row.get("expires_at")):
        return False
    effective = row.get("effective_status")
    if effective:
        return effective in _REUSABLE_EFFECTIVE_STATUSES
    return row.get("status") in _ACTIVE_STATUSES


def _now():
    return datetime.now(UTC)


# status x starts_at x expires_at x revoked_at, covering every shape a real row
# can take. `status` values are the four the column documents plus an unknown
# one, which effective_status() returns VERBATIM.
_STATUSES = ["active", "revoked", "expired", "pending", "suspended-unknown"]
# Day offsets from now. `None` means "column is NULL", which is a distinct case
# from any offset — annotated so the optional ones do not widen the non-optional
# `_STARTS` (starts_at is NOT NULL, so it has no None case).
_STARTS: dict[str, int] = {"past": -1, "future": +1}
_EXPIRES: dict[str, int | None] = {"none": None, "past": -1, "future": +1}
_REVOKED: dict[str, int | None] = {"none": None, "set": -1}

_MATRIX = [
    (s, sk, ek, rk) for s in _STATUSES for sk in _STARTS for ek in _EXPIRES for rk in _REVOKED
]


def _offset(days: int | None):
    """`None` -> a NULL column; an int -> that many days from now."""
    return None if days is None else _now() + timedelta(days=days)


def _build(status: str, starts_key: str, expires_key: str, revoked_key: str) -> License:
    now = _now()
    return License(
        status=status,
        starts_at=now + timedelta(days=_STARTS[starts_key]),
        expires_at=_offset(_EXPIRES[expires_key]),
        revoked_at=_offset(_REVOKED[revoked_key]),
        # Column defaults only fire on flush; set them so to_dict() -> pydantic
        # round-trips work on an unpersisted instance.
        created_at=now,
        updated_at=now,
    )


class TestProvisioningVerdictIsUnchanged:
    """The switch must be a no-op for every reachable row.

    The ONE deliberate exception is a STORED ``status='pending'`` row, which is
    asserted separately in :class:`TestStoredPendingIsTheIntendedImprovement`
    rather than waved through here.
    """

    @pytest.mark.parametrize(
        "status,starts_key,expires_key,revoked_key",
        [c for c in _MATRIX if c[0] != "pending"],
    )
    def test_both_branches_agree(self, status, starts_key, expires_key, revoked_key):
        lic = _build(status, starts_key, expires_key, revoked_key)
        row_after = {
            "status": lic.status,
            "effective_status": lic.effective_status(),
            "starts_at": lic.starts_at,
            "expires_at": lic.expires_at,
            "revoked_at": lic.revoked_at,
        }
        row_today = {k: v for k, v in row_after.items() if k != "effective_status"}

        assert _verdict_today(row_today) == _verdict_after(row_after), (
            f"provisioning's verdict CHANGED for status={status!r} "
            f"starts={starts_key} expires={expires_key} revoked={revoked_key}: "
            f"today={_verdict_today(row_today)} after={_verdict_after(row_after)} "
            f"(effective_status={row_after['effective_status']!r})"
        )

    def test_the_matrix_is_not_vacuous(self):
        """Both verdicts must actually occur, or agreement proves nothing.

        A matrix where every row is False would pass `today == after` trivially.
        """
        verdicts = set()
        for case in _MATRIX:
            lic = _build(*case)
            verdicts.add(
                _verdict_after(
                    {
                        "status": lic.status,
                        "effective_status": lic.effective_status(),
                        "expires_at": lic.expires_at,
                        "revoked_at": lic.revoked_at,
                    }
                )
            )
        assert verdicts == {True, False}, f"matrix is one-sided: {verdicts}"

    def test_pending_is_reusable_not_dead(self):
        """The one case where the NEW branch carries information the old one
        could not: a future-dated licence reads `pending`, and must stay
        REUSABLE — treating it as dead is what opens the 409 loop."""
        lic = _build("active", "future", "none", "none")
        assert lic.effective_status() == "pending"
        assert (
            _verdict_after(
                {
                    "status": "active",
                    "effective_status": "pending",
                    "expires_at": None,
                    "revoked_at": None,
                }
            )
            is True
        )


class TestStoredPendingIsTheIntendedImprovement:
    """A STORED ``status='pending'`` row is the one place the verdict changes,
    and the change is the point rather than a regression.

    Today provisioning evaluates ``"pending" in (None, "", "active")`` -> **not
    reusable**, so it would try to mint a new licence for a key that already owns
    the ``(tenant_id, app_id, subject_id)`` partial-unique slot -> 409, forever.
    With the field present it evaluates ``"pending" in ("active", "pending")`` ->
    **reusable**, which is what provisioning's own constant documents:

        ``pending`` is here for the same reason ``starts_at`` is not read at all:
        the row owns the unique-index slot, so it is the license this key HAS,
        and re-minting for it is impossible upstream.

    Scope, measured 2026-08-12 so nobody has to re-derive it: this service has NO
    code path that writes ``status='pending'`` (the value appears only in the
    column comment and in ``effective_status()``'s DERIVED output), and prod holds
    0 such rows — 148 active / 68 revoked / 2 expired, and 0 active rows with a
    future ``starts_at``. So the improvement is latent today, not a live change.
    """

    def test_stored_pending_becomes_reusable(self):
        lic = _build("pending", "past", "none", "none")
        assert lic.effective_status() == "pending", "verbatim passthrough expected"
        row = {
            "status": "pending",
            "effective_status": "pending",
            "expires_at": None,
            "revoked_at": None,
        }
        assert (
            _verdict_today({"status": "pending", "expires_at": None, "revoked_at": None}) is False
        )
        assert _verdict_after(row) is True

    def test_a_revoked_or_expired_stored_status_is_still_refused(self):
        """The widening is scoped to `pending` alone — it must not leak into the
        other non-active stored statuses."""
        for status in ("revoked", "expired", "suspended-unknown"):
            lic = _build(status, "past", "none", "none")
            row = {
                "status": status,
                "effective_status": lic.effective_status(),
                "expires_at": None,
                "revoked_at": None,
            }
            assert _verdict_after(row) is False, f"{status} must not become reusable"

    def test_the_floors_still_outrank_a_pending_label(self):
        """A revoked_at timestamp or a lapsed expiry beats the computed label —
        `pending` must not resurrect a row the floors already killed."""
        row = {
            "status": "pending",
            "effective_status": "pending",
            "expires_at": None,
            "revoked_at": _now() - timedelta(days=1),
        }
        assert _verdict_after(row) is False
        row2 = {
            "status": "pending",
            "effective_status": "pending",
            "expires_at": _now() - timedelta(days=1),
            "revoked_at": None,
        }
        assert _verdict_after(row2) is False


class TestTheKnownDivergenceIsUnreachable:
    """`status IS NULL` is the ONE input where the branches disagree.

    `effective_status()` does `str(self.status)`, so a NULL status becomes the
    TRUTHY string `"None"` -> not reusable, where the old branch has
    `None in (None, "", "active")` -> reusable. It is unreachable because the
    column is NOT NULL, and this test pins that rather than the divergence:
    if someone ever makes `status` nullable, this goes red and the divergence
    becomes real.
    """

    def test_status_column_is_not_nullable(self):
        assert License.__table__.c.status.nullable is False, (
            "License.status became nullable — effective_status() would emit the "
            "string 'None' and flip provisioning's verdict from reusable to not"
        )

    def test_empty_string_status_still_falls_through_to_the_old_branch(self):
        """`""` is falsy, so `if effective:` does NOT take the new branch and the
        old comparison still runs. This is why only NULL diverges, not empty."""
        lic = License(
            status="", starts_at=_now() - timedelta(days=1), expires_at=None, revoked_at=None
        )
        assert lic.effective_status() == ""
        row = {"status": "", "effective_status": "", "expires_at": None, "revoked_at": None}
        assert _verdict_after(row) == _verdict_today(
            {"status": "", "expires_at": None, "revoked_at": None}
        )


class TestItIsActuallyOnTheWire:
    def test_to_dict_emits_it(self):
        lic = _build("active", "past", "future", "none")
        data = lic.to_dict()
        assert "effective_status" in data, "to_dict() does not emit effective_status"
        assert data["effective_status"] == "active"

    def test_it_is_not_merely_echoing_status(self):
        """Guards the cheapest wrong implementation: `= self.status`.

        A future-dated row is stored 'active' but is effectively 'pending', so an
        echo implementation is caught here.
        """
        lic = _build("active", "future", "none", "none")
        data = lic.to_dict()
        assert data["status"] == "active"
        assert data["effective_status"] == "pending"
        assert data["effective_status"] != data["status"]

    def test_license_response_carries_it(self):
        from main import LicenseResponse

        lic = _build("active", "past", "past", "none")
        resp = LicenseResponse(**lic.to_dict())
        assert resp.effective_status == "expired"
        assert resp.status == "active", "the STRUCTURAL status must be preserved alongside it"

    def test_license_response_still_accepts_a_payload_without_the_field(self):
        """Optional-with-default: a dict built before this change must still
        validate, so nothing that constructs a LicenseResponse by hand breaks."""
        from main import LicenseResponse

        data = _build("active", "past", "none", "none").to_dict()
        data.pop("effective_status")
        resp = LicenseResponse(**data)
        assert resp.effective_status is None
