"""The startup line that names the database must never carry the password.

`licenses` printed its full DSN — including the `licenses_user` production
password — on every pod start (`main.py`, "Database initialized"). Verified
live on 2026-08-11: two occurrences in the running log window, one per pod.
Pod logs are readable by anyone with `kubectl logs` on the namespace and are
shipped onward to whatever collects them, so this published a production
database credential continuously.

Every assertion below checks that the SECRET IS ABSENT, not that a placeholder
is present. A redactor that emits `***REDACTED***` *and* leaves the password
somewhere else in the string passes the second check and fails the first.
"""

from __future__ import annotations

from typing import Any

import pytest

from models import redact_dsn

SECRET = "sUp3r-s3cr3t-pw"


class TestRedactionRemovesTheSecret:
    def test_the_userinfo_password_is_gone(self):
        url = f"postgresql+psycopg://licenses_user:{SECRET}@pgbouncer.example.com:6432/licenses"
        out = redact_dsn(url)
        assert SECRET not in out, out

    def test_a_password_query_parameter_is_gone(self):
        """libpq accepts the password as a query parameter too.

        A redactor that only rewrites the userinfo leaves this one intact —
        and it is the same credential.
        """
        url = f"postgresql+psycopg://licenses_user@pgbouncer.example.com:6432/licenses?password={SECRET}"
        out = redact_dsn(url)
        assert SECRET not in out, out

    def test_an_sslpassword_query_parameter_is_gone(self):
        """`sslpassword` is the client-key passphrase — just as sensitive."""
        url = f"postgresql+psycopg://u@h:6432/licenses?sslmode=verify-full&sslpassword={SECRET}"
        out = redact_dsn(url)
        assert SECRET not in out, out

    def test_both_at_once_are_gone(self):
        url = (
            f"postgresql+psycopg://licenses_user:{SECRET}@pgbouncer.example.com:6432/licenses"
            f"?sslmode=require&password={SECRET}"
        )
        out = redact_dsn(url)
        assert SECRET not in out, out

    def test_a_percent_encoded_password_is_gone(self):
        """A percent-encoded password must not survive as its decoded form either."""
        url = "postgresql+psycopg://licenses_user:p%40ss%3Aword@pgbouncer.example.com:6432/licenses"
        out = redact_dsn(url)
        for leaked in ("p%40ss%3Aword", "p@ss:word"):
            assert leaked not in out, out

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://licenses_user:pw@host:6432/db",
            "postgresql://u:pw@h/d",
            "postgresql+psycopg://u:pw@h:5432/d?sslmode=disable",
        ],
    )
    def test_no_bare_pw_survives_any_shape(self, url):
        assert "pw" not in redact_dsn(url).replace("password", "").replace("sslpassword", "")

    def test_a_path_valued_parameter_is_not_redacted(self):
        """`passfile`/`sslkey`/`sslcert` are paths, not secrets.

        Pins the substring rule from BOTH sides: it must catch anything named
        like a credential, and must not swallow the operationally useful paths.
        """
        url = f"postgresql+psycopg://u:{SECRET}@h:6432/d?passfile=/etc/pgpass&sslkey=/certs/k.pem"
        out = redact_dsn(url)
        assert "%2Fetc%2Fpgpass" in out or "/etc/pgpass" in out, out
        assert "k.pem" in out, out
        assert SECRET not in out, out

    def test_an_unknown_credential_shaped_parameter_is_redacted(self):
        """The rule must keep working for parameters nobody enumerated."""
        for key in ("some_password", "api_secret", "auth_token"):
            url = f"postgresql+psycopg://u@h:6432/d?{key}={SECRET}"
            assert SECRET not in redact_dsn(url), key

    def test_a_secret_embedded_in_another_parameters_value_is_redacted(self):
        """`parse_qsl` has not split on `;` since Python 3.10.

        `?sslmode=require;password=X` therefore parses as the SINGLE pair
        ("sslmode", "require;password=X"), so a redactor keyed only on the
        parameter NAME sees a harmless key and prints the password. Found by
        running the redactor against adversarial input, not by reading it.
        """
        url = f"postgresql+psycopg://u@h:6432/db?sslmode=require;password={SECRET}"
        assert SECRET not in redact_dsn(url), redact_dsn(url)

    def test_an_uppercase_parameter_name_is_still_redacted(self):
        url = f"postgresql+psycopg://u@h:6432/db?PASSWORD={SECRET}"
        assert SECRET not in redact_dsn(url), redact_dsn(url)

    def test_a_libpq_keyword_value_dsn_is_refused_not_echoed(self):
        """`DATABASE_URL` could hold libpq keyword/value syntax, which is not a URL.

        It has no scheme, so the fail-closed guard must refuse it rather than
        hand back a string containing `password=...`.
        """
        dsn = f"host=pgbouncer.example.com port=6432 dbname=licenses password={SECRET}"
        assert SECRET not in redact_dsn(dsn), redact_dsn(dsn)

    def test_a_value_that_is_itself_a_url_with_userinfo_is_redacted(self):
        """Same class as the `;password=` hole: a credential inside a VALUE.

        A key-based rule cannot see `callback=https://user:pass@host`, because
        the key ("callback") is innocent and the secret rides in the value.
        """
        url = f"postgresql+psycopg://u@h:6432/db?callback=https%3A%2F%2Fx%3A{SECRET}%40y.com"
        assert SECRET not in redact_dsn(url), redact_dsn(url)

    def test_the_username_is_deliberately_not_treated_as_a_secret(self):
        """The username is KEPT on purpose — it is `licenses_user`, not a credential.

        Pinned so nobody "hardens" it away: this line exists to tell an
        operator which role the pod connected as, and redacting it would make
        the line useless while protecting nothing.
        """
        out = redact_dsn("postgresql+psycopg://licenses_user:pw@h:6432/db")
        assert "licenses_user" in out, out


