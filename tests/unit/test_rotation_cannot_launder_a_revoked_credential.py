"""Rotation must not turn a revoked credential into an unbounded one.

`regenerate_token_for_license` derives the replacement's expiry from the tokens it
replaces. When the live (unrevoked) set is EMPTY it has nothing to inherit and falls
into the "nothing replaced" arm, which mints a PERPETUAL token.

That arm is correct for a license that never had a token. It is catastrophic once a
token has existed and been revoked, because the replacement is unbounded where its
predecessor was not.

Both routes into that state were MEASURED on `origin/main` before this fix:

* **Ordinary API use.** `/license/revoke` by token requires the token to introspect
  ACTIVE — which a live 30-day token is. Revoking a leaked token is exactly what an
  operator should do; pressing regenerate afterwards returned `expires_at = None`.
  The incident response for a bounded credential handed back an unbounded one.
* **The #159 sweep.** `revoke_all_expired_tokens` revokes every expired token
  estate-wide. Before it, such a license refused with `AllTokensExpiredError`; after
  it, the live set is empty and the same rotation minted a perpetual token — the
  sweep silently converted a refusal into the laundering that refusal prevents.

The module docstring asserted this state was unreachable ("Verified 2026-08-16 that
no HTTP route can reach that state ... It needs a direct DB write"). The first case
above refutes that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from models import AllTokensExpiredError, AllTokensRevokedError, Token


def _tokens(db_manager, license_id):
    session = db_manager.get_session()
    try:
        return session.query(Token).filter(Token.license_id == license_id).all()
    finally:
        session.close()


class TestRevokeThenRotate:
    """The API-reachable path. No DB write, no sweep, no extra privilege."""

    def test_revoking_a_dated_token_then_rotating_is_refused(self, db_manager, seed_license):
        """The sharpest case: leaked 30-day token -> revoke -> regenerate."""
        _tok, raw = db_manager.create_token(
            license_id=seed_license.id, expires_at=datetime.now(UTC) + timedelta(days=30)
        )
        assert db_manager.revoke_token_for_tenant(raw, str(seed_license.tenant_id)) == (
            "revoked",
            1,
        )
        with pytest.raises(AllTokensRevokedError):
            db_manager.regenerate_token_for_license(seed_license.id)

    def test_revoking_a_perpetual_token_then_rotating_is_refused(self, db_manager, seed_license):
        _tok, raw = db_manager.create_token(license_id=seed_license.id, expires_at=None)
        assert db_manager.revoke_token_for_tenant(raw, str(seed_license.tenant_id)) == (
            "revoked",
            1,
        )
        with pytest.raises(AllTokensRevokedError):
            db_manager.regenerate_token_for_license(seed_license.id)

    def test_the_refusal_destroys_nothing(self, db_manager, seed_license):
        """A refusal that revoked something would be worse than the bug."""
        tok, raw = db_manager.create_token(
            license_id=seed_license.id, expires_at=datetime.now(UTC) + timedelta(days=30)
        )
        db_manager.revoke_token_for_tenant(raw, str(seed_license.tenant_id))
        with pytest.raises(AllTokensRevokedError):
            db_manager.regenerate_token_for_license(seed_license.id)
        rows = _tokens(db_manager, seed_license.id)
        assert len(rows) == 1, "rotation must not have minted or deleted a row"
        assert str(rows[0].id) == str(tok.id)


class TestTheSweepPath:
    """#159's estate-wide sweep must not convert a refusal into a perpetual token."""

    def test_a_swept_license_still_refuses(self, db_manager, seed_license):
        db_manager.create_token(
            license_id=seed_license.id, expires_at=datetime.now(UTC) - timedelta(days=1)
        )
        # Before the sweep the license refuses because the inherited expiry is past.
        with pytest.raises(AllTokensExpiredError):
            db_manager.regenerate_token_for_license(seed_license.id)
        db_manager.revoke_all_expired_tokens()
        # After the sweep it must STILL refuse — with the other error, not a mint.
        with pytest.raises(AllTokensRevokedError):
            db_manager.regenerate_token_for_license(seed_license.id)


class TestTheFreshMintFallbackSurvives:
    """The discriminating half. Refusing on 'no tokens at all' would break a real path.

    A license that has never held a token has no policy to preserve, so it is a NEW
    mint (the 365-day api default since licenses#244) and is documented as such. A
    guard that refused here would be reverted, taking the fix with it.
    """

    def test_a_license_with_no_tokens_still_mints(self, db_manager, seed_license):
        assert _tokens(db_manager, seed_license.id) == []
        _count, token, _raw = db_manager.regenerate_token_for_license(seed_license.id)
        assert token.expires_at is not None
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((token.expires_at - expected).total_seconds()) < 120

    def test_a_live_token_still_rotates_and_inherits_its_expiry(self, db_manager, seed_license):
        expiry = datetime.now(UTC) + timedelta(days=30)
        db_manager.create_token(license_id=seed_license.id, expires_at=expiry)
        _count, token, _raw = db_manager.regenerate_token_for_license(seed_license.id)
        assert token.expires_at is not None
        assert abs((token.expires_at - expiry).total_seconds()) < 2

    def test_a_revoked_token_alongside_a_live_one_still_rotates(self, db_manager, seed_license):
        """The guard keys on the live set being EMPTY, not on a revoked row existing.

        Every ordinary rotation leaves revoked rows behind, so a guard that fired
        whenever one existed would break rotation permanently after the first use.
        """
        expiry = datetime.now(UTC) + timedelta(days=30)
        _old, raw = db_manager.create_token(license_id=seed_license.id, expires_at=expiry)
        db_manager.revoke_token_for_tenant(raw, str(seed_license.tenant_id))
        db_manager.create_token(license_id=seed_license.id, expires_at=expiry)
        _count, token, _raw = db_manager.regenerate_token_for_license(seed_license.id)
        assert token.expires_at is not None


class TestTheRouteAnswersADistinctDiscriminator:
    def test_the_409_names_all_tokens_revoked(self, auth_client_admin, seed_license, db_manager):
        _tok, raw = db_manager.create_token(
            license_id=seed_license.id, expires_at=datetime.now(UTC) + timedelta(days=30)
        )
        db_manager.revoke_token_for_tenant(raw, str(seed_license.tenant_id))
        response = auth_client_admin.post(f"/licenses/{seed_license.id}/regenerate-token")
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "all_tokens_revoked"
        assert "remedy" in detail
