"""
Unit tests for Token utility methods and database token lifecycle.

Tests hash_token(), generate_token() static methods AND real database operations.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from models import Token


class TestTokenHashToken:
    """Tests for Token.hash_token() static method."""

    def test_hash_token_sha256(self):
        """Test that hash_token uses SHA-256."""
        raw_token = "my-secret-token"
        result = Token.hash_token(raw_token)

        expected = hashlib.sha256(raw_token.encode()).hexdigest()
        assert result == expected

    def test_hash_token_deterministic(self):
        """Test that same input produces same hash."""
        raw_token = "same-token"
        hash1 = Token.hash_token(raw_token)
        hash2 = Token.hash_token(raw_token)

        assert hash1 == hash2

    def test_hash_token_different_inputs_different_hashes(self):
        """Test that different inputs produce different hashes."""
        hash1 = Token.hash_token("token1")
        hash2 = Token.hash_token("token2")

        assert hash1 != hash2

    def test_hash_token_matches_stored_in_db(self, db_manager, seed_license):
        """Create token via db_manager, verify stored hash matches Token.hash_token(raw)."""
        token_obj, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        expected_hash = Token.hash_token(raw_token)
        assert token_obj.token_hash == expected_hash

    def test_hash_token_enables_lookup(self, db_manager, seed_license):
        """Create token, look it up by hash via db_manager.get_token_by_hash."""
        token_obj, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        computed_hash = Token.hash_token(raw_token)
        found = db_manager.get_token_by_hash(computed_hash)

        assert found is not None
        assert str(found.id) == str(token_obj.id)
        assert found.token_hash == computed_hash

    def test_hash_token_wrong_raw_token_no_match(self, db_manager, seed_license):
        """Create token, verify lookup with wrong raw token returns None."""
        db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        wrong_hash = Token.hash_token("completely-wrong-token")
        found = db_manager.get_token_by_hash(wrong_hash)

        assert found is None

    def test_hash_token_empty_string_no_db_match(self, db_manager, seed_license):
        """Verify empty string hash can't match any real token in the database."""
        db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        empty_hash = Token.hash_token("")
        found = db_manager.get_token_by_hash(empty_hash)

        assert found is None

    def test_hash_token_full_cycle_with_db(self, db_manager, seed_license):
        """Create token, hash it, look it up, verify the full round-trip."""
        token_obj, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        # Hash the raw token, look up by hash, introspect via raw token
        found = db_manager.get_token_by_hash(Token.hash_token(raw_token))
        assert found is not None
        assert str(found.license_id) == str(seed_license.id)

        result = db_manager.introspect_token(raw_token)
        assert result is not None
        assert result["active"] is True

    def test_introspect_throttles_last_used_at_write(self, db_manager, seed_license):
        """Two introspects in quick succession must not re-write last_used_at.

        The hot-path write is throttled (TOKEN_LAST_USED_THROTTLE_SECONDS) so the
        common poll path stays read-only; a second call inside the window leaves
        the timestamp untouched.
        """
        _, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        token_hash = Token.hash_token(raw_token)

        assert db_manager.introspect_token(raw_token)["active"] is True
        first = db_manager.get_token_by_hash(token_hash).last_used_at
        assert first is not None

        assert db_manager.introspect_token(raw_token)["active"] is True
        second = db_manager.get_token_by_hash(token_hash).last_used_at
        assert second == first  # throttled: no second write

    def test_introspect_updates_stale_last_used_at(self, db_manager, seed_license):
        """Once last_used_at is older than the throttle window, it is refreshed."""
        _, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        token_hash = Token.hash_token(raw_token)

        # Force a stale last_used_at well past the throttle window.
        stale = datetime.now(UTC) - timedelta(hours=1)
        session = db_manager.get_session()
        try:
            tok = session.query(Token).filter(Token.token_hash == token_hash).first()
            tok.last_used_at = stale
            session.commit()
        finally:
            session.close()

        assert db_manager.introspect_token(raw_token)["active"] is True
        refreshed = db_manager.get_token_by_hash(token_hash).last_used_at
        assert refreshed > stale  # stale entry refreshed

    def test_hash_token_long_invalid_token_introspects_not_found(self, db_manager, seed_license):
        """Introspection with a very long invalid token returns token_not_found."""
        # Ensure seed data exists but we use a bogus token
        db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        long_token = "x" * 10000
        result = db_manager.introspect_token(long_token)

        assert result is not None
        assert result["active"] is False
        assert result["reason"] == "token_not_found"


