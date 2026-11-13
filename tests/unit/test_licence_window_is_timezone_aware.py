"""A licence window must carry a UTC offset, or the stored instant is a guess.

THE DEFECT. `LicenseCreate.starts_at` / `.expires_at` were `str | None`, parsed in
the handler with `datetime.fromisoformat()`. That function returns a **naive**
datetime for an offset-less input like ``"2027-01-01T00:00:00"`` — a perfectly
valid ISO 8601 string, and exactly what the old field description "(ISO 8601)"
invited. The naive value was written into a `timestamptz` column, so PostgreSQL
interpreted it in the **session TimeZone**.

The consequence is not a crash, it is a silently wrong answer: the same request
stores a different instant depending on a server setting, and
`License.effective_status` — which compares against `datetime.now(UTC)` — then
reports the licence live or expired off by the UTC offset. That verdict is served
to `provisioning` and to fr's licence gate.

WHY REJECT RATHER THAN ASSUME UTC. Assuming is the same guess PostgreSQL was
already making, relocated somewhere the caller cannot see. A 422 naming the field
is checkable and is fixed once by the caller.

NOT BREAKING. Pydantic accepts any RFC 3339 / ISO 8601 **string** for a datetime
field, so offset-bearing callers are unaffected — including the only producer in
the tree, `provisioning/src/clients/licenses_client.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def _body(app_id, plan_id, **extra):
    return {"app_id": str(app_id), "plan_id": str(plan_id), **extra}


class TestALicenceWindowMustCarryAnOffset:
    def test_a_naive_expires_at_is_refused(
        self, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        """RED on the pre-fix tree: this was accepted and stored as a guess."""
        response = auth_client_admin.post(
            "/licenses/user/naive-window-subject",
            json=_body(seed_app.id, seed_plan.id, expires_at="2027-01-01T00:00:00"),
        )

        assert response.status_code == 422, (
            f"an offset-less datetime was accepted ({response.status_code}); it will be "
            f"reinterpreted in the database session timezone: {response.text[:200]}"
        )
        # The error must name the field, or the caller cannot act on it.
        assert "expires_at" in response.text, response.text[:300]

    def test_a_naive_starts_at_is_refused(self, auth_client_admin: TestClient, seed_app, seed_plan):
        response = auth_client_admin.post(
            "/licenses/user/naive-start-subject",
            json=_body(seed_app.id, seed_plan.id, starts_at="2027-01-01T00:00:00"),
        )
        assert response.status_code == 422, response.text[:200]
        assert "starts_at" in response.text, response.text[:300]

    def test_an_offset_bearing_window_is_accepted_and_stored_exactly(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """THE CONTROL. Without it, rejecting everything would pass the two above.

        Also asserts the round trip: what comes back is the instant that was sent,
        which is the property the naive path could not guarantee.
        """
        expires = (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0)

        response = auth_client_admin.post(
            "/licenses/user/aware-window-subject",
            json=_body(seed_app.id, seed_plan.id, expires_at=expires.isoformat()),
        )

        assert response.status_code == 201, response.text[:300]
        stored = response.json()["expires_at"]
        assert datetime.fromisoformat(stored) == expires, (
            f"stored instant {stored!r} is not the instant sent {expires.isoformat()!r}"
        )

    def test_a_zulu_suffix_is_accepted(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """`Z` is the spelling every real caller uses; it must not be collateral."""
        response = auth_client_admin.post(
            "/licenses/user/zulu-window-subject",
            json=_body(seed_app.id, seed_plan.id, expires_at="2027-01-01T00:00:00Z"),
        )
        assert response.status_code == 201, response.text[:300]


class TestThePublishedContractSaysSo:
    def test_the_schema_declares_date_time_format(self):
        """A generated client can only enforce what the document declares.

        Before this change both fields were a bare `{"type": "string"}` with the
        ISO-8601 requirement living only in prose, so orval emitted `string` and
        nothing validated until the request reached the service.
        """
        import json
        from pathlib import Path

        spec = json.loads(
            (Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text()
        )
        props = spec["components"]["schemas"]["LicenseCreate"]["properties"]

        for field in ("starts_at", "expires_at"):
            variants = props[field].get("anyOf") or [props[field]]
            formats = {v.get("format") for v in variants}
            assert "date-time" in formats, (
                f"{field} does not publish format=date-time: {props[field]}"
            )