class TestRedactionKeepsTheLineUseful:
    """A line that answers nothing gets reverted to the raw URL by the next person."""

    def test_driver_user_host_port_and_database_survive(self):
        url = f"postgresql+psycopg://licenses_user:{SECRET}@pgbouncer.example.com:6432/licenses"
        out = redact_dsn(url)
        for kept in (
            "postgresql+psycopg",
            "licenses_user",
            "pgbouncer.example.com",
            "6432",
            "licenses",
        ):
            assert kept in out, f"{kept!r} must survive redaction, got: {out}"

    def test_a_non_secret_query_parameter_survives(self):
        url = f"postgresql+psycopg://u:{SECRET}@h:6432/d?sslmode=verify-full"
        out = redact_dsn(url)
        assert "sslmode=verify-full" in out, out
        assert SECRET not in out, out

    def test_a_passwordless_url_is_unchanged_in_substance(self):
        url = "postgresql+psycopg://localhost:5432/licenses"
        out = redact_dsn(url)
        assert "localhost" in out and "5432" in out and "licenses" in out, out

    def test_an_ipv6_host_keeps_its_brackets(self):
        """Without brackets "::1" + ":6432" renders as "::1:6432" — ambiguous.

        This line exists to tell an operator which host the pod attached to, so
        a mangled host defeats its only purpose.
        """
        out = redact_dsn(f"postgresql+psycopg://u:{SECRET}@[::1]:6432/db")
        assert "[::1]:6432" in out, out
        assert SECRET not in out, out


class TestRedactionFailsClosed:
    """Never fall back to the input. The malformed case is the untested one."""

    @pytest.mark.parametrize(
        "url",
        [
            f"postgresql+psycopg://user:{SECRET}@[unclosed-bracket:6432/db",
            f"://:{SECRET}@",
        ],
    )
    def test_an_unparseable_url_never_echoes_the_secret(self, url):
        out = redact_dsn(url)
        assert SECRET not in out, (
            f"a parse failure must return a placeholder, never the input — got: {out}"
        )

    def test_the_empty_string_does_not_raise(self):
        assert isinstance(redact_dsn(""), str)