class TestTokenGenerateToken:
    """Tests for Token.generate_token() static method."""

    def test_generate_token_url_safe(self):
        """Test that generated token is URL-safe."""
        for _ in range(100):
            token = Token.generate_token()
            for char in token:
                assert char.isalnum() or char in "-_", f"Non-URL-safe char: {char}"

    def test_generate_token_unique(self):
        """Test that generate_token produces unique tokens."""
        tokens = set()
        for _ in range(50):
            token = Token.generate_token()
            assert token not in tokens, "Duplicate token generated"
            tokens.add(token)

    def test_generate_token_sufficient_length(self):
        """Test that generated token has sufficient entropy."""
        token = Token.generate_token()
        assert len(token) >= 32, "Token too short for security"

    def test_generate_token_creates_valid_db_token(self, db_manager, seed_license):
        """Generate via db_manager.create_token, verify introspection succeeds."""
        token_obj, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        result = db_manager.introspect_token(raw_token)

        assert result is not None
        assert result["active"] is True
        assert result["license_id"] == str(seed_license.id)
        assert result["tenant_id"] == "01000000-0000-7000-8000-000000000001"

    def test_generate_token_each_call_unique_in_db(self, db_manager, seed_license):
        """Create multiple tokens for same license, all independently introspectable."""
        tokens = []
        for _ in range(5):
            token_obj, raw_token = db_manager.create_token(
                license_id=UUID(str(seed_license.id)),
                token_type="api",
            )
            tokens.append((token_obj, raw_token))

        # All should introspect independently
        for token_obj, raw_token in tokens:
            result = db_manager.introspect_token(raw_token)
            assert result is not None
            assert result["active"] is True
            assert result["license_id"] == str(seed_license.id)

        # All hashes should be different
        hashes = [t.token_hash for t, _ in tokens]
        assert len(set(hashes)) == len(hashes), "Token hashes should all be unique"

    def test_generate_token_different_hashes_for_same_license(self, db_manager, seed_license):
        """Two tokens for the same license must not share the same hash."""
        _, raw1 = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        _, raw2 = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        assert Token.hash_token(raw1) != Token.hash_token(raw2)


class TestTokenIntegration:
    """Integration tests for token lifecycle through the database."""

    def test_create_and_introspect_active_token(self, db_manager, seed_license):
        """Create token, introspect, verify active=True with correct metadata."""
        token_obj, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        result = db_manager.introspect_token(raw_token)

        assert result is not None
        assert result["active"] is True
        assert result["tenant_id"] == "01000000-0000-7000-8000-000000000001"
        assert result["license_id"] == str(seed_license.id)
        assert result["subject_id"] == str(seed_license.subject_id)
        assert result["status"] == "active"
        # App and plan info should be present
        assert result["app"] is not None
        assert result["app"]["key"] == "test-app"
        assert result["plan"] is not None
        assert result["plan"]["code"] == "trial"

    def test_revoke_then_introspect_inactive(self, db_manager, seed_license):
        """Create token, revoke it, introspect returns active=False."""
        token_obj, raw_token = db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        # Verify it's active first
        result = db_manager.introspect_token(raw_token)
        assert result["active"] is True

        # Revoke
        revoked = db_manager.revoke_token(raw_token)
        assert revoked is True

        # Introspect again
        result = db_manager.introspect_token(raw_token)
        assert result is not None
        assert result["active"] is False
        assert result["reason"] == "token_invalid"

    def test_introspect_wrong_token_not_found(self, db_manager, seed_license):
        """Introspect garbage token returns active=False, reason=token_not_found."""
        # Create a real token so the DB isn't empty
        db_manager.create_token(
            license_id=UUID(str(seed_license.id)),
            token_type="api",
        )
        result = db_manager.introspect_token("totally-bogus-garbage-token")

        assert result is not None
        assert result["active"] is False
        assert result["reason"] == "token_not_found"

    def test_revoke_all_tokens_for_license(self, db_manager, seed_license):
        """Create 3 tokens, revoke_tokens_for_license, verify all introspect as inactive."""
        raw_tokens = []
        for _ in range(3):
            _, raw_token = db_manager.create_token(
                license_id=UUID(str(seed_license.id)),
                token_type="api",
            )
            raw_tokens.append(raw_token)

        # Verify all 3 are active
        for raw in raw_tokens:
            result = db_manager.introspect_token(raw)
            assert result["active"] is True

        # Revoke all tokens for the license
        count = db_manager.revoke_tokens_for_license(UUID(str(seed_license.id)))
        assert count == 3

        # Verify all 3 are now inactive
        for raw in raw_tokens:
            result = db_manager.introspect_token(raw)
            assert result is not None
            assert result["active"] is False
            assert result["reason"] == "token_invalid"