class TestTheStartupLineItself:
    """End-to-end: the emitted record, not just the helper."""

    def test_the_logged_startup_line_carries_no_password(self, caplog):
        """The line main.py ACTUALLY emits, extracted from main.py's own source.

        This test used to author its own call —
        ``main.logger.info("Database initialized: %s", main.redact_dsn(...))``
        — and assert on that. It therefore said nothing whatsoever about
        main.py, while its docstring claimed the opposite. An adversarial pass
        restored the original defect by concatenation and this test stayed
        green.

        The real structural guard now lives in
        ``tests/unit/test_no_dsn_reaches_a_log_call.py``, which walks every
        logging call in main.py/models.py. This one keeps a behavioural check:
        it compiles and runs the startup statement EXACTLY as written in
        main.py, so the emitted record is the production one.
        """
        import ast
        import logging
        from pathlib import Path

        import main

        source = Path(main.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        stmts = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and "Database initialized" in ast.dump(node.value)
        ]
        assert len(stmts) == 1, (
            f"expected exactly one 'Database initialized' log statement, found {len(stmts)} — "
            "this test can no longer locate the production call site"
        )

        url = f"postgresql+psycopg://licenses_user:{SECRET}@pgbouncer.example.com:6432/licenses"
        namespace = {
            "logger": main.logger,
            "redact_dsn": main.redact_dsn,
            "DATABASE_URL": url,
        }
        module = ast.Module(body=[stmts[0]], type_ignores=[])
        ast.fix_missing_locations(module)

        with caplog.at_level(logging.INFO, logger="main"):
            exec(compile(module, main.__file__, "exec"), namespace)  # noqa: S102

        assert caplog.records, "the assertion is vacuous if nothing was logged"
        assert SECRET not in caplog.text, caplog.text
        assert "pgbouncer.example.com" in caplog.text, caplog.text

    def test_the_source_call_site_does_not_interpolate_the_raw_url(self):
        """Pin the call site, not just its output.

        The original defect was `logger.info(f"Database initialized: {DATABASE_URL}")`.
        A future edit back to that form would keep every other test in this
        file green, because they all exercise `_redact_dsn` directly.
        """
        from pathlib import Path

        source = Path(main_source_path()).read_text()
        assert 'f"Database initialized: {DATABASE_URL}"' not in source, (
            "the startup line must not interpolate the raw DSN"
        )
        assert "redact_dsn(DATABASE_URL)" in source, (
            "the startup line must route the DSN through _redact_dsn"
        )


class TestTheSslBranchInModels:
    """`_append_ssl_params` logged the DSN too — with an INVERTED redaction.

    The old line was::

        logger.info("Database URL with SSL: %s", result.split("@")[0] + "@...")  # Don't log creds

    Everything before the first `@` is `scheme://user:password`, so it printed
    the password verbatim and hid the hostname — the precise opposite of the
    comment above it. Production escapes it only because POSTGRES_SSLMODE is
    `disable` there and the function returns before this line; the CODE DEFAULT
    is `require` (models.py) and docker-compose.yml sets `require`, so every
    other environment reached it.
    """

    def test_the_ssl_branch_logs_no_password(self, monkeypatch, caplog):
        import logging

        from models import DatabaseManager

        monkeypatch.setenv("POSTGRES_SSLMODE", "require")
        # No cert env / no discoverable certs -> the sslmode-only branch, which
        # is the one that reaches the log line.
        for var in ("PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"):
            monkeypatch.delenv(var, raising=False)

        manager = DatabaseManager.__new__(DatabaseManager)
        url = f"postgresql+psycopg://licenses_user:{SECRET}@pgbouncer.example.com:6432/licenses"

        with caplog.at_level(logging.INFO, logger="models"):
            manager._append_ssl_params(url)

        assert caplog.records, "vacuous: the SSL branch logged nothing at all"
        assert SECRET not in caplog.text, caplog.text

    def test_the_old_inverted_form_is_gone_from_the_source(self):
        from pathlib import Path

        import models

        assert models.__file__ is not None
        # Comment lines are excluded on purpose: the fix DOCUMENTS the old
        # form in a comment so nobody reintroduces it, and a naive substring
        # search over the whole file matches that explanation and fails.
        # Assert on statements, not on prose.
        code_lines = [
            line
            for line in Path(models.__file__).read_text().splitlines()
            if not line.lstrip().startswith("#")
        ]
        offenders = [line for line in code_lines if 'result.split("@")[0]' in line]
        assert not offenders, (
            f"the inverted 'redaction' keeps the credential and drops the host: {offenders}"
        )


class TestTheDecompositionIsFaithful:
    """`urlsplit` succeeding is not evidence it split where we assume.

    A password containing `#`, `/`, `?` or `@` ends the netloc early, so
    credential bytes land in the HOST or PATH slot. Every component then looks
    individually plausible, the guards pass, and the rebuilt line prints raw
    password characters next to a `***REDACTED***` token — which is what makes
    it read as correctly redacted.

    Measured over 50k random 16-char symbol passwords in the production DSN
    shape: 0.502% leaked >=6 contiguous password characters before this check,
    0.000% after (same corpus and seed).
    """

    def test_a_hash_in_the_password_does_not_put_the_rest_in_the_host(self):
        """`#` cuts the URL; the tail of the password landed in the host slot.

        The fragment used to be DROPPED with a comment saying a DSN has no
        meaningful fragment — correct premise, wrong conclusion: unrecognised
        input must be refused, not silently truncated.
        """
        out = redact_dsn(
            f"postgresql+psycopg://licenses_user:p@{SECRET}#x@pgbouncer.example.com:6432/licenses"
        )
        assert SECRET.lower() not in out.lower(), out

    def test_a_hash_in_the_password_does_not_put_the_rest_in_the_path(self):
        out = redact_dsn(
            f"postgresql+psycopg://licenses_user:/{SECRET}#x@pgbouncer.example.com:6432/licenses"
        )
        assert SECRET.lower() not in out.lower(), out

    def test_an_at_sign_surviving_into_the_query_is_refused(self):
        """The only legal `@` in a DSN is the userinfo separator."""
        out = redact_dsn(
            f"postgresql+psycopg://licenses_user:/db?{SECRET}@pgbouncer.example.com:6432/licenses"
        )
        assert SECRET.lower() not in out.lower(), out

    def test_a_non_host_charset_in_the_host_slot_is_refused(self):
        out = redact_dsn(f"postgresql+psycopg://u:x@{SECRET};&^weird")
        assert SECRET.lower() not in out.lower(), out

    @pytest.mark.parametrize(
        "url",
        [
            # `options` is an ALLOWLISTED key, and the value carries no
            # `password=` assignment — only the bare word plus a space.
            f"postgresql+psycopg://u@h:6432/db?options=password%20{SECRET}",
            # `sslmode` is a closed enum; `require;<secret>` is one parse pair.
            f"postgresql+psycopg://u@h:6432/db?sslmode=require;{SECRET}",
        ],
    )
    def test_a_secret_without_an_assignment_separator_is_redacted(self, url):
        assert SECRET not in redact_dsn(url), redact_dsn(url)

    def test_no_random_password_leaks_a_contiguous_run(self):
        """Property check over the shape production actually builds.

        docker-compose.yml interpolates `${POSTGRES_PASSWORD_LICENSES}` into the
        DSN unencoded, so the redactor must hold for arbitrary password bytes,
        not just the ones someone thought to write a case for.
        """
        import random
        import string

        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+?/.,;:"
        template = (
            "postgresql+psycopg://licenses_user:{pw}"
            "@pgbouncer.example.com:6432/licenses?sslmode=disable"
        )
        rng = random.Random(12)
        asserted = 0
        for _ in range(4000):
            pw = "".join(rng.choice(alphabet) for _ in range(16))
            out = redact_dsn(template.format(pw=pw))
            if out == "<unparseable database url — redacted>":
                continue
            # NO exemption for `@`-in-password here. An earlier version skipped
            # it as "the documented undecidable residual"; measured over 50k of
            # this exact corpus that skipped 10.11% of cases and NONE of them
            # leaked — in this template a real `@host` always follows the
            # password and `rpartition` takes the LAST `@`, so the residual
            # cannot arise. The exemption protected nothing and un-protected a
            # tenth of the corpus against future regressions.
            asserted += 1
            leaked = [pw[i : i + 6] for i in range(len(pw) - 5) if pw[i : i + 6] in out]
            assert not leaked, f"pw={pw!r} leaked {leaked!r} in {out!r}"

        # Anti-vacuity: ~45% of this corpus is legitimately refused, so without
        # a floor a redactor that refused EVERYTHING would pass the loop above.
        assert asserted > 1500, (
            f"only {asserted}/4000 cases reached an assertion — the redactor is "
            "refusing so much that this test no longer proves anything"
        )

    def test_an_empty_port_is_refused(self):
        """`host:` with no digits is evidence the split went wrong.

        `parts.port` DESTROYS that evidence — it returns None both for "no
        colon" and for "colon, no digits". `…//licenses_user:/<password>` splits
        into netloc `licenses_user:` plus path `/<password>`, and with no `@`
        anywhere the password is a legal dbname sitting in the path slot.
        """
        out = redact_dsn(f"postgresql+psycopg://licenses_user:/{SECRET}")
        assert SECRET.lower() not in out.lower(), out

    @pytest.mark.parametrize(
        "netloc",
        [
            # IPvFuture: urlsplit accepts `v<hex>.` + ANY tail, and the colon in
            # that tail used to satisfy a `":" not in hostname` escape hatch,
            # skipping the hostname charset check completely.
            "u:p@[v7.{secret}:XYZ]",
            "u:p@[v7.{secret}]",
        ],
    )
    def test_a_bracketed_host_must_really_be_an_ipv6_literal(self, netloc):
        out = redact_dsn(f"postgresql+psycopg://{netloc.format(secret=SECRET)}/db")
        assert SECRET.lower() not in out.lower(), out

    def test_an_ipv6_zone_id_must_look_like_an_interface_name(self):
        """`ipaddress` accepts ANY zone id and urlsplit does not lowercase it.

        Scoped honestly: this constrains the zone to an interface-name charset,
        which refuses punctuation-bearing junk. It does NOT make an
        alphanumeric zone distinguishable from arbitrary bytes — a zone id
        legitimately looks like `eth0`, so that is the same undecidability as
        the unencoded-`@` residual below, and reaching it requires a password
        containing both `[` and `%`.
        """
        # A real interface name still renders — this must not become a blanket
        # refusal of every bracketed host.
        assert "[::1%eth0]:6432" in redact_dsn("postgresql+psycopg://u:p@[::1%eth0]:6432/db")
        junk = f"{SECRET}!$&'()*+,;="
        out = redact_dsn(f"postgresql+psycopg://u:p@[::1%{junk}]/db")
        assert junk not in out, out

    @pytest.mark.parametrize(
        "key,good",
        [
            ("sslmode", "require"),
            ("gssencmode", "disable"),
            ("channel_binding", "require"),
            ("target_session_attrs", "read-write"),
            ("replication", "database"),
            ("load_balance_hosts", "random"),
            ("sslsni", "1"),
            ("sslcompression", "0"),
            ("keepalives", "1"),
            ("connect_timeout", "10"),
            ("port", "6432"),
        ],
    )
    def test_every_closed_enum_key_rejects_a_smuggled_secret(self, key, good):
        """Fixing one key and leaving its siblings is how this class survives.

        `?sslmode=require;<secret>` was closed while `?gssencmode=disable;
        <secret>` — the identical shape — was still printed.
        """
        out = redact_dsn(f"postgresql+psycopg://u@h:6432/db?{key}={good};{SECRET}")
        assert SECRET not in out, out
        # and the legitimate value must still render
        assert SECRET not in redact_dsn(f"postgresql+psycopg://u@h:6432/db?{key}={good}")

    def test_a_non_str_input_fails_closed_instead_of_raising(self):
        """The docstring claims it fails closed; for `bytes` that was false.

        The faithfulness check sat OUTSIDE the try, so a bytes input raised
        TypeError straight out of the function.
        """
        # Typed as Any deliberately: the point is what happens when a caller
        # violates the annotation, which is exactly what a type-checker would
        # otherwise stop us from expressing.
        bad_inputs: list[Any] = [b"postgresql://u:pw@h/db", bytearray(b"postgresql://u:pw@h/db")]
        for bad in bad_inputs:
            assert isinstance(redact_dsn(bad), str)

    def test_an_unencoded_at_in_the_password_is_a_known_undecidable_residual(self):
        """Pinned deliberately: this one is NOT fixed, and cannot be here.

        `postgresql://licenses_user:p@aB9xQmT2vLp` is byte-identical to a
        legitimate DSN with user `licenses_user`, password `p` and host
        `aB9xQmT2vLp`. No parser can separate the two readings, so the bytes
        after the `@` are printed in the host slot.

        RFC 3986 requires `@` to be percent-encoded inside userinfo, so the
        precondition is a malformed DSN — the fix belongs where the DSN is
        BUILT (percent-encode the password), not here. `@` is not in the
        base64 alphabet, so `openssl rand -base64` passwords are unaffected.

        This test exists so the gap is visible rather than mistaken for
        covered. If a future change makes it decidable, this test will fail and
        should be replaced by a real assertion.
        """
        out = redact_dsn("postgresql+psycopg://licenses_user:p@aB9xQmT2vLp")
        assert "ab9xqmt2vlp" in out.lower(), (
            "the undecidable residual changed shape — re-examine whether it is "
            f"now decidable, and update this pin: {out}"
        )


def main_source_path() -> str:
    import main

    assert main.__file__ is not None
    return main.__file__
