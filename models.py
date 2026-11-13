"""
Database models for licensing service
Uses SQLAlchemy with PostgreSQL
No PII, identity handled by Keycloak
"""

import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, cast
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid7

import psycopg.errors
from pydantic import AwareDatetime, TypeAdapter, ValidationError
from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    and_,
    case,
    create_engine,
    exists,
    false,
    or_,
    text,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy import column as sql_column
from sqlalchemy import table as sql_table
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    column_property,
    joinedload,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


# How a posture was arrived at. ⛔ THIS IS THE DIFFERENCE BETWEEN A FACT AND A
# FABRICATION, and before it existed the two were the same value.
#
# `byo_mode='byo_required'` is returned in THREE situations that are not the same
# claim: the row says so, there is no row, or the read FAULTED. A caller holding
# only the string cannot tell them apart, so every caller was forced to treat a
# fabrication as a measurement. billing does exactly that — it caches a 200 from
# `GET /tenants/{n}/byo-mode` and freezes it into an immutable invoice, so one
# database blip bills an internal tenant commercially, forever, with no 5xx to
# retry on.
#
# The three values are NOT a severity ordering and must not be collapsed into a
# boolean: `missing_row` is a real, durable fact about the registry (an
# unprovisioned tenant), while `fault` is the ABSENCE of a fact. A caller may
# legitimately cache the first and must never cache the second.
RESOLUTION_ROW = "row"  # read from a `tenants` row. A measurement.
RESOLUTION_MISSING_ROW = "missing_row"  # no such tenant. Durable, cacheable.
RESOLUTION_FAULT = "fault"  # the read failed. NOT an answer; do not cache.
RESOLUTIONS = (RESOLUTION_ROW, RESOLUTION_MISSING_ROW, RESOLUTION_FAULT)


class TenantPosture(NamedTuple):
    """Everything the auth hot path needs from the ``tenants`` row, in ONE read.

    ⭐ WHY A TUPLE AND NOT TWO LOOKUPS. ``_introspect_from_session`` is pinned by
    ``tests/unit/test_introspect_query_count.py`` to exactly TWO SELECTs — the
    token->license->app->plan join, and this tenant row. Resolving ``archived``
    with its own query would make that three, adding a cross-node pgbouncer
    round-trip to every ext_authz call in the fleet. The tenant row was already
    being fetched for ``byo_mode`` and simply threw the rest away; this carries
    it instead. The archived gate therefore costs ZERO additional statements.

    ``name`` is carried so the refusal log line can name the tenant without a
    second read. It is deliberately NOT exported as a metric label — see the gate
    in ``_introspect_from_session`` for why gitops #3795 refused to put tenant
    identity in Prometheus.

    ``resolution`` says WHERE ``byo_mode`` came from — see ``RESOLUTIONS``. It
    DEFAULTS to ``RESOLUTION_ROW`` so every existing positional construction
    (``TenantPosture(mode, archived, name)``) keeps meaning "read from a row",
    which is what those three sites are: only the fabricating paths set it.
    Carrying it costs no additional statement, for the same reason ``archived``
    does not.
    """

    byo_mode: str
    archived: bool
    name: str | None
    resolution: str = RESOLUTION_ROW


# The value fabricated when the tenant row cannot be resolved — no row at all, or
# a DB fault that survived a retry.
#
# ⛔ THE TWO FIELDS FAIL IN OPPOSITE DIRECTIONS, AND THAT ASYMMETRY IS THE WHOLE
# POINT. Do not "make it consistent".
#
#   byo_mode='byo_required'  fails CLOSED. It withholds a PRIVILEGE (inference on
#       the owner's GPU). The cost of being wrong is that a tenant is told to
#       bring its own key for up to one cache window.
#
#   archived=False           fails OPEN. Refusing here does not withhold a
#       privilege, it de-authorizes the licence ENTIRELY. Fabricating that on an
#       infrastructure blip would take every affected caller offline — the exact
#       failure ``introspect_retry_exhausted`` already refuses to cause when it
#       re-raises rather than returning a fabricated "token invalid". An
#       unresolvable tenant is not evidence of archiving; it is evidence of
#       nothing, and "evidence of nothing" must never be spent as a kill.
#
# A tenant with no row cannot be archived either: ``archived_at`` is a column on
# a row that does not exist. False is the only truthful answer, not merely the
# safe one.
#
# ⛔ TWO CONSTANTS, NOT ONE, AND THE SPLIT IS THE WHOLE POINT OF `resolution`.
# Both carry the IDENTICAL fail-closed byo_mode and the IDENTICAL fail-open
# archived flag, so no access gate's behaviour depends on which one it got — the
# only difference is that they say WHY, which is the thing a caller that writes
# money needs and could not previously ask.
_MISSING_ROW_TENANT_POSTURE = TenantPosture(
    byo_mode="byo_required", archived=False, name=None, resolution=RESOLUTION_MISSING_ROW
)
_FAULTED_TENANT_POSTURE = TenantPosture(
    byo_mode="byo_required", archived=False, name=None, resolution=RESOLUTION_FAULT
)
# Retained as an alias of the MISSING-ROW case, which is what every historical
# reader of this name meant by it (the fault path built the same tuple, so the
# name was ambiguous rather than wrong). New code must pick one of the two above.
_UNRESOLVED_TENANT_POSTURE = _MISSING_ROW_TENANT_POSTURE


# libpq accepts the password in the URI userinfo AND as a query parameter, so a
# redactor that only handles the first one still leaks the second. Matching on
# a SUBSTRING rather than an exact key list covers `password` and libpq's
# `sslpassword` (the client-key passphrase) with one rule, and keeps covering
# whatever the next parameter is called.
#
# An explicit {"password", "sslpassword"} set used to sit alongside this and was
# pure redundancy — both members contain "password", so deleting the set changed
# no behaviour at all. A mutation run caught it: the mutant that removed the set
# SURVIVED, which is the signature of dead code, not of a missing test.
#
# Deliberately NOT matched: `sslkey`, `sslcert`, `sslrootcert`, `passfile` —
# all filesystem PATHS, useful to an operator, and not themselves secrets.
# ("passfile" does not contain "password", so the substring rule leaves it.)
_SECRET_QUERY_SUBSTRINGS = ("password", "secret", "token")
_REDACTED = "***REDACTED***"
_UNPARSEABLE = "<unparseable database url — redacted>"

# A database host is a DNS name, an IPv4 literal, or an IPv6 literal. None of
# those can contain the punctuation a generated password is full of, so the
# charset is what separates "this really is a host" from "this is the tail of a
# password that `urlsplit` handed us in the host slot".
_HOSTNAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
# An IPv6 zone id (`[::1%eth0]`) is an interface name, not free text.
_ZONE_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,32}\Z")
# A DSN path is `/<dbname>`. PostgreSQL identifiers are letters, digits, `_`,
# `$`, and (quoted) `-`/`.`; nothing else belongs here.
_DBPATH_RE = re.compile(r"\A/[A-Za-z0-9_.$-]*\Z")
# `sslmode` is a closed enum. Anything else in that slot is not an sslmode, so
# it is not printed — `?sslmode=require;<secret>` parses as ONE pair whose value
# is `require;<secret>`, which no key-based or separator-based rule catches.
#
# EVERY closed-enum libpq parameter, not just `sslmode`. Fixing one key and
# leaving its siblings is how this class survives a fix: `?gssencmode=disable;
# <secret>` is the identical shape and was still printed when only `sslmode`
# was checked. Integer-valued keys are just as closed and just as smuggle-able.
_ENUM_QUERY_VALUES: dict[str, frozenset[str]] = {
    "sslmode": frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}),
    "gssencmode": frozenset({"disable", "prefer", "require"}),
    "channel_binding": frozenset({"disable", "prefer", "require"}),
    "target_session_attrs": frozenset(
        {"any", "read-write", "read-only", "primary", "standby", "prefer-standby"}
    ),
    "replication": frozenset({"true", "false", "on", "off", "yes", "no", "1", "0", "database"}),
    "load_balance_hosts": frozenset({"disable", "random"}),
    "sslsni": frozenset({"0", "1"}),
    "sslcompression": frozenset({"0", "1"}),
    "keepalives": frozenset({"0", "1"}),
}
_INT_QUERY_KEYS = frozenset(
    {
        "port",
        "connect_timeout",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "tcp_user_timeout",
    }
)
_INT_VALUE_RE = re.compile(r"\A[0-9]{1,10}\Z")


def _decomposition_is_faithful(parts: SplitResult) -> bool:
    """True when ``urlsplit`` split the DSN where we assume it did.

    ``urlsplit`` does not fail on a URL whose password contains ``/``, ``?``,
    ``#`` or ``@`` — it silently ends the netloc at that character and parks the
    remainder, credential included, in ``path`` / ``query`` / ``fragment``.
    Every component then looks individually plausible and the old guards passed,
    so the redactor rebuilt a string containing raw password bytes in the HOST
    or PATH slot while still printing ``***REDACTED***`` next to them.

    Rather than enumerate the bad shapes, assert the properties a real DSN has:

    * no fragment at all — a DSN has none, so a non-empty one means the input
      was cut at a ``#`` that belonged to the password. The previous code
      DROPPED the fragment, which silently truncated the evidence instead of
      refusing the input.
    * ``@`` only ever as the userinfo separator, which lives in the netloc.
      One in ``path`` or ``query`` means the netloc ended early.
    * host and path match the charsets above.
    """
    if parts.fragment:
        return False
    if "@" in parts.path or "@" in parts.query:
        return False
    # An EMPTY port (`host:` with nothing after the colon) is evidence, and
    # ``parts.port`` destroys it — it returns None for both "no colon" and
    # "colon, no digits", so the normalised accessor cannot tell them apart.
    # Check the raw slice instead: `…//licenses_user:/<password>` splits into
    # netloc `licenses_user:` plus path `/<password>`, and with no `@` anywhere
    # the password is a legal dbname sitting in the path slot. Found by fuzzing
    # this function after the fragment/`@` rules were already in place.
    hostport = parts.netloc.rpartition("@")[2]
    if hostport.endswith(":"):
        return False
    # A BRACKETED host must really be an IPv6 literal — checked on the raw
    # slice, because `parts.hostname` drops the brackets and so cannot tell a
    # bracketed host from a bare one.
    #
    # This replaces a `":" not in hostname` escape hatch, which was itself the
    # hole: `[v7.Zq7Wv3Kx9Bn5:XYZ]` is IPvFuture, which urlsplit's bracket check
    # accepts as `v<hex>.` plus ANY tail. The colon in that tail satisfied the
    # escape and skipped `_HOSTNAME_RE` entirely, so arbitrary bytes rendered as
    # the host. Validating the literal itself removes the exemption rather than
    # widening it.
    if hostport.startswith("["):
        close = hostport.find("]")
        if close < 0:
            return False
        address, _, zone = hostport[1:close].partition("%")
        try:
            ipaddress.IPv6Address(address)
        except ValueError:
            return False
        # `ipaddress` accepts ANY zone id and urlsplit deliberately does not
        # lowercase it, so an unvalidated zone is free text echoed verbatim.
        if zone and not _ZONE_ID_RE.match(zone):
            return False
    else:
        hostname = parts.hostname or ""
        if hostname and not _HOSTNAME_RE.match(hostname):
            return False
    return not (parts.path and not _DBPATH_RE.match(parts.path))


def _value_is_off_enum(key: str, value: str) -> bool:
    """True when a closed-enum parameter carries something that is not a member.

    ``?sslmode=require;<secret>`` parses as the single pair
    ``("sslmode", "require;<secret>")``. The key is in the allowlist and the
    value carries no ``password=``-style assignment, so every other rule passes
    it through. Checking the value against the enum it is REQUIRED to be a
    member of catches the whole shape without guessing at what a secret looks
    like.

    Applied to every closed-enum and integer key, because the shape is not
    specific to ``sslmode`` — an adversarial pass reproduced it verbatim on
    ``gssencmode`` while only ``sslmode`` was covered.
    """
    lowered = key.lower()
    if lowered in _ENUM_QUERY_VALUES:
        return value.strip().lower() not in _ENUM_QUERY_VALUES[lowered]
    if lowered in _INT_QUERY_KEYS:
        return not _INT_VALUE_RE.match(value.strip())
    return False


def _is_secret_query_key(key: str) -> bool:
    lowered = key.lower()
    return any(s in lowered for s in _SECRET_QUERY_SUBSTRINGS)


# Every libpq/SQLAlchemy DSN parameter this service legitimately uses. Anything
# else in KEY position is not a parameter name — it is almost certainly a
# fragment of a password that contained an unencoded `&` — so it is redacted
# rather than printed. An allowlist, because the set of real parameter names is
# small and known while the set of things a password can look like is not.
_KNOWN_QUERY_KEYS = frozenset(
    {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "sslcrl",
        "sslsni",
        "sslcompression",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "connect_timeout",
        "client_encoding",
        "options",
        "application_name",
        "fallback_application_name",
        "keepalives",
        "keepalives_idle",
        "keepalives_interval",
        "keepalives_count",
        "tcp_user_timeout",
        "replication",
        "gssencmode",
        "krbsrvname",
        "service",
        "target_session_attrs",
        "passfile",
        "channel_binding",
        "requirepeer",
        "load_balance_hosts",
    }
)


def _is_unrecognised_key(key: str) -> bool:
    """True when a query KEY is not a real DSN parameter name."""
    return key.lower() not in _KNOWN_QUERY_KEYS


def _fully_decoded(value: str) -> str:
    """Percent-decode until stable, so one extra encoding layer cannot hide a secret.

    `?callback=https%253A%252F%252Fx%253ASECRET%2540y.com` decodes ONCE to a
    still-encoded string, which the `"://" in value` test does not match.
    Bounded so a pathological input cannot loop.
    """
    seen = value
    for _ in range(4):
        nxt = unquote(seen)
        if nxt == seen:
            break
        seen = nxt
    return seen


def _value_hides_a_credential(value: str) -> bool:
    """True when a query VALUE contains an embedded credential assignment.

    Redacting by KEY alone is not enough. ``parse_qsl`` has not treated ``;``
    as a parameter separator since Python 3.10, so ``?sslmode=require;password=x``
    parses as the SINGLE pair ``("sslmode", "require;password=x")`` — a
    key-only redactor sees a harmless key and prints the password.
    Found by running the redactor against adversarial inputs, not by review.

    Any value carrying a ``<secret-ish>=`` assignment is redacted whole. A
    legitimate DSN parameter never needs one, so the false-positive cost is a
    redacted value in a log line; the false-negative cost is a leaked password.
    """
    # Decoded first: one extra percent-encoding layer walked past both rules
    # below (`https%253A%252F%252Fx%253ASECRET%2540y.com`).
    lowered = _fully_decoded(value).lower()
    # The word alone, NOT `word=` / `word:`. Requiring a separator meant
    # `?options=password%20<secret>` (a space, no separator) printed the secret
    # under a key that IS in the allowlist, so neither the key rule nor the
    # separator rule looked at it. A legitimate DSN value never contains the
    # word "password"/"secret"/"token" at all, so the false-positive cost is a
    # redacted value; the false-negative cost is a leaked credential.
    if any(s in lowered for s in _SECRET_QUERY_SUBSTRINGS):
        return True
    if any(s in lowered for s in ("pwd", "passwd")):
        return True
    # A value that is itself a URL carrying userinfo — `https://user:pass@host`
    # — hides a credential the key-based rule cannot see either.
    return "://" in lowered and "@" in lowered.split("://", 1)[1]


def redact_dsn(url: str) -> str:
    """Return a log-safe rendering of ``url`` with every credential removed.

    Two places in this service printed the DSN, and the DSN carries the
    ``licenses_user`` password. Pod logs are readable by anyone with
    ``kubectl logs`` on the namespace and are shipped onward to whatever
    collects them, so printing it raw published a production database
    credential.

    Keeps the parts an operator actually needs — driver, user, host, port,
    database — because those lines exist to answer "which database did this pod
    attach to?", and a fully-redacted string would answer nothing and get
    "fixed" back to the raw URL by the next person.

    FAILS CLOSED: every component is either recognised and rebuilt, or the
    whole input is refused. Nothing is carried through unexamined.

    That wording is deliberate and was earned twice. Both earlier versions
    claimed to fail closed and did not:

    * v1 used try/except with an "unchanged" fallback. ``urlsplit("://:pw@")``
      does not raise — it parks the whole string in ``path`` — so the fallback
      handed the input back.
    * v2 added a ``scheme and netloc`` guard but still passed ``parts.path``
      through untouched, so a password containing ``/`` echoed the input
      BYTE-FOR-BYTE: in ``…//licenses_user:1234/xK9pQm@host:6432/db`` the
      netloc truncates at the slash to ``licenses_user:1234``, which parses as
      host+port with NO username and NO password, and the rest — including the
      credential — lands in ``path``. Both guards were satisfied. Measured over
      200k random base64 passwords, **1.83%** produce this shape.

    So ``path`` is now VALIDATED, not carried: a DSN path is ``/<dbname>``, and
    anything else (an ``@``, a ``:``, or a second ``/``) means the split did not
    mean what we assumed and the input is refused.

    * v3 validated ``path`` but still trusted ``hostname``, and DROPPED the
      fragment instead of refusing it. A password containing ``#`` cuts the URL
      there, so ``…:p@aB9xQmT2vLp#x@host/db`` left ``aB9xQmT2vLp`` in the HOST
      slot and threw the real host away — printed next to ``***REDACTED***``,
      which made it read as correctly redacted. Measured over 50k random
      16-char symbol passwords, **0.502%** leaked >=6 contiguous password
      characters this way; with ``_decomposition_is_faithful`` it is **0.000%**
      (same corpus, same seed). Base64 passwords were already 0.000% and stay
      there, so the check costs nothing on the shapes that were already safe.
    """
    try:
        parts = urlsplit(url)
    except Exception:
        # Deliberately silent about the input: including it in a log message
        # would echo the very string we are refusing to print.
        return _UNPARSEABLE

    if not parts.scheme or not parts.netloc:
        return _UNPARSEABLE

    try:
        # Before trusting ANY component: check the split means what we assume.
        # Every leak in this function's history came from rebuilding a string
        # out of components that were individually plausible and collectively
        # wrong.
        #
        # INSIDE the try on purpose. It sat outside, and a `bytes` input then
        # raised TypeError straight out of this function — the docstring says
        # it fails closed, and for one input class that was literally false.
        if not _decomposition_is_faithful(parts):
            return _UNPARSEABLE

        userinfo = ""
        if parts.username:
            userinfo = parts.username
            if parts.password is not None:
                userinfo = f"{userinfo}:{_REDACTED}"
            userinfo += "@"
        elif parts.password is not None:
            userinfo = f":{_REDACTED}@"

        # ``hostname`` strips the brackets from an IPv6 literal, so they have
        # to go back: without them "::1" + ":6432" renders as "::1:6432",
        # which is ambiguous in the one line whose job is to name the host.
        hostinfo = parts.hostname or ""
        if ":" in hostinfo:
            hostinfo = f"[{hostinfo}]"
        if parts.port is not None:
            hostinfo = f"{hostinfo}:{parts.port}"

        # A DSN path is `/<dbname>`. Anything else means urlsplit did not
        # decompose this the way we assumed — most importantly, a password
        # containing `/` truncates the netloc and dumps the credential here.
        path = parts.path
        if path and (path.count("/") > 1 or "@" in path or ":" in path):
            return _UNPARSEABLE

        query = ""
        if parts.query:
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            # The KEY is checked as well as the value. An unencoded `&` inside
            # a password splits it, and every fragment after the first lands in
            # KEY position — where a value-only rule never looks and urlencode
            # re-emits it verbatim. `?password=a&b&SECRET&sslmode=require`
            # printed `SECRET` as a key.
            query = urlencode(
                [
                    (
                        _REDACTED if _is_unrecognised_key(key) else key,
                        _REDACTED
                        if _is_secret_query_key(key)
                        or _value_hides_a_credential(value)
                        or _is_unrecognised_key(key)
                        or _value_is_off_enum(key, value)
                        else value,
                    )
                    for key, value in pairs
                ]
            )

        # The fragment was checked above, not dropped: a DSN has none, so a
        # non-empty one means the input was cut at a `#` inside the password.
        return urlunsplit((parts.scheme, f"{userinfo}{hostinfo}", parts.path, query, ""))
    except Exception:
        return _UNPARSEABLE


class ClientSafeError(ValueError):
    """A DAL error whose MESSAGE the API layer may return to the caller verbatim.

    The API layer must never interpolate a caught exception into an
    ``HTTPException(detail=...)`` — an arbitrary ``ValueError`` reaching a
    ``except ValueError`` handler may just as easily have come from psycopg,
    pydantic or SQLAlchemy, and those carry the failing statement plus its bound
    parameters (DSN, host, credentials). The unsafe thing is therefore the CATCH,
    not the message.

    This type marks the narrow set of errors this module *authored itself* from
    caller-supplied input (an invalid ``byo_mode``, a tenant-name conflict). Their
    text is written here, contains only what the caller already sent, and is the
    caller's only actionable signal. Handlers that want to echo a message MUST
    catch ``ClientSafeError`` specifically; ``tests/unit/test_no_exception_leak.py``
    enforces that no other caught type may be interpolated into ``detail=``.

    Subclasses ``ValueError`` so existing ``except ValueError`` callers (the
    ``provision_*`` scripts, tests) keep working unchanged.
    """


class TenantNameTakenError(ClientSafeError):
    """A rename targeted a name another tenant already holds (``ix_tenants_name``).

    Its own type so the route can answer 409 (a conflict with server state)
    while every other ``rename_tenant`` refusal -- an invalid name -- stays a 422.
    Raised from BOTH the pre-check and the ``IntegrityError`` of the unique
    index: the pre-check alone is racy (two renames to one free name both pass
    it and the loser's commit fails), and that loser must read as the same 409,
    never a 500. Issue #258.
    """


class TenantExternalRefTakenError(ClientSafeError):
    """Un-archiving a tenant whose ``external_ref`` a LIVE tenant of the same owner
    now holds (``uq_tenants_owner_external_ref``, live-only since migration 053).

    The route answers 409 ``tenant_external_ref_taken``. Typically the customer was
    re-provisioned while this tenant was archived, so the new live tenant is the
    one the owner's retries resolve to; restoring the old one would split it.
    """


class AllTokensRevokedError(ClientSafeError):
    """This license has tokens, but every one of them is revoked.

    Sibling of ``AllTokensExpiredError`` for the OTHER way a license can end up
    with no live credential. Both refuse rather than rotate, for the same reason:
    with nothing live to inherit from, ``regenerate_token_for_license`` falls into
    the "nothing replaced" arm and mints a PERPETUAL token.

    That arm is correct for a license that has never had a token — there is no
    policy to preserve, and the fresh-mint fallback is a documented, real path. It
    is catastrophic once a token has existed and been revoked, because the
    replacement is UNBOUNDED where its predecessor was not. The sharpest case is
    the ordinary incident response: revoke a leaked 30-day token, press
    regenerate, receive a credential that never expires.

    ``AllTokensExpiredError`` cannot be reused here. Its ``newest_expiry`` is
    typed non-optional on purpose (a perpetual token can never reach it), and a
    revoked token may well be perpetual — so the two states need two types.

    The way forward is the same: mint explicitly via
    ``POST /licenses/{license_id}/tokens``, where the expiry is an argument.
    """

    def __init__(self, license_id: object, revoked_count: int) -> None:
        self.license_id = str(license_id)
        self.revoked_count = revoked_count
        super().__init__(
            "every token on this license is revoked; rotation would mint an "
            "unbounded replacement. Mint one explicitly instead."
        )


class AllTokensExpiredError(ClientSafeError):
    """Every unrevoked token on this license has already expired.

    Raised by ``regenerate_token_for_license`` INSTEAD of rotating. There is no
    live credential to carry an expiry forward from, and the two alternatives are
    both worse than refusing:

    * inherit the expired expiry -> the replacement is dead on arrival and
      ``/license/introspect`` rejects it as ``token_invalid`` on first use, while
      the tokens it destroyed are unrecoverable (only the SHA-256 hash is kept);
    * fall into the "nothing replaced" arm -> a PERPETUAL replacement minted from
      an expired one, which is the credential-laundering defect closed 2026-08-08.

    Refusing destroys nothing. The way forward is the mint route,
    ``POST /licenses/{license_id}/tokens``, where an expiry is an EXPLICIT
    argument — which is exactly the "rotate AND extend needs its own clamped API"
    boundary ``regenerate_token_for_license`` already documents.

    ``newest_expiry`` is operator diagnostics for the log line; it is deliberately
    NOT put in the client's error body (``tests/unit/test_no_exception_leak.py``
    keeps ``detail=`` free of caught-exception state, and the caller's action does
    not depend on the timestamp).
    """

    def __init__(self, license_id: object, newest_expiry: datetime) -> None:
        # NOT ``datetime | None``: the only raise site is guarded on a non-None
        # expiry, and a perpetual token can never reach here (``None`` means
        # perpetual, which is precisely the case that must NOT refuse). Typing it
        # optional would document a state that cannot exist and let a future
        # caller raise this for a license that is perfectly fine.
        self.license_id = str(license_id)
        self.newest_expiry = newest_expiry
        super().__init__(
            f"license {self.license_id} cannot be rotated: every unrevoked token has "
            f"expired (most recent expiry {newest_expiry})"
        )


# ============= New-token expiry policy (licenses#244) =============
#
# THE FINDING. Measured 2026-10-02: 199 active tokens with `expires_at IS NULL`
# (127 api, 72 internal) and NO mint path that ever set an expiry —
# `create_token(expires_at=None)` and `POST /licenses/{id}/tokens` without
# `expires_days` both meant "perpetual", silently.
#
# THE RULING (option 2 on licenses#244). NEW mints only:
#   * an `api` token minted without an expiry gets TOKEN_API_DEFAULT_EXPIRY_DAYS;
#   * a token may be perpetual ONLY as a REGISTERED, OWNED long-lived token
#     (`long_lived_owner` + `long_lived_reason`, migration 044), and only for
#     `internal`/`node` tokens or the n8n licence-token path (`api` on a tenant
#     in LONG_LIVED_API_TENANTS — n8n_custom_nodes/CLAUDE.md: those tokens are
#     long-living and are never modified or regenerated);
#   * `internal`/`node` with no expiry and no registration is REFUSED, not
#     defaulted: whether service tokens become bounded + rotated is undecided
#     (2026-12-01), so neither silently-perpetual nor a guessed lifetime is right.
#   * any other type with no expiry is REFUSED (fail closed).
#
# ⛔ EXISTING tokens are untouched: no backfill, no revocation. A legacy
# perpetual row keeps introspecting as active; only how NEW rows are born
# changes. Rotation (`regenerate_token_for_license`) still INHERITS its
# predecessor's expiry — that is the lifecycle of an existing credential.


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


#: Lifetime given to a NEW `api` token minted without an explicit expiry.
TOKEN_API_DEFAULT_EXPIRY_DAYS = _positive_int_env("TOKEN_API_DEFAULT_EXPIRY_DAYS", 365)

#: Token types that may be registered long-lived on any licence.
LONG_LIVED_TOKEN_TYPES = frozenset({"internal", "node"})

#: Tenants (name or id) whose `api` tokens may be registered long-lived: the n8n
#: licence-token path. Matched against the LICENCE's tenant row, never a header.
LONG_LIVED_API_TENANTS = frozenset(
    t.strip() for t in os.getenv("LONG_LIVED_API_TENANTS", "n8n-dev").split(",") if t.strip()
)


class LongLivedImplicitClient(NamedTuple):
    """One registered caller: the VERIFIED (iss, azp) pair plus the registration
    its expiry-less ``api`` mints receive."""

    iss: str
    azp: str
    owner: str
    reason: str


#: The secure-apps realm's issuer, EXACTLY as Keycloak emits it in ``iss`` (no
#: trailing slash; KC_HOSTNAME=https://auth.nonagentic.ai, so a token minted via the
#: in-cluster token URL carries this public issuer too). Measured 2026-10-03: the
#: public and the in-cluster ``/realms/secure-apps/.well-known/openid-configuration``
#: both advertise this string, and it is what trusted_issuers() derives from
#: licenses' KEYCLOAK_URL (https://auth.nonagentic.ai).
SECURE_APPS_ISSUER = "https://auth.nonagentic.ai/realms/secure-apps"

# ⛔ CALLERS THAT CANNOT PASS AN EXPIRY (licenses#244 validator, F2). commonpractice
# (m4-owned; not editable from here) mints a tenant / member-seat ``api`` token with
# NO expiry (backend/src/services/licenses-admin.ts:461, called from
# license-provisioning.ts:186 and :491), stores it, and introspects it on every
# request (middleware/license-gate.ts Gate 2). A silent 365-day default there is a
# DELAYED OUTAGE: a year after each onboarding every request 402s while the licence
# is still active. So that one caller keeps perpetual tokens, but as a REGISTERED,
# OWNED long-lived class, never as the unregistered perpetual default.
#
# Keyed on the VERIFIED JWT ``(iss, azp)`` PAIR, NEVER on ``azp`` alone and never on
# a request field or header. ``azp`` is just a Keycloak client_id: any realm admin can
# create a client named ``commonpractice-licenses-admin`` in ANOTHER trusted realm
# (e.g. ``portal``), and its tokens verify here too (trusted_issuers() accepts both
# realms). Only the issuer pins WHICH realm's client minted the token, so the entry is
# pinned to SECURE_APPS_ISSUER, where commonpractice's SA lives (LICENSE_SA_TOKEN_URL
# .../realms/secure-apps/..., LICENSE_SA_CLIENT_ID in gitops
# clusters/strato/services/commonpractice/deployment.yaml). Both must match exactly.
#
# Applies ONLY to an ``api`` mint with no ``expires_days`` and no explicit
# ``long_lived``; an explicit expiry from the same client is honoured. Remove an entry
# once its caller passes an expiry (and has a renewal path).
#
# Env override ``LONG_LIVED_IMPLICIT_CLIENTS``: a JSON ARRAY of entries of the same
# shape, ``[{"iss": "...", "azp": "...", "owner": "...", "reason": "..."}]``;
# ``[]`` disables the class. Every field is required and non-empty.
_DEFAULT_LONG_LIVED_IMPLICIT_CLIENTS: tuple[LongLivedImplicitClient, ...] = (
    LongLivedImplicitClient(
        iss=SECURE_APPS_ISSUER,
        azp="commonpractice-licenses-admin",
        owner="m4 / commonpractice",
        reason="caller cannot pass expiry; migrate before removal",
    ),
)

_IMPLICIT_CLIENT_FIELDS = ("iss", "azp", "owner", "reason")


def _index_implicit_clients(
    entries: Iterable[LongLivedImplicitClient],
) -> dict[tuple[str, str], LongLivedImplicitClient]:
    return {(c.iss, c.azp): c for c in entries}


def _long_lived_implicit_clients_from_env() -> dict[tuple[str, str], LongLivedImplicitClient]:
    raw = os.getenv("LONG_LIVED_IMPLICIT_CLIENTS")
    if raw is None or raw.strip() == "":
        return _index_implicit_clients(_DEFAULT_LONG_LIVED_IMPLICIT_CLIENTS)
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(
            "LONG_LIVED_IMPLICIT_CLIENTS must be a JSON array of "
            '{"iss", "azp", "owner", "reason"} objects'
        )
    clients: list[LongLivedImplicitClient] = []
    for i, entry in enumerate(parsed):
        fields = entry if isinstance(entry, dict) else {}
        values = {k: str(fields.get(k) or "").strip() for k in _IMPLICIT_CLIENT_FIELDS}
        if not all(values.values()):
            raise ValueError(
                f"LONG_LIVED_IMPLICIT_CLIENTS[{i}] needs a non-empty iss, azp, owner and reason"
            )
        clients.append(LongLivedImplicitClient(**values))
    return _index_implicit_clients(clients)


#: Verified ``(iss, azp)`` -> the long-lived registration its expiry-less ``api``
#: mints get.
LONG_LIVED_IMPLICIT_CLIENTS: dict[tuple[str, str], LongLivedImplicitClient] = (
    _long_lived_implicit_clients_from_env()
)


class TokenExpiryPolicyError(ValueError):
    """A NEW token mint that the expiry policy refuses. Nothing was written.

    ``code`` is the machine-readable discriminator the mint route returns.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class LongLivedRegistration(NamedTuple):
    """Who owns a perpetual token, and why it may be perpetual.

    ``implicit_client`` is set ONLY by the mint route, to the caller's VERIFIED
    ``(iss, azp)`` after it matched LONG_LIVED_IMPLICIT_CLIENTS; it admits a
    long-lived ``api`` token off the n8n tenants for that registered caller alone.
    """

    owner: str
    reason: str
    implicit_client: tuple[str, str] | None = None


def implicit_long_lived_registration(
    iss: str | None,
    azp: str | None,
    token_type: str,
    has_expiry: bool,
    explicit_long_lived: bool,
) -> LongLivedRegistration | None:
    """The registration an allowlisted caller's expiry-less ``api`` mint gets, else None.

    ``iss`` and ``azp`` MUST be the verified JWT claims, never request values; BOTH
    must equal a registered entry exactly (no normalisation).
    """
    if not iss or not azp or token_type != "api" or has_expiry or explicit_long_lived:
        return None
    client = LONG_LIVED_IMPLICIT_CLIENTS.get((iss, azp))
    if client is None:
        return None
    return LongLivedRegistration(
        client.owner, client.reason, implicit_client=(client.iss, client.azp)
    )


#: Why each refusal happened, keyed by the discriminator the mint route returns.
TOKEN_EXPIRY_REFUSALS: dict[str, str] = {
    "long_lived_requires_owner_and_reason": (
        "a long-lived token needs a non-empty owner AND reason"
    ),
    "long_lived_with_expiry": (
        "a long-lived token is perpetual by definition; drop the expiry or drop long_lived"
    ),
    "long_lived_not_permitted": (
        "this token type may not be long-lived on this licence: allowed for internal/node, "
        "and for api only on the n8n licence-token tenants (LONG_LIVED_API_TENANTS)"
    ),
    "perpetual_token_not_registered": (
        "an internal/node token needs an explicit expiry, or long_lived with an owner and "
        "a reason; an unregistered perpetual token is refused (licenses#244)"
    ),
}


def token_expiry_refusal(
    token_type: str,
    has_expiry: bool,
    long_lived: LongLivedRegistration | None,
    tenant_keys: frozenset[str] = frozenset(),
) -> str | None:
    """The policy's refusal code for a NEW mint, or ``None`` when it may proceed.

    ONE decision, used by ``resolve_new_token_expiry`` (the DB boundary) and by
    ``POST /licenses/{id}/tokens`` (which must answer each refusal with a literal
    discriminator). ``tenant_keys`` is the licence's tenant (id and name); it only
    matters for a long-lived ``api`` token.
    """
    if long_lived is not None:
        if not long_lived.owner.strip() or not long_lived.reason.strip():
            return "long_lived_requires_owner_and_reason"
        if has_expiry:
            return "long_lived_with_expiry"
        if token_type in LONG_LIVED_TOKEN_TYPES:
            return None
        if token_type == "api" and tenant_keys & LONG_LIVED_API_TENANTS:
            return None
        if token_type == "api" and long_lived.implicit_client in LONG_LIVED_IMPLICIT_CLIENTS:
            return None
        return "long_lived_not_permitted"
    if has_expiry or token_type == "api":
        return None
    # internal/node, AND any unknown type: refused, never defaulted (fail closed).
    return "perpetual_token_not_registered"


def resolve_new_token_expiry(
    token_type: str,
    expires_at: datetime | None,
    long_lived: LongLivedRegistration | None,
    *,
    tenant_keys: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> datetime | None:
    """The expiry a NEW token is stored with, or ``TokenExpiryPolicyError``.

    Returns ``None`` (perpetual) only for a registered long-lived mint; an ``api``
    mint with no expiry gets TOKEN_API_DEFAULT_EXPIRY_DAYS.
    """
    refusal = token_expiry_refusal(token_type, expires_at is not None, long_lived, tenant_keys)
    if refusal is not None:
        raise TokenExpiryPolicyError(
            refusal, f"{TOKEN_EXPIRY_REFUSALS[refusal]} (token_type={token_type!r})"
        )
    if long_lived is not None or expires_at is not None:
        return expires_at
    return (now or datetime.now(UTC)) + timedelta(days=TOKEN_API_DEFAULT_EXPIRY_DAYS)


# introspect_token runs on the platform-wide auth hot path (every protected
# request across every service polls through it). Updating last_used_at on every
# call is a row-lock + WAL + commit per introspect; throttle it so the common
# path is read-only. Coarse last-used tracking (~1 min granularity) is fine for
# its only consumers (audit / staleness), and removing the write is what keeps
# the endpoint fast enough to stay under caller introspect timeouts under load.
_LAST_USED_THROTTLE = timedelta(seconds=int(os.getenv("TOKEN_LAST_USED_THROTTLE_SECONDS", "60")))

# libpq connect_timeout for every session, in seconds. Module scope (not
# __init__) so the tests can read the same value the engine is built from —
# the suite constructs DatabaseManager via __new__ and hand-assigns the engine,
# so a literal buried in __init__ would be unassertable. Overridable for the
# rare operator who needs to widen it; see the rationale at the create_engine
# call for why the default is 2 and why it is load-bearing under NullPool.
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "2"))

# The transient-fault set for introspect_token's ONE bounded retry. Module
# scope, not __init__ — the test suite builds DatabaseManager via __new__ and
# never runs __init__, so anything configured there is invisible to every test.
#
# OperationalError is what the observed fault produces at BOTH layers: psycopg
# raises psycopg.OperationalError for 'connection failed: ... server closed the
# connection unexpectedly', and SQLAlchemy wraps it inside Connection.__init__,
# i.e. BEFORE any statement is sent. It also covers every SQLSTATE class that
# means "the connection or the server", not "the query": 08* connection
# exceptions, 53* insufficient_resources (pgbouncer rejecting on
# max_client_conn), and 57* operator intervention (AdminShutdown,
# CannotConnectNow).
#
# InterfaceError is here because it is NOT a subclass of OperationalError —
# sqlalchemy.exc.InterfaceError inherits DBAPIError DIRECTLY, skipping
# DatabaseError — yet it is the same physical fault arriving one layer up, when
# the connection object is already closed or broken. Catching only
# OperationalError silently misses it.
#
# NOT keyed on DBAPIError.connection_invalidated: at CONNECT time SQLAlchemy
# evaluates is_disconnect(e, None, None), and the psycopg dialect returns False
# whenever `connection is None`, so the wrapped error from the very fault this
# seam exists for carries connection_invalidated=False. That attribute would
# reject the incident it looks like it was made for.
#
# DELIBERATELY EXCLUDED, and still propagating as a 500: ProgrammingError (an
# unapplied migration), InternalError (25P02 InFailedSqlTransaction — an
# app-side session-reuse bug), IntegrityError, QueryCanceled, and every
# non-DBAPI Exception. This is NARROWER than _lookup_byo_mode's bare `except
# Exception`, on purpose: that arm FABRICATES a security posture, so a wrong
# answer there is a silent denial cached for minutes and it is worth trying
# hard. This arm only chooses between "500" and "500 one connect later", so
# re-running a deterministic fault doubles latency on the platform auth hot
# path and can recover nothing. Do not "harmonize" the two.
_INTROSPECT_RETRYABLE = (OperationalError, InterfaceError)

# QueryCanceled (SQLSTATE 57014, a statement timeout) is a SUBCLASS of
# psycopg.OperationalError, so it arrives wrapped in a sqlalchemy
# OperationalError and _INTROSPECT_RETRYABLE would otherwise swallow it. A
# timeout is not a connection fault: the connection is fine and the server is
# simply slow, so a retry can only spend the budget twice and push the caller
# past its own deadline. Excluded explicitly rather than by omission, because
# the subclassing makes the omission impossible to write.
_INTROSPECT_NOT_RETRYABLE = (psycopg.errors.QueryCanceled,)


def _is_retryable_db_fault(exc: BaseException) -> bool:
    """True when ``exc`` is a connect/connection-scoped fault worth ONE re-dial.

    Split out so the predicate is testable on its own and so the two
    exclusions above cannot drift apart from the ``except`` clause that uses
    them.
    """
    if not isinstance(exc, _INTROSPECT_RETRYABLE):
        return False
    # ``orig`` is the underlying DBAPI exception SQLAlchemy wrapped. It is None
    # when the fault never reached the driver, which cannot be a statement
    # timeout — so a missing ``orig`` stays retryable.
    return not isinstance(getattr(exc, "orig", None), _INTROSPECT_NOT_RETRYABLE)


# The raw-psycopg twin of ``_INTROSPECT_RETRYABLE``. ``scripts/apply_migrations``
# does NOT go through SQLAlchemy — ``apply_all`` opens its own
# ``psycopg.connect(dsn, autocommit=True)`` — so a connect failure there arrives
# as a bare ``psycopg.OperationalError``, which is not a subclass of anything in
# ``_INTROSPECT_RETRYABLE`` and would be classified as "not a connect fault" by
# ``_is_retryable_db_fault`` alone. ``psycopg.InterfaceError`` is listed for the
# same reason its SQLAlchemy counterpart is: DBAPI makes it a SIBLING of
# ``DatabaseError``, not a subclass, so catching OperationalError misses it.
_RAW_PSYCOPG_CONNECT_FAULTS = (psycopg.OperationalError, psycopg.InterfaceError)


def is_db_unreachable(exc: BaseException) -> bool:
    """True when ``exc`` means "the database was not reachable", at EITHER layer.

    The boot-time counterpart to ``_is_retryable_db_fault``: ``lifespan`` has to
    tell a **transient** fault (the database is down — recoverable, so serve
    unready and keep retrying) apart from a **permanent** one (a migration is
    broken — refuse to serve, because starting would corrupt or 500). Getting
    that distinction wrong in the safe-looking direction is what turns a
    30-second pgbouncer blip into a CrashLoopBackOff of both replicas of the
    service that backs ext_authz for the whole platform.

    Public, unlike its sibling, because ``main.lifespan`` is a legitimate second
    consumer and importing a leading-underscore name across modules hides that.

    ``QueryCanceled`` (a statement timeout) is excluded at BOTH layers via
    ``_is_retryable_db_fault``'s ``orig`` check and the explicit test below: it
    subclasses ``psycopg.OperationalError``, so omission alone cannot exclude it.
    A timeout means the server answered and was slow, which is not "unreachable".
    """
    if isinstance(exc, _INTROSPECT_NOT_RETRYABLE):
        return False
    if isinstance(exc, _RAW_PSYCOPG_CONNECT_FAULTS):
        return True
    return _is_retryable_db_fault(exc)


class Base(DeclarativeBase):
    pass


#: Built once — the same validator the HTTP boundary uses for LicenseCreate.
_AWARE_DATETIME = TypeAdapter(AwareDatetime)


class DatetimeArgError(ValueError):
    """An operator-supplied datetime this service refuses to guess at."""


def parse_aware_datetime_arg(raw: str, *, arg: str) -> datetime:
    """Return ``raw`` as a UTC-normalised aware datetime, or raise.

    For NON-HTTP entrypoints — the ``provision_*`` scripts — which never cross the
    API's ``AwareDatetime`` boundary. They used ``datetime.fromisoformat()``, which
    accepts ``"2027-01-01"`` and ``"2027-01-01T00:00:00"`` and returns a **naive**
    datetime; both are valid ISO 8601 and the old ``--expires-at`` help text invited
    exactly that.

    A naive value written to ``license.expires_at`` (``timestamptz``) is resolved
    against the database session's ``TimeZone``, so the same command stores a
    different instant depending on a server setting.
    ``License.effective_status`` compares ``expires_at`` against
    ``datetime.now(UTC)``, so the licence reads live or expired off by the UTC
    offset — the verdict ``provisioning`` and fr's licence gate consume. The failure
    is not a crash, it is a silently wrong expiry on a real customer tenant, so an
    offset-less value is REFUSED rather than guessed at.

    Uses ``pydantic.AwareDatetime`` — the same validator the HTTP boundary uses —
    so the CLI and the API agree on what a datetime is by construction rather than
    via two implementations that must be kept in step.

    Raises:
        DatetimeArgError: if ``raw`` is not ISO 8601, or carries no UTC offset.
    """
    try:
        parsed = _AWARE_DATETIME.validate_python(raw)
    except ValidationError as exc:
        raise DatetimeArgError(
            f"{arg} {raw!r} must be ISO-8601 WITH a UTC offset "
            f"(e.g. 2027-01-01T00:00:00+00:00 or 2027-01-01T00:00:00Z). "
            f"An offset-less value is ambiguous: it would be resolved against the "
            f"database session's TimeZone, so the licence would expire at an instant "
            f"that depends on a server setting."
        ) from exc
    return parsed.astimezone(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a datetime to UTC before it reaches a ``timestamptz`` column.

    A NAIVE value is INTERPRETED as UTC; an aware value is CONVERTED, preserving
    the instant it already denotes. ``None`` passes through, so a perpetual
    ``expires_at`` stays perpetual.

    Why this exists: PostgreSQL resolves a naive bind against the *session*
    ``TimeZone`` GUC, so the instant stored in ``license.starts_at`` /
    ``.expires_at`` depended on which connection served the write. #152 closed
    that at the HTTP boundary by typing the fields ``AwareDatetime``. This closes
    it for the DAL's NON-HTTP callers, which never pass through that boundary:
    ``scripts/provision_{kag,fr,w2v}_tenant.py`` build ``expires_at`` from a CLI
    argument and call ``create_license`` directly.

    Interpreting naive as UTC is a choice, and the only safe one here: every
    in-repo producer of these values is UTC (``datetime.now(UTC)``, ``utc_now``),
    and the alternative — trusting the session GUC — is the defect itself.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now():
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(UTC)


class Region(Base):
    """Region lookup for region-scoped plans/licenses"""

    __tablename__ = "region"

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    code = Column(String(50), nullable=False, unique=True)  # 'eu', 'us', 'apac'
    name = Column(String(255), nullable=False)  # 'Europe', 'United States'
    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "code": self.code,
            "name": self.name,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


#: UUID-shaped, any case — the SQL guard below that makes the ``::uuid`` cast safe. In
#: Postgres ``~`` a ``$`` matches ONLY at the end of the string, so ``^…$`` is exact there.
#: ⛔ NEVER reuse it with Python ``re.match``: Python's ``$`` also matches BEFORE a
#: trailing ``\n``, so ``'<uuid>\n'`` passes. Python checks use the ``\A…\Z`` regex below
#: with ``fullmatch``.
_KEYCLOAK_SUB_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: The only shape a Keycloak user ``sub`` takes: a LOWER-CASE hyphenated UUID (Keycloak
#: never issues upper case; measured 2026-09-27: 60 of 60 UUID-shaped prod subs are lower
#: case). ``\A…\Z`` + ``fullmatch``: no trailing newline, no other case, no padding.
_KEYCLOAK_SUB_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_ANY_CASE_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# A lightweight second reference to the subject table for the collision EXISTS below. A
# plain ``table()`` (not the mapped Table) so the subquery never auto-correlates it away.
_SUBJECT_ROW = sql_table("subject", sql_column("id", PGUUID(as_uuid=True))).alias("subject_row")


class Subject(Base):
    """Opaque user/account reference - no PII"""

    __tablename__ = "subject"
    __table_args__ = (
        Index("idx_subject_realm", "keycloak_realm"),
        # Mirrors migration 029. Declared HERE as well as in SQL on purpose: the
        # runner baselines every current migration on a database that already has
        # the tables, so on a fresh bootstrap 029 never executes and create_all is
        # the only thing that builds this index. A SQL-only index would exist on
        # no fresh database — the drift class test_schema_parity.py exists for.
        Index("idx_subject_tenant", "tenant_id"),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    keycloak_sub = Column(String(255), nullable=False, unique=True)  # from Keycloak "sub" claim
    keycloak_realm = Column(String(255), nullable=False)  # realm name or ID
    # The tenant this subject BELONGS TO — the org half of the identity (migration
    # 029). Before this column the registry could say "(tenant, app, subject) holds
    # a licence" but could not say "this subject is a MEMBER of this tenant", so
    # membership had to be inferred from the licence rows — which inverts the
    # relationship, making membership a consequence of entitlement instead of its
    # precondition.
    #
    # NULLABLE, AND NULL IS THE HONEST DEFAULT. It means "nobody has stated this
    # subject's tenant yet", never "no tenant". 029 performs no backfill: for most
    # of the 254 existing subjects the correct value is a business fact this
    # database does not hold, and deriving it from the licence join is not even
    # well defined — a subject may hold licences under more than one tenant, so
    # that join returns a SET, not a parent.
    #
    # ⛔ THE 43 `svc-*` ROWS STILL SIT IN `tenants`. This column makes the corrected
    # shape (tenant = the org and never a machine; a service actor is a SUBJECT of
    # the tenant it acts for) SAYABLE; it does not apply it. Moving those rows is
    # ordering-sensitive — `billing_service/src/tenancy.py::LEGACY_TENANT_NAMES` is
    # an INGRESS resolver keyed on those exact names and a miss is a 400 — and the
    # plan is `docs/svc-tenant-migration-plan.md`. Read it before writing to this
    # column in bulk.
    #
    # FK shape mirrors License.tenant_id / TenantAppByoMode.tenant_id: ON DELETE
    # RESTRICT (a tenant with members cannot be deleted out from under them), and
    # the constraint is NAMED — unnamed, create_all emits PostgreSQL's default
    # `subject_tenant_id_fkey` while 029 emits `fk_subject_tenant`, so the two
    # schemas would differ by constraint name and 029's own idempotency guard
    # would read FALSE on a create_all'd database.
    #
    # NO ``index=True`` here, deliberately — ``__table_args__`` above declares
    # Index("idx_subject_tenant"). ``index=True`` would emit a SECOND identical
    # btree named ``ix_subject_tenant_id``, which is exactly how
    # ``license.tenant_id`` ended up with two copies of one index on a fresh
    # database and one on prod.
    tenant_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_subject_tenant"),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    # The Keycloak ``sub`` that ACTS when this subject's licence token is presented
    # (migration 047). ``keycloak_sub`` above is the registry LOOKUP key and, for most
    # service subjects, a label (``svc-kag-merqi``) that no Keycloak ever issued — so the
    # licence path had no actor. Owner rule: a service's actor is its OWN Keycloak
    # service-account sub. NULL = nobody has stated it; never a sentinel, never derived.
    # Read through ``Subject.actor_identity`` (keycloak_sub wins when it IS a sub).
    actor_sub = Column(PGUUID(as_uuid=True), nullable=True)

    # ⛔ ``subject.keycloak_sub`` IS NOT GUARANTEED TO BE A KEYCLOAK SUB. Before
    # ``create_or_get_subject`` resolved row ids, ``POST /licenses/user/{id}`` given another
    # subject's ROW id minted a NEW subject whose keycloak_sub WAS that row id (2 of 282 on
    # prod, 2026-09-27 — one of them carries fr-test's own licence subject id). Emitting such
    # a value as an identity would let that licence's token claim everything recorded under
    # the other subject. This flag says "this keycloak_sub names a subject row" — computed in
    # SQL so it rides the introspect SELECT (deferred everywhere else; introspect undefers it).
    # CASE, not AND: CASE is the one construct Postgres evaluates in order, so the ::uuid cast
    # never sees a non-UUID sub (most live subs are not UUIDs).
    keycloak_sub_names_a_subject_row = column_property(
        case(
            (
                keycloak_sub.op("~")(_KEYCLOAK_SUB_UUID_PATTERN),
                exists().where(_SUBJECT_ROW.c.id == sql_cast(keycloak_sub, PGUUID(as_uuid=True))),
            ),
            else_=false(),
        ),
        deferred=True,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "keycloak_sub": self.keycloak_sub,
            "keycloak_realm": self.keycloak_realm,
            # str, not UUID — every other id this file emits is a JSON string, and
            # None stays None so an unstated parent is distinguishable from a
            # stated one. See the column comment: NULL means undecided.
            "tenant_id": str(self.tenant_id) if self.tenant_id else None,
            "actor_sub": str(self.actor_sub) if self.actor_sub else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _parse_uuid_or_none(value: str) -> UUID | None:
    # No ``.strip()``: a padded id must not resolve to the unpadded row. Callers refuse
    # padded identifiers first (``subject_identifier_problem``); this does not launder them.
    try:
        return UUID(str(value))
    except ValueError, AttributeError, TypeError:
        return None


def _bundle_holders(conn: Any, bundle_id: UUID) -> list[tuple[str, str | None, int]]:
    rows = conn.execute(
        text(
            "SELECT tb.tenant_id, t.name, "
            "  (SELECT count(*) FROM license l WHERE l.tenant_id = tb.tenant_id "
            "     AND l.bundle_id = tb.bundle_id AND l.status = 'active' "
            "     AND l.deleted_at IS NULL) AS n "
            "FROM tenant_bundle tb LEFT JOIN tenants t ON t.id = tb.tenant_id "
            "WHERE tb.bundle_id = :b ORDER BY t.name, tb.tenant_id"
        ),
        {"b": bundle_id},
    ).all()
    return [(str(r[0]), None if r[1] is None else str(r[1]), int(r[2])) for r in rows]


class MalformedSubjectIdentifierError(ClientSafeError):
    """A caller-supplied subject identifier that may never be stored as a ``keycloak_sub``.

    Its own type so the route can answer 400 (the input is malformed) rather than the 409
    ``ClientSafeError`` means there (a conflict with server state).
    """


#: ``subject.keycloak_sub`` is ``VARCHAR(255)``. Checked in Python so an over-long
#: identifier is a clear refusal, never a database ``StringDataRightTruncation``.
SUBJECT_SUB_MAX_LEN = 255

#: Tenant-level (non-member) licences are held by ``svc:<tenant-uuid>:<app-key>``
#: (``scripts/provision_tenant_bundle.py``), in realm ``service``.
SERVICE_SUBJECT_PREFIX = "svc:"

#: The longest app key whose service subject still fits ``SUBJECT_SUB_MAX_LEN``:
#: 255 - len("svc:") - 36 (canonical tenant uuid) - len(":") = 214. ``app.key`` itself
#: is VARCHAR(255), so a legal app key CAN overflow the subject.
SERVICE_SUBJECT_APP_KEY_MAX_LEN = SUBJECT_SUB_MAX_LEN - len(SERVICE_SUBJECT_PREFIX) - 36 - 1


def service_subject_app_key_problem(app_key: str) -> str | None:
    """Why ``app_key`` cannot name a tenant-level service subject — or None if it can."""
    if len(app_key) > SERVICE_SUBJECT_APP_KEY_MAX_LEN:
        return (
            f"app key is {len(app_key)} characters; a tenant-level licence subject "
            f"svc:<tenant-uuid>:<app> allows at most {SERVICE_SUBJECT_APP_KEY_MAX_LEN} "
            f"(subject.keycloak_sub is VARCHAR({SUBJECT_SUB_MAX_LEN}))"
        )
    return None


def service_subject_sub(tenant_id: str, app_key: str) -> str:
    """The deterministic tenant-level subject ``svc:<tenant-uuid>:<app-key>``.

    Raises ``MalformedSubjectIdentifierError`` when the result would not fit
    ``subject.keycloak_sub`` — a clear refusal instead of a database truncation error.
    """
    tenant = str(UUID(str(tenant_id)))
    problem = service_subject_app_key_problem(app_key)
    if problem is not None:
        raise MalformedSubjectIdentifierError(f"cannot name a service subject: {problem}")
    return f"{SERVICE_SUBJECT_PREFIX}{tenant}:{app_key}"


def is_tenant_level_subject(keycloak_sub: str | None, keycloak_realm: str | None) -> bool:
    """True for a subject that holds TENANT-level licences, never a member's.

    Ruling 2026-09-28: bundles and licences are tenant-level. A tenant-level holder is
    a ``svc:`` service subject, or any subject in a non-human realm
    (``NON_HUMAN_SUBJECT_REALMS`` — the same "is this a person" split
    ``count_tenant_seats`` bills by). Everything else is a member (a person).

    ⛔ EXCEPT the externally-minted non-human realms
    (``STATEABLE_NON_HUMAN_SUBJECT_REALMS``, e.g. ``cp-tenant``): they are not people
    (no seat), but their licences belong to the service that minted them, not to a
    bundle. Reading them as tenant-level would let ``bundle_ensure`` adopt one into a
    bundle, or see several candidates and refuse the app. Bundle planners skip them
    outright (``is_externally_held_subject``).
    """
    if keycloak_sub is not None and str(keycloak_sub).startswith(SERVICE_SUBJECT_PREFIX):
        return True
    if is_externally_held_subject(keycloak_realm):
        return False
    return keycloak_realm in NON_HUMAN_SUBJECT_REALMS


def is_externally_held_subject(keycloak_realm: str | None) -> bool:
    """True for a subject in a non-human realm another service mints and owns
    (``STATEABLE_NON_HUMAN_SUBJECT_REALMS``): not a seat, not a bundle holder, not a member."""
    return keycloak_realm in STATEABLE_NON_HUMAN_SUBJECT_REALMS


def subject_identifier_problem(value: object) -> str | None:
    """Why ``value`` may not be stored as ``subject.keycloak_sub`` — or None if it may.

    ⛔ REFUSE, NEVER NORMALISE. ``'<A_sub>\\n'`` stripped IS ``<A_sub>``: normalising
    would silently alias an existing subject, and storing it raw mints a subject that a
    ``.strip()``-ing consumer reads as A (ingestion does). So an identifier with any
    whitespace or control/format character, or empty, is refused outright. A UUID-shaped
    identifier must be the lower-case form Keycloak issues: an upper-case variant would
    be a second subject for one person under any case-insensitive comparison.
    """
    if not isinstance(value, str) or not value:
        return "subject identifier must be a non-empty string"
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in value):
        return "subject identifier must not contain whitespace or control characters"
    if len(value) > SUBJECT_SUB_MAX_LEN:
        return f"subject identifier must be at most {SUBJECT_SUB_MAX_LEN} characters"
    if _ANY_CASE_UUID_RE.fullmatch(value) and not _KEYCLOAK_SUB_RE.fullmatch(value):
        return "a UUID-shaped subject identifier must be lower case (the form Keycloak issues)"
    return None


def keycloak_sub_as_identity(
    keycloak_sub: str | None, names_a_subject_row: bool | None
) -> str | None:
    """The subject's keycloak_sub when it can be trusted as a Keycloak identity, else None.

    None when it is not UUID-shaped (Keycloak user subs always are; ``test-user-sub``,
    ``svc-*`` are registry labels no consumer ever received as a JWT ``sub``) or when it
    equals a ``subject.id`` (the minted-from-a-row-id defect above). Fails closed: an
    unknown collision flag is treated as a collision.
    """
    # fullmatch on a \A…\Z lower-case pattern: a legacy '<uuid>\n', ' <uuid>' or upper-case
    # row is never emitted (a consumer that strips or case-folds would read it as another).
    if not isinstance(keycloak_sub, str) or not _KEYCLOAK_SUB_RE.fullmatch(keycloak_sub):
        return None
    if names_a_subject_row is not False:
        return None
    return str(keycloak_sub)


def actor_identity(keycloak_identity: str | None, actor_sub: object) -> str | None:
    """The Keycloak sub that acts for a licence subject, or None.

    ``keycloak_identity`` is ``keycloak_sub_as_identity(...)`` — the subject's own sub
    when it can be trusted as one — and wins. Otherwise the stated ``actor_sub`` (a UUID
    column, so always canonical when present). Never derived from anything else.
    """
    if keycloak_identity:
        return keycloak_identity
    return str(actor_sub) if actor_sub else None


#: The BYO-entitlement vocabulary, in one place. Mirrors `ck_tenants_byo_mode`
#: (migration 021) and the identical CHECKs on `tenant_app_byo_mode` and
#: `offering`. Every setter validates against THIS rather than an inline literal,
#: so a value can never be accepted by one path and rejected by the constraint.
BYO_MODES: tuple[str, ...] = ("local_allowed", "byo_required", "platform_only")

#: ``tenants.kind`` values, in one place. Mirrors ``ck_tenants_kind`` (migrations
#: 027/033) so a value can never be accepted by a write path and then rejected by
#: the constraint — the same contract BYO_MODES has above. ``main.TenantKind`` (the
#: API Literal on ``PUT /tenants/{id}``) is pinned equal to this tuple by a test.
#: ``'unclassified'`` is the column default: the fail-closed "nobody decided yet".
TENANT_KINDS: tuple[str, ...] = ("customer", "internal", "service", "test", "unclassified")

#: ``tenants.budget_currency`` values (migration 043, ``ck_tenants_budget_currency``).
#: NULL is the token-budget default. EUR/USD/CAD per PLAN REV F (owner, 2026-10-02).
#: ``main.BudgetCurrency`` (the API Literal) is pinned equal to this tuple by a test.
BUDGET_CURRENCIES: tuple[str, ...] = ("EUR", "USD", "CAD")


class BudgetCurrencyImmutableError(ClientSafeError):
    """A ``budget_currency`` change the caller may not make: clearing it to NULL
    (never allowed), or switching to a DIFFERENT currency without the owner
    capability (``allow_change``)."""


class BudgetCurrencyTenantArchivedError(ClientSafeError):
    """``budget_currency`` of an ARCHIVED tenant is not changed (mirrors bundle ensure's
    ``tenant_archived`` 409): an archived tenant is never provisioned again."""


#: The chargeback vocabulary, in one place. Mirrors `ck_tenants_cost_centre`
#: (migration 034) so a value can never be accepted by a write path and then
#: rejected by the constraint — the same contract BYO_MODES has above.
#:
#: MEASURED on the primary 2026-09-08 across all 76 tenant rows: service 45,
#: tests 9, customers 6, ci 4, improvement 4, merqi 4, NULL 4. These six ARE the
#: population; the tuple is not a guess at what might be wanted.
#:
#: ⚠️ NULL is deliberately NOT in this tuple and is deliberately still allowed by
#: the column and the CHECK. Four tenants (`default-anon`, `george-dekker`,
#: `nonagentic`, `portal`) have no cost centre, and which one they belong to is
#: an apportionment decision this service must never guess — migration 030
#: established that rule when it backfilled 12 rows and left exactly these four.
#: "Not yet decided" and "belongs to no centre" are the same state here, and it
#: is spelled NULL.
COST_CENTRES: tuple[str, ...] = ("service", "tests", "customers", "ci", "improvement", "merqi")

#: Keycloak realms whose `subject` rows are ROBOTS, not colleagues. Used by
#: :meth:`DatabaseManager.count_tenant_seats` to separate people from machines
#: under the per-seat charge, and declared here rather than inline so the one
#: place that decides "is this a person" is greppable.
#:
#: ⚠️ This is `Subject.keycloak_realm`, NOT `Tenant.kind` — a different column on a
#: different table, and they are not interchangeable. `Tenant.kind == 'service'`
#: says "this TENANT row is a service-mesh principal" (see `list_tenants`);
#: `Subject.keycloak_realm == 'service'` says "this PERSON is a machine account",
#: and a machine account belongs to a real customer tenant. `ahtapol`
#: (`kind='customer'`) holds a licence for `svc-workers-ahtapol`, which is exactly
#: that case.
#:
#: MEASURED on the primary 2026-09-11 across all 254 `subject` rows: `secure-apps`
#: 166, `service` 58, `portal` 28 (and the human realms are BOTH `portal` and
#: `secure-apps`, which is why this is an exclusion list and not an allowlist of
#: one human realm). A tuple rather than a bare string because a second robot
#: realm is a configuration change, not a code change.
#:
#: ⭐ ``cp-tenant`` (2026-10-12): CommonPractice binds each workspace's TENANT-level
#: licence to the synthetic subjects ``cp-tenant:<tenantId>:a|b`` (two, so it can
#: rotate without a gap), minted via ``POST /licenses/user/{sub}`` with
#: ``subject_realm: "cp-tenant"``. They are not people: billing them would add one
#: seat to every CP workspace. See ``STATEABLE_NON_HUMAN_SUBJECT_REALMS``.
NON_HUMAN_SUBJECT_REALMS: tuple[str, ...] = ("service", "cp-tenant")

#: Non-human realms a MINTING SERVICE may state on ``LicenseCreate.subject_realm``,
#: each mapped to the ``keycloak_sub`` prefix its subjects carry. The EXACT accepted
#: subs are decided by :func:`stated_non_human_subject_problem`.
#:
#: ⛔ THE SUB SHAPE IS THE WHOLE SAFETY ARGUMENT. ``subject_realm`` refuses non-human
#: realms because stating one would hide a PERSON from the seat count. ``cp-tenant``
#: is accepted only for ``cp-tenant:<the licence's own tenant id>:a|b`` — never a
#: Keycloak person (those are UUIDs), and at most two per tenant, so a caller can
#: neither launder a human out of ``human_subjects`` nor mint unlimited unbilled
#: licences. ``service`` is deliberately absent: its subs (``svc-*``, ``svc:``) are
#: provisioned by this service's own scripts, never minted.
#:
#: ⚠️ These subjects are ALSO not bundle holders: a ``cp-tenant`` licence is owned by
#: CommonPractice, carries no ``bundle_id``, and must never be adopted into, or
#: migrated onto, a ``svc:<tenant>:<app>`` bundle licence — see
#: :func:`is_tenant_level_subject`.
STATEABLE_NON_HUMAN_SUBJECT_REALMS: dict[str, str] = {"cp-tenant": "cp-tenant:"}

#: CommonPractice's two rotation slots: ``cp-tenant:<tenantId>:a`` and ``…:b``.
CP_TENANT_SUBJECT_SLOTS: tuple[str, ...] = ("a", "b")


def stated_non_human_subject_problem(
    subject_realm: str | None, subject_sub: str, tenant_id: str
) -> str | None:
    """Why ``subject_sub`` may not be minted under a stated non-human ``subject_realm``
    for a licence in ``tenant_id`` — or None if it may (or the realm is not one of
    ``STATEABLE_NON_HUMAN_SUBJECT_REALMS``, which the request validator handles).

    ``cp-tenant`` requires EXACTLY ``cp-tenant:<tenant_id>:a`` or ``…:b``, where
    ``tenant_id`` is the licence's own tenant (compared lower-case; the sub must be
    the lower-case form, as every other stored sub is)."""
    prefix = STATEABLE_NON_HUMAN_SUBJECT_REALMS.get(subject_realm or "")
    if prefix is None:
        return None
    tenant = str(tenant_id).strip().lower()
    allowed = {f"{prefix}{tenant}:{slot}" for slot in CP_TENANT_SUBJECT_SLOTS}
    if subject_sub not in allowed:
        return (
            f"subject_realm '{subject_realm}' is only valid for subject "
            f"'{prefix}<this licence's tenant id>:a' or ':b' (tenant {tenant})"
        )
    return None


class Tenant(Base):
    """Canonical tenant identity: maps a human-readable name to a UUIDv7 id.

    Foundation for the cluster-wide tenant identity standard
    ``(Tenant UUIDv7 + tenant_name)``. Today most service repos use a free-form
    ``tenant_id: VARCHAR(255)`` (e.g. ``"n8n-dev"``, ``"gerard-dev"``); going
    forward, license-authz translates incoming string tenant names to canonical
    UUIDs via ``GET /license/tenants/{name}`` and propagates both downstream
    (``X-Tenant-Id`` UUID + ``X-Tenant-Name`` string).

    Names are constrained to lowercase alphanumeric + hyphen (matches existing
    ``n8n-dev`` / ``gerard-dev`` shape; rejects accidental uppercase, spaces,
    or special chars).
    """

    __tablename__ = "tenants"
    __table_args__ = (
        # Migration 050: a tenant NAME is unique only WITHIN its owning app
        # (`owner_key`), so two apps can each have a customer called `netcare`.
        # `ix_tenants_name` survives as a plain lookup index; the global UNIQUE is
        # gone. `external_ref` is the owner's own id for the tenant (optional) and
        # is unique per owner when present — POST /tenants is idempotent on it.
        # Migration 053: both are unique among LIVE tenants only (`archived_at IS
        # NULL`), so an archived tenant never blocks its name / external_ref from
        # being registered again by the same owner. A name therefore resolves to the
        # live row only (`_tenant_by_name`); an archived tenant is addressed by id.
        Index("ix_tenants_name", "name"),
        Index(
            "uq_tenants_owner_name",
            "owner_key",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index(
            "uq_tenants_owner_external_ref",
            "owner_key",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL AND archived_at IS NULL"),
        ),
        CheckConstraint(
            "name ~ '^[a-z0-9][a-z0-9-]*$'",
            name="ck_tenants_name_format",
        ),
        # Mirrors TenantAppByoMode.ck_tenant_app_byo_mode below, which has always
        # declared the identical three values. This one was written only in SQL
        # (migration 006, widened by 007) and never in the ORM — so it existed on
        # NO database: measured 2026-08-12, prod `tenants` carried only
        # ck_tenants_name_format, and a create_all'd database had nothing either.
        # The runner baselines 006/007 as applied without running them whenever
        # the schema already exists, which is every real bootstrap.
        #
        # Defence in depth, NOT a live hole: every write path already validates
        # (main.py PUT/PATCH tenant handlers, DatabaseManager.set_*_byo_mode), and
        # prod data is clean — byo_required 27 / local_allowed 23 / platform_only
        # 7, zero invalid, zero NULL. Migration 021 adds it to existing databases;
        # this line is what puts it on a FRESH one.
        CheckConstraint(
            "byo_mode IN ('local_allowed', 'byo_required', 'platform_only')",
            name="ck_tenants_byo_mode",
        ),
        # Mirrors migration 027. Declared HERE as well as in SQL on purpose: the
        # runner baselines every current migration on a database that already has
        # the tables, so on a fresh bootstrap 027 never executes and create_all is
        # the only thing that builds this column. A SQL-only constraint would
        # exist on no database — exactly the drift ck_tenants_byo_mode above
        # documents having suffered.
        CheckConstraint(
            "kind IN ('customer', 'internal', 'service', 'test', 'unclassified')",
            name="ck_tenants_kind",
        ),
        # Mirrors migration 034. `cost_centre` arrived in 027 with NO constraint
        # at all — measured 2026-09-08, not one of the four CHECKs on `tenants`
        # mentioned it — so any string up to 64 chars was accepted. It is a
        # chargeback JOIN KEY, and a typo in a join key does not fail: it silently
        # produces a chargeback bucket of one, and the spend that should have
        # landed in the real centre never appears there.
        #
        # ⚠️ NULL IS ADMITTED ON PURPOSE. Four rows have no cost centre and this
        # service must not guess which one they belong to (see COST_CENTRES).
        # `cost_centre IS NULL OR ...` rather than a bare IN, because `NULL IN
        # (...)` evaluates to NULL — which a CHECK treats as PASSING, so the bare
        # form would work by accident rather than by statement.
        #
        # ⚠️ It deliberately does NOT correlate `kind` and `cost_centre`.
        # `platform-core` and `system` are kind='internal' with
        # cost_centre='service'; that is either a mis-set kind or a mis-set cost
        # centre, an operator's call either way, and a correlating CHECK would
        # refuse both rows at apply time and CrashLoop the pod.
        CheckConstraint(
            "cost_centre IS NULL OR cost_centre IN "
            "('service', 'tests', 'customers', 'ci', 'improvement', 'merqi')",
            name="ck_tenants_cost_centre",
        ),
        # Migration 031's own header proposed this exact CHECK and declined to add
        # it because `scripts/provision_service_token.py`'s insert path could not
        # set `byo_mode`, so every service principal it minted was born
        # kind='service' + byo_mode='byo_required' — a combination `ensure_tenant`
        # would then have rejected on the live provisioning path. That ONE call
        # site was fixed in #183 (`ensure_tenant` now accepts `byo_mode` and the
        # script passes 'local_allowed'). Migration 033 adds the constraint 031
        # wanted, and closes the primitive's OWN gap in the same commit:
        # `ensure_tenant(kind="service")` with no explicit `byo_mode` now defaults
        # to 'local_allowed' instead of falling through to the column's
        # server_default ('byo_required' again since migration 058, so the
        # explicit service default is load-bearing) — see
        # `ensure_tenant` below. Without that
        # default this CHECK would 500 every caller that relies on it (several
        # existing tests did, before 033).
        #
        # A machine identity has no account and nowhere to have put an API key, so
        # 'byo_required' ("commercial: must bring their own provider") is not a
        # strict policy for a `kind='service'` row, it is an unsatisfiable one —
        # the only reachable outcome is a 402 `byo_llm_not_configured`, raised
        # WITHOUT LOGGING (worker_management/src/api/message_endpoints.py:628).
        # svc-kag took exactly that 402 on 2026-09-03T04:16:53Z; migration 031
        # repaired the 34 rows that had already accumulated.
        CheckConstraint(
            "NOT (kind = 'service' AND byo_mode = 'byo_required')",
            name="ck_tenants_service_not_byo_required",
        ),
        # Migration 043 (PLAN §2.2). Declared here as well so a FRESH (create_all)
        # database carries it — the parity gate pins the two arms equal.
        CheckConstraint(
            "budget_currency IN ('EUR', 'USD', 'CAD')",
            name="ck_tenants_budget_currency",
        ),
        Index("ix_tenants_kind", "kind"),
    )

    # ⚠️ THE ONE server_default here that follows the CORPUS rather than PROD,
    # and it is called out because every other one in this file follows prod.
    # Migration 003 declares `id UUID PRIMARY KEY DEFAULT uuidv7()`, but prod's
    # `tenants.id` has NO default (measured live on the primary 2026-08-12) — 003 is
    # one of the twelve files BASELINED on prod 2026-07-09, so it never ran and
    # the table was hand-built without it. Prod is the outlier against its own
    # migration, not the ORM.
    # Harmless either way, but for a narrower reason than "every insert is
    # ORM-mediated" — that would be false. There ARE raw INSERTs into `tenants`:
    # seed_platform_tenants() below (every boot), tests/conftest.py, and
    # migrations 003/004/013/014. Every one of them supplies `id` explicitly, so
    # the server default is never reached by any of them; the ORM path is covered
    # separately by `default=uuid7`, which fires before a server default can.
    #
    # ⚠️ `uuidv7()` is a PostgreSQL 18 BUILT-IN. Declaring it as a server_default
    # means `create_all` now emits it, so building a fresh licences database from
    # the ORM requires PG18+ — a floor the ORM did not have before, though the
    # migration corpus already did (003 uses the same function). The whole fleet,
    # CI and docker-compose are PG18, so this is disclosure, not a live
    # constraint. `gen_random_uuid()` elsewhere in this file needs only PG13+.
    # Following the corpus keeps the parity gate meaningful; following prod would
    # just move the divergence from one arm to the other.
    id = Column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    name = Column(String(255), nullable=False)
    keycloak_realm = Column(String(255), nullable=True)
    # Migration 050: the app that OWNS this tenant (tenant_owner.key). Set from the
    # verified caller identity (api_client) on every API create, never from the
    # request body. There is NO default, in the DB or here: every writer names the
    # owner (operator scripts take a required --owner; the boot seed names
    # 'platform' for its own platform tenants), so a forgotten owner is a
    # NOT NULL violation, never a silent 'platform'. Every pre-050 tenant was
    # backfilled to 'platform' except the curated list in 050.
    owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_tenants_owner"),
        nullable=False,
    )
    # The owner's OWN stable id for this tenant (e.g. fr's tenant_slug). Optional.
    external_ref = Column(String(255), nullable=True)
    # BYO LLM entitlement (per-tenant). New tenants default to 'byo_required'
    # (migration 058, owner ruling 2026-10-15, licenses#273: platform Mac GPU is
    # only for the owner's existing tenants; every new early-access tenant brings
    # its own API tokens). This reverses migration 042 (George 2026-10-02,
    # 'local_allowed'). EXISTING rows are untouched by the default change; BYO stays
    # switchable per tenant via set_tenant_byo_mode. kind='service' rows are still
    # born 'local_allowed' by ensure_tenant (migration 033).
    #   'local_allowed' — may use platform GPU (local-llm) OR BYO.
    #   'byo_required'  (DEFAULT) — commercial: must bring their own provider;
    #                   worker_management hard-fails a run with no configured one.
    #   'platform_only' — demo/test/trial: locked to platform inference, BYO
    #                   disabled (any stored external row is ignored by WM).
    # Surfaced via token introspection so worker_management can enforce it.
    # ⚠️ A MISSING tenant row still fails CLOSED to 'byo_required' at lookup time
    # (_lookup_tenant_posture) — that is the unprovisioned-tenant rule, not this
    # column default, and 042 deliberately does not change it.
    byo_mode = Column(String(32), nullable=False, server_default="byo_required")
    # Soft delete. NULL = live; a timestamp = retired.
    #
    # There is deliberately no hard DELETE for a tenant, and this column is why.
    # A tenant OWNS licences, tokens, budgets, workspaces and fr `user_roles`
    # rows; a cascade would destroy billable history and an audit trail to undo
    # a typo. Archiving hides it from the operator lists and blocks new
    # provisioning while leaving every one of those rows intact and every
    # existing token working — retiring a tenant must not be a way to
    # accidentally revoke live access.
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # What this row is FOR, commercially. See migration 027 for the full rationale
    # and the measured spend that motivated it.
    #   'customer'     an external party we sell to
    #   'internal'     our own staff, product and platform tenants
    #   'test'         e2e loops, probes and fixtures — disposable by design
    #   'service'      a service-mesh auth principal, NOT a tenant commercially;
    #                  exclude these from any tenant inventory
    #   'unclassified' (DEFAULT) nobody has decided yet
    #
    # DEFAULT FAILS CLOSED. A new tenant must not be silently counted as a
    # customer (inflating revenue) or as internal (hiding cost).
    #
    # ⚠️ This is NOT `archived_at`. Archiving blocks provisioning; the 42 svc-*
    # principals are load-bearing (58 lines across 27 gitops files pin their
    # subject ids in *_ONBEHALF_SUBJECTS allowlists). Reclassifying changes no
    # name and no UUID, so every one of those lines stays byte-identical.
    kind = Column(String(16), nullable=False, server_default="unclassified")
    # Join key for CI and infrastructure chargeback. Deliberately unpopulated
    # until the apportionment basis exists — a guessed value later reads as a
    # measurement.
    cost_centre = Column(String(64), nullable=True)
    # Migration 043 (tenant-inference-provisioning PLAN §2.2). NULL = the tenant's
    # budget is TOKENS (every tenant that existed when 043 shipped); 'EUR' = its
    # budget is MONEY, held by billing (tenant_money_budget). For an 'EUR' tenant
    # this service pushes NO license_sync token budget — not at licence creation
    # (_provision_budget) and not in the nightly reconcile — only the request quota.
    # NULL -> a currency (license-admin); currency -> another currency only with the
    # owner capability, audited (set_tenant_budget_currency). Never back to NULL.
    budget_currency = Column(CHAR(3), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "keycloak_realm": self.keycloak_realm,
            "byo_mode": self.byo_mode,
            "budget_currency": self.budget_currency,
            "kind": self.kind,
            "cost_centre": self.cost_centre,
            "owner_key": self.owner_key,
            "external_ref": self.external_ref,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


PLATFORM_OWNER = "platform"

# Every tenant owned by another app carries an active grant to 'platform' (the
# platform is infrastructure: portal-bff-service, owner 'platform', serves portal
# users of EVERY tenant and must not 403 on a CP- or fr-created tenant). Written in
# the SAME transaction as the tenant row by create_tenant / ensure_tenant, and
# backfilled by migration 050.
PLATFORM_GRANT_BY = "platform-infrastructure"

# Migration 050 seed — the apps that own tenants. Mirrored by the boot seed
# (DatabaseManager.seed_ownership_reference) because a FRESH database baselines
# 050 without running it; tests/unit/test_migration_050_tenant_ownership.py pins
# the two equal.
TENANT_OWNERS: tuple[tuple[str, str], ...] = (
    ("platform", "Platform (internal, service and test tenants)"),
    ("fr", "FR"),
    ("commonpractice", "Common Practice"),
    ("w2v", "W2V"),
    ("merqi", "merqi / RaiseMyTickets"),
    ("book-fulfillment", "Book fulfillment"),
    # Migration 057 — the separated kag app owns its own tenants.
    ("kag", "KAG"),
    # Migration 067 — the strata apps own their own tenants (cell apps: each issues
    # its own app's licences, like commonpractice). strata-configurator is RETIRED
    # (084: its tenants moved to `agent`, app revoked, client deleted); the owner row
    # stays because the revoked app's issuer row and 069's revoked grant name it.
    ("strata-configurator", "Strata Configurator"),
    ("care-bus", "Care Bus"),
    # Migration 070 — the agent app (agent.nonagentic.ai) owns its own tenants.
    ("agent", "Agent"),
    # Migration 082 — mcp owns NO tenant: it is the edge caller of mcp -> {coding,
    # strata-rag, strata-ingest, laya} and the owner of mcp-bundle-minter, admitted on a
    # tenant only through the tenant_dependency_grant rows ensure writes for it.
    ("mcp", "MCP"),
)

# Migration 072/074/076/077/078 seed — (realm, owner_key). Mirrored by the boot seed because
# a FRESH database baselines 072/074/076/077/078 without running them; pinned equal by
# tests/unit/test_migration_072_realm_owner.py, test_migration_074_realm_owner_agent.py,
# test_migration_076_realm_owner_w2v.py, test_migration_077_realm_owner_fr.py and
# test_migration_078_care_bus_early_access.py.
# Only app realms: never portal or secure-apps (those keep the pre-072 behaviour).
REALM_OWNERS: tuple[tuple[str, str], ...] = (
    ("commonpractice", "commonpractice"),  # 072
    ("agent", "agent"),  # 074 — the agent app's own realm
    ("w2v", "w2v"),  # 076 — Studio's own realm (internal id w2v)
    ("fr", "fr"),  # 077 — the fr app's own realm
    ("care-bus", "care-bus"),  # 078 — care-bus's own realm (early access)
)

# (realm, client_id, owner_key, cross_owner) — the Keycloak clients that hold a
# licence-writing role (Keycloak DB census 2026-10-13, read-only). A client NOT
# listed here is refused (403) on every tenant / licence / grant / bundle write.
# secure-apps/licenses-admin is the ONLY cross-owner (break-glass) client.
API_CLIENTS: tuple[tuple[str, str, str, bool], ...] = (
    ("portal", "fr-backend", "fr", False),
    ("secure-apps", "fr-backend", "fr", False),
    ("portal", "provisioning-backend", "fr", False),
    ("portal", "w2v-backend", "w2v", False),
    ("secure-apps", "commonpractice-licenses-admin", "commonpractice", False),
    ("secure-apps", "control-service-account", "merqi", False),
    ("secure-apps", "book-fulfillment-svc", "book-fulfillment", False),
    # The NonAgentic customer portal's BFF (portal-bff/src/config.py
    # service_client_id): proxies portal users' own licence CRUD — platform's.
    ("portal", "portal-bff-service", "platform", False),
    # admin_app = RaiseMyTickets' tenant control plane (gitops keycloak
    # realm-patch "admin-app-service-account"); RMT is merqi's product line.
    ("secure-apps", "admin-app-service-account", "merqi", False),
    ("secure-apps", "licenses-admin", "platform", True),
    # Migration 054 — the four `bundle-token-minter` holders (gitops keycloak
    # realm-patch declare_n8n_bundle_resolver / declare_service_bundle_minters).
    # POST /internal/tenants/{id}/bundle-token is owner-scoped since 054, so each
    # needs its owner. merqi's marketing + sales services mint w2v /
    # raise-my-tickets on merqi's tenants (Loki 2026-10-07..14);
    # worker_management mints inference-router for every tenant and n8n's
    # resolver serves every tenant's n8n nodes — platform infrastructure, which
    # holds an active grant on every non-platform tenant (050 grant rule 1).
    ("secure-apps", "marketing-bundle-minter", "merqi", False),
    ("secure-apps", "sales-bundle-minter", "merqi", False),
    ("secure-apps", "worker-management-bundle-minter", "platform", False),
    ("secure-apps", "n8n-bundle-resolver", "platform", False),
    # Migration 055 — provisioning's tenant-inference smoke mints an
    # inference-router bundle token on the fr-owned tenant it provisioned
    # (gitops keycloak realm-patch PATCH_ONLY=provisioning-bundle-minter;
    # holds `bundle-token-minter` only). fr owns provisioning (cf.
    # portal/provisioning-backend above).
    ("secure-apps", "provisioning-bundle-minter", "fr", False),
    # Migration 056 — growth (merqi's Twenty app, growth#459) mints per-tenant
    # raise-my-tickets bundle tokens at runtime on the merqi-owned tenants it
    # serves (gitops keycloak realm-patch, gitops-production#4636; holds
    # `bundle-token-minter` only). merqi, like marketing/sales minters above.
    ("secure-apps", "growth-bundle-minter", "merqi", False),
    # Migration 057 — the separated kag app's licences SA (gitops keycloak
    # realm-patch, kag-frontend separation P3; holds `license-admin`). It registers
    # kag's own tenants, ensures `kag-research` on them and mints kag's apps there.
    ("secure-apps", "kag-licenses-admin", "kag", False),
    # Migration 059 — the dedicated per-tenant bundle-token minters of fr (the
    # conveyor's workers calls, integrations/workers.ts) and w2v (w2v_common's
    # WMClient). Each holds `bundle-token-minter` only (gitops keycloak realm-patch,
    # BUNDLE_MINTER_CLIENTS); fr-backend / w2v-backend never get that role.
    ("secure-apps", "fr-bundle-minter", "fr", False),
    ("secure-apps", "w2v-bundle-minter", "w2v", False),
    # Migration 060 — kag-frontend's per-tenant bundle-token minter: `workers` (a
    # dependency licence via kag-research's closure, kag -> workers) and `kag` on
    # kag-owned tenants. Holds `bundle-token-minter` only (gitops keycloak realm-patch,
    # BUNDLE_MINTER_CLIENTS); separate from kag's own minter (per-service secrets).
    ("secure-apps", "kag-frontend-bundle-minter", "kag", False),
    # Migration 061 — kag's OWN outbound minter (kag.kag -> workers / mlx-backend /
    # mlx-ingestion-backend / inference-router, the END tenant's per-app token). Owner
    # kag because the kag -> {workers, mlx-*} tenant_dependency_grant rows are keyed on
    # it. Holds `bundle-token-minter` only; separate from kag-frontend's (per-service secrets).
    ("secure-apps", "kag-bundle-minter", "kag", False),
    # Migration 064 — karma's OWN outbound minter (karma.karma -> workers /
    # inference-router, the END tenant's per-app token). Owner kag, same as 061's
    # kag-bundle-minter: karma runs inside the kag product. Holds `bundle-token-minter`
    # only; a separate client from kag's and kag-frontend's (per-service secrets).
    ("secure-apps", "karma-bundle-minter", "kag", False),
    # Migration 067 added strata_configurator's licences SA
    # ('secure-apps', 'strata-configurator-licenses-admin'); migration 084 retired it
    # with the app (merged into `agent`). It must NOT be listed here: the boot seed
    # would re-create it on every boot.
    # Migration 068 — care-bus's licences SA (holds `license-admin`, gitops keycloak
    # realm-patch). Registers care-bus's tenants (owner care-bus), issues / revokes their
    # `care-bus` licences and mints the licence tokens it introspects — the same shape
    # as strata-configurator-licenses-admin (067).
    ("secure-apps", "care-bus-licenses-admin", "care-bus", False),
    # Migration 070 — the agent app's licences SA (agent.nonagentic.ai; holds
    # `license-admin`, gitops keycloak realm-patch). Registers agent's tenants (owner
    # agent), issues / revokes their `agent` licences and mints the licence tokens it
    # introspects — strata-configurator-licenses-admin's shape (067).
    ("secure-apps", "agent-licenses-admin", "agent", False),
    # Migration 078 — care-bus's per-tenant bundle-token minter: the tenant's own
    # mcp / strata-rag / strata-ingest licences (dependencies of care-bus) for the
    # care-bus service's tool calls. Holds `bundle-token-minter` only (gitops keycloak
    # realm-patch, BUNDLE_MINTER_CLIENTS); separate from care-bus-licenses-admin
    # (license-admin never admits bundle-token; per-service secrets).
    ("secure-apps", "care-bus-bundle-minter", "care-bus", False),
    # Migration 082 — the mcp broker's per-tenant minter (LicenceMinterCredentialResolver):
    # the tenant's own coding / strata-rag / strata-ingest / laya licences (dependencies
    # of mcp), admitted ONLY through the tenant_dependency_grant (tenant, mcp, app) rows;
    # it passes bundle=<product bundle> explicitly. And agent's per-tenant minter: the
    # tenant's `mcp` licence for the agent service's tool calls. Both hold
    # `bundle-token-minter` only (gitops keycloak realm-patch, BUNDLE_MINTER_CLIENTS).
    ("secure-apps", "mcp-bundle-minter", "mcp", False),
    ("secure-apps", "agent-bundle-minter", "agent", False),
    # Migration 083 — realm-per-app P7 chain C ("portal = staff only"): the secure-apps
    # twins of portal/w2v-backend and portal/provisioning-backend (same client ids, same
    # owners; gitops keycloak PATCH_ONLY=p7-s2s-secure-apps). The portal rows above stay
    # until the portal clients are deleted.
    ("secure-apps", "w2v-backend", "w2v", False),
    ("secure-apps", "provisioning-backend", "fr", False),
    # Migration 085 — the m4 Studio cell's OWN S2S client (gitops #4594): owner w2v so
    # its X-Tenant-Id retarget is admitted for w2v's tenants under `license-reader`
    # (replaces the unscoped `service-internal` gitops #4935 had to give it).
    ("secure-apps", "w2v-cell-m4", "w2v", False),
    # Migration 090 — the Rust conveyor's S2S client (gitops keycloak secure-apps client
    # `conveyor-backend`): POST /api/provision as `provision-caller` (provisioning mints the
    # licence with ITS OWN fr-owned SA, hence app_issuer conveyor -> fr), reads the app's
    # entitlement as `license-reader`, and reads per-actor billing as `billing-internal`.
    # Owner fr, like provisioning-backend; NOT cross_owner.
    ("secure-apps", "conveyor-backend", "fr", False),
)

# Which owners may ISSUE (mint/revoke) an app's licences. Apps not listed are the
# platform long tail: platform AND fr (fr's super-admin console mints across the
# whole catalogue through provisioning, CALLER_ALLOWED_APP_KEYS="*").
APP_ISSUER_OVERRIDES: dict[str, tuple[str, ...]] = {
    "commonpractice": ("commonpractice",),
    "fr": ("fr",),
    "fr-frontend": ("fr",),
    # Migration 057: kag issues its own apps; fr KEEPS kag for the transition (its
    # admin console mints kag through provisioning) until fr is narrowed.
    "kag": ("fr", "kag"),
    "kag-graph": ("platform", "fr", "kag"),
    "kag-orchestrator": ("platform", "fr", "kag"),
    "inference-router": ("fr", "commonpractice", "kag"),
    # Migration 062: kag-frontend calls Evidence Review directly with the tenant's own
    # evidence-review licence (a dependency of kag). kag issues it (product owner);
    # platform keeps admin revoke. Not fr (being narrowed, George 10-12).
    "evidence-review": ("kag", "platform"),
    "w2v": ("w2v",),
    "w2v-image": ("w2v",),
    "raise-my-tickets": ("merqi",),
    "raise-my-tickets-admin": ("merqi",),
    "raise-my-tickets-analytics": ("merqi",),
    "raise-my-tickets-core": ("merqi",),
    "raisemytickets": ("merqi",),
    "merqi": ("merqi",),
    "marketing": ("merqi",),
    "sales": ("merqi",),
    "core-app": ("merqi",),
    "chat-bridge-merqi": ("merqi",),
    "chat-bridge-rmt": ("merqi",),
    "book-fulfillment": ("book-fulfillment",),
    # Migration 067: each strata app issues its own licences, nobody else (cell rule;
    # commonpractice's shape). Not fr / platform: no caller of theirs issues them.
    # strata-configurator is retired (084: app revoked, client deleted) but its live
    # issuer row stays; kept here so the key never falls back to {platform, fr}.
    "strata-configurator": ("strata-configurator",),
    "care-bus": ("care-bus",),
    # Migration 070: the agent app issues its own licences, nobody else (cell rule).
    "agent": ("agent",),
    # Migration 075: speech (TTS / ASR, shared platform services) is issued by the
    # platform owner only. Not fr: no fr flow mints it.
    "speech": ("platform",),
    # Migration 078: the strata tools care-bus calls for a tenant are shared platform
    # services issued by platform only (speech's shape). care-bus does NOT issue them:
    # its tenants hold them as DEPENDENCY licences of care-bus (APP_REQUIRES_SEED).
    "strata-rag": ("platform",),
    "strata-ingest": ("platform",),
}
DEFAULT_APP_ISSUERS: tuple[str, ...] = ("platform", "fr")


# Which owners may ENSURE each bundle (migration 052; its header carries the evidence
# per row). An owner that issues every app of the bundle, plus the evidenced cross-app
# flows: CP's create flow, merqi -> w2v, platform's non-agentic-ai tenants.
# ``commonpractice`` is CommonPractice's alone.
BUNDLE_ENSURER_SEED: dict[str, tuple[str, ...]] = {
    "commonpractice": ("commonpractice",),
    "book-fulfillment": ("book-fulfillment",),
    "knowledge": ("fr", "commonpractice"),
    "research": ("fr", "commonpractice"),
    "non-agentic-ai": ("fr", "commonpractice", "platform"),
    "merqi": ("merqi", "commonpractice"),
    "raisemytickets": ("merqi", "commonpractice"),
    "w2v": ("w2v", "merqi", "commonpractice"),
    "inference-router": ("fr", "commonpractice", "merqi", "w2v", "platform"),
    "n8n-nodes": ("commonpractice", "platform", "fr"),
    # Migration 057 — rule 1: kag issues all four apps of the bundle.
    "kag-research": ("kag",),
    # Migration 059 — rule 1: fr issues every app of the conveyor bundle.
    "conveyor": ("fr",),
    # Migration 063 — rule 1: kag issues kag, the bundle's only app. Not platform
    # (does not issue kag); a platform-owned tenant is ensured through break-glass.
    "kag-service": ("kag",),
    # Migration 078 — rule 1: care-bus issues care-bus, the bundle's only app.
    "care-bus": ("care-bus",),
    # Migration 082 — rule 1: agent issues agent, the bundle's only app.
    "agent": ("agent",),
}

#: Audit reason stamped on a dependency licence released because no remaining active
#: licence on its tenant requires it (``DatabaseManager._release_orphaned_dependencies``).
DEPENDENCY_RELEASE_REASON = "dependency released: no remaining dependent licence"
#: Audit reason stamped on a dependency licence revoked explicitly by id
#: (``POST /license/revoke``), with the same grant cleanup as a release.
DEPENDENCY_EXPLICIT_REVOKE_REASON = "dependency revoked explicitly"
#: How long a revoke waits for the tenant's bundle-ensure lock before revoking
#: without the release (``revoke_license_releasing_dependencies``).
DEPENDENCY_RELEASE_LOCK_TIMEOUT = "10s"

# Migration 059 — what a bundle SELLS vs what its apps NEED. A bundle lists only the
# apps a customer buys; the apps those apps call on the tenant's behalf are
# ``app_requires`` edges, and ensure provisions their transitive closure as
# DEPENDENCY licences (``license.is_dependency``: bundle-token only, never a user
# token). (app, required_app, caller_owner): ``caller_owner`` is the owner whose
# bundle-token minter mints ``required_app`` for the tenant (its ``api_client``
# owner), granted on the tenant by ensure. Evidence per edge: 059's header.
APP_REQUIRES_SEED: tuple[tuple[str, str, str], ...] = (
    ("w2v", "workers", "w2v"),
    ("w2v", "kag", "w2v"),
    ("marketing", "workers", "merqi"),
    ("sales", "workers", "merqi"),
    ("kag", "workers", "kag"),
    ("kag", "mlx-backend", "kag"),
    ("kag", "mlx-ingestion-backend", "kag"),
    # Migration 062: kag-frontend (owner kag) -> Evidence Review, with the tenant's
    # own evidence-review licence (bundle-token as kag-frontend-bundle-minter).
    ("kag", "evidence-review", "kag"),
    ("workers", "inference-router", "platform"),
    # Migration 078: care-bus calls its tools for the tenant with the tenant's own
    # licence for each (bundle-token as care-bus-bundle-minter).
    ("care-bus", "mcp", "care-bus"),
    ("care-bus", "strata-rag", "care-bus"),
    ("care-bus", "strata-ingest", "care-bus"),
    # Migration 080: care-bus calls laya (GPU, metered per licence at license-authz on
    # `/laya/`) for the tenant with the tenant's own laya licence (bundle-token as
    # care-bus-bundle-minter). NOT vllm: that uses a per-service token.
    ("care-bus", "laya", "care-bus"),
    # Migration 082: the mcp broker calls the toolkit backends for the tenant with the
    # tenant's own licence for each (bundle-token as mcp-bundle-minter); agent calls mcp
    # with the tenant's own mcp licence (bundle-token as agent-bundle-minter). The
    # closure is transitive: agent / care-bus -> mcp -> these four.
    ("mcp", "coding", "mcp"),
    ("mcp", "strata-rag", "mcp"),
    ("mcp", "strata-ingest", "mcp"),
    ("mcp", "laya", "mcp"),
    ("agent", "mcp", "agent"),
    # Migration 086: agent calls inference-router for the tenant with the tenant's own
    # inference-router licence (bundle-token as agent-bundle-minter). Direct edge.
    ("agent", "inference-router", "agent"),
    # Migration 089: agent calls the conveyor's own /mcp for the tenant with the
    # tenant's own conveyor licence (bundle-token as agent-bundle-minter). Direct edge,
    # the same shape as 086; SPEC D19.
    ("agent", "conveyor", "agent"),
)

# Migration 059 — (owner, app): an owner whose service calls one of a bundle's OWN
# apps for the holding tenant (fr -> workers for the conveyor, fr
# integrations/workers.ts). Ensure writes a tenant_dependency_grant scoped to that app.
BUNDLE_CALLER_SEED: dict[str, tuple[tuple[str, str], ...]] = {
    # Migration 065 (George 2026-10-16): fr proves the tenant to secrets with a minted
    # app=secrets licence, as it does for workers (fr#1579).
    # Migration 066 (George 2026-10-16, fr P1.5 agent skills): fr mints app=skill-builder
    # per tenant the same way (reverses 065's "skill-builder ... stay out").
    "conveyor": (("fr", "workers"), ("fr", "secrets"), ("fr", "skill-builder")),
}


def issuers_for_app_key(app_key: str) -> tuple[str, ...]:
    """The issuing owners migration 050 assigns to ``app_key``."""
    return APP_ISSUER_OVERRIDES.get(app_key, DEFAULT_APP_ISSUERS)


class TenantOwner(Base):
    """An app that owns tenants (migration 050). Tenant names are unique per owner."""

    __tablename__ = "tenant_owner"
    __table_args__ = (
        CheckConstraint("key ~ '^[a-z0-9][a-z0-9-]*$'", name="ck_tenant_owner_key_format"),
    )

    key = Column(String(64), primary_key=True)
    display_name = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )


class ApiClient(Base):
    """Verified caller (token ``iss`` realm + ``azp``) -> owning app (migration 050).

    The ONLY source of a caller's owner: never a body field or a header.
    ``cross_owner`` is break-glass — such a client bypasses owner scoping, and
    every action it takes outside its own owner is audited.
    """

    __tablename__ = "api_client"
    __table_args__ = (PrimaryKeyConstraint("realm", "client_id", name="api_client_pkey"),)

    realm = Column(String(255), nullable=False)
    client_id = Column(String(255), nullable=False)
    owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_api_client_owner"),
        nullable=False,
    )
    cross_owner = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )


class RealmOwner(Base):
    """Keycloak realm -> the app that owns its users (migration 072, realm-per-app P0).

    A verified token from a realm with a row here may act ONLY on tenants that
    owner owns or holds an active ``tenant_app_grant`` on — otherwise a realm
    admin of app A could set ``tenant_id`` to a tenant of app B (cross-realm
    tenant forgery). A realm with no row keeps the pre-072 behaviour; ``portal``
    and ``secure-apps`` are deliberately NOT rows.
    """

    __tablename__ = "realm_owner"

    realm = Column(String(255), primary_key=True)
    owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_realm_owner_owner"),
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )


class AppIssuer(Base):
    """Which owners may issue an app's licences (migration 050)."""

    __tablename__ = "app_issuer"
    __table_args__ = (PrimaryKeyConstraint("app_id", "owner_key", name="app_issuer_pkey"),)

    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE", name="fk_app_issuer_app"),
        nullable=False,
    )
    owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_app_issuer_owner"),
        nullable=False,
    )


class TenantAppGrant(Base):
    """The tenant's OWNER lets another app act on the tenant (migration 050).

    A grantee may read the tenant BY ID and mint/revoke licences for apps it
    issues. It may not rename, archive, change BYO mode or grant onward. One
    ACTIVE grant per (tenant, grantee); revoking stamps ``revoked_at`` so the
    history survives.
    """

    __tablename__ = "tenant_app_grant"
    __table_args__ = (
        Index(
            "uq_tenant_app_grant_active",
            "tenant_id",
            "grantee_owner_key",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id = Column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid7, server_default=text("uuidv7()")
    )
    tenant_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_tenant_app_grant_tenant"),
        nullable=False,
    )
    grantee_owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_tenant_app_grant_owner"),
        nullable=False,
    )
    granted_by = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "grantee_owner_key": self.grantee_owner_key,
            "granted_by": self.granted_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


class TenantNameAmbiguousError(ClientSafeError):
    """A bare tenant NAME matched tenants of more than one owner (cross-owner caller)."""


def _tenant_by_name(
    session: Any, name: str, owner_key: str | None = None, *, for_update: bool = False
) -> Tenant | None:
    """Resolve a tenant NAME to its LIVE row — unique only per owner since 050.

    LIVE ONLY (migration 053). Names are unique per owner among NON-ARCHIVED
    tenants, so one owner may hold a live ``acme`` and any number of archived
    ones. A name therefore resolves to the live row or to nothing; an archived
    tenant is reachable by ID only. Picking "the" archived row for a name would
    need a tie-break among several, and the result would silently move to a new
    tenant the moment the name is re-registered — so a name never names one.

    With ``owner_key`` the lookup is scoped to that owner (unique by
    ``uq_tenants_owner_name``). Without one, a name that matches live tenants of
    MORE THAN ONE owner returns None and logs ``tenant_name_ambiguous``: a bare
    name is no longer a key, and picking one of several tenants would
    mis-attribute whatever the caller does next (billing, posture, seats). Never a
    pick.
    """
    q = session.query(Tenant).filter(Tenant.name == name, Tenant.archived_at.is_(None))
    if owner_key is not None:
        q = q.filter(Tenant.owner_key == owner_key)
    if for_update:
        q = q.with_for_update()
    rows = q.limit(2).all()
    if len(rows) > 1:
        logger.warning(
            "tenant_name_ambiguous: name=%r matches tenants of several owners; "
            "refusing to pick one (address the tenant by id)",
            name,
        )
        return None
    return rows[0] if rows else None


# The tenant-NAME shape, as the database itself enforces it: `ck_tenants_name_format`
# above is `name ~ '^[a-z0-9][a-z0-9-]*$'`. Compiled once so
# `resolve_tenant_selector` decides "could this string ever name a tenant" against
# the SERVER's own definition rather than a second opinion — the same reason
# `auth.middleware._is_tenant_uuid` resolves UUIDs through the UUID constructor
# instead of a hand-written regex. ⚠️ `fullmatch` supplies the anchors; adding `^`
# and `$` here as well would let a trailing newline through, which is precisely the
# permissive direction that function documents as a landmine.
_TENANT_NAME_SHAPE = re.compile(r"[a-z0-9][a-z0-9-]*")

# The three outcomes of resolving a caller-supplied tenant selector. Named rather
# than booleans because the route maps them to three DIFFERENT status codes, and a
# two-valued answer is exactly what made a malformed selector indistinguishable
# from an absent tenant on the wire.
TENANT_LOOKUP_FOUND = "found"
TENANT_LOOKUP_NOT_FOUND = "not_found"  # legal selector, no such tenant -> 404
TENANT_LOOKUP_MALFORMED = "malformed_selector"  # cannot name a tenant at all -> 400
TENANT_LOOKUP_OUTCOMES = (
    TENANT_LOOKUP_FOUND,
    TENANT_LOOKUP_NOT_FOUND,
    TENANT_LOOKUP_MALFORMED,
)


class TenantLookup(NamedTuple):
    """``resolve_tenant_selector``'s answer: the row, plus WHY there isn't one.

    ``tenant`` is None for both non-found outcomes, so a caller that only wants
    the row can still read ``.tenant`` and ignore the rest; a caller that must
    publish a status code reads ``.outcome``.

    Declared HERE, immediately below ``Tenant``, because the annotation names it:
    this module has no ``from __future__ import annotations``, so a definition
    above the class would be a forward reference resolved only by PEP 649's
    deferred evaluation — true on py314, but a needless dependency on it.
    """

    tenant: Tenant | None
    outcome: str


# The tenants whose ids the rest of the fleet HARDCODES, and which therefore have
# to exist on any licenses database — not just the one that happened to be
# migrated in place. `(id, name, byo_mode)`, transcribed from migrations 013 and
# 014, which are the rows those files restore.
#
# Read `DatabaseManager.seed_platform_tenants` for why a migration cannot carry
# them: the SQL runner baselines every file as applied on a fresh database, so
# 013/014 never execute there.
#
# Deliberately only the hardcoded ids. 003 (`n8n-dev`, `gerard-dev`) and 004
# (`marketing`, `sales`) seed uuidv7-RANDOM ids that nothing can reference by
# literal value, so recreating them elsewhere would invent data, not restore
# identity.
PLATFORM_TENANTS: tuple[tuple[str, str, str], ...] = (
    # fr-test Keycloak-user tenant — the identity the browser-test user carries.
    ("0195f48b-d000-7000-8000-000000000099", "fr-test-user", "local_allowed"),
    # System / anon-S2S / platform control-plane family.
    ("00000000-0000-0000-0000-000000000000", "default-anon", "platform_only"),
    ("00000000-0000-0000-0000-000000000001", "system", "platform_only"),
    # ⚠️ NOT anonymous control-plane slots — each is a real tenant's canonical
    # uuid7 in the Keycloak PORTAL realm, i.e. the literal `tenant_id` claim in
    # live user JWTs. Seeded 2026-07-10 under invented `platform-cp-N` labels;
    # migration 024 renamed them to state what they are. A fresh database never
    # runs 024 (the SQL runner baselines it), so these names must be correct
    # HERE for a rebuilt database to carry the honest identity.
    #
    # The suffix is deliberate: `n8n-dev` ALSO exists at 019de842-c92a-… , so two
    # rows describe one human tenant. That is billing #86 and is NOT settled here.
    ("0195f48b-d000-7000-8000-000000000001", "n8n-dev-portal-realm", "platform_only"),
    ("0195f48b-d000-7000-8000-000000000003", "george-dev-portal-realm", "platform_only"),
    #
    # …0004 / …0005 were renamed by migration 026 on the same principle: they are
    # fr's prompt- and procedure-improvement tenants. fr migrations 062/066 INSERT
    # these uuids under exactly these names, fr's `streams_kind_tenant_check` pins
    # kind->uuid, and the Keycloak realm-patch script mints them as the `tenant_id`
    # claim for system-prompt-operator / aa-card-writer-prompt / -procedure. The
    # names are TRANSCRIBED from the owning system, not invented.
    #
    # ⛔ …0002 IS ABSENT FROM THIS CONSTANT, AND STAYS ABSENT. It has never been
    # seeded here — the `platform-core` row exists only in prod, created outside
    # this corpus — so a fresh database asserts no name for that uuid at all. That
    # is the honest state while the dispute is open: fr calls it
    # `workflow-improvement`, this registry's prod row calls it `platform-core`,
    # and the portal realm retired `gerard-dev` at it, over 20 active licences
    # (billing #86). Adding it here under any name would settle that by seeding,
    # with no decision and no migration. Two well-sourced contradicting claims is
    # a decision, not a transcription.
    ("0195f48b-d000-7000-8000-000000000004", "prompt-improvement", "platform_only"),
    ("0195f48b-d000-7000-8000-000000000005", "procedure-improvement", "platform_only"),
    # Migration 013's render-E2E tenant. INCLUDED as of 2026-08-12 — the earlier
    # exclusion rested on a premise that has since been measured false and fixed.
    #
    # WHAT THE OLD COMMENT SAID: two prod rows split the identity — a random uuid7
    # `019f7ed1-…` held the name `portal-render-e2e` (3 licences) while 013's
    # literal sat under `portal-render-e2e-e148e8af` (2 licences), because 013 hit
    # UNIQUE(name) and was renamed to get past it. It concluded "the live render-E2E
    # identity is the RANDOM id".
    #
    # THAT CONCLUSION WAS WRONG, and token usage is what shows it:
    #   019f7ed1-…  3 licences,  0 tokens, never used
    #   e148e8af-…  2 licences,  5 tokens, 3 of them used on 2026-08-12
    # The row the old comment called dead is the one carrying all the live traffic.
    # It matters because ``get_tenant`` resolves by NAME as well as id, so the name
    # `portal-render-e2e` was pointing at the row nothing uses.
    #
    # Prod was corrected 2026-08-12: `019f7ed1-…` renamed to
    # `portal-render-e2e-stale-019f7ed1`, and `e148e8af-…` given the canonical name.
    # No licence or token moved — only the two labels. That is precisely what
    # migration 013 does on its own (`ON CONFLICT (id) DO UPDATE SET name =
    # EXCLUDED.name`), so prod, the migration and this constant now agree, and a
    # fresh database reproduces the identity the fleet actually uses.
    #
    # ⛔ RE-KEYED 2026-10-06 (owner ruling), migration 046. The paragraphs above are
    # the dated record of the NAME dispute and still call the tenant `e148e8af-…`;
    # that id is ALSO a `subject.keycloak_sub` (a deleted `portal`-realm user), so
    # the tenant now lives on the uuid7 below and `e148e8af-…` is a principal only.
    # This entry MUST name the new id: boot order is create -> migrate -> seed, and
    # an entry left on the old id would be re-born on every fresh database.
    ("01a1113b-674f-72b0-a84b-dc746d39afca", "portal-render-e2e", "local_allowed"),
)


class App(Base):
    """Licensed application or module.

    ``audience`` says WHO holds a licence for this app, and exists because the
    distinction was previously encoded only in prose. fr's super-admin provisioning
    picker offered all 47 registry rows as checkboxes, including service-to-service
    modules no person should ever hold — and on 2026-08-26 an operator selected three
    of them, which is how a run reached an app with no active plan and left orphaned
    licences behind. fr now filters, but it did so from a hardcoded list duplicated in
    its own frontend and backend: a classification about THIS service's data, living
    in another repo. This column is that classification's home.

      * ``human``     — an end-user product a person is licensed to use.
      * ``platform``  — service-to-service. fr drives these on the user's behalf over
                        the trusted network; see ``scripts/seed_apps.py``, which has
                        always said "those need no per-user license".
      * ``automation``— n8n action modules. Never a person.

    The default is ``platform``, deliberately: a newly registered app is not offered
    to people until someone says it should be. Fail closed.
    """

    __tablename__ = "app"
    __table_args__ = (
        CheckConstraint(
            "audience IN ('human', 'platform', 'automation')",
            name="ck_app_audience",
        ),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    key = Column(String(255), nullable=False, unique=True)  # 'code_graph_helper', 'n8n_node_x'
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    revoked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    audience = Column(
        String(16), nullable=False, default="platform", server_default=text("'platform'")
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "revoked": self.revoked,
            # Emitted unconditionally: ``AppResponse`` mirrors this dict exactly, so a
            # field missing here is a field no consumer can ever see. That is how the
            # picker ended up filtering on a list hardcoded in fr instead of on data.
            "audience": self.audience,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Bundle(Base):
    """The sellable grouping of apps — the missing link between tenant and licence.

    A tenant does not buy an ``app``; it buys a BUNDLE, and the bundle says which app
    licences that grant mints underneath it. ``non-agentic-ai`` is fr + kanban + n8n +
    the n8n node modules; ``knowledge`` is fr + the KAG family; ``merqi`` is
    marketing + sales.

    ⚠️ **Grouping only. A bundle never meters.** Consumption is always counted against
    the individual app licence, because that is the grain at which cost is actually
    incurred and the grain at which a microservice can be priced. The bundle is the
    commercial and provisioning object sitting above that — it answers "what did this
    customer buy" and "what does this customer's KAG usage cost in total", never
    "which call cost what".

    This is NOT ``Offering``. An offering is a per-``(app, code)`` usage-policy
    template (BYO posture, markup, surcharges) and cannot span products —
    ``ux_offering_app_code`` makes that structural. The two compose: a bundle says
    WHICH apps, an offering says on what commercial terms each is served.
    """

    __tablename__ = "bundle"

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    key = Column(String(64), nullable=False, unique=True)  # 'non-agentic-ai', 'knowledge'
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    revoked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "revoked": self.revoked,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BundleApp(Base):
    """Bundle membership. MANY-TO-MANY, and that is not incidental.

    ``fr`` belongs to ``non-agentic-ai``, ``knowledge`` AND ``research`` — the operator
    named all three on 2026-09-04. So membership cannot be a ``bundle_id`` column on
    ``app``: one app genuinely belongs to several bundles at once, and a column would
    silently force a choice between them the first time someone seeded the second
    bundle. A join table is the only shape that can represent what was asked for.

    Composite primary key ``(bundle_id, app_id)`` — an app is in a bundle once or not
    at all, so the membership itself carries no identity worth minting.
    """

    __tablename__ = "bundle_app"
    __table_args__ = (Index("idx_bundle_app_app", "app_id"),)

    bundle_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("bundle.id", ondelete="CASCADE"),
        primary_key=True,
    )
    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": str(self.bundle_id),
            "app_id": str(self.app_id),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TenantBundle(Base):
    """Which bundles a tenant HOLDS (migration 039).

    The grant a tenant buys. Under it sits ONE per-app ``License`` per bundle app, each
    with ``bundle_id`` set (``scripts/provision_tenant_bundle.py``); this row is the
    record that the tenant holds the bundle at all. Constraint names match 039
    exactly so the two schema arms agree (tests/unit/test_schema_parity.py).
    """

    __tablename__ = "tenant_bundle"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "bundle_id", name="tenant_bundle_pkey"),
        Index("idx_tenant_bundle_bundle", "bundle_id"),
    )

    tenant_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_tenant_bundle_tenant"),
        nullable=False,
    )
    bundle_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("bundle.id", ondelete="RESTRICT", name="fk_tenant_bundle_bundle"),
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )


class BundleEnsurer(Base):
    """Which owners may ENSURE a bundle on a tenant they own (migration 052).

    The bundle-ensure route mints a licence for every app in the bundle, so without
    this any owner could obtain another app's single-issuer licence (fr ensuring
    ``commonpractice``). The caller's owner must be listed here (403
    ``bundle_not_ensurable_by_caller``); break-glass (``cross_owner``) bypasses it,
    audited. A bundle with no row is ensurable only through break-glass.
    """

    __tablename__ = "bundle_ensurer"
    __table_args__ = (PrimaryKeyConstraint("bundle_id", "owner_key", name="bundle_ensurer_pkey"),)

    bundle_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("bundle.id", ondelete="CASCADE", name="fk_bundle_ensurer_bundle"),
        nullable=False,
    )
    owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_bundle_ensurer_owner"),
        nullable=False,
    )


class AppRequires(Base):
    """App ``app_id`` calls ``required_app_id`` on the tenant's behalf (migration 059).

    A bundle lists what is SOLD; this edge says what a sold app NEEDS. Ensuring a
    bundle provisions the transitive closure of its apps' requirements as
    DEPENDENCY licences (``License.is_dependency``) — ONE per (tenant, app), shared
    by every bundle whose closure needs the app — and grants ``caller_owner_key``
    (the owner of the minter that mints ``required_app`` for the tenant) on the
    tenant. A cycle is refused at ensure time (``services.app_requires``).
    """

    __tablename__ = "app_requires"
    __table_args__ = (
        PrimaryKeyConstraint("app_id", "required_app_id", name="app_requires_pkey"),
        CheckConstraint("app_id <> required_app_id", name="ck_app_requires_not_self"),
    )

    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE", name="fk_app_requires_app"),
        nullable=False,
    )
    required_app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE", name="fk_app_requires_required_app"),
        nullable=False,
    )
    caller_owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_app_requires_caller_owner"),
        nullable=False,
    )


class BundleCaller(Base):
    """An owner whose service calls ONE of a bundle's own apps for the holding tenant
    (migration 059): fr calls ``workers`` for every tenant holding ``conveyor``.

    Ensure writes a ``TenantDependencyGrant`` scoped to (tenant, owner, app) — unless
    the owner owns the tenant — so that owner's bundle-token minter can mint THAT app
    there and nothing else.
    """

    __tablename__ = "bundle_caller"
    __table_args__ = (
        PrimaryKeyConstraint("bundle_id", "owner_key", "app_id", name="bundle_caller_pkey"),
    )

    bundle_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("bundle.id", ondelete="CASCADE", name="fk_bundle_caller_bundle"),
        nullable=False,
    )
    owner_key = Column(
        String(64),
        ForeignKey("tenant_owner.key", ondelete="RESTRICT", name="fk_bundle_caller_owner"),
        nullable=False,
    )
    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE", name="fk_bundle_caller_app"),
        nullable=False,
    )


class TenantDependencyGrant(Base):
    """An APP-SCOPED grant: ``owner_key``'s minter may mint ``app_id`` on ``tenant_id``
    through bundle-token, and NOTHING else (migration 059).

    WHY A SEPARATE TABLE, NOT ``tenant_app_grant.app_id``. A ``tenant_app_grant`` row is
    read by every owner-scoped path (``get_tenant_scoped``, ``owner_can_access_tenant``,
    ``_authorize_issuance``'s tenant resolution, grant listings, ``_has_active_grant``)
    and means "act on this tenant": read it, issue the grantee's apps, mint any bundle
    app. A nullable scope column would have to be filtered in EVERY one of those
    readers, and one that forgot would silently widen a dependency grant to the full
    grant (fail OPEN). A separate table is fail CLOSED: no existing reader sees it, so
    it can never read the tenant or issue a licence; only the bundle-token route
    consults it, for exactly (tenant, owner, app). Existing ``tenant_app_grant`` rows
    keep their meaning untouched.

    Written by bundle ensure: one row per (edge caller, required app) walked in the
    bundle's ``app_requires`` closure and per ``bundle_caller`` (owner, app), never for
    the tenant's own owner. Removing the row is the revocation.
    """

    __tablename__ = "tenant_dependency_grant"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "owner_key", "app_id", name="tenant_dependency_grant_pkey"
        ),
    )

    tenant_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_tenant_dependency_grant_tenant"),
        nullable=False,
    )
    owner_key = Column(
        String(64),
        ForeignKey(
            "tenant_owner.key", ondelete="RESTRICT", name="fk_tenant_dependency_grant_owner"
        ),
        nullable=False,
    )
    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE", name="fk_tenant_dependency_grant_app"),
        nullable=False,
    )
    granted_by = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )


class TenantAppByoMode(Base):
    """Per-(tenant, product/app) BYO posture override.

    When a row exists it OVERRIDES the tenant-wide ``Tenant.byo_mode`` for that
    product; absent → the tenant-wide value still applies. This lets one tenant
    be, e.g., 'byo_required' for product A but 'platform_only' for product B —
    the foundation for per-product commercial offerings (free/paid access ×
    BYO/markup, resolved per product). Resolved by ``_lookup_byo_mode`` when the
    caller passes an ``app_key``; set via
    ``PUT /tenants/{name_or_id}/apps/{app_key}/byo-mode`` (license-admin).
    """

    __tablename__ = "tenant_app_byo_mode"
    __table_args__ = (
        CheckConstraint(
            "byo_mode IN ('local_allowed', 'byo_required', 'platform_only')",
            name="ck_tenant_app_byo_mode",
        ),
        Index("idx_tenant_app_byo_mode_tenant", "tenant_id"),
    )

    tenant_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE"),
        primary_key=True,
    )
    byo_mode = Column(String(32), nullable=False)
    # WHICH offering produced this posture, when one did. NULL means the posture
    # was set directly (PUT .../byo-mode) or predates migration 025 — NOT "no
    # offering". History cannot be backfilled: the code was never written down.
    # A plain string, not a FK: this is a historical fact about what was applied
    # and must survive the offering being renamed, re-priced or deleted.
    offering_code = Column(String(64), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "app_id": str(self.app_id),
            "byo_mode": self.byo_mode,
            "offering_code": self.offering_code,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Offering(Base):
    """A per-product commercial offering = a reusable usage-policy template.

    An offering bundles, for one product (``app``), the **usage policy** the
    platform charges under: the BYO ``byo_mode`` posture + a ``markup_pct`` and
    absolute per-1M-token / per-request surcharges (mirroring billing_service's
    ``markup_rules``). Provisioning an offering onto a tenant materializes it into
    two stores: the per-(tenant, product) posture row (``tenant_app_byo_mode``,
    local) and the tenant's billing ``markup_rule`` (S2S). This is the
    usage-policy half of the two-axis commercial model. The **access** half — a
    fixed ``access_price_usd`` + ``access_cadence`` (provisioned as a billing
    ``tenant_access_charge``) — makes "free vs paid" real; the included-token
    allowance remains a later phase. Named by ``(app, code)``, e.g.
    ``(w2v-image, 'paid-managed')``.
    """

    __tablename__ = "offering"
    __table_args__ = (
        UniqueConstraint("app_id", "code", name="ux_offering_app_code"),
        CheckConstraint(
            "byo_mode IN ('local_allowed', 'byo_required', 'platform_only')",
            name="ck_offering_byo_mode",
        ),
        CheckConstraint(
            "access_cadence IN ('monthly', 'one_time')",
            name="ck_offering_access_cadence",
        ),
        Index("idx_offering_app", "app_id"),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    app_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("app.id", ondelete="CASCADE"),
        nullable=False,
    )
    code = Column(String(64), nullable=False)  # 'free-high-markup', 'paid-managed', ...
    name = Column(String(255), nullable=False)
    byo_mode = Column(String(32), nullable=False)
    # Usage policy — mirrors billing_service markup_rules / MarkupRuleCreate.
    markup_pct = Column(Numeric(6, 2), nullable=True)  # relative %, NULL = no pct component
    surcharge_input_per_1m_usd = Column(Numeric(20, 8), nullable=True)
    surcharge_output_per_1m_usd = Column(Numeric(20, 8), nullable=True)
    surcharge_cache_creation_per_1m_usd = Column(Numeric(20, 8), nullable=True)
    surcharge_cache_read_per_1m_usd = Column(Numeric(20, 8), nullable=True)
    surcharge_per_request_usd = Column(Numeric(20, 8), nullable=True)
    # Access policy — the ACCESS half of the two-axis model: a fixed per-product
    # access fee, distinct from usage markup. NULL price = a free-access offering.
    # Provisioned onto a tenant as a billing_service tenant_access_charge (S2S).
    access_price_usd = Column(Numeric(20, 8), nullable=True)
    access_cadence = Column(String(16), nullable=False, server_default="monthly")
    # Included-usage allowance (the ALLOWANCE half of the ACCESS axis): a monthly
    # USD credit bundled into the access grant that offsets the tenant's incurred
    # usage BEFORE markup. Provisioned as tenant_access_charge.included_cost_usd.
    included_allowance_usd = Column(Numeric(20, 8), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    app = relationship("App")

    def to_dict(self) -> dict[str, Any]:
        def _num(v: Any) -> float | None:
            return float(v) if v is not None else None

        return {
            "id": str(self.id),
            "app_id": str(self.app_id),
            "app_key": self.app.key if self.app else None,
            "code": self.code,
            "name": self.name,
            "byo_mode": self.byo_mode,
            "markup_pct": _num(self.markup_pct),
            "surcharge_input_per_1m_usd": _num(self.surcharge_input_per_1m_usd),
            "surcharge_output_per_1m_usd": _num(self.surcharge_output_per_1m_usd),
            "surcharge_cache_creation_per_1m_usd": _num(self.surcharge_cache_creation_per_1m_usd),
            "surcharge_cache_read_per_1m_usd": _num(self.surcharge_cache_read_per_1m_usd),
            "surcharge_per_request_usd": _num(self.surcharge_per_request_usd),
            "access_price_usd": _num(self.access_price_usd),
            "access_cadence": self.access_cadence,
            "included_allowance_usd": _num(self.included_allowance_usd),
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def effective_metering_values(
    token_budget_override: int | None,
    quota_override: int | None,
    plan_token_budget_monthly: int | None,
    plan_quota_monthly: int | None,
) -> tuple[int | None, int | None]:
    """THE rule for what a licence owes billing: ``(token_budget, quota)``.

    A licence's own override (migration 041, #239) wins when it is not None; NULL
    falls back to the plan's value. 0 is a real BLOCK-ALL value on either side and is
    returned as 0 — never collapsed into "unset". A result of None means unlimited
    (owes nothing on that leg), which only an unlimited PLAN can produce.

    Scalar form so the reconcile sweep (which works on selected tuples, not ORM rows)
    and ``_provision_budget`` (which snapshots scalars before releasing its session)
    share ONE implementation with ``effective_metering``.
    """
    budget = (
        token_budget_override if token_budget_override is not None else plan_token_budget_monthly
    )
    quota = quota_override if quota_override is not None else plan_quota_monthly
    return budget, quota


def effective_metering(license: Any, plan: Any) -> tuple[int | None, int | None]:
    """``effective_metering_values`` over a ``License`` and its ``Plan`` row."""
    return effective_metering_values(
        license.token_budget_override,
        license.quota_override,
        plan.token_budget_monthly,
        plan.quota_monthly,
    )


class Plan(Base):
    """Per-app plan definitions (test/trial/full, possibly region-scoped)"""

    __tablename__ = "plan"
    __table_args__ = (
        # PROD's shape, not the obvious one. A plain UNIQUE(app_id, code,
        # region_id) does NOT constrain global plans: region_id is NULL for them
        # and NULLs never collide in a Postgres unique index, so a fresh database
        # would ACCEPT two global plans sharing (app, code) while prod REFUSES
        # them. init.sql:67 has always used the COALESCE form; the ORM did not,
        # and #129 measured the divergence.
        #
        # Aligned ORM -> prod, never prod -> ORM. Verified before changing it:
        # prod holds ZERO duplicate global plans (0 of 88), and no ON CONFLICT
        # anywhere infers (app_id, code, region_id), so no upsert loses its
        # inference target by this becoming an expression index.
        Index(
            "ux_plan_app_code_region",
            "app_id",
            "code",
            text("COALESCE(region_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
        # quota_monthly semantics: NULL = unlimited, 0 = block-all, >0 = cap.
        # Negatives are invalid — rejected at the API (Field ge=0) and here as
        # defence in depth so a bad write can never land a negative quota.
        CheckConstraint(
            "quota_monthly IS NULL OR quota_monthly >= 0",
            name="ck_plan_quota_monthly_nonneg",
        ),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    app_id = Column(PGUUID(as_uuid=True), ForeignKey("app.id", ondelete="CASCADE"), nullable=False)
    code = Column(String(50), nullable=False)  # 'test', 'trial', 'full'
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    region_id = Column(
        PGUUID(as_uuid=True), ForeignKey("region.id"), nullable=True
    )  # NULL = global plan
    duration_days = Column(Integer, nullable=True)  # NULL = no automatic expiry
    # Rate limiting fields for Envoy rate limit service
    rate_limit_rpm = Column(
        Integer, nullable=False, default=60, server_default=text("60")
    )  # requests per minute
    rate_limit_burst = Column(
        Integer, nullable=False, default=10, server_default=text("10")
    )  # burst allowance
    quota_monthly = Column(Integer, nullable=True)  # optional hard cap per month (NULL = unlimited)
    token_budget_monthly = Column(
        Integer, nullable=True
    )  # optional monthly LLM token budget (NULL = unlimited)
    revoked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )

    # Relationships
    app = relationship("App")
    region = relationship("Region")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "app_id": str(self.app_id),
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "region_id": str(self.region_id) if self.region_id else None,
            "duration_days": self.duration_days,
            "rate_limit_rpm": self.rate_limit_rpm,
            "rate_limit_burst": self.rate_limit_burst,
            "quota_monthly": self.quota_monthly,
            "token_budget_monthly": self.token_budget_monthly,
            "revoked": self.revoked,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class License(Base):
    """Subject's entitlement to use an app under a plan"""

    __tablename__ = "license"
    __table_args__ = (
        Index("idx_license_subject", "subject_id"),
        Index("idx_license_app", "app_id"),
        Index("idx_license_status", "status"),
        Index("idx_license_expires_at", "expires_at"),
        Index("idx_license_tenant", "tenant_id"),
        # migration 039: the bundle a bundle licence speaks for (NULL on every other row).
        Index("idx_license_bundle", "bundle_id"),
        # At most one ACTIVE license per (tenant, app, subject). Prevents the
        # duplicate-active minting seen via portal-bff; create_user_license is
        # idempotent against this key. Partial index → only active rows compete;
        # revoked/expired history is unconstrained.
        Index(
            "uq_license_active_tenant_app_subject",
            "tenant_id",
            "app_id",
            "subject_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        # Measured on prod 2026-09-07: all 258 rows hold 'active' (164), 'revoked'
        # (92) or 'expired' (2). `pending` is a FOURTH documented value (the
        # column comment below, and effective_status()'s own docstring) — no
        # stored row has ever held it (nothing writes it; see
        # tests/unit/test_effective_status_contract.py::TestStoredPendingIsTheIntendedImprovement),
        # but it is named as an intended future stored state, not a stale
        # leftover, so excluding it here would foreclose that direction for no
        # reason and would also break
        # test_models.py::TestLicenseModel::test_license_is_active_status_logic,
        # which persists it today to pin is_active()'s behaviour on an
        # unrecognised-but-anticipated status.
        CheckConstraint(
            "status IN ('active', 'revoked', 'expired', 'pending')",
            name="ck_license_status",
        ),
        # migration 041 (#239): same `IS NULL OR >= 0` shape as ck_plan_quota_monthly_nonneg.
        CheckConstraint(
            "token_budget_override IS NULL OR token_budget_override >= 0",
            name="ck_license_token_budget_override_nonneg",
        ),
        CheckConstraint(
            "quota_override IS NULL OR quota_override >= 0",
            name="ck_license_quota_override_nonneg",
        ),
        # migration 059: a dependency licence is shared by every bundle whose closure
        # needs the app, so it never names one bundle.
        CheckConstraint(
            "NOT is_dependency OR bundle_id IS NULL",
            name="ck_license_dependency_unbundled",
        ),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    subject_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("subject.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id = Column(PGUUID(as_uuid=True), ForeignKey("app.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(
        PGUUID(as_uuid=True), ForeignKey("plan.id", ondelete="RESTRICT"), nullable=False
    )
    region_id = Column(PGUUID(as_uuid=True), ForeignKey("region.id"), nullable=True)
    # Real FK to the canonical tenant registry (migration 016). Retyped from a
    # free-form VARCHAR(255) with no FK: an unprovisioned or wrong (e.g.
    # fr-user-id-shaped) tenant_id can no longer be persisted. ON DELETE RESTRICT:
    # a tenant with live licenses cannot be deleted out from under them. Mirrors
    # TenantAppByoMode.tenant_id.
    #
    # The constraint is NAMED, and the name is load-bearing. Unnamed, create_all
    # emits PostgreSQL's default `license_tenant_id_fkey`, while prod carries
    # `fk_license_tenant` (migration 016 chose it) — so the two schemas differed
    # by constraint name, and 016's own idempotency guard
    # (`WHERE conname = 'fk_license_tenant'`) reads FALSE on a create_all'd
    # database. Any future change that lets the migrations replay on a fresh
    # schema would therefore add a SECOND, duplicate FK. Naming it here makes
    # fresh and prod identical and makes that guard correct on both.
    # NO ``index=True`` here, deliberately. ``__table_args__`` above already
    # declares Index("idx_license_tenant", "tenant_id"), which is the index PROD
    # actually has (measured on the primary 2026-08-12). ``index=True`` emits a
    # SECOND, identical btree named ``ix_license_tenant_id`` — so a FRESH database
    # got two copies of the same index on the hottest column of the hottest table
    # while prod had one, because create_all(checkfirst=True) skips existing
    # tables wholesale and never added it to prod.
    tenant_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT", name="fk_license_tenant"),
        nullable=False,
    )
    # migration 039. The bundle a PER-APP licence was provisioned under by
    # scripts/provision_tenant_bundle.py (one licence per (tenant, bundle app)); NULL = an
    # ordinary licence outside any bundle. Provisioning/billing grouping ONLY: introspect
    # never widens a token beyond this licence's own `app`.
    # Named FK for the same reason as fk_license_tenant above (arm parity).
    bundle_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("bundle.id", ondelete="RESTRICT", name="fk_license_bundle"),
        nullable=True,
    )
    # migration 041 (#239). THIS licence's monthly token budget / request quota,
    # pushed to billing instead of the plan's. NULL = the plan's value. Durable on
    # purpose: the reconcile sweep re-pushes an absent billing row from here, so an
    # override that lived only in the provisioning push was replaced by the PLAN
    # value on the first repair (raising a pooled tenant spend cap). Always read
    # through ``effective_metering`` — never ``plan.*`` directly — when deciding
    # what to push or what counts as drift.
    token_budget_override = Column(Integer, nullable=True)
    quota_override = Column(Integer, nullable=True)
    # migration 059. TRUE = provisioned by bundle ensure only because a bundled app
    # REQUIRES this app (``app_requires``): internal-only. Services mint it through
    # bundle-token; user token issuance (``POST /licenses/{id}/tokens``,
    # ``regenerate-token``) refuses it, so a tenant cannot call the dependency
    # directly. A column, not ``metadata.dependency``: ``metadata`` is caller-written
    # on ``POST /licenses/user``, and a guard must not read a caller's claim.
    # Cleared when a bundle that SELLS the app adopts the licence.
    is_dependency = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    status = Column(String(50), nullable=False)  # 'active', 'revoked', 'expired', 'pending'
    starts_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)  # NULL = perpetual
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=text("now()"),
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    license_metadata = Column("metadata", JSONB, nullable=True)
    # When licenses→billing metering provisioning last COMPLETED for this row
    # (migration 018). NULL = it never completed, i.e. this licence may be
    # UNMETERED in billing. Written by main._provision_budget only after every
    # push the plan owes has been accepted (a plan owing nothing — NULL quota
    # AND NULL budget — counts as complete), and by the reconciler.
    #
    # ⚠️ NOT a coverage signal, and migration 018's own comment overstated this.
    # 018 stamped every pre-existing row inside the same guard that created the
    # column, so a non-NULL value here can mean either "we observed a successful
    # push" or "the migration baselined this row sight unseen". Measured against
    # the prod licences DB on 2026-08-10: 205 of 208 rows carry the single
    # migration-transaction timestamp 2026-08-09 20:30+00, and only 3 stamps
    # were ever genuinely observed. Roughly half the baselined, budget-owing
    # licences had NO billing.token_budgets row while this column called them
    # provisioned. Use metering_verified_at for coverage.
    metering_provisioned_at = Column(DateTime(timezone=True), nullable=True)
    # When licences last ASKED billing what it holds for this row and found every
    # config the plan owes already present (migration 019). NULL = never
    # verified. Unlike metering_provisioned_at this column was never baselined,
    # so it starts NULL for the whole estate and cannot inherit 018's false
    # "already provisioned" claim. Written only by the reconcile sweep, and only
    # after billing itself answered — never on a dry run, and never when billing
    # failed to answer.
    #
    # The unmetered-licence question, honestly:
    #   SELECT l.id FROM license l JOIN plan p ON p.id = l.plan_id
    #    WHERE l.status = 'active'
    #      AND (p.quota_monthly IS NOT NULL OR p.token_budget_monthly IS NOT NULL)
    #      AND l.metering_verified_at IS NULL;
    metering_verified_at = Column(DateTime(timezone=True), nullable=True)

    # ── Soft delete (migration 036) ───────────────────────────────────────────
    # ⛔ THESE FOUR EXIST SO A CLOSED INVOICE STAYS REPRODUCIBLE. The hard delete
    # behind `DELETE /licenses/user/{subject_id}` destroyed the row (and its
    # tokens, via ON DELETE CASCADE) with no record at all, while the seat-count
    # endpoint billing bills from admits in its own `as_of` description that it
    # "cannot reconstruct a licence that has since been deleted". Retaining the
    # row with a deletion timestamp is what closes that.
    #
    # ⚠️ CORRECTED — this used to say all four are NULL on every row "until
    # `LICENSE_SOFT_DELETE` is armed", which described the world where the flag
    # defaulted False. It now defaults **True**, so these four are populated by
    # every delete and `effectively_active_clauses` no longer reduces to
    # `status = 'active'`. Rows deleted before the flip are still gone.
    # See migration 036 for why `deleted_from_status` is not redundant (the
    # partial unique index forces `status` to change).
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(Text, nullable=True)
    delete_reason = Column(Text, nullable=True)
    deleted_from_status = Column(String(50), nullable=True)

    # Relationships
    subject = relationship("Subject")
    app = relationship("App")
    plan = relationship("Plan")
    region = relationship("Region")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "subject_id": str(self.subject_id),
            "tenant_id": str(self.tenant_id),  # UUID column (migration 016) → keep JSON a string
            "app_id": str(self.app_id),
            "plan_id": str(self.plan_id),
            "region_id": str(self.region_id) if self.region_id else None,
            "status": self.status,
            # STRUCTURAL status resolved against the time window. `status` alone
            # OVERSTATES validity — nothing in the estate transitions a row out
            # of 'active' when expires_at passes — so every consumer that needed
            # the real answer was re-deriving it. Two already are:
            # provisioning `src/entitlements.py::is_active_license` reads this
            # key first (its comment: "absent today, adopted automatically
            # later") and fr `backend/src/lib/license-status.ts` re-implements
            # the rule by hand. Emitting it here makes this the single source.
            #
            # ⚠️ This is NOT a passive field add: the moment it appears,
            # provisioning switches from `status in (None, "", "active")` to
            # `effective_status in ("active", "pending")`. Those verdicts agree
            # across every reachable row — pinned by
            # tests/unit/test_effective_status_contract.py, which asserts the
            # agreement rather than just the field's presence.
            "effective_status": self.effective_status(),
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "metadata": self.license_metadata,
            "metering_provisioned_at": (
                self.metering_provisioned_at.isoformat() if self.metering_provisioned_at else None
            ),
            "token_budget_override": self.token_budget_override,
            "quota_override": self.quota_override,
            # migration 059 / 039: a caller offboarding a tenant tells a shared
            # DEPENDENCY licence (released automatically) from its bundle's own.
            "is_dependency": bool(self.is_dependency),
            "bundle_id": str(self.bundle_id) if self.bundle_id is not None else None,
            "metering_verified_at": (
                self.metering_verified_at.isoformat() if self.metering_verified_at else None
            ),
        }

    def effective_status(self) -> str:
        """EFFECTIVE (time-aware) validity: ``active`` | ``pending`` | ``expired`` |
        ``revoked``.

        The stored ``status`` column is only STRUCTURAL validity — nothing in the
        estate transitions a row out of ``'active'`` when ``expires_at`` passes
        (see ``DatabaseManager.expire_stale_licenses``), so ``status`` alone
        overstates validity. This is the single place that resolves the stored
        status against the time window, so callers that need to explain WHY a
        license is unusable do not re-derive the rule.

        Resolution order for a stored ``'active'`` row: ``pending`` (starts_at in
        the future) is reported before ``expired``, so a row that is somehow both
        reads as ``pending`` — the earlier defect in its own lifecycle.

        A stored status outside the known set is returned VERBATIM rather than
        normalised, so an unexpected value can never be laundered into something
        that reads as valid.
        """
        stored = str(self.status)
        if stored != "active":
            return stored
        now = datetime.now(UTC)
        if self.starts_at and self.starts_at > now:
            return "pending"
        if self.expires_at and self.expires_at < now:
            return "expired"
        return "active"

    def is_active(self) -> bool:
        """Check if license is currently active.

        Expressed in terms of ``effective_status()`` so the two can never drift.
        Behaviourally IDENTICAL to the previous hand-rolled implementation
        (status == 'active' AND starts_at <= now AND (expires_at is NULL or
        > now)) — token introspection returns ``license_inactive`` off this, so
        a behaviour change here would silently re-authorize or de-authorize every
        caller. Pinned by a parametrised test that checks this method, the legacy
        expression, and ``effective_status() == "active"`` all agree across the
        full state matrix.
        """
        return self.effective_status() == "active"

    def accepts_new_token(self) -> bool:
        """May a NEW token be minted on this licence (``POST /licenses/{id}/tokens``)?

        Deliberately WIDER than ``is_active()`` by exactly one case: a licence that
        is ``pending`` only because ``starts_at`` is in the future. Everything else
        that ``is_active()`` refuses is refused here too.

        True iff ALL of:

        * not soft-deleted (``deleted_at IS NULL``) — the soft delete also writes
          ``status='revoked'``, so this is belt-and-braces, not the only fence;
        * stored ``status == 'active'`` — so ``revoked``, ``expired``,
          ``suspended`` and any unknown value are refused verbatim;
        * not already past ``expires_at`` — a stored-``active`` row whose window
          has closed is effectively ``expired``.

        ``starts_at`` is deliberately NOT consulted. provisioning
        (``provisioning/src/licensing.py`` step 5, and ``src/orchestration.py``)
        mints its one-time raw token through this route immediately after
        ``create_license``, with a CALLER-SUPPLIED ``starts_at``, and chose this
        route over ``regenerate-token`` precisely because rotation 409s on
        ``pending``. Refusing pending here would break every forward-dated grant.
        A token minted on a pending licence is harmless before the start date:
        introspection still answers ``license_inactive`` off ``is_active()``
        until ``starts_at`` passes.
        """
        if self.deleted_at is not None:
            return False
        if str(self.status) != "active":
            return False
        return not (self.expires_at and self.expires_at < datetime.now(UTC))


# The TOTAL ordering used to resolve (tenant_id, app_key) -> ONE license.
#
# Defined once, at module level, and applied in SQL rather than in Python: every
# replica of this service must provably sort an identical candidate set into an
# identical sequence. Before this existed the query had NO ``ORDER BY`` at all,
# so ``licenses[0]`` was PostgreSQL heap order — which changes after any UPDATE,
# VACUUM or restore, and differs between replicas of the same data.
#
# 1. ``expires_at DESC NULLS FIRST`` — perpetual first, then the furthest-out
#    expiry. This is the only ordering that is MONOTONE UNDER TIME: any
#    created_at-first ordering lets the chosen license expire underneath the
#    caller while a longer-lived sibling sits unchosen, producing an intermittent
#    402 that reproduces once per license lifetime and never in a test.
# 2. ``created_at ASC`` — prefer the INCUMBENT, whose token, budget row and
#    metering history already exist, over a duplicate minted five minutes ago.
# 3. ``id ASC`` — makes the ordering TOTAL. ``created_at`` has a PYTHON-side
#    default, so a seed script or a batched provision run can produce
#    byte-identical timestamps; without this clause those rows fall back to heap
#    order. ``id`` is uuid7, so this tiebreak is also creation-ordered.
ACTIVE_LICENSE_PICK_ORDER = (
    License.expires_at.desc().nullsfirst(),
    License.created_at.asc(),
    License.id.asc(),
)


def effectively_active_clauses(as_of: datetime) -> tuple[Any, ...]:
    """The ONE definition of "this licence is a seat at ``as_of``", as SQL.

    ⛔ WHY IT IS A FUNCTION AND NOT TWO COPIES OF AN EXPRESSION. Two endpoints
    that billing reads FOR THE SAME INVOICE disagreed about what an active
    licence is:

      * ``GET /internal/tenants/{id}/seat-count`` applied the three-part rule
        below (stored status AND started AND unexpired) against ``as_of``;
      * ``GET /internal/licenses?status=active`` filtered the STORED ``status``
        column alone.

    So one row that reads ``status='active'`` with ``expires_at`` in the past was
    a seat on one endpoint and not on the other, and billing had no way to know
    which number it was holding. ``status`` is not validity: NOTHING in the estate
    transitions the column when ``expires_at`` passes (see
    ``expire_stale_licenses``), so the stored value drifts permanently and only
    ever in the over-counting direction.

    This is the SQL twin of ``License.effective_status`` / ``License.is_active``,
    with ONE deliberate difference: those evaluate against ``now()`` in Python on
    an already-loaded row, while this evaluates in the database against a
    caller-supplied instant, because a CLOSED billing period must be counted at
    its own instant and not at whenever the query was re-run.

    ⚠️ ``starts_at IS NULL`` and ``expires_at IS NULL`` are tolerated on PURPOSE
    and they are not the same tolerance: a NULL ``expires_at`` is a PERPETUAL
    licence (a real, common state — see ``ACTIVE_LICENSE_PICK_ORDER``), while a
    NULL ``starts_at`` is an unstated start, which ``effective_status`` also
    treats as "not pending". ⚠️ ``get_active_license`` deliberately does NOT use
    this helper: it filters ``starts_at <= now`` WITHOUT the NULL arm, so
    adopting this would silently WIDEN the token/ext_authz entitlement path. That
    is a live authorization surface and is out of this predicate's scope; the two
    sites that bill are what must agree.

    ⭐ IT ALSO HONOURS THE SOFT DELETE (migration 036), which is what makes a
    CLOSED period's count reproducible after a licence is removed. A row deleted
    AFTER ``as_of`` was still a seat at ``as_of``; one deleted before it was not.
    ⚠️ That arm needs ``deleted_from_status``, not just the timestamp: the soft
    delete sets ``status='revoked'`` to free the partial unique index, so the
    CURRENT status of a deleted row says nothing about what it was. Reading the
    timestamp alone would count a licence that had already been revoked before
    anyone deleted it.

    ⚠️ CORRECTED — THE DELETE ARM IS NOW LIVE. This paragraph used to read "ON
    TODAY'S DATA THE DELETE ARM IS INERT … ``LICENSE_SOFT_DELETE`` defaults off, so
    ``deleted_at`` is NULL on every row", and that was true when the predicate was
    written: the flag defaulted **False**, every delete destroyed its row, and the
    expression reduced to ``status = 'active'`` plus the window. The flag now
    defaults **True**, so ``DELETE /licenses/user/{subject_id}`` RETAINS and stamps
    the row and this arm decides real rows on every call. Writing the predicate for
    the armed world ahead of time is exactly what made arming the flag a config
    change and not also a query change — but do not read this helper any more as if
    the second arm were unreachable, and do not "simplify" it away on the grounds
    that ``deleted_at`` is always NULL. It is not, from this change onward.
    ⚠️ Rows deleted BEFORE the flip are still gone and cannot be reconstructed by
    any ``as_of``; the reproducibility guarantee starts at the flip, not before it.

    Returned as a tuple of clauses (not a single ``and_``) so a caller can splice
    them into an existing ``.filter(...)`` alongside its own predicates without a
    nesting level, which is how both call sites use it.
    """
    return (
        # "was stored-active AT as_of". Two arms, because a soft-deleted row's
        # CURRENT status is 'revoked' by construction.
        or_(
            and_(License.deleted_at.is_(None), License.status == "active"),
            and_(
                License.deleted_at.isnot(None),
                License.deleted_at > as_of,
                License.deleted_from_status == "active",
            ),
        ),
        # Not `pending`: a licence that has not started is not a seat.
        or_(License.starts_at.is_(None), License.starts_at <= as_of),
        # Not `expired`. ⚠️ Measured 2026-09-10 there were ZERO
        # stored-active-but-past-expiry rows, so this clause cannot currently
        # change any answer — it is here because nothing transitions the column
        # and the day one expires is the day a count without it over-bills.
        or_(License.expires_at.is_(None), License.expires_at > as_of),
    )


def active_during_clauses(period_start: datetime, period_end: datetime) -> tuple[Any, ...]:
    """ "This licence was a seat at SOME instant in ``[period_start, period_end)``", as SQL.

    ⛔ DELIBERATELY NOT A PARAMETER ON ``effectively_active_clauses``. That predicate is
    shared with ``/internal/licenses`` and pinned by ``test_seat_predicate_is_shared``;
    widening it would silently widen what billing reads elsewhere. A period is a
    different question ("at any instant in", not "at"), so it gets its own function
    that only the period seat count uses.

    ⭐ WHY A PERIOD AND NOT AN INSTANT. Per-seat billing (CommonPractice) bills
    "distinct employees who held an active licence at any point in the period": a
    mid-month joiner and a mid-month leaver each bill one seat. A point-in-time count at
    period end misses the leaver, and at period start misses the joiner.

    ⚠️ THE REVOCATION ARM IS THE REASON THIS EXISTS. ``effectively_active_clauses``
    excludes a revoked row by its CURRENT ``status``, so a member revoked on the 20th
    is not counted even for the 1st. Here a row revoked AFTER ``period_start`` was
    active during the period. That is only reconstructible because ``revoked_at`` is
    stamped on the FIRST transition only (``revoke_license``) — a rewritten timestamp
    would move the seat out of the period it was held in.

    ⚠️ There is still no licence history table: a row revoked BEFORE the period, then
    re-activated in place, reads as active throughout. Billing freezes the count it
    receives into the invoice snapshot; that frozen number is the historical record.
    """
    return (
        # Existed during the period.
        License.created_at < period_end,
        # Started before the period ended.
        or_(License.starts_at.is_(None), License.starts_at < period_end),
        # Had not expired before the period began.
        or_(License.expires_at.is_(None), License.expires_at > period_start),
        or_(
            # Live row, still (or stored-expired, which the window above bounds).
            and_(License.deleted_at.is_(None), License.status.in_(("active", "expired"))),
            # Live row revoked during or after the period: it was a seat until then.
            and_(
                License.deleted_at.is_(None),
                License.status == "revoked",
                License.revoked_at.isnot(None),
                License.revoked_at > period_start,
            ),
            # Soft-deleted during or after the period, from a state that was a seat.
            and_(
                License.deleted_at.isnot(None),
                License.deleted_at > period_start,
                License.deleted_from_status == "active",
            ),
            # Soft-deleted after a revocation that itself fell inside the period.
            and_(
                License.deleted_at.isnot(None),
                License.deleted_from_status == "revoked",
                License.revoked_at.isnot(None),
                License.revoked_at > period_start,
            ),
        ),
    )


class Token(Base):
    """API tokens used by clients/nodes to call the license API"""

    __tablename__ = "token"
    __table_args__ = (
        # A bare unique INDEX, not a UniqueConstraint — the same shape mismatch
        # this file just fixed for ux_plan_app_code_region, and found by the same
        # measurement. init.sql:115 declares
        #   CREATE UNIQUE INDEX ux_token_license_hash ON token(license_id, token_hash)
        # and prod carries it that way: 1 row in pg_indexes, ZERO in pg_constraint
        # (measured on the primary 2026-08-12). A UniqueConstraint additionally emits a
        # pg_constraint row, so a fresh database grew a catalog object prod does
        # not have. Same uniqueness either way; this makes fresh and prod identical.
        Index("ux_token_license_hash", "license_id", "token_hash", unique=True),
        Index("idx_token_license", "license_id"),
        Index("idx_token_revoked", "revoked"),
        Index("idx_token_expires_at", "expires_at"),
        # Introspection filters on token_hash ALONE (see introspect_token); the
        # composite ux_token_license_hash cannot serve a hash-only lookup
        # (wrong leading column) so without this the hot path is a seq scan that
        # grows O(n) with token count and trips caller introspect timeouts under
        # burst load. Standalone hash index -> index scan.
        Index("idx_token_hash", "token_hash"),
        # Measured on prod 2026-09-07: 'api' (176) and 'internal' (74), zero
        # 'node' rows. 'node' is NOT excluded for that reason: `main.TokenType`
        # (a StrEnum) declares it and `POST /licenses/{id}/tokens?token_type=node`
        # is a live, reachable route that persists it — zero rows today only
        # means nobody has minted one yet. All three match the column comment
        # below.
        CheckConstraint(
            "token_type IN ('api', 'node', 'internal')",
            name="ck_token_token_type",
        ),
        # ⛔ migration 038. `granted_roles` is the ONLY place a licence token can carry a
        # role, and these two CHECKs make a mis-mint unrepresentable: the only grantable
        # role is `platform-admin`, and a token carrying one must expire no later than
        # created_at + 65 min (POST /licenses/{id}/operator-token caps at 60; 5 min of
        # clock skew). The authority is the caller's own Keycloak JWT at mint time — see
        # `OPERATOR_GRANTABLE_ROLES` in main.py.
        CheckConstraint(
            "granted_roles <@ ARRAY['platform-admin']::text[]",
            name="ck_token_granted_roles_allowlist",
        ),
        CheckConstraint(
            "cardinality(granted_roles) = 0 OR (expires_at IS NOT NULL AND "
            "expires_at <= created_at + interval '65 minutes')",
            name="ck_token_granted_roles_short_lived",
        ),
        # ⛔ migration 044 (licenses#244). A REGISTERED long-lived token names an owner
        # and a reason, is perpetual, and carries no role. Both columns NULL is every
        # ordinary token AND every legacy perpetual row (unregistered; untouched).
        CheckConstraint(
            "(long_lived_owner IS NULL AND long_lived_reason IS NULL) OR ("
            "expires_at IS NULL AND cardinality(granted_roles) = 0 "
            "AND length(btrim(long_lived_owner)) > 0 "
            "AND length(btrim(long_lived_reason)) > 0)",
            name="ck_token_long_lived_registered",
        ),
    )

    id = Column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
        server_default=text("gen_random_uuid()"),
    )
    license_id = Column(
        PGUUID(as_uuid=True),
        ForeignKey("license.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash = Column(String(128), nullable=False)  # SHA-256 hash of raw token
    token_type = Column(String(50), nullable=False)  # 'api', 'node', 'internal'
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=text("now()")
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)  # NULL = until revoked
    revoked = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # Roles this ONE token carries on /license/introspect (migration 038). Empty for every
    # ordinary token; see the two CHECKs in __table_args__.
    granted_roles = Column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # The long-lived register (migration 044): set ONLY by a mint that registered a
    # perpetual token. NULL on every legacy perpetual row — those are unregistered.
    long_lived_owner = Column(Text, nullable=True)
    long_lived_reason = Column(Text, nullable=True)

    # Relationships
    license = relationship("License")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "license_id": str(self.license_id),
            "token_type": self.token_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "last_used_at": (self.last_used_at.isoformat() if self.last_used_at else None),
        }

    def is_valid(self) -> bool:
        """Check if token is currently valid"""
        if self.revoked:
            return False
        if self.expires_at and self.expires_at < datetime.now(UTC):
            return False
        return True

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Hash a raw token using SHA-256"""
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @staticmethod
    def generate_token() -> str:
        """Generate a new random token"""
        return secrets.token_urlsafe(32)


def _granted_roles(token: Token) -> list[str]:
    """The token's granted roles as a plain ``list[str]`` — FAIL CLOSED on any odd value.

    Only strings survive; a NULL (impossible under the NOT NULL column, but a row built in
    memory by a test or a pre-038 ORM object can lack it) is ``[]``.
    """
    raw = getattr(token, "granted_roles", None) or []
    return [r for r in raw if isinstance(r, str) and r]


# The TOTAL ordering used to resolve ONE raw token -> ONE token row.
#
# A raw token is SUPPOSED to identify exactly one row, but nothing in the schema
# says so: the only UNIQUE index is ``ux_token_license_hash (license_id,
# token_hash)`` and ``idx_token_hash`` is NON-unique, so the same hash can — and
# in production DOES — sit on several licences at once (measured on the primary
# 2026-08-19: three hashes spread over 7 unrevoked, perpetual rows across
# 2–3 active licences each, all in one tenant). Every lookup below was a bare
# ``.first()`` with no ``ORDER BY``, i.e. PostgreSQL heap order, which changes
# after any UPDATE/VACUUM/restore and differs between replicas of the same data.
# ``_introspect_from_session`` then UPDATEs ``last_used_at`` on whichever row it
# landed on, so the ordering actively churns and two replicas can answer the same
# request with DIFFERENT licences — different app, plan, rate limit, quota and
# per-app BYO override.
#
# This is the token-level sibling of ``ACTIVE_LICENSE_PICK_ORDER`` and shares its
# top-level rationale — define the order ONCE, at module level, and apply it in
# SQL so every replica sorts an identical candidate set into an identical
# sequence. The CLAUSES deliberately invert that constant's preference, because
# the two situations are not the same:
#
#   * For a license, picking the longest-lived candidate is right: duplicates are
#     ordinary, and choosing a dying row 402s a paying customer.
#   * For a token, sharing a hash across licences is itself an ANOMALY. The safe
#     resolution of an anomaly is the LEAST capability, and — decisively — it
#     must be FAIL-CLOSED under revocation: once an operator has killed any row
#     carrying a leaked hash, no ordering may hand back a live twin.
#
# 1. ``revoked DESC`` — a revoked row wins. If ANY replica of this hash has been
#    revoked, introspection resolves to it and answers inactive, so a partially
#    revoked leak cannot authenticate through the surviving twin.
# 2. ``expires_at ASC NULLS LAST`` — soonest expiry first, perpetual last: the
#    least capability, and an already-expired row is preferred for the same
#    fail-closed reason as (1).
# 3. ``created_at ASC`` — prefer the INCUMBENT, whose license, budget row and
#    metering history already exist, over a duplicate minted later.
# 4. ``id ASC`` — makes the ordering TOTAL. ``created_at`` has a PYTHON-side
#    default, so a seed or batched provision run can produce byte-identical
#    timestamps; without this those rows fall back to heap order. ``id`` is
#    uuid7, so this tiebreak is also creation-ordered.
TOKEN_HASH_PICK_ORDER = (
    Token.revoked.desc(),
    Token.expires_at.asc().nullslast(),
    Token.created_at.asc(),
    Token.id.asc(),
)


def _hash_prefix(token_hash: str) -> str:
    """First 8 chars of a token HASH, for logs.

    The hash is not the credential, but it is derived from it and is the value
    stored in the ``token`` table, so it is treated as sensitive: enough to
    correlate a warning with a row, not enough to be useful on its own.
    """
    return token_hash[:8]


def _warn_on_hash_multiplicity(rows: list[Token], token_hash: str, site: str) -> None:
    """Make token-hash multiplicity VISIBLE.

    Before this, nothing anywhere said it was happening: the duplicates were
    found only by querying the primary by hand. A hash on more than one row means
    (a) revocation of that credential is not a single write, and (b) which
    entitlement it carries is decided by an ordering rather than by identity —
    both worth an operator's attention every time they are exercised.

    WARNING, not ERROR: the request still resolves deterministically and is
    served, so this is a standing data defect to be reconciled, not a failure of
    this call.
    """
    if len(rows) > 1:
        logger.warning(
            "token_hash resolves to %d token rows (site=%s hash_prefix=%s license_ids=%s). "
            "The same raw token exists on multiple licences; resolution is decided by "
            "TOKEN_HASH_PICK_ORDER, and revoking it requires writing every row.",
            len(rows),
            site,
            _hash_prefix(token_hash),
            [str(row.license_id) for row in rows],
        )


# The independent stale-licence count backing ``expire_all_stale_licenses``'
# anti-vacuity and over-fire guards (see ``DatabaseManager.count_stale_licenses``
# and ``scripts/expire_stale_licences.py``).
#
# ⚠️ HAND-WRITTEN SQL ON PURPOSE, and it must NOT be refactored into a call to
# the sweep's own ORM query. The guards exist to catch that query drifting; two
# expressions of the same rule can disagree, one expression compared with itself
# never can.
_STALE_LICENSE_COUNT_SQL = """
SELECT count(*)
  FROM license
 WHERE status = 'active'
   AND expires_at IS NOT NULL
   AND expires_at < :now
"""


#: The independent second opinion behind the token sweep's guards
#: (``scripts/revoke_expired_tokens.py``). Hand-written ON PURPOSE — see
#: ``count_expired_unrevoked_tokens``.
#:
#: "revoked" and "expired" are DIFFERENT things here, and that is the whole defect:
#: `revoked` is a flag this service sets, `expires_at` is a clock. Nothing ever moved
#: a token from the second state to the first, so expired-but-unrevoked rows accumulate
#: — the exact population that made rotation mint a dead credential (#135).
_EXPIRED_TOKEN_COUNT_SQL = """
SELECT count(*)
  FROM token
 WHERE revoked = false
   AND expires_at IS NOT NULL
   AND expires_at < :now
"""


#: The independent second opinion behind the licence REAPER's guards
#: (``scripts/reap_expired_licences.py``). Hand-written ON PURPOSE, exactly like the
#: two above — see ``count_stale_licenses``.
#:
#: ⚠️ THIS IS A DIFFERENT POPULATION FROM ``_STALE_LICENSE_COUNT_SQL``, and confusing
#: the two is the dangerous mistake. That one selects ``status = 'active'`` rows the
#: clock has killed — LIVE entitlements, retired by ``expire_stale_licences.py``. This
#: one selects rows ALREADY in the terminal ``'expired'`` state, long past their
#: window, and archives them. It can never touch an active or a perpetual licence:
#:
#:   * ``status = 'expired'``    — the row is already terminal. An ``'active'`` row is
#:     not this job's to judge; a ``'pending'`` row is a future licence; a
#:     ``'revoked'`` row is already where this job would put it (so re-running is a
#:     no-op — the idempotency).
#:   * ``expires_at IS NOT NULL`` — written explicitly even though NULL comparisons
#:     are never true. ``expires_at IS NULL`` means PERPETUAL (see the column comment
#:     on ``License``); dropping this leg is how a reaper eats the estate.
#:   * ``expires_at < :cutoff``  — ``:cutoff`` is ``now - grace``, NOT ``now``. See
#:     ``reap_all_expired_licenses`` for why the grace window is load-bearing.
_REAPABLE_LICENSE_COUNT_SQL = """
SELECT count(*)
  FROM license
 WHERE status = 'expired'
   AND expires_at IS NOT NULL
   AND expires_at < :cutoff
"""


def _require_owner_row(session: Any, owner_key: str) -> None:
    """A named owner must be a ``tenant_owner`` row — a typo in an operator's
    ``--tenant-owner`` is a named refusal, not an opaque FK IntegrityError."""
    if not owner_key or session.get(TenantOwner, owner_key) is None:
        raise ClientSafeError(
            f"unknown tenant owner {owner_key!r}: not a tenant_owner key (migration 050)"
        )


def bundle_grantees(tenant_owner: str | None, issuers: set[str]) -> set[str]:
    """The owners ONE bundled app grants on a tenant (least privilege).

    * the tenant's owner is an issuer of the app -> nobody;
    * a single issuer -> that issuer;
    * several issuers -> ``platform`` only, and only if it is one of them. The
      bundle definition names no preferred issuer (``bundle`` has no such column),
      so a multi-issuer app never grants an app owner on its own.
    """
    if tenant_owner in issuers:
        return set()
    if len(issuers) == 1:
        return set(issuers)
    return {PLATFORM_OWNER} & issuers


def _add_platform_grant(session: Any, tenant: Tenant) -> None:
    """Stage the 'platform' grant for a NEW tenant owned by another app, in the
    caller's (uncommitted) session — so the tenant and its grant commit together.
    Flushes first so the tenant's id is assigned and the FK holds."""
    if tenant.owner_key == PLATFORM_OWNER:
        return
    session.flush()
    session.add(
        TenantAppGrant(
            tenant_id=tenant.id,
            grantee_owner_key=PLATFORM_OWNER,
            granted_by=PLATFORM_GRANT_BY,
        )
    )


class DatabaseManager:
    """Manages database connections and operations"""

    def __init__(self, database_url: str | None = None):
        """
        Initialize database manager

        Args:
            database_url: SQLAlchemy database URL
        """
        if not database_url:
            # No localhost fallback: it reaches nothing inside a pod (licenses#162)
            # and on a laptop it silently targets whatever Postgres the host runs.
            raise ValueError(
                "DatabaseManager requires a database URL — pass the DATABASE_URL "
                "value explicitly; there is no default database."
            )

        # Append SSL params to URL if certificates are available
        database_url = self._append_ssl_params(database_url)

        # Use NullPool: pgbouncer (transaction mode) is the connection pool.
        # An app-side pool layered on top of pgbouncer holds connections
        # across requests and breaks across pgbouncer's per-tx assignment;
        # it also blocks DB restarts in tests because stuck connections
        # never recycle. Each session opens a fresh connection through
        # pgbouncer; pgbouncer reuses backend conns server-side.
        #
        # connect_timeout is LOAD-BEARING, and NullPool is exactly why: every
        # session is a fresh connect, so an unbounded connect is paid on EVERY
        # call rather than once per pool refill.
        #
        # Without it a connect blocks for the OS TCP timeout whenever the peer
        # BLACKHOLES (drops SYNs without an RST) rather than refusing — a node
        # outage, a network partition, a Tailscale UDP blip. Measured on
        # 2026-08-11 in a scratch namespace: ~130s per connect. That stall is
        # what turned gitops #2553 (readiness -> /health/ready) into a
        # CrashLoopBackOff of BOTH replicas: each readiness probe parked a
        # worker inside a blocking connect, /health/live queued behind it and
        # blew its own 5s timeout DESPITE touching no database, and liveness
        # killed the container at T0+74s. #2553 was reverted (#2556); this is
        # one of the two prerequisites for re-landing it.
        #
        # 2 seconds, chosen against the deadlines that actually exist:
        #   * k8s readinessProbe timeoutSeconds 3 (and liveness 5)
        #   * license-authz's introspect budget of 3.0s
        # A healthy in-cluster connect to pgbouncer is single-digit ms, so 2s
        # is ~2 orders of magnitude of headroom and only fires on a genuine
        # hang. Note this bounds the CONNECT only — a query that is merely slow
        # is governed by statement_timeout, not by this.
        #
        # NB PR #111 added a bounded retry to introspect_token. Without this
        # timeout that retry made a blackhole hang cost TWICE the wall clock;
        # with it, both attempts are bounded.
        self.engine = create_engine(
            database_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_SECONDS},
        )
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, expire_on_commit=False, bind=self.engine
        )

    def _append_ssl_params(self, database_url: str) -> str:
        """
        Append SSL parameters to database URL for psycopg3.

        psycopg3 accepts SSL params directly in the connection string:
        - sslmode=require|verify-ca|verify-full
        - sslcert=/path/to/client-cert.pem
        - sslkey=/path/to/client-key.pem
        - sslrootcert=/path/to/ca-cert.pem

        Checks for certificates in these locations (in order):
        1. Environment variables: PGSSLCERT, PGSSLKEY, PGSSLROOTCERT
        2. Container paths: /app/certs/
        3. Local dev paths: ~/Movies/postgres-ssl-setup/certs/

        Returns:
            URL with SSL params appended, or original URL if SSL disabled
        """
        sslmode = os.getenv("POSTGRES_SSLMODE", "require").lower()

        if sslmode == "disable":
            logger.info("PostgreSQL SSL disabled")
            return database_url

        # Check for certificate paths
        ca_cert = os.getenv("PGSSLROOTCERT") or self._find_cert("ca-cert.pem")
        client_cert = os.getenv("PGSSLCERT") or self._find_cert(
            "licenses-client-cert.pem", "mini1-cert.pem"
        )
        client_key = os.getenv("PGSSLKEY") or self._find_cert(
            "licenses-client-key.pem", "mini1-key.pem"
        )

        # Build SSL query params (always has at least sslmode)
        ssl_params: list[str] = [f"sslmode={sslmode}"]

        # Only add CA cert for verify modes (not require/prefer/allow)
        if sslmode in ("verify-ca", "verify-full"):
            if ca_cert and os.path.exists(ca_cert):
                ssl_params.append(f"sslrootcert={ca_cert}")
                logger.info("Using CA certificate: %s", ca_cert)

            if (
                client_cert
                and client_key
                and os.path.exists(client_cert)
                and os.path.exists(client_key)
            ):
                ssl_params.append(f"sslcert={client_cert}")
                ssl_params.append(f"sslkey={client_key}")
                logger.info("Using client certificate for mTLS: %s", client_cert)
        elif sslmode == "require":
            # For require mode, don't add certs - just encrypt without verification
            logger.info("SSL enabled without certificate verification (sslmode=require)")

        # An `sslmode` already in the DSN WINS, and is not duplicated.
        #
        # This used to append unconditionally, so a DSN that already carried one
        # came out with the key twice — `?sslmode=disable&…&sslmode=require`.
        # libpq takes the LAST occurrence, so the operator's explicit value was
        # silently replaced by the env var: docker-compose.yml ships
        # `DATABASE_URL=…?sslmode=disable` together with `POSTGRES_SSLMODE:
        # require`, and the connection went out as `require`. Against a
        # pgbouncer that is not serving TLS that is a hard connect failure with
        # a DSN that reads as though it should work.
        #
        # The DSN is the more specific source, so it wins; the env var stays the
        # default for a DSN that does not specify one. Production is unaffected
        # either way — it sets POSTGRES_SSLMODE=disable and returns above, and
        # its DSN carries no sslmode (verified against the live pod).
        existing_keys = {key.lower() for key, _ in parse_qsl(urlsplit(database_url).query)}
        if "sslmode" in existing_keys:
            ssl_params = [p for p in ssl_params if not p.lower().startswith("sslmode=")]
            logger.info("DSN already specifies sslmode; POSTGRES_SSLMODE=%s not applied", sslmode)

        if not ssl_params:
            return database_url

        separator = "&" if "?" in database_url else "?"
        result = f"{database_url}{separator}{'&'.join(ssl_params)}"
        # The previous form here was `result.split("@")[0] + "@..."` under a
        # comment reading "Don't log creds" — it did the exact opposite.
        # Everything BEFORE the first `@` is `scheme://user:password`, so it
        # printed the password verbatim and hid the hostname. Production
        # escapes it only because POSTGRES_SSLMODE=disable returns above this
        # line; the code default is `require` and docker-compose.yml sets
        # `require`, so every other environment reached it.
        logger.info("Database URL with SSL: %s", redact_dsn(result))
        return result

    def _find_cert(self, *names: str) -> str | None:
        """Find a certificate file in known locations."""
        search_paths = [
            "/app/certs",
            "/certs",
            os.path.expanduser("~/Movies/postgres-ssl-setup/certs"),
        ]
        for path in search_paths:
            for name in names:
                full_path = os.path.join(path, name)
                if os.path.exists(full_path):
                    return full_path
        return None

    def database_is_pristine(self) -> bool:
        """Is this database EMPTY — no app tables AND no migration history?

        ONLY MEANINGFUL BEFORE ``create_tables()``. Asked after it, the app-tables
        half is always False — which is precisely the defect this exists to make
        visible.

        ``scripts/apply_migrations.py`` decides whether to BASELINE (stamp every
        ``.sql`` file as applied without running it) by probing for the
        ``offering`` table. ``_run_db_bootstrap`` calls ``create_tables()``
        immediately before the runner, and that creates ``offering``. So the
        runner's probe cannot distinguish "populated prod, hand-applied" from
        "brand-new empty database", and on a fresh database every migration is
        baselined away, permanently — observed live 2026-08-12 against a genuinely
        empty database: ``baselined 21 files as applied (existing schema)``.

        WHY BOTH HALVES, and it is not belt-and-braces. The caller uses this to
        assert that the runner's baseline branch WAS taken, and "no app tables" on
        its own does not entail that. ``schema_migrations`` is created by raw DDL
        in the runner, NOT by the ORM, so ``Base.metadata.drop_all()`` does not
        remove it: a database whose app tables were dropped still carries its 21
        stamps, the runner's ``if not applied`` guard is False, the baseline is
        NOT taken, and a one-half probe would report "fresh" and claim a
        baselining that never happened. The baseline requires the tracking table
        to be absent OR empty, so that is what is measured.

        Probing ``license`` for the app-tables half: it is genesis (``init.sql``)
        and is in ``models.py``, so it exists in every shape of this database that
        has ever held data, whereas ``offering`` arrives only at migration 010.

        ⚠️ KNOWN FALSE NEGATIVE, stated rather than left to be discovered. A
        LEGACY hand-built database — app tables present, no ``schema_migrations``
        — is not pristine, so the caller stays silent, and yet the runner still
        baselines all 21 files for it. That silence is deliberate: such a schema
        was built by hand and is not ORM-only, so the warning's claim would be
        false there. The runner's own probe is likewise NOT changed; ``offering``
        is the stricter, safer target for exactly that database.

        Raises rather than returns on an unreachable database, deliberately: it is
        a SQLAlchemy connect fault, ``is_db_unreachable()`` classifies it, and
        ``lifespan`` defers the bootstrap instead of exiting. Swallowing it here
        would report "fresh database" for a database we simply could not reach.

        Schema-qualified to ``public.`` to match the runner's own probe exactly.
        An unqualified name is ``search_path``-dependent, and a probe that reasons
        about another probe's decision must not be able to disagree with it about
        which schema it is talking about.
        """
        from sqlalchemy import text

        with self.engine.connect() as conn:
            app_tables, tracking = conn.execute(
                text(
                    "SELECT to_regclass('public.license'), to_regclass('public.schema_migrations')"
                )
            ).one()
            if app_tables is not None:
                return False
            if tracking is None:
                return True
            # The tracking table outlived a drop_all. Pristine only if it is empty,
            # because that is what the runner's `if not applied` guard tests.
            return (
                conn.execute(
                    text("SELECT NOT EXISTS (SELECT 1 FROM public.schema_migrations)")
                ).scalar()
                is True
            )

    def create_tables(self):
        """Create all tables"""
        Base.metadata.create_all(bind=self.engine)

    def seed_platform_tenants(self) -> int:
        """Ensure the platform's hardcoded-UUID tenants exist. Returns rows inserted.

        WHY THIS IS NOT A MIGRATION. ``scripts/apply_migrations.py`` BASELINES on
        first run against a schema that already has the app tables — it stamps
        every ``.sql`` file as applied WITHOUT running it. Its existence probe is
        ``to_regclass('public.offering')``, and ``create_all()`` (which runs
        immediately before it) creates ``offering``. So the probe cannot tell
        "populated prod, hand-applied" from "brand-new empty database", and on a
        FRESH database everything that lives only in SQL is skipped forever.

        Measured 2026-08-12: prod holds all 7 UUIDs below (57 tenants total); a
        create_all'd database gets ZERO. These ids are hardcoded across the fleet,
        so a disaster-recovery rebuild or a new environment would come up without
        the identities other services name directly. Emitting them from the ORM
        makes "what a licenses database must contain" a reviewable fact instead of
        an emergent property of 20 SQL files.

        Scoped deliberately to the 7 HARDCODED ids. The other four seeded tenants
        (003 `n8n-dev`/`gerard-dev`, 004 `marketing`/`sales`) are uuidv7-random and
        environment-specific — nothing can reference them by literal id, so
        re-creating them elsewhere would invent data rather than restore identity.

        Idempotent by construction. It never UPDATEs, so a byo_mode an operator has
        deliberately changed is preserved — unlike migration 014, which re-affirms
        posture on every apply. Safe to run on every boot, which is what
        ``_run_db_bootstrap`` does.

        ⚠️ ``ON CONFLICT DO NOTHING`` with **no conflict target**, deliberately.
        Targeting ``(id)`` would swallow only an id collision and let a NAME
        collision raise — and ``tenants.name`` is UNIQUE. That is not theoretical:
        prod holds ``portal-render-e2e`` at a random uuid7 *and* migration 013's
        literal id under the suffixed name ``portal-render-e2e-e148e8af``, because
        013 hit exactly this collision and was renamed to get past it. An untargeted
        DO NOTHING absorbs both.

        FAIL-SOFT ON PURPOSE. This runs inside ``_run_db_bootstrap``, where anything
        that is not a connect fault propagates and the pod REFUSES TO SERVE — and
        this service backs ext_authz for the whole platform. A tenant name taken by
        an unrelated row must not take authz down for everyone; the blast radius of
        one missing identity is smaller than the blast radius of a CrashLoop. So a
        row that could not be seeded is WARNed, loudly and by name, and the boot
        continues.

        ⛔ IT NOW APPLIES THE PRINCIPAL-ID GUARD, WHICH IS THE ONE THING IT WAS
        STRUCTURALLY UNABLE TO DO BEFORE. ``ensure_tenant`` refuses to register an
        id that is already a Keycloak principal (``subject.keycloak_sub`` or
        ``subject.id``) — but this seed never went through ``ensure_tenant``, so the
        guard could not see it from any position, and two of the ids below
        (``e148e8af-…`` and the ``00000000-…0000`` sentinel) are re-INSERTed on
        EVERY boot despite violating that invariant. A guard the boot path can
        bypass is a guard the defect can be reborn through: the mechanism re-armed
        on every restart.

        The check is the SAME predicate, imported from the same place
        (``services.tenant_subject_invariant.subject_for_tenant_id``), so the seed
        and ``ensure_tenant`` cannot drift into disagreeing about what a violation
        is.

        ⛔ A HIT IS A REFUSAL PLUS A COUNTER, NEVER A CRASH AND NEVER A REWRITE.
          * Never a crash: see FAIL-SOFT above. Both known hits are conditions that
            have been true for weeks, and this runs at boot on the auth path.
          * Never a rewrite: re-keying an existing row is a data migration with its
            own blast radius (1,399 billing rows / $86.02 in 24h hang off
            ``e148e8af-…``) and is explicitly out of scope here. This method has
            never UPDATEd anything and still does not.
          * Refusing changes NO DATA in the known cases: those rows already exist,
            so ``ON CONFLICT DO NOTHING`` was already inserting nothing. What the
            guard actually prevents is the id being BORN as a tenant on a fresh
            database that has a ``subject`` row for it — and it makes the fact
            countable (``nonagentic_license_tenant_seed_principal_refusals``)
            instead of silent.

        ⚠️ THE GUARD RUNS ON ITS OWN CONNECTION, BEFORE the insert transaction, and
        that is not tidiness. Running it inside ``engine.begin()`` would put a
        SELECT in the same transaction as the INSERTs, so one faulting guard query
        would abort the transaction and every REMAINING seed row would fail with
        ``InFailedSqlTransaction`` — a probe taking down the thing it protects. A
        guard fault is therefore contained and fails OPEN (seed proceeds, as it did
        before this existed): being unable to evaluate the invariant is not
        evidence of a violation, and the fresh-database DR rebuild these ids exist
        for must not be blocked by an unreadable ``subject`` table.
        """
        from sqlalchemy import text

        from services.tenant_subject_invariant import (
            record_seed_refusal,
            subject_for_tenant_id,
        )

        # Migration 050: every tenant row needs its owner, and a fresh database
        # never ran 050's seed. The INSERT below names owner_key = 'platform'
        # explicitly; the column has NO default (a forgotten owner fails NOT NULL).
        self.seed_ownership_reference()

        # id -> (name, subject_id, realm, match_kind) for entries the guard refuses.
        refused: dict[str, tuple[str, str, str, str]] = {}
        try:
            with self.engine.connect() as probe:
                for tenant_id, name, _byo in PLATFORM_TENANTS:
                    hit = subject_for_tenant_id(probe, tenant_id)
                    if hit is not None:
                        subject_id, realm, match_kind = hit
                        refused[tenant_id] = (name, subject_id, realm, match_kind)
        except Exception:
            # Fails OPEN, and says so. An unreadable `subject` table is not
            # evidence that any id is a principal, and a fresh-database rebuild
            # (the case these hardcoded ids exist for) must not be blocked by it.
            logger.warning(
                "seed_platform_tenants: could not evaluate the principal-id guard; "
                "seeding UNGUARDED for this boot (an unevaluable invariant is not a "
                "violation). New violations remain refused by ensure_tenant.",
                exc_info=True,
            )
            refused = {}

        for tenant_id, (name, subject_id, realm, match_kind) in sorted(refused.items()):
            # ERROR, not WARNING: this is the mechanism that put a principal id in
            # `tenants` in the first place, and the fix is to edit PLATFORM_TENANTS.
            # Not caller-drivable — it is a property of a constant in this file — so
            # the ERROR-rate concern that keeps the byo-mode 404 at WARNING does not
            # apply.
            logger.error(
                "seed_platform_tenants: REFUSING to seed tenant %s (%r) — that UUID is "
                "already a Keycloak principal id via subject.%s (subject %s, realm %r). "
                "A tenant is an ORGANISATION; registering a principal id as one makes the "
                "authenticating identity the thing that pays, which nothing downstream can "
                "detect (billing_cost_events has no FK to tenants). This is a REFUSAL, not "
                "a repair: an existing row is left exactly as it is. Fix PLATFORM_TENANTS "
                "to name the organisation's id. Counted by "
                "nonagentic_license_tenant_seed_principal_refusals.",
                tenant_id,
                name,
                match_kind,
                subject_id,
                realm,
            )
            record_seed_refusal(tenant_id, name, match_kind)

        inserted = 0
        with self.engine.begin() as conn:
            for tenant_id, name, byo_mode in PLATFORM_TENANTS:
                if tenant_id in refused:
                    continue
                result = conn.execute(
                    text(
                        "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) "
                        "VALUES (:id, :name, :byo_mode, :owner, now()) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "id": tenant_id,
                        "name": name,
                        "byo_mode": byo_mode,
                        "owner": PLATFORM_OWNER,
                    },
                )
                inserted += result.rowcount or 0

            # Verify rather than trust the insert count: a suppressed conflict is
            # indistinguishable from "already present" by rowcount alone, and the
            # two mean very different things.
            present = {
                str(r[0])
                for r in conn.execute(
                    text("SELECT id FROM tenants WHERE id = ANY(:ids)"),
                    {"ids": [t[0] for t in PLATFORM_TENANTS]},
                ).fetchall()
            }

        # ⚠️ SPLIT BY CAUSE. A refused entry is absent BY DECISION, and reporting it
        # under the name-collision message below would attribute this service's own
        # deliberate refusal to an unrelated row — the wrong cause, in the log line
        # an operator reads first. The refusals already logged at ERROR above.
        missing = [
            (tid, name)
            for tid, name, _ in PLATFORM_TENANTS
            if tid not in present and tid not in refused
        ]
        refused_and_absent = [
            (tid, refused[tid][0]) for tid, _n, _b in PLATFORM_TENANTS if tid in refused
        ]
        refused_and_absent = [(tid, name) for tid, name in refused_and_absent if tid not in present]
        if inserted:
            logger.info(
                "seed_platform_tenants: inserted %d of %d platform tenants",
                inserted,
                len(PLATFORM_TENANTS),
            )
        if missing:
            logger.warning(
                "seed_platform_tenants: %d platform tenant(s) are ABSENT after seeding "
                "— almost certainly because another row already holds the name "
                "(tenants.name is UNIQUE). Serving anyway; flows that hardcode these "
                "ids will not resolve. Missing: %s",
                len(missing),
                ", ".join(f"{name}={tid}" for tid, name in missing),
            )
        if refused_and_absent:
            logger.warning(
                "seed_platform_tenants: %d platform tenant(s) are absent BY REFUSAL, not "
                "by collision — the id is a Keycloak principal (see the ERROR lines "
                "above). Flows that hardcode these ids will not resolve until "
                "PLATFORM_TENANTS names the ORGANISATION's id instead. Refused: %s",
                len(refused_and_absent),
                ", ".join(f"{name}={tid}" for tid, name in refused_and_absent),
            )
        return inserted

    def get_session(self) -> Session:
        """Get a new database session"""
        return self.SessionLocal()

    # Region operations
    def create_region(self, code: str, name: str, active: bool = True) -> Region:
        """Create a new region"""
        session = self.get_session()
        try:
            region = Region(code=code, name=name, active=active)
            session.add(region)
            session.commit()
            session.refresh(region)
            return region
        finally:
            session.close()

    def get_region(self, region_id: UUID) -> Region | None:
        """Get region by ID"""
        session = self.get_session()
        try:
            return session.query(Region).filter(Region.id == region_id).first()
        finally:
            session.close()

    def get_region_by_code(self, code: str) -> Region | None:
        """Get region by code"""
        session = self.get_session()
        try:
            return session.query(Region).filter(Region.code == code).first()
        finally:
            session.close()

    def list_regions(self, active_only: bool = True) -> list[Region]:
        """List all regions"""
        session = self.get_session()
        try:
            query = session.query(Region)
            if active_only:
                query = query.filter(Region.active.is_(True))
            return query.all()
        finally:
            session.close()

    # Subject operations
    def create_or_get_subject(
        self, keycloak_sub: str, keycloak_realm: str, *, self_tenant_mint: bool = False
    ) -> Subject:
        """Create a new subject or get existing. Safe under concurrent first-login.

        ``Subject.keycloak_sub`` is UNIQUE and this sits on the licence-mint hot
        path (``main.create_user_license``, whose docstring notes portal-bff
        provisions on EVERY session) with ``replicas: 2`` behind Envoy. The
        SELECT-then-INSERT is therefore genuinely racy: a brand-new user opening
        two tabs (or an SPA firing two parallel bootstraps) lands on both
        replicas, both miss the SELECT, and both INSERT. The loser's
        ``UniqueViolation`` used to propagate to that handler's generic
        ``except Exception``, which returns 500 with ``str(e)`` interpolated
        into ``detail`` — so a user's very first login failed AND leaked raw
        psycopg error text (constraint name, SQL, bound parameters).

        Absorbed the same way as the two existing races in this codebase — the
        License race in ``main.create_user_license`` and ``ensure_tenant``
        below: catch ``IntegrityError``, roll back, return the row that won.
        Preferred over a Postgres ``ON CONFLICT DO NOTHING`` because DO NOTHING
        returns no row on conflict, so it would need this same re-SELECT anyway
        while additionally forcing a dialect-specific ``insert()`` construct
        that bypasses the mapper defaults (uuid7 ``id``, ``utc_now``
        ``created_at``) — more machinery, identical outcome, and inconsistent
        with the established pattern.

        The rollback is load-bearing, not hygiene: a failed flush poisons the
        transaction and PostgreSQL rejects every subsequent statement with
        InFailedSqlTransaction until it is rolled back, so the re-SELECT can
        only run on a rolled-back session.

        ⛔ ``keycloak_sub`` MAY NOT BE AN ID THAT IS ALREADY A REGISTERED TENANT.
        Raises ``ClientSafeError`` (a ``ValueError`` subclass whose message may be
        echoed) before the INSERT if it is. A tenant is an ORGANISATION and a
        subject is an identity that AUTHENTICATES within one; registering one id
        as both makes the authenticating identity the thing that PAYS, and nothing
        downstream can notice (``billing_cost_events`` has no FK to ``tenants``).

        This is the MIRROR of the guard in ``ensure_tenant`` and closes the second
        of the two doors onto that defect — the one the
        ``LicenseTenantIdIsKeycloakPrincipal`` gauge cannot see, because no tenant
        row is inserted when it is used. Measured 2026-09-23: 82 of 85 registry
        tenants have no subject carrying their id, so each was one call to THIS
        METHOD away from a new violation; among them is ``e4f70a83-…``
        ``nonagentic-gtm``, the known fourth offender that is invisible to the
        gauge precisely because it was never written to ``subject``.

        ⛔ THE REFUSAL IS NARROW, AND THE NARROWING IS THE POINT — the reasoning
        sits at the check itself. Two exemptions, both derived from live state
        rather than from any list of known ids: ``self_tenant_mint`` (the caller is
        minting a licence whose TENANT is this same id — the documented portal-bff
        path) and "this tenant already holds licences" (a pre-existing identity
        that is already trading). An exempted collision is ADMITTED, logged at
        ERROR and left to the gauge; only a genuinely NEW principal-as-subject
        registration raises.

        ⚠️ ``self_tenant_mint`` IS A STATEMENT ABOUT THE CALL, NOT A PERMISSION
        FLAG, and only ``main.create_user_license`` sets it — from
        ``tenant_id == subject_id``, where ``tenant_id`` has ALREADY had to be a
        registered ``tenants`` row to get past the mint guard. It defaults to
        ``False``, so every provisioning script keeps the strict behaviour.

        ⚠️ EXISTING subjects are returned UNCHANGED by the idempotent early return
        above and are never refused — the three prod rows that already violate
        keep working and are REPORTED by the gauge. ⛔ THAT EARLY RETURN IS NOT
        ENOUGH ON ITS OWN, which is exactly what the first version of this guard
        got wrong: ``e4f70a83-…`` ``nonagentic-gtm`` holds 5 live licences and has
        NO subject row, so "already has a subject row" spares only the violators
        that happen to have been written, and a DR rebuild restores no subject row
        that was never there. Same rule as ``ensure_tenant``: existing violations
        are reported, new ones are refused at the primitive.

        ⚠️ The predicate is ``services.tenant_subject_invariant.tenant_for_subject_sub``
        — imported, never restated, so this guard and the gauge cannot drift into
        disagreeing about what a violation is.
        """
        # ⛔ REFUSE A MALFORMED IDENTIFIER BEFORE ANY LOOKUP — never strip it. '<A_sub>\n'
        # used to mint a NEW subject whose introspected sub a `.strip()`-ing consumer
        # (ingestion) read as A's. Stripping here instead would silently alias A. The one
        # deferred shape is an upper-case UUID: it may still resolve as a ROW id below
        # (GET's resolution order), and is refused only if it would be stored.
        problem = subject_identifier_problem(keycloak_sub)
        case_only = problem is not None and bool(_ANY_CASE_UUID_RE.fullmatch(keycloak_sub))
        if problem is not None and not case_only:
            raise MalformedSubjectIdentifierError(
                f"cannot register subject {keycloak_sub!r}: {problem}"
            )
        session = self.get_session()
        try:
            # Check if exists
            subject = session.query(Subject).filter(Subject.keycloak_sub == keycloak_sub).first()
            if subject:
                return subject

            # ⛔ AN IDENTIFIER THAT IS A SUBJECT ROW ID RESOLVES TO THAT ROW — it is never
            # registered as a NEW subject's keycloak_sub. `GET /licenses/user/{id}` already
            # resolves both forms (keycloak_sub first, then row id), and admin callers
            # (fr's licence admin) hand back the `subject_id` a licence response gave
            # them. Before this, that id minted a second subject whose keycloak_sub WAS
            # the other subject's id (2 of 282 on prod, 2026-09-27) — a row that
            # introspection would have presented as the other subject's identity.
            # Resolve, not 409: same order and meaning as GET, and the caller (license
            # admin/manager) can already mint for that subject by its real sub.
            as_row_id = _parse_uuid_or_none(keycloak_sub)
            if as_row_id is not None:
                subject = session.query(Subject).filter(Subject.id == as_row_id).first()
                if subject:
                    return subject
            if problem is not None:
                # An upper-case UUID that is not a row id would be STORED: refuse it.
                raise MalformedSubjectIdentifierError(
                    f"cannot register subject {keycloak_sub!r}: {problem}"
                )

            # ⛔ A SUBJECT'S `keycloak_sub` MAY NOT BE AN ID THAT IS ALREADY A
            # REGISTERED TENANT. This is the SECOND door onto the invariant
            # `services/tenant_subject_invariant.py` exists for, and until now it
            # was the unguarded one.
            #
            # Checked HERE — AFTER the idempotent early return, BEFORE the INSERT
            # — which is the same placement, for the same reasons, as the
            # principal guard in `ensure_tenant`:
            #   * AFTER the early return, so an EXISTING subject stays readable and
            #     returnable. The three rows that violate on prod today
            #     (`e148e8af-…` portal-render-e2e, `1d0090ac-…` book-fulfillment
            #     and the `00000000-…0000` sentinel) are REPORTED by the gauge, not
            #     refused here — refusing them would break licence lookups for
            #     identities that have been serving for months.
            #   * BEFORE the INSERT, so a NEW one cannot be born.
            #
            # ⚠️ THE HOT PATH DOES NOT PAY FOR THIS. This method sits on the
            # licence-mint path that portal-bff drives on EVERY session, but an
            # existing subject returns above without reaching this line. Only a
            # genuinely NEW subject costs one extra primary-key lookup.
            #
            # ⚠️ IT FAILS CLOSED, deliberately and without adding a failure mode.
            # `ensure_tenant`'s guard lets a probe fault propagate and so does this
            # one: the probe runs on the session that is ALREADY querying, so a
            # database fault that could break it would break the SELECT above and
            # the INSERT below anyway. (`seed_platform_tenants` fails OPEN instead,
            # because it runs at boot on the auth path where an unreadable table
            # must not stop the pod serving. Different position, different rule.)
            #
            # ClientSafeError, not IntegrityError: the message is authored here and
            # `POST /licenses/user/{subject_id}` echoes it in a 409. ⚠️ That handler
            # HAD a comment asserting this DAL path "contains no raise" and relying
            # on it — adding this raise site without updating the call site would
            # have turned a caller error into a 500 with the message swallowed.
            from services.tenant_subject_invariant import (
                record_subject_refusal,
                tenant_for_subject_sub,
                tenant_has_licences,
            )

            collision = tenant_for_subject_sub(session, keycloak_sub)
            if collision is not None:
                tenant_id, tenant_name = collision
                # ⛔ THE NARROWING. A collision is refused ONLY when it is a
                # GENUINELY NEW principal-as-subject registration. Two exemptions,
                # each pinned by a test, each for a DIFFERENT pre-existing shape:
                #
                #   1. `self_tenant_mint` — the caller is minting a licence whose
                #      TENANT is this very id, i.e. the documented self-tenant mint
                #      path `POST /licenses/user/{id}` with `X-Tenant-Id: {id}` that
                #      portal-bff drives (it forwards `sub` as the tenant header
                #      when the token carries no `tenant_id` claim).
                #      `TestCreateUserLicenseMintGuard::
                #      test_guard_does_not_fire_for_real_self_tenant` asserts 201 for
                #      exactly this shape (on a synthetic id since migration 046
                #      re-keyed portal-render-e2e, the original example), and the
                #      mint guard has ALREADY established that the id is a registered
                #      tenant before control reaches here. Refusing it would turn a
                #      hygiene guard into an outage on a live path.
                #   2. `tenant_has_licences` — the id is already TRADING as an
                #      organisation. `e4f70a83-…` nonagentic-gtm holds 5 live licences
                #      and has NO subject row, so the idempotent early return above
                #      does not spare it (measured 2026-09-23). Its next mint, and the
                #      first mint of every identity in that shape after a DR rebuild,
                #      would otherwise 409.
                #
                # ⛔ NEITHER IS AN ALLOWLIST. Both are derived from live state on
                # every call, so a fourth offender is spared if and only if it is
                # genuinely pre-existing, and an id that never traded is never
                # exempt. A hardcoded set of the three known ids would fail OPEN for
                # the fourth and would rot at the next rebuild.
                #
                # ⚠️ AN EXEMPTED COLLISION IS STILL A VIOLATION BEING BORN, so it is
                # logged at ERROR and will appear in
                # `nonagentic_license_tenant_is_subject_rows{class="violation"}` from
                # the next scrape. It is NOT counted in
                # `…_subject_principal_refusals`: that gauge counts what this guard
                # PREVENTED, and mixing admissions into it would leave a refusal
                # counter unable to say what it refused.
                if self_tenant_mint or tenant_has_licences(session, tenant_id):
                    logger.error(
                        "create_or_get_subject: ADMITTING subject %r (realm %r) whose id "
                        "is ALREADY tenant %s (%r) — narrowed guard, exemption=%s. This IS "
                        "the tenant/subject violation (an identity that AUTHENTICATES is "
                        "also the thing that PAYS, and billing_cost_events has no FK to "
                        "tenants), but the identity is pre-existing and refusing it would "
                        "break a live mint path. Reported, not prevented: it will appear "
                        "in nonagentic_license_tenant_is_subject_rows from the next "
                        "scrape.",
                        keycloak_sub,
                        keycloak_realm,
                        tenant_id,
                        tenant_name,
                        "self_tenant_mint" if self_tenant_mint else "tenant_has_licences",
                    )
                else:
                    record_subject_refusal(keycloak_sub, keycloak_realm, tenant_name)
                    logger.error(
                        "create_or_get_subject: REFUSING to register subject %r (realm %r) "
                        "— that id is ALREADY tenant %s (%r) and holds NO licences, and "
                        "this is not a self-tenant mint, so it is a GENUINELY NEW "
                        "principal-as-subject registration. A tenant is an ORGANISATION "
                        "and a subject is an identity that AUTHENTICATES within one; "
                        "registering one id as both makes the authenticating identity the "
                        "thing that pays, which nothing downstream can detect "
                        "(billing_cost_events has no FK to tenants). This is the SECOND "
                        "door onto the LicenseTenantIdIsKeycloakPrincipal defect and the "
                        "one the tenant-side guards cannot see. Counted by "
                        "nonagentic_license_subject_principal_refusals.",
                        keycloak_sub,
                        keycloak_realm,
                        tenant_id,
                        tenant_name,
                    )
                    raise ClientSafeError(
                        f"cannot register subject {keycloak_sub!r}: that id is already "
                        f"registered tenant {tenant_id} ({tenant_name!r}). A tenant is an "
                        f"ORGANISATION and a subject is an identity that authenticates "
                        f"within one; registering a tenant id as a principal makes the "
                        f"authenticating identity the thing that pays, which nothing "
                        f"downstream can detect (billing_cost_events has no FK to tenants, "
                        f"and UUID version does not separate the two). Pass the "
                        f"PRINCIPAL's Keycloak sub, and attach it to its organisation via "
                        f"subject.tenant_id."
                    )

            # Create new
            subject = Subject(keycloak_sub=keycloak_sub, keycloak_realm=keycloak_realm)
            session.add(subject)
            try:
                session.commit()
            except IntegrityError:
                # Lost a race to a concurrent provision (unique keycloak_sub) —
                # return the subject that won instead of 500ing.
                session.rollback()
                won = session.query(Subject).filter(Subject.keycloak_sub == keycloak_sub).first()
                if won is None:
                    # Not the race we absorb (e.g. a NOT NULL violation) — fail loud.
                    raise
                return won
            session.refresh(subject)
            return subject
        finally:
            session.close()

    def attribute_subject_to_tenant(self, subject_id: UUID, tenant_id: str) -> str:
        """State a subject's parent org in ``subject.tenant_id`` — or refuse, with a reason.

        WHAT THIS CLOSES. Migration 029 added ``subject.tenant_id`` and backfilled
        nothing; migration 032 then set it on the 51 subjects whose parent the
        database already answered, and deliberately left the rest NULL. Both are
        ONE-SHOT files, and NO code path has ever written this column — so the NULL
        population REGROWS. Measured on prod 2026-09-14: 213 of 262 subjects NULL,
        and **8 of those are in 032's own admissible bucket** (exactly one licence
        tenant, that tenant not a machine). Those 8 are subjects minted AFTER 032
        ran: 032 could have resolved every one of them, and nothing was there to.
        Without a runtime writer the only repair mechanism is "write another
        hand-audited migration", which is why the column has been inert since 029.

        ⛔ THE ADMISSION RULE IS 032'S, VERBATIM, and it is the point of this method
        — not a formality. Set ``tenant_id`` ONLY where the subject's licences name
        EXACTLY ONE tenant and that tenant is not itself a machine:

          * ``already_set`` — never overwritten. A stated parentage is an operator
            fact (032 or a later decision); a licence mint is not evidence to
            revise it. Overwriting would make the last POST win, silently.
          * ``tenant_unknown`` — the tenant has no ``tenants`` row. This is the
            money-bug shape: portal-bff forwards the Keycloak ``sub`` as
            ``X-Tenant-Id`` when the token carries no tenant claim, and a ``sub``
            IS UUID-shaped. The FK would reject it anyway; refusing here keeps the
            log line honest rather than surfacing as an IntegrityError.
          * ``tenant_is_machine`` — ``kind='service'``. Writing ``svc-kag`` the
            subject -> ``svc-kag`` the tenant would record the defect
            ``docs/svc-tenant-migration-plan.md`` exists to UNDO a second time, in
            the very column added to express the corrected shape — and launder it,
            because a derived self-reference is indistinguishable from a parentage
            an operator decided. 032 refuses those 41 rows for this reason; so does
            this. ⚠️ The discriminator is the TENANT, never the subject's name: 17
            of the 51 rows 032 DID set are ``svc-*`` subjects sitting under a real
            org tenant, which is the target model already in effect.
          * ``ambiguous`` — the subject holds licences under more than one tenant.
            A person can be a member of several orgs; ``tenant_id`` is one column,
            so the honest value for a question the schema cannot answer is NULL.
            Counted over ALL licences, not just active ones, exactly as 032 does —
            counting only active rows manufactures certainty.
          * ``set`` — written.

        CALL IT AFTER THE LICENCE IS COMMITTED, not before: the ambiguity check
        reads the licence rows, and the licence just minted is itself evidence
        about this subject's parent. Called before, a subject's FIRST licence would
        see zero rows and attribute on no evidence at all.

        BEST-EFFORT BY CONTRACT. This is bookkeeping hung off the licence-mint hot
        path (``replicas: 2``, portal-bff POSTs every session). It opens its own
        session and returns a reason string; it raises nothing the caller must
        handle, because a failure to state parentage must never fail a licence.

        Returns the reason code, for the caller's log line and for tests.
        """
        session = self.get_session()
        try:
            subject = session.query(Subject).filter(Subject.id == subject_id).first()
            if subject is None:
                return "subject_unknown"
            if subject.tenant_id is not None:
                return "already_set"

            try:
                tid = UUID(str(tenant_id))
            except ValueError, AttributeError, TypeError:
                # Not even UUID-shaped — cannot be a tenants.id. Treated as
                # "unknown" rather than raising: the caller is a log line.
                return "tenant_unknown"

            tenant = session.query(Tenant).filter(Tenant.id == tid).first()
            if tenant is None:
                return "tenant_unknown"
            if tenant.kind == "service":
                return "tenant_is_machine"

            # count(DISTINCT tenant_id) over ALL of this subject's licences.
            distinct_tenants = {
                row[0]
                for row in session.query(License.tenant_id)
                .filter(License.subject_id == subject_id)
                .distinct()
                .all()
            }
            if distinct_tenants != {tid}:
                # Either zero licences (no evidence) or more than one tenant
                # (ambiguous). Both stay NULL; they are distinguished in the
                # reason code only so an operator can read the counts.
                return "no_licences" if not distinct_tenants else "ambiguous"

            subject.tenant_id = tid
            try:
                session.commit()
            except IntegrityError:
                # Raced by a concurrent mint, or the FK moved under us. The other
                # writer's value stands — see `already_set`.
                session.rollback()
                return "race_lost"
            return "set"
        finally:
            session.close()

    def subject_tenant_parentage_partition(self) -> dict[str, int]:
        """Count subjects by parentage bucket — the diagnostic 029/032 never shipped.

        The four buckets are migration 032's partition, evaluated live instead of
        once in a file header. ``resolvable_unstated`` is the number that matters:
        subjects whose parent THIS DATABASE ALREADY ANSWERS and that are still
        NULL. It was 0 the moment 032 ran and had regrown to 8 by 2026-09-14 —
        that regrowth is the defect ``attribute_subject_to_tenant`` closes, and
        this is how anyone sees it without re-deriving the query.

        A non-zero ``resolvable_unstated`` on a version that HAS the runtime writer
        means subjects are being minted through a path that does not attribute, or
        that a licence gained a tenant after its subject was last seen. It is a
        finding to investigate, never something to backfill blind: the other three
        buckets are NOT repairable from this database and must stay NULL
        (``service_tenant`` needs the operator's 43-row svc->org table,
        ``ambiguous`` cannot be expressed in one column, ``no_licences`` has no
        join to derive from).

        Read-only. One query, no session left open.
        """
        session = self.get_session()
        try:
            rows = session.execute(
                text(
                    """
                    WITH per_subject AS (
                        SELECT s.id,
                               s.tenant_id IS NULL AS unstated,
                               (SELECT count(DISTINCT l.tenant_id)
                                  FROM license l WHERE l.subject_id = s.id) AS n_tenants,
                               (SELECT bool_or(t.kind = 'service')
                                  FROM license l JOIN tenants t ON t.id = l.tenant_id
                                 WHERE l.subject_id = s.id) AS machine_tenant
                          FROM subject s
                    )
                    SELECT CASE
                             WHEN n_tenants = 0 THEN 'no_licences'
                             WHEN n_tenants > 1 THEN 'ambiguous'
                             WHEN machine_tenant THEN 'service_tenant'
                             ELSE 'resolvable'
                           END AS bucket,
                           unstated,
                           count(*)
                      FROM per_subject
                     GROUP BY 1, 2
                    """
                )
            ).all()
        finally:
            session.close()

        out = {
            "subjects_total": 0,
            "stated": 0,
            "unstated": 0,
            "resolvable_unstated": 0,
            "ambiguous_unstated": 0,
            "service_tenant_unstated": 0,
            "no_licences_unstated": 0,
        }
        for bucket, unstated, count in rows:
            out["subjects_total"] += count
            if unstated:
                out["unstated"] += count
                out[f"{bucket}_unstated"] += count
            else:
                out["stated"] += count
        return out

    def get_subject(self, subject_id: UUID) -> Subject | None:
        """Get subject by ID"""
        session = self.get_session()
        try:
            return session.query(Subject).filter(Subject.id == subject_id).first()
        finally:
            session.close()

    def get_subject_by_keycloak_sub(self, keycloak_sub: str) -> Subject | None:
        """Get subject by Keycloak sub"""
        session = self.get_session()
        try:
            return session.query(Subject).filter(Subject.keycloak_sub == keycloak_sub).first()
        finally:
            session.close()

    # Tenant operations
    def create_tenant(
        self,
        name: str,
        keycloak_realm: str | None = None,
        cost_centre: str | None = None,
        budget_currency: str | None = None,
        *,
        owner_key: str,
        external_ref: str | None = None,
    ) -> Tenant:
        """Create a new tenant. Name must match ^[a-z0-9][a-z0-9-]*$ (DB CheckConstraint).

        ``owner_key`` is REQUIRED (no default): the owning app. A tenant owned by any
        app other than 'platform' gets an active 'platform' grant in the SAME
        transaction (``PLATFORM_GRANT_BY``).

        ``budget_currency`` (migration 043): None = token budgets (default), 'EUR' =
        money budget in billing, so no token budget is ever pushed for it.

        ``cost_centre`` is the chargeback join key (models.py:934, nullable, no
        vocabulary — deliberately unpopulated until an apportionment basis
        exists). This is the write path for it: pass it when the caller already
        knows the answer. Never invented here — a guessed value later reads as a
        measurement.
        """
        session = self.get_session()
        try:
            _require_owner_row(session, owner_key)
            tenant = Tenant(
                name=name,
                keycloak_realm=keycloak_realm,
                owner_key=owner_key,
                external_ref=external_ref,
            )
            if cost_centre is not None:
                setattr(tenant, "cost_centre", cost_centre)
            if budget_currency is not None:
                if budget_currency not in BUDGET_CURRENCIES:
                    raise ClientSafeError(f"invalid budget_currency: {budget_currency!r}")
                setattr(tenant, "budget_currency", budget_currency)
            session.add(tenant)
            _add_platform_grant(session, tenant)
            session.commit()
            session.refresh(tenant)
            return tenant
        finally:
            session.close()

    def ensure_tenant(
        self,
        tenant_id: str | UUID,
        name: str,
        keycloak_realm: str | None = None,
        kind: str | None = None,
        byo_mode: str | None = None,
        cost_centre: str | None = None,
        budget_currency: str | None = None,
        *,
        owner_key: str,
    ) -> Tenant:
        """Idempotently register a tenant with an EXPLICIT id.

        ``owner_key`` (migration 050, REQUIRED — no default) is the app that owns the
        tenant; a NEW tenant owned by any app other than 'platform' gets an active
        'platform' grant in the same transaction. The name
        clash check is scoped to it (names are unique per owner). An EXISTING id is
        returned unchanged whatever its owner — the API route compares owners and
        409s ``tenant_owned_by_other_app``; this primitive does not.

        ``budget_currency`` (migration 043) is applied ONLY on INSERT, like ``kind``
        and ``byo_mode``; an existing row is returned untouched. Change it with
        ``set_tenant_budget_currency``.

        Unlike ``create_tenant`` (which mints a random uuid7 id), this registers a
        ``tenants`` row whose ``id`` equals a caller-supplied UUID — required when
        the id is already committed elsewhere and must match: a ``license.tenant_id``,
        a deterministic per-service ``uuid5``, or a legacy tenant UUID being
        backfilled. This is the primitive behind the FK campaign's "register the
        tenant before minting its license" invariant (used by the provision_*
        scripts and the orphan resolver).

        Idempotent: returns the existing row if one already exists for ``tenant_id``.
        Raises ``ClientSafeError`` (a ``ValueError`` subclass) if a *different* tenant
        already owns ``name`` (name is UNIQUE), so a name typo can never silently
        repoint. The message is caller-authored input only, so ``POST /tenants/{id}``
        may echo it in its 409 body. ``name`` must match
        ^[a-z0-9][a-z0-9-]*$ (DB CheckConstraint) or the insert fails loud.

        ``kind`` classifies the row on creation (one of the five values in
        ``ck_tenants_kind``). Pass it from the provisioning scripts so a freshly
        minted service principal is born ``'service'`` rather than
        ``'unclassified'`` — otherwise it lands in the operator tenant inventory
        that ``list_tenants`` exists to keep clean, and the classification has to
        be redone by hand afterwards. Left ``None`` the column default
        (``'unclassified'``) applies, which is the deliberate fail-closed default
        for a tenant nobody has decided about yet.

        ``byo_mode`` sets the BYO entitlement on creation (one of the three values
        in ``ck_tenants_byo_mode``, migration 021). Left ``None`` the column
        server_default applies: ``'byo_required'`` since migration 058 (owner ruling
        2026-10-15, licenses#273); it was ``'local_allowed'`` under 042.

        ⚠️ PASS IT FROM ``provision_service_token.py``. ``'byo_required'`` means
        "commercial: must bring their own provider; worker_management hard-fails a
        run with no configured one" — a statement a MACHINE identity cannot
        satisfy, because an ``svc-*`` service principal has no account and nowhere
        to have put an API key, so the only reachable outcome is a 402
        ``byo_llm_not_configured`` on the first BYO-gated call it makes. Before
        this parameter existed the provisioning script could pass ``kind`` but not
        ``byo_mode``, so every service principal was born
        ``kind='service' + byo_mode='byo_required'`` and had to be repaired by
        hand; migration 031 repaired 34 such rows at once and its header names
        this exact gap as the unfixed generator. This parameter IS that fix — the
        durable control, in place of a CHECK constraint that would have failed the
        live provisioning path closed.

        ⚠️ ``kind`` and ``byo_mode`` are applied ONLY on INSERT. An existing row is
        returned untouched, because this call is idempotent and an operator's
        later reclassification is the authority — the same rule migration 027
        states for its own backfill. To change an existing row use
        ``set_tenant_byo_mode``.

        An invalid ``byo_mode`` raises ``ClientSafeError`` here rather than
        surfacing as an opaque ``IntegrityError`` from ``ck_tenants_byo_mode``,
        matching ``set_tenant_byo_mode``'s behaviour.

        ⚠️ ``kind='service'`` COUPLES to ``byo_mode``, and this is the SECOND half
        of the generator fix migration 033 relies on (the first half is 033's own
        ``ck_tenants_service_not_byo_required`` CHECK). Passing ``kind='service'``
        with no explicit ``byo_mode`` used to fall through to the column's
        ``server_default('byo_required')`` — reproducing the exact incoherent row
        migration 031 repaired, just through a caller other than
        ``provision_service_token.py`` (which #183 fixed at ITS call site only).
        So here: left unset, a ``kind='service'`` row is now born
        ``byo_mode='local_allowed'`` instead. An EXPLICIT ``byo_mode='byo_required'``
        alongside ``kind='service'`` is refused with ``ClientSafeError`` — a
        machine identity has no account and nowhere to have put an API key, so
        that combination is not a strict policy, it is unsatisfiable, and letting
        it reach the CHECK would surface as an opaque ``IntegrityError`` instead
        of a named one.

        ⛔ ``tenant_id`` MAY NOT BE A KEYCLOAK PRINCIPAL ID, in either of its
        two forms. If the id is already a ``subject.keycloak_sub`` (the
        principal's ``sub`` claim) OR a ``subject.id`` (this registry's own PK
        for that principal), this raises ``ClientSafeError`` before the INSERT.
        A tenant is an ORGANISATION and a subject is an identity that
        AUTHENTICATES within one; conflating them makes the authenticating
        identity the thing that PAYS, with nothing downstream able to notice
        (``billing_cost_events`` has no FK to ``tenants``).

        ⚠️ "Principal", not "person": of the four known offending ids only
        ``portal-render-e2e``'s subject is a human — ``book-fulfillment`` and
        ``nonagentic-gtm`` are ``secure-apps`` SERVICE ACCOUNTS. The defect is
        identical for a machine.

        Three prod rows already violate this and are DELIBERATELY still
        returnable via the idempotent early return above; see
        ``services/tenant_subject_invariant.py`` for why they are reported rather
        than refused, for the gauge that measures the residue, and for the
        BLIND SPOT this guard shares with it (a tenant whose Keycloak principal
        was never written to ``subject`` — ``nonagentic-gtm`` — is invisible to
        both).

        ``cost_centre`` is the chargeback join key (models.py:934). INSERT-only,
        same rule as ``kind``/``byo_mode``: never invented here, only passed
        through when the caller already knows the answer.
        """
        if kind is not None and kind not in TENANT_KINDS:
            raise ClientSafeError(f"invalid kind: {kind!r} (one of {', '.join(TENANT_KINDS)})")
        if byo_mode is not None and byo_mode not in BYO_MODES:
            raise ClientSafeError(f"invalid byo_mode: {byo_mode!r}")
        if budget_currency is not None and budget_currency not in BUDGET_CURRENCIES:
            raise ClientSafeError(f"invalid budget_currency: {budget_currency!r}")
        if kind == "service":
            if byo_mode is None:
                byo_mode = "local_allowed"
            elif byo_mode == "byo_required":
                raise ClientSafeError(
                    "cannot register a kind='service' tenant with byo_mode="
                    "'byo_required': a machine identity has no account and nowhere "
                    "to have put an API key, so the only reachable outcome is a 402 "
                    "byo_llm_not_configured on its first BYO-gated call (migration "
                    "031). Pass 'local_allowed' or 'platform_only', or omit "
                    "byo_mode to get 'local_allowed' automatically."
                )
        tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        session = self.get_session()
        try:
            existing = session.query(Tenant).filter(Tenant.id == tid).first()
            if existing is not None:
                return existing
            # ⛔ A TENANT IS AN ORG. ITS ID MUST BE NEITHER A KEYCLOAK PRINCIPAL'S
            # `sub` NOR THIS REGISTRY'S OWN `subject.id` FOR THAT PRINCIPAL.
            #
            # Checked HERE — after the idempotent early return, before the INSERT.
            # What that placement buys, stated only as far as it is true:
            #   * AFTER the early return, so this guard refuses a NEW registration
            #     of a principal id and NEVER an EXISTING row. The three rows that
            #     already violate on prod stay readable and re-registerable through
            #     this call; they are REPORTED instead —
            #     `services.tenant_subject_invariant.report_at_boot` and the
            #     `nonagentic_license_tenant_is_subject_rows` gauge.
            #   * BEFORE the INSERT, so a new violation is rejected at the
            #     primitive rather than counted afterwards.
            #
            # ⛔ THE BOOT SEED IS NOT WHAT THIS PLACEMENT PROTECTS, and an earlier
            # version of this comment claimed it was. `seed_platform_tenants()`
            # does NOT call `ensure_tenant` at all — it issues a raw `INSERT … ON
            # CONFLICT DO NOTHING` on its own connection. Two of the ids it
            # re-INSERTs on every boot (`e148e8af-…` and the `00000000-…0000`
            # sentinel) do violate this invariant, but they enter through that raw
            # SQL, which this guard cannot see from any position. So moving the
            # check above the early return would not CrashLoop the seed, and
            # leaving it below does not rescue the seed: the seed is simply not a
            # caller. The reason a fresh database boots clean is separate and
            # simpler — `subject` is EMPTY there, so the predicate matches nothing
            # during bootstrap.
            #
            # ✅ THE SEED NOW CARRIES ITS OWN COPY OF THIS CHECK (2026-09-13), so
            # the bypass is closed without either function calling the other.
            # `seed_platform_tenants` imports the SAME `subject_for_tenant_id`
            # predicate and REFUSES a hit, rather than routing through
            # `ensure_tenant` — because it must not adopt this function's raising
            # contract at boot (`ClientSafeError` from `_run_db_bootstrap` makes the
            # pod refuse to serve) and must keep its untargeted `ON CONFLICT DO
            # NOTHING`, which `ensure_tenant`'s name-collision branch does not have.
            # Same predicate, different failure mode: here it RAISES, there it
            # refuses-logs-and-counts. ⚠️ An earlier version of this comment cited
            # `models.py:2237-2247` for the seed; that range was already stale when
            # written — cite the function name, never a line range in a 5,000-line
            # file.
            #
            # Measured 2026-09-10: `e148e8af-3751-49bf-a0fa-56ab46d31ddc` sits in
            # `tenants` as `portal-render-e2e` AND is the `keycloak_sub` of subject
            # `8382f0b3-…` (realm `portal`). It drew 1,399 billing rows / $86.02 in
            # 24h with its Keycloak user deleted since 2026-08-27. Nothing
            # downstream can catch it: `billing_cost_events` has no FK to any
            # tenant table and UUID version does not separate the two populations
            # (`george-dekker`, `merqi`, `nonagentic-gtm` are all v4, like a sub).
            #
            # ⚠️ This guard covers the SHARED PRIMITIVE, which is what
            # `POST /tenants/{id}` and every `provision_*` script go through. It
            # does NOT cover raw SQL — and raw SQL is how the live defect was
            # created (migration 013 INSERTs that literal id). A trigger would
            # cover that, and was rejected on a measured fact rather than on
            # taste; the reasoning is in `services/tenant_subject_invariant.py`.
            # `create_tenant` is deliberately unguarded: it mints a random uuid7
            # and has no caller-supplied id to collide.
            #
            # ClientSafeError, not IntegrityError: the message names the subject,
            # and `POST /tenants/{id}` may echo it in its 409 body — the same
            # contract as the `kind='service'` coupling guard above.
            from services.tenant_subject_invariant import subject_for_tenant_id

            hit = subject_for_tenant_id(session, tid)
            if hit is not None:
                subject_id, realm, match_kind = hit
                raise ClientSafeError(
                    f"cannot register tenant {tid}: that UUID is already an existing "
                    f"Keycloak principal id via subject.{match_kind} (subject "
                    f"{subject_id}, realm {realm!r}). A tenant is an ORGANISATION and "
                    f"a subject is an identity that authenticates within one; "
                    f"registering a principal id as a tenant makes the authenticating "
                    f"identity the thing that pays, which nothing downstream can "
                    f"detect (billing_cost_events has no FK to tenants and UUID "
                    f"version does not separate the two — george-dekker, merqi and "
                    f"nonagentic-gtm are all v4, like a Keycloak sub). Register the "
                    f"ORGANISATION's id instead, or attach the subject to a tenant "
                    f"via subject.tenant_id."
                )
            _require_owner_row(session, owner_key)
            by_name = _tenant_by_name(session, name, owner_key)
            if by_name is not None:
                raise TenantNameTakenError(
                    f"cannot register tenant {tid}: name {name!r} already belongs to "
                    f"tenant {by_name.id}"
                )
            tenant = Tenant(id=tid, name=name, keycloak_realm=keycloak_realm, owner_key=owner_key)
            if kind is not None:
                # setattr, not `tenant.kind = kind`: the ORM attribute types as
                # `Column[str]` and the type checker refuses a plain `str`. Same
                # workaround as set_tenant_archived just below.
                setattr(tenant, "kind", kind)
            if byo_mode is not None:
                # INSERT-only, exactly like `kind` above. Setting it on an
                # existing row would silently overwrite an operator's choice.
                setattr(tenant, "byo_mode", byo_mode)
            if cost_centre is not None:
                setattr(tenant, "cost_centre", cost_centre)
            if budget_currency is not None:
                setattr(tenant, "budget_currency", budget_currency)
            session.add(tenant)
            try:
                _add_platform_grant(session, tenant)
                session.commit()
            except IntegrityError:
                # Lost a race on the id or name unique constraint — return the row
                # that won for this id, else re-raise (a genuine name collision).
                session.rollback()
                won = session.query(Tenant).filter(Tenant.id == tid).first()
                if won is None:
                    # Lost a race on uq_tenants_owner_name: another request
                    # registered this name under the same owner first.
                    if _tenant_by_name(session, name, owner_key) is not None:
                        raise TenantNameTakenError(
                            f"cannot register tenant {tid}: name {name!r} is already "
                            f"used by another tenant of owner {owner_key!r}"
                        ) from None
                    raise
                return won
            session.refresh(tenant)
            return tenant
        finally:
            session.close()

    def set_tenant_cost_centre(self, name_or_id: str, cost_centre: str | None) -> Tenant | None:
        """Set (or clear) a tenant's chargeback cost centre.

        ``name_or_id`` may be the tenant UUID or its human name. Returns the
        updated Tenant, or None if no such tenant.

        WHY THIS EXISTS. ``cost_centre`` was INSERT-ONLY. ``create_tenant`` and
        ``ensure_tenant`` both accept it, and both apply it only when building a
        NEW row — ``ensure_tenant`` returns an existing tenant unchanged, by
        design, so re-registering an id can never overwrite an operator's choice.
        The consequence was that a tenant created without one could never be given
        one: ``PATCH /tenants/{name_or_id}`` renames and nothing else, and the
        four rows measured NULL on 2026-09-08 (`default-anon`, `george-dekker`,
        `nonagentic`, `portal`) were reachable only by direct SQL against the
        primary. A value an operator is expected to decide has to be settable
        through the API that owns it.

        ⚠️ UPDATE, not upsert, and deliberately narrow: it writes ONE column. It
        cannot create a tenant and cannot rename one, so it can never be the
        accidental path by which a typo'd ``name_or_id`` invents a row.

        ``None`` CLEARS the value, and that is a real operation rather than an
        oversight: "this tenant's apportionment is undecided" is a state the
        column is required to express (see ``COST_CENTRES``), so an operator who
        set the wrong centre must be able to return the row to it rather than
        being forced to pick a second wrong answer.
        """
        if cost_centre is not None and cost_centre not in COST_CENTRES:
            # ClientSafeError, not ValueError: the route turns this into a 422 and
            # the message is the caller's only actionable signal. Validating here
            # as well as at the edge is the point — `scripts/` callers reach this
            # method directly, without passing a Pydantic model.
            raise ClientSafeError(
                f"invalid cost_centre: {cost_centre!r} (expected one of "
                f"{', '.join(COST_CENTRES)}, or null to clear)"
            )
        session = self.get_session()
        try:
            tenant = None
            try:
                tenant = session.query(Tenant).filter(Tenant.id == UUID(str(name_or_id))).first()
            except ValueError, TypeError:
                tenant = _tenant_by_name(session, name_or_id)
            if tenant is None:
                return None
            setattr(tenant, "cost_centre", cost_centre)
            session.commit()
            session.refresh(tenant)
            return tenant
        finally:
            session.close()

    def set_tenant_byo_mode(self, name_or_id: str, byo_mode: str) -> Tenant | None:
        """Set a tenant's BYO entitlement ('local_allowed' | 'byo_required' | 'platform_only').

        ``name_or_id`` may be the tenant UUID or its human name. Returns the
        updated Tenant, or None if no such tenant.
        """
        if byo_mode not in BYO_MODES:
            raise ClientSafeError(f"invalid byo_mode: {byo_mode!r}")
        session = self.get_session()
        try:
            tenant = None
            try:
                tenant = session.query(Tenant).filter(Tenant.id == UUID(str(name_or_id))).first()
            except ValueError, TypeError:
                tenant = _tenant_by_name(session, name_or_id)
            if tenant is None:
                return None
            setattr(tenant, "byo_mode", byo_mode)
            session.commit()
            session.refresh(tenant)
            return tenant
        finally:
            session.close()

    def set_tenant_budget_currency(
        self,
        name_or_id: str,
        budget_currency: str | None,
        *,
        actor: str | None = None,
        via: str | None = None,
        allow_change: bool = False,
    ) -> Tenant | None:
        """Set a tenant's money-budget currency (migration 043, PLAN REV F).

        * NULL -> a ``BUDGET_CURRENCIES`` value: any caller of this method.
        * Re-sending the value the row already holds (incl. NULL -> NULL): an
          idempotent no-op, nothing written, nothing audited.
        * A currency -> a DIFFERENT currency: only with ``allow_change=True`` (the
          route passes it for the owner capability, realm role ``platform-admin``);
          otherwise ``BudgetCurrencyImmutableError``. "All decisions at provisioning
          time need to be able to be updated during a running month" (owner,
          2026-10-02). Converting the remaining money budget is billing's job; this
          row only says which currency the tenant's budget is held in.
        * A currency -> NULL: ALWAYS ``BudgetCurrencyImmutableError``, even with
          ``allow_change``: no token budgets were pushed for a money tenant, so
          returning it to tokens would leave it with no budget at all.
        * An ARCHIVED tenant: ``BudgetCurrencyTenantArchivedError`` (nothing written).

        Every write is AUDITED by one ``budget_currency_changed`` WARNING line
        carrying who (``actor`` — the forwarded owner when a relay vouched for one,
        else the caller), through which client (``via``, the caller's ``azp``), the
        tenant, from, to and when. Never a token. This service has no
        audit table (see ``soft_delete_licenses_for_subject``); the log line is the
        record, the same mechanism ``licence_delete_confirmed`` uses. It is emitted
        only after the commit succeeds, so it never records a change that rolled back.

        The read and the write share one row lock (``SELECT ... FOR UPDATE``), so two
        racing callers cannot both observe the same prior value and diverge. Returns
        None when no such tenant.
        """
        if budget_currency is not None and budget_currency not in BUDGET_CURRENCIES:
            raise ClientSafeError(f"invalid budget_currency: {budget_currency!r}")
        session = self.get_session()
        try:
            tenant = self._resolve_tenant_for_update(session, name_or_id)
            if tenant is None:
                return None
            if tenant.archived_at is not None:
                # Checked under the row lock, before the idempotent arm: an archived
                # tenant is refused even when it already holds the value.
                raise BudgetCurrencyTenantArchivedError(
                    f"tenant {tenant.id} is ARCHIVED; its budget_currency is not changed"
                )
            current = tenant.budget_currency
            if current == budget_currency:
                # commit, not rollback: it releases the row lock without writing,
                # and (expire_on_commit=False) keeps the instance readable.
                session.commit()
                return tenant
            if current is not None and budget_currency is None:
                raise BudgetCurrencyImmutableError(
                    f"budget_currency of tenant {tenant.id} is {current!r} and cannot be "
                    "cleared back to token budgets"
                )
            if current is not None and not allow_change:
                raise BudgetCurrencyImmutableError(
                    f"budget_currency of tenant {tenant.id} is {current!r} and cannot change "
                    f"to {budget_currency!r} without the owner (platform-admin) capability"
                )
            setattr(tenant, "budget_currency", budget_currency)
            session.commit()
            session.refresh(tenant)
            logger.warning(
                "budget_currency_changed: tenant=%s name=%s from=%s to=%s actor=%s via=%s at=%s",
                tenant.id,
                tenant.name,
                current,
                budget_currency,
                actor,
                via,
                datetime.now(UTC).isoformat(),
            )
            return tenant
        finally:
            session.close()

    @staticmethod
    def _resolve_tenant_for_update(session: Any, name_or_id: str) -> Tenant | None:
        """``_resolve_tenant`` with a row lock (id first, else name)."""
        try:
            tid = UUID(str(name_or_id))
        except ValueError, TypeError:
            return _tenant_by_name(session, name_or_id, for_update=True)
        return session.query(Tenant).filter(Tenant.id == tid).with_for_update().first()

    def rename_tenant(self, name_or_id: str, new_name: str) -> Tenant | None:
        """Rename a tenant. Returns the updated Tenant, or None if not found.

        The tenant UUID is the identity and never changes — this is a LABEL
        change. That distinction matters because `name` is a resolver key:
        license-authz translates an incoming tenant name to the canonical UUID,
        so a rename re-points every caller that addresses this tenant by name.
        Callers holding the UUID are unaffected.

        Validated here rather than left to the CHECK constraint so the caller
        gets a ClientSafeError naming the rule instead of an IntegrityError, and
        so a name already taken reads as a conflict rather than a 500.
        """
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", new_name or ""):
            raise ClientSafeError(
                f"invalid tenant name: {new_name!r} — lowercase letters, digits and "
                "hyphens only, and it may not start with a hyphen"
            )
        session = self.get_session()
        try:
            tenant = self._resolve_tenant(session, name_or_id)
            if tenant is None:
                return None
            if tenant.name == new_name:
                return tenant  # idempotent: renaming to the current name is a no-op
            # Names are unique per OWNER (migration 050): another app's tenant
            # with the same name is not a clash.
            clash = _tenant_by_name(session, new_name, cast(str, tenant.owner_key))
            if clash is not None:
                raise TenantNameTakenError(f"tenant name already in use: {new_name!r}")
            setattr(tenant, "name", new_name)
            try:
                session.commit()
            except IntegrityError as exc:
                # The pre-check above lost a race: another writer took the name
                # between it and this commit, and the unique index is the real
                # arbiter. Keyed on SQLSTATE 23505 (unique_violation), not on an
                # index NAME: prod carries both `ix_tenants_name` and
                # `tenants_name_key`, and this UPDATE changes only `name`, so a
                # unique violation here can only be the name. Any other integrity
                # failure is not the caller's fault and must stay a 500.
                session.rollback()
                if getattr(exc.orig, "sqlstate", None) != "23505":
                    raise
                raise TenantNameTakenError(f"tenant name already in use: {new_name!r}") from None
            session.refresh(tenant)
            return tenant
        finally:
            session.close()

    def set_tenant_archived(self, name_or_id: str, archived: bool) -> Tenant | None:
        """Archive or restore a tenant. Returns the Tenant, or None if not found.

        Idempotent in both directions: archiving an archived tenant keeps the
        ORIGINAL timestamp rather than moving it, because that timestamp is the
        answer to "when was this retired" and a repeated call must not rewrite
        history.

        ⚠️ This does NOT revoke anything. Licences, tokens, budgets and role
        grants all survive, and existing tokens keep authenticating — retiring a
        tenant must not become an accidental way to cut off live access. It
        removes the tenant from the operator lists and is the signal that new
        provisioning should not target it.

        UN-ARCHIVING INTO A TAKEN NAME (migration 053). Names and external_refs are
        unique per owner among LIVE tenants only, so while this tenant was archived
        the owner may have registered a new live tenant with the same name or
        external_ref. Restoring it then raises ``TenantNameTakenError`` /
        ``TenantExternalRefTakenError`` (route: 409) and writes nothing — from the
        pre-check under the row lock and, for a concurrent writer that wins the
        partial unique index first, from the ``IntegrityError`` as well. Rename the
        archived tenant first if both must be live.
        """
        session = self.get_session()
        try:
            try:
                tid: UUID | None = UUID(str(name_or_id))
            except ValueError, TypeError:
                tid = None
            if tid is not None:
                # Row lock: the conflict pre-check and the write see the same row.
                tenant = session.query(Tenant).filter(Tenant.id == tid).with_for_update().first()
            else:
                # A name resolves to a LIVE tenant only, so by name this can archive
                # but never restore; an archived tenant is restored by id.
                tenant = _tenant_by_name(session, name_or_id, for_update=True)
            if tenant is None:
                return None
            restoring = not archived and tenant.archived_at is not None
            if restoring:
                self._refuse_restore_into_taken_identity(session, tenant)
            if archived and tenant.archived_at is None:
                setattr(tenant, "archived_at", utc_now())
            elif not archived:
                setattr(tenant, "archived_at", None)
            try:
                session.commit()
            except IntegrityError as exc:
                # Lost a race to a writer that took the name / external_ref between
                # the pre-check and this commit; the partial index is the arbiter.
                session.rollback()
                if not restoring or getattr(exc.orig, "sqlstate", None) != "23505":
                    raise
                constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                if constraint == "uq_tenants_owner_external_ref":
                    raise TenantExternalRefTakenError(
                        f"cannot restore tenant {tenant.id}: external_ref "
                        f"{tenant.external_ref!r} is held by a live tenant of the same owner"
                    ) from None
                raise TenantNameTakenError(
                    f"cannot restore tenant {tenant.id}: name {tenant.name!r} is held by "
                    "a live tenant of the same owner"
                ) from None
            session.refresh(tenant)
            return tenant
        finally:
            session.close()

    @staticmethod
    def _refuse_restore_into_taken_identity(session: Any, tenant: Tenant) -> None:
        """Raise when a LIVE tenant of ``tenant``'s owner holds its name or external_ref."""
        owner_key = cast(str, tenant.owner_key)
        clash = _tenant_by_name(session, cast(str, tenant.name), owner_key)
        if clash is not None and clash.id != tenant.id:
            raise TenantNameTakenError(
                f"cannot restore tenant {tenant.id}: name {tenant.name!r} is held by live "
                f"tenant {clash.id} of the same owner"
            )
        if tenant.external_ref is not None:
            ref_clash = (
                session.query(Tenant.id)
                .filter(
                    Tenant.owner_key == owner_key,
                    Tenant.external_ref == tenant.external_ref,
                    Tenant.archived_at.is_(None),
                    Tenant.id != tenant.id,
                )
                .first()
            )
            if ref_clash is not None:
                raise TenantExternalRefTakenError(
                    f"cannot restore tenant {tenant.id}: external_ref "
                    f"{tenant.external_ref!r} is held by live tenant {ref_clash[0]} of "
                    "the same owner"
                )

    @staticmethod
    def _resolve_tenant(session: Any, name_or_id: str) -> Tenant | None:
        """UUID-or-name resolution, shared by the tenant mutators.

        Three call sites had already grown their own copy of this try/except;
        a fourth and fifth would have been the point where they drifted.
        """
        try:
            return session.query(Tenant).filter(Tenant.id == UUID(str(name_or_id))).first()
        except ValueError, TypeError:
            return _tenant_by_name(session, name_or_id)

    def list_tenants(
        self,
        include_archived: bool = False,
        include_service_actors: bool = False,
        owner_key: str | None = None,
    ) -> list[Tenant]:
        """Return tenants, newest first (read-only; operator/admin listing).

        Archived tenants are EXCLUDED by default: the list is what an operator
        picks from, and a retired tenant should not be pickable. Pass
        ``include_archived=True`` for the admin view that has to show them in
        order to un-archive one — otherwise archiving would be a one-way door
        through the API.

        Service actors (``kind='service'``) are EXCLUDED by default too, for the
        same pick-list reason and because migration 027 says so in as many words:
        "a service-mesh auth principal, NOT a tenant in the commercial sense;
        these should be excluded from any tenant inventory". They are 33 of the
        59 live rows, so including them makes the inventory unreadable and has
        repeatedly produced wrong customer counts.

        ⚠️ They are FILTERED, never archived. Archiving them would block
        provisioning (that is what this very filter does) and their subject ids
        are pinned across gitops ``*_ONBEHALF_SUBJECTS`` allowlists — see the
        header of migration 027. Pass ``include_service_actors=True`` for the
        admin completeness view and for any flow that provisions a licence TO a
        service principal.

        ``owner_key`` scopes the listing to what that owner may ACT ON: its own
        tenants PLUS tenants another owner has granted it (migration 050,
        ``tenant_app_grant`` with ``revoked_at IS NULL``). ⚠️ Visibility and
        ACTION must not diverge: ``owner_can_access_tenant`` (the retarget / mint
        check, via ``_has_active_grant``) already honoured an ACTIVE grant while
        this filter ignored it, so fr held a ``granted_by='migration-050'``
        grant on ``svc-n8n-local-conveyor`` and could act on it by id while
        ``/tenants`` hid it. This is the SAME predicate; keep the two in step.
        """
        session = self.get_session()
        try:
            q = session.query(Tenant)
            if owner_key is not None:
                # Correlated EXISTS rather than a join: one row per tenant, and
                # the grant predicate is the one ``_has_active_grant`` states.
                q = q.filter(
                    or_(
                        Tenant.owner_key == owner_key,
                        exists().where(
                            and_(
                                TenantAppGrant.tenant_id == Tenant.id,
                                TenantAppGrant.grantee_owner_key == owner_key,
                                TenantAppGrant.revoked_at.is_(None),
                            )
                        ),
                    )
                )
            if not include_archived:
                q = q.filter(Tenant.archived_at.is_(None))
            if not include_service_actors:
                q = q.filter(Tenant.kind != "service")
            return q.order_by(Tenant.created_at.desc()).all()
        finally:
            session.close()

    def get_tenant(self, name_or_id: str) -> Tenant | None:
        """Resolve a tenant by UUID or human name (read-only).

        ``name_or_id`` may be the tenant UUID or its human name. Returns the
        Tenant, or None if no such tenant. Mirrors the resolver in
        ``set_tenant_byo_mode``; used by the ``GET /tenants/{id}/byo-mode``
        internal lookup so worker_management can enforce byo_required on
        trusted-network (no-token) S2S worker runs.

        ⚠️ ``None`` HERE IS AMBIGUOUS ON PURPOSE — IT HAS TO BE, because dozens of
        callers only ask "did I get a tenant". It conflates "no such tenant" with
        "that selector could never have named one", and a route that maps it
        straight to 404 publishes those as the same fact. A caller that needs them
        apart (a route deciding 400 vs 404, anything a biller reads) must use
        ``resolve_tenant_selector`` below instead.
        """
        session = self.get_session()
        try:
            try:
                return session.query(Tenant).filter(Tenant.id == UUID(str(name_or_id))).first()
            except ValueError, TypeError:
                return _tenant_by_name(session, name_or_id)
        finally:
            session.close()

    # ── Ownership (migration 050) ────────────────────────────────────────────

    def seed_ownership_reference(self) -> None:
        """Insert the 050 owner + api_client seed rows if absent (every boot).

        A FRESH database baselines migration 050 without running it, so without
        this every tenant insert would fail its owner FK and every write would
        403 for want of an api_client row. ``ON CONFLICT DO NOTHING`` only: an
        operator's later change to an existing row is never overwritten.
        """
        with self.engine.begin() as conn:
            for key, display in TENANT_OWNERS:
                conn.execute(
                    text(
                        "INSERT INTO tenant_owner (key, display_name) VALUES (:k, :d) "
                        "ON CONFLICT (key) DO NOTHING"
                    ),
                    {"k": key, "d": display},
                )
            for realm, client_id, owner, cross in API_CLIENTS:
                conn.execute(
                    text(
                        "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
                        "VALUES (:r, :c, :o, :x) ON CONFLICT (realm, client_id) DO NOTHING"
                    ),
                    {"r": realm, "c": client_id, "o": owner, "x": cross},
                )
            for realm, owner in REALM_OWNERS:
                conn.execute(
                    text(
                        "INSERT INTO realm_owner (realm, owner_key) VALUES (:r, :o) "
                        "ON CONFLICT (realm) DO NOTHING"
                    ),
                    {"r": realm, "o": owner},
                )

    def resolve_realm_owner(self, realm: str) -> str | None:
        """The owner_key of a verified token's realm (migration 072), or None."""
        session = self.get_session()
        try:
            row = session.query(RealmOwner.owner_key).filter(RealmOwner.realm == realm).first()
            return str(row[0]) if row is not None else None
        finally:
            session.close()

    def resolve_api_client(self, realm: str, client_id: str) -> tuple[str, bool] | None:
        """``(owner_key, cross_owner)`` for a verified (realm, azp), or None if unknown."""
        session = self.get_session()
        try:
            row = (
                session.query(ApiClient.owner_key, ApiClient.cross_owner)
                .filter(ApiClient.realm == realm, ApiClient.client_id == client_id)
                .first()
            )
            return (str(row[0]), bool(row[1])) if row is not None else None
        finally:
            session.close()

    @staticmethod
    def _has_active_grant(session: Any, tenant_id: Any, owner_key: str) -> bool:
        return (
            session.query(TenantAppGrant.id)
            .filter(
                TenantAppGrant.tenant_id == tenant_id,
                TenantAppGrant.grantee_owner_key == owner_key,
                TenantAppGrant.revoked_at.is_(None),
            )
            .first()
            is not None
        )

    def owner_can_access_tenant(self, owner_key: str, tenant_id: str) -> bool:
        """True iff ``owner_key`` OWNS ``tenant_id`` or holds an ACTIVE grant on it."""
        try:
            tid = UUID(str(tenant_id))
        except ValueError, TypeError:
            return False
        session = self.get_session()
        try:
            tenant = session.query(Tenant.owner_key).filter(Tenant.id == tid).first()
            if tenant is None:
                return False
            if tenant[0] == owner_key:
                return True
            return self._has_active_grant(session, tid, owner_key)
        finally:
            session.close()

    def get_tenant_scoped(
        self,
        name_or_id: str,
        owner_key: str | None,
        *,
        cross_owner: bool = False,
        allow_grant: bool = True,
    ) -> Tenant | None:
        """Resolve a tenant AS SEEN BY a caller owner (migration 050).

        * by UUID — the tenant if the caller owns it, or (``allow_grant``) holds an
          active grant on it; otherwise None (the route answers 404, so another
          app's tenant is indistinguishable from an absent one).
        * by NAME — only within the caller's own owner. A grant never makes a
          tenant resolvable by name.
        * ``cross_owner`` (break-glass) — any tenant by id; by name only when the
          name is unique across owners, else ``TenantNameAmbiguousError``.

        A NAME only ever resolves to a LIVE tenant (migration 053, see
        ``_tenant_by_name``); an archived tenant is reachable by id only.
        """
        try:
            tid: UUID | None = UUID(str(name_or_id))
        except ValueError, TypeError:
            tid = None
        session = self.get_session()
        try:
            if tid is not None:
                tenant = session.query(Tenant).filter(Tenant.id == tid).first()
                if tenant is None or cross_owner:
                    return tenant
                if owner_key is None:
                    return None
                if tenant.owner_key == owner_key:
                    return tenant
                if allow_grant and self._has_active_grant(session, tid, owner_key):
                    return tenant
                return None
            if cross_owner:
                rows = (
                    session.query(Tenant)
                    .filter(Tenant.name == name_or_id, Tenant.archived_at.is_(None))
                    .limit(2)
                    .all()
                )
                if len(rows) > 1:
                    raise TenantNameAmbiguousError(
                        f"tenant name {name_or_id!r} is used by several owners; "
                        "address the tenant by id"
                    )
                return rows[0] if rows else None
            if owner_key is None:
                return None
            return _tenant_by_name(session, name_or_id, owner_key)
        finally:
            session.close()

    def get_tenant_by_external_ref(self, owner_key: str, external_ref: str) -> Tenant | None:
        """The owner's LIVE tenant carrying ``external_ref`` (POST /tenants idempotency).

        Live only (migration 053): an archived tenant never satisfies the retry, so
        re-provisioning an offboarded customer creates a NEW live tenant rather than
        handing back the archived one, whose licences are refused anyway.
        """
        session = self.get_session()
        try:
            return (
                session.query(Tenant)
                .filter(
                    Tenant.owner_key == owner_key,
                    Tenant.external_ref == external_ref,
                    Tenant.archived_at.is_(None),
                )
                .first()
            )
        finally:
            session.close()

    def owner_exists(self, owner_key: str) -> bool:
        session = self.get_session()
        try:
            return (
                session.query(TenantOwner.key).filter(TenantOwner.key == owner_key).first()
                is not None
            )
        finally:
            session.close()

    def app_issuers(self, app_id: UUID) -> set[str]:
        """Owners allowed to issue ``app_id``'s licences."""
        session = self.get_session()
        try:
            return {
                str(r[0])
                for r in session.query(AppIssuer.owner_key)
                .filter(AppIssuer.app_id == UUID(str(app_id)))
                .all()
            }
        finally:
            session.close()

    def tenant_has_bundle_licence(
        self, tenant_id: UUID, app_id: UUID, *, active_only: bool = False
    ) -> bool:
        """True iff ``tenant_id`` holds a licence for ``app_id`` that a bundle minted
        (``bundle_id`` set) — any status, or only an effectively active one
        (``effectively_active_clauses`` at now) with ``active_only``."""
        session = self.get_session()
        try:
            q = session.query(License.id).filter(
                License.tenant_id == UUID(str(tenant_id)),
                License.app_id == UUID(str(app_id)),
                License.bundle_id.isnot(None),
            )
            if active_only:
                # Effective validity (status AND started AND unexpired), never the
                # stored ``status`` alone, which nothing moves when ``expires_at`` passes.
                q = q.filter(*effectively_active_clauses(datetime.now(UTC)))
            return q.first() is not None
        finally:
            session.close()

    def add_app_issuers(self, app_id: UUID, owner_keys: Iterable[str]) -> int:
        """Declare issuers for an app (idempotent). Returns rows inserted."""
        inserted = 0
        with self.engine.begin() as conn:
            for owner in owner_keys:
                result = conn.execute(
                    text(
                        "INSERT INTO app_issuer (app_id, owner_key) VALUES (:a, :o) "
                        "ON CONFLICT (app_id, owner_key) DO NOTHING"
                    ),
                    {"a": str(app_id), "o": owner},
                )
                inserted += result.rowcount or 0
        return inserted

    _GRANT_INSERT = text(
        "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
        "VALUES (:t, :o, :b) "
        "ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING"
    )

    def put_tenant_grant(
        self, tenant_id: str, grantee_owner_key: str, granted_by: str
    ) -> tuple[TenantAppGrant, bool]:
        """Ensure an ACTIVE grant for (tenant, grantee). Returns (grant, created)."""
        tid = UUID(str(tenant_id))
        session = self.get_session()
        try:
            result = session.execute(
                self._GRANT_INSERT, {"t": str(tid), "o": grantee_owner_key, "b": granted_by}
            )
            session.commit()
            grant = (
                session.query(TenantAppGrant)
                .filter(
                    TenantAppGrant.tenant_id == tid,
                    TenantAppGrant.grantee_owner_key == grantee_owner_key,
                    TenantAppGrant.revoked_at.is_(None),
                )
                .one()
            )
            return grant, bool(cast(Any, result).rowcount)
        finally:
            session.close()

    def revoke_tenant_grant(self, tenant_id: str, grantee_owner_key: str) -> bool:
        """Stamp ``revoked_at`` on the active grant. False when there was none."""
        tid = UUID(str(tenant_id))
        session = self.get_session()
        try:
            grant = (
                session.query(TenantAppGrant)
                .filter(
                    TenantAppGrant.tenant_id == tid,
                    TenantAppGrant.grantee_owner_key == grantee_owner_key,
                    TenantAppGrant.revoked_at.is_(None),
                )
                .with_for_update()
                .first()
            )
            if grant is None:
                return False
            setattr(grant, "revoked_at", utc_now())
            session.commit()
            return True
        finally:
            session.close()

    def list_tenant_grants(
        self, tenant_id: str, *, active_only: bool = True
    ) -> list[TenantAppGrant]:
        session = self.get_session()
        try:
            q = session.query(TenantAppGrant).filter(
                TenantAppGrant.tenant_id == UUID(str(tenant_id))
            )
            if active_only:
                q = q.filter(TenantAppGrant.revoked_at.is_(None))
            return q.order_by(TenantAppGrant.created_at).all()
        finally:
            session.close()

    def ensure_bundle_grants(
        self,
        tenant_id: str,
        bundle_id: UUID,
        granted_by: str,
        *,
        hold: bool,
        dependency_grants: Iterable[tuple[str, str]] = (),
    ) -> list[str]:
        """ONE transaction: optionally record the bundle hold, then grant the
        bundled apps' issuers access to the tenant — LEAST PRIVILEGE. Idempotent.
        Returns the owner keys newly granted.

        Per bundled app, with ``issuers`` = its ``app_issuer`` owners:
          * the tenant's owner is itself an issuer -> nobody (it serves that app);
          * a SINGLE issuer -> that issuer;
          * SEVERAL issuers -> only ``platform``, if it is one of them. The bundle
            table names no preferred issuer, so a multi-issuer app never grants an
            app owner by itself (``inference-router`` = {fr, commonpractice} grants
            nobody; it used to grant both).

        This is how a tenant's owner sanctions another app on its tenant through a
        bundle (merqi ensuring the ``w2v`` bundle grants ``w2v``).

        ``dependency_grants`` (migration 059): ``(owner_key, app_key)`` pairs — the
        owners whose minters mint ONE app for this tenant (``bundle_caller`` and every
        ``app_requires`` edge in the closure). Written to ``tenant_dependency_grant``
        (APP-SCOPED: bundle-token for that app only; never a tenant read or an
        issuance), in the same transaction; never for the tenant's own owner. Returned
        as ``"<owner>:<app>"`` when newly written.
        """
        tid = UUID(str(tenant_id))
        created: list[str] = []
        with self.engine.begin() as conn:
            owner = conn.execute(
                text("SELECT owner_key FROM tenants WHERE id = :t"), {"t": str(tid)}
            ).scalar()
            if hold:
                conn.execute(
                    text(
                        "INSERT INTO tenant_bundle (tenant_id, bundle_id) VALUES (:t, :b) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"t": str(tid), "b": str(bundle_id)},
                )
            issuers_by_app: dict[str, set[str]] = {}
            for app_id, issuer in conn.execute(
                text(
                    "SELECT ba.app_id, ai.owner_key FROM bundle_app ba "
                    "JOIN app_issuer ai ON ai.app_id = ba.app_id "
                    "WHERE ba.bundle_id = :b"
                ),
                {"b": str(bundle_id)},
            ).fetchall():
                issuers_by_app.setdefault(str(app_id), set()).add(str(issuer))
            grantees = sorted(
                {g for issuers in issuers_by_app.values() for g in bundle_grantees(owner, issuers)}
            )
            for grantee in grantees:
                result = conn.execute(
                    self._GRANT_INSERT, {"t": str(tid), "o": grantee, "b": granted_by}
                )
                if result.rowcount:
                    created.append(grantee)
            for dep_owner, app_key in sorted(set(dependency_grants)):
                if dep_owner == owner:
                    continue
                result = conn.execute(
                    text(
                        "INSERT INTO tenant_dependency_grant "
                        "(tenant_id, owner_key, app_id, granted_by) "
                        "SELECT :t, :o, a.id, :b FROM app a WHERE a.key = :k "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"t": str(tid), "o": dep_owner, "k": app_key, "b": granted_by},
                )
                if result.rowcount:
                    created.append(f"{dep_owner}:{app_key}")
        return created

    def dependency_grant_apps(self, tenant_id: str, owner_key: str) -> set[str]:
        """App keys ``owner_key`` holds an APP-SCOPED dependency grant for on the tenant
        (``tenant_dependency_grant``, migration 059). Read-only."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.key FROM tenant_dependency_grant g JOIN app a ON a.id = g.app_id "
                    "WHERE g.tenant_id = :t AND g.owner_key = :o AND NOT a.revoked"
                ),
                {"t": str(UUID(str(tenant_id))), "o": owner_key},
            ).fetchall()
        return {str(r[0]) for r in rows}

    def resolve_tenant_selector(self, name_or_id: str) -> TenantLookup:
        """Resolve a tenant selector and say WHICH of three outcomes happened.

        ⛔ WHY THIS EXISTS. ``get_tenant`` degrades a non-UUID selector to a NAME
        lookup and returns ``None`` for everything that does not resolve. Three
        genuinely different conditions therefore arrived at the route as one
        value, and the route published all three as **404**:

          1. a registered tenant that does not exist          -> a real 404
          2. a selector that is neither a UUID nor a legal     -> a CLIENT fault
             tenant name (``ck_tenants_name_format``:             (400): no tenant
             ``^[a-z0-9][a-z0-9-]*$``)                            could ever match
          3. an unmatched ROUTE (FastAPI's own 404)            -> not this call

        That collapse is expensive because of who reads it: billing maps a 404
        from ``GET /tenants/{n}/byo-mode`` to ``byo_required``, i.e. COMMERCIAL,
        and freezes it into an invoice. A typo'd selector and a genuinely
        unregistered tenant then produce the same charge, and the measured traffic
        says both shapes occur — 8 of 79 byo-mode lookups on the deployed pods
        (2026-08-19) answered 404, from ``1769980520185202330`` (a snowflake id,
        not a UUID) and ``ad976f5b-…`` (a UUIDv4; tenant ids are UUIDv7, so this
        one is well-formed and genuinely absent).

        ⚠️ ``outcome == MALFORMED`` IS A STATEMENT ABOUT THE STRING, NEVER ABOUT
        THE DATABASE — it is decided before any query runs, so it cannot be
        confused with a fault, and it is deliberately NARROW. An all-digit
        selector like ``1769980520185202330`` is a legal NAME shape, so it stays
        a 404 (absent), not a 400 (impossible): widening this to "does not look
        like a UUID" would turn every name lookup into a client error and 400 the
        name-based callers that ``get_tenant`` exists to serve.

        ``found`` never raises for a malformed selector, and a DB fault still
        propagates — deciding what an unreachable database means is the route's
        job, exactly as it is for ``_lookup_tenant_posture``.
        """
        selector = str(name_or_id)
        try:
            tenant_uuid: UUID | None = UUID(selector)
        except ValueError, TypeError, AttributeError:
            tenant_uuid = None
        if tenant_uuid is None and not _TENANT_NAME_SHAPE.fullmatch(selector):
            # Decided WITHOUT a query: this string cannot be a tenant id and
            # cannot be a tenant name, so there is nothing to look up.
            return TenantLookup(tenant=None, outcome=TENANT_LOOKUP_MALFORMED)
        session = self.get_session()
        try:
            if tenant_uuid is not None:
                tenant = session.query(Tenant).filter(Tenant.id == tenant_uuid).first()
            else:
                tenant = _tenant_by_name(session, selector)
            if tenant is None:
                return TenantLookup(tenant=None, outcome=TENANT_LOOKUP_NOT_FOUND)
            return TenantLookup(tenant=tenant, outcome=TENANT_LOOKUP_FOUND)
        finally:
            session.close()

    def count_tenant_seats(
        self,
        tenant_id: str,
        as_of: datetime | None = None,
        *,
        app_key: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Count a tenant's BILLABLE SEATS at ``as_of``. ``None`` = unknown tenant.

        ⭐ THE PREDICATE LIVES HERE, next to the columns that define it, and that
        placement is the point of this method.

        billing-service bills a per-seat access charge ($25/seat, George
        2026-09-10) and needs a seat count. It already calls
        ``GET /internal/licenses``, so counting that list in billing looks free —
        and it would FABRICATE REVENUE. Measured 2026-09-10: **164** rows carry
        ``status='active'`` (107 ``internal`` tenants, 34 ``service``, 15
        ``test``, **8** ``customer``), and neither ``kind`` nor ``archived_at`` is
        on that endpoint at all. A naive count over it bills ~156 phantom seats,
        roughly $3,900/month of invented revenue. Assembling the predicate in
        billing would also mean joining two endpoints and keeping a copy of this
        service's own policy in another repo's code.

        ⛔ READ THIS BEFORE RELYING ON THE COUNT: IT DOES **NOT** FILTER ON
        ``kind``. Measured live 2026-09-10 after deploy, which is how the
        overstatement above was caught — ``n8n-dev`` (``kind='internal'``, 23
        active licences) returns ``seats: 23``, not 0. ``tenant_kind`` is
        DISCLOSED in the response instead, deliberately: a seat count of 0 for a
        non-customer tenant is a fabricated zero, indistinguishable from a
        customer who genuinely has no seats, and this module refuses to
        manufacture those anywhere else (see the 404-not-zero branch below).

        So what actually stops an internal tenant being billed 23 seats is TWO
        gates in billing, neither of them here, both measured 2026-09-10:
          1. a per-seat charge only exists for a tenant an operator materialised
             ``tenant_access_charge`` terms for — and there were 0 such rows; and
          2. ``services.billing.generate_billing`` skips the ENTIRE access-charge
             block when ``effective_billing_mode`` is ``internal``, which every
             ``byo_mode`` of ``local_allowed`` / ``platform_only`` resolves to.
        ⚠️ Gate 2 is about ``byo_mode``, NOT about ``kind`` — and they disagree:
        ``ahtapol`` is ``kind='customer'`` with ``byo_mode='local_allowed'``, so
        it bills $0 today despite being a customer. Do not treat `kind` as a
        billability flag in either direction.

        ⚠️ ``status='active'`` IS NOT VALIDITY, and this is the trap the stored
        column sets. Nothing in the estate transitions a row out of ``'active'``
        when ``expires_at`` passes (see ``expire_stale_licenses`` and
        ``License.effective_status``), and a row whose ``starts_at`` is in the
        future is ``pending``, not active. So the count here applies the same
        three-part rule ``effective_status`` does, but against ``as_of`` rather
        than ``now()`` — a closed billing period must be counted at its own
        instant, not at whenever someone re-ran the query.

        ⛔ THAT RULE NOW LIVES IN ``effectively_active_clauses`` AND IS SHARED WITH
        ``/internal/licenses`` (``list_licenses(effective=True)``). It used to be
        written out only here, and the other endpoint — which billing reads FOR
        THE SAME INVOICE — filtered the stored ``status`` alone, so one
        stored-active-but-expired row was a seat on one endpoint and not the
        other. Do not inline the expression back into either call site.

        ⭐ TWO COUNTS ARE RETURNED BECAUSE THEY ARE NOT THE SAME NUMBER, and
        choosing between them is a commercial decision, not an implementation
        detail. ``seats`` is active LICENCES — the agreed basis. ``subjects`` is
        distinct HUMANS. They diverge because provisioning mints one row per
        onboarded user per app while fr/kanban/n8n are sold per-tenant (see
        ``get_active_license``), so one person holding three products is three
        licences and one subject. Measured 2026-09-10: ahtapol has 3 active
        licences across 3 apps but only **2** distinct subjects — $75 vs $50 at
        $25/seat. Returning only the billed number would hide that entirely.

        ⭐ THE BASIS IS A PERSON (George, 2026-09-11) — AND ``subjects`` IS NOT
        THAT NUMBER. ``subjects`` counts distinct ``License.subject_id``, and a
        ``subject`` row is an authenticated PRINCIPAL, which includes machine
        accounts. Measured on the primary 2026-09-11, with the customer tenants
        named because two of the five are wrong under the person basis:
          * ``ahtapol`` — 3 active licences, 2 distinct subjects, spanning realms
            ``portal`` AND ``service``; one of the two is ``svc-workers-ahtapol``,
            a robot. ``subjects`` would bill it $25/month as a colleague.
          * ``cdmsfirst`` — 2 active licences, 2 distinct subjects, same shape
            (``portal`` + ``service``).
          * ``better-results-group``, ``book-fulfillment``, ``subhankar-kag`` — 1
            licence / 1 subject each, realm ``secure-apps``: genuine humans.
        Estate-wide, 58 of the 254 ``subject`` rows are in realm ``service``.
        ``human_subjects`` is therefore ``subjects`` minus the subjects whose
        ``Subject.keycloak_realm`` is in ``NON_HUMAN_SUBJECT_REALMS``, and it is
        the number that matches the chosen basis.

        ⛔ THE EXCLUSION IS DISCLOSED, NEVER SILENT: ``service_subjects`` reports
        exactly what ``human_subjects`` dropped, so a reader can see WHY the human
        count is below ``subjects`` without re-querying. This module does not
        manufacture a quiet number anywhere else either — ``tenant_kind`` is
        disclosed rather than filtered on, and an unknown tenant is ``None`` rather
        than 0. The three subject fields PARTITION ``subjects`` exactly:
        ``human_subjects + service_subjects + unresolved_subjects == subjects``.

        ⚠️ ``unresolved_subjects`` EXISTS SO THAT AN UNKNOWN IS NEVER PROMOTED TO A
        PERSON. It counts distinct ``License.subject_id`` values with no matching
        ``subject`` row — including ``NULL``, were one ever possible. Both shapes
        are currently IMPOSSIBLE at the schema level and the field should always
        read 0: ``License.subject_id`` is ``nullable=False`` with an FK to
        ``subject.id`` ``ON DELETE CASCADE``, so a deleted person takes their
        licences with them. It is still counted, because the alternative is a join
        that decides the question by accident — an inner join silently deletes the
        licence from every subject count (under-billing, invisibly), and an outer
        join with ``realm != 'service'`` turns the unknown into a PERSON
        (over-billing a ghost). A third field makes a broken FK or a future
        nullable column show up as a number nobody can read as either.

        ⚠️ DO NOT COUNT A TENANT'S PEOPLE VIA ``subject.tenant_id``. 203 of the 254
        rows have it NULL (``secure-apps`` 145/166, ``service`` 41/58, ``portal``
        15/28) — migration 029 added the column with no backfill and NULL means
        "nobody has stated this subject's tenant", not "no tenant" (see the column
        comment). Counting that way reported "``better-results-group`` has 0
        humans" when it has 1. The authoritative person→tenant link is
        ``License.subject_id → subject.id``, which is what the join below uses.

        ``seats`` and ``subjects`` are UNCHANGED by the person basis, deliberately:
        billing reads them today, and the operator's choice has to stay auditable
        against the licence number it replaced.

        An ARCHIVED tenant returns 0 with ``basis='tenant_archived'`` rather than
        a bare 0: the caller can tell "turned off" from "has no seats". All five
        counts are present on that branch too, so the response shape never varies
        by branch — a caller must never have to test for a missing key.
        """
        # ⭐ ``app_key`` + ``period_start``/``period_end`` (2026-09-17, per-seat billing for
        # CommonPractice). Without ``app_key`` a tenant holding a licence for ANY other app
        # inflated a single product's seat count; without a period, a mid-month leaver
        # was invisible at period end. Both are optional and default to the original
        # all-apps, point-in-time behaviour, which every existing caller keeps.
        # An unknown ``app_key`` answers ``{"app_not_found": True}`` — never a bare 0,
        # which would be a fabricated "no seats" for a typo.
        if (period_start is None) != (period_end is None):
            raise ClientSafeError("period_start and period_end must be given together")
        if period_start is not None and period_end is not None and period_start >= period_end:
            raise ClientSafeError("period_start must be before period_end")
        period = period_start is not None
        as_of = as_of or datetime.now(UTC)
        session = self.get_session()
        try:
            try:
                tenant = session.query(Tenant).filter(Tenant.id == UUID(str(tenant_id))).first()
            except ValueError, TypeError:
                tenant = _tenant_by_name(session, tenant_id)
            if tenant is None:
                return None

            app_id = None
            if app_key is not None:
                app_row = session.query(App.id).filter(App.key == app_key).first()
                if app_row is None:
                    return {"app_not_found": True, "app_key": app_key}
                app_id = app_row[0]

            base = {
                "tenant_id": str(tenant.id),
                "tenant_kind": tenant.kind,
                "tenant_archived": tenant.archived_at is not None,
                "as_of": as_of,
                "app_key": app_key,
                "period_start": period_start,
                "period_end": period_end,
            }
            if tenant.archived_at is not None:
                # An archived tenant is off; it cannot accrue seat charges. Said
                # with a basis rather than a silent 0. ⚠️ Every field the
                # active branch returns appears here as well — a response whose
                # KEYS depend on the branch forces the caller to guess.
                return {
                    **base,
                    "seats": 0,
                    "subjects": 0,
                    "human_subjects": 0,
                    "service_subjects": 0,
                    "unresolved_subjects": 0,
                    "basis": "tenant_archived",
                }

            # ⛔ The clauses are SHARED with `/internal/licenses` via `effectively_active_clauses`
            # — do not inline them. A PERIOD count uses `active_during_clauses` instead, which
            # nothing else reads, so the shared predicate never widens for other callers.
            clauses = (
                active_during_clauses(period_start, period_end)  # type: ignore[arg-type]
                if period
                else effectively_active_clauses(as_of)
            )
            query = session.query(License.subject_id).filter(
                License.tenant_id == tenant.id, *clauses
            )
            if app_id is not None:
                query = query.filter(License.app_id == app_id)
            rows = query.all()
            # ⛔ A PARTITION OF `subjects`, NOT A SECOND FILTERED COUNT. The realms
            # are fetched for the subject ids ALREADY SELECTED by the licence
            # predicate above, so nothing here can add or remove a licence: `seats`
            # and `subjects` keep their exact prior values (billing reads them) and
            # the three person-basis fields can only redistribute `subjects`.
            #
            # Done as a second SELECT over a bounded id set rather than as a JOIN on
            # the licence query on purpose: a join would change the shape of the
            # query that produces `seats`, which is the number this service is
            # billed on, and an outer join would additionally make the
            # missing-subject case silently pick a side (see the docstring).
            subject_ids = {r[0] for r in rows}
            realm_by_subject: dict[Any, Any] = {}
            if subject_ids:
                realm_by_subject = dict(
                    session.query(Subject.id, Subject.keycloak_realm)
                    .filter(Subject.id.in_(subject_ids))
                    .all()
                )
            service_ids = {
                sid for sid in subject_ids if realm_by_subject.get(sid) in NON_HUMAN_SUBJECT_REALMS
            }
            # A subject_id the `subject` table does not resolve — or a NULL one. It
            # is promoted to NEITHER a person NOR a robot; it gets its own number.
            # Structurally impossible today (subject_id is NOT NULL + FK ON DELETE
            # CASCADE), which is why this should always read 0 and why a nonzero
            # value is a schema regression worth seeing rather than absorbing.
            unresolved_ids = subject_ids - realm_by_subject.keys()
            return {
                **base,
                "seats": len(rows),
                "subjects": len(subject_ids),
                # people, robots excluded — the operator's basis (2026-09-11)
                "human_subjects": len(subject_ids - service_ids - unresolved_ids),
                "service_subjects": len(service_ids),
                "unresolved_subjects": len(unresolved_ids),
                "basis": "active_during_period" if period else "active_licences",
            }
        finally:
            session.close()

    def set_tenant_app_byo_mode(
        self,
        name_or_id: str,
        app_key: str,
        byo_mode: str,
        offering_code: str | None = None,
    ) -> dict[str, Any] | None:
        """Set a PER-PRODUCT BYO posture override for ``(tenant, app)``.

        Overrides the tenant-wide ``Tenant.byo_mode`` for this product only.
        Upserts the ``tenant_app_byo_mode`` row. Returns the row as a dict, or
        None if the tenant or the app (by key) does not exist. Raises
        ``ClientSafeError`` on an invalid ``byo_mode``.

        The returned dict carries ``prior_byo_mode``: the value this write
        OVERWROTE, or ``None`` when it created the row. That is what makes an
        upsert undoable. Without it the only available "undo" is
        :meth:`clear_tenant_app_byo_mode`, a DELETE — so a caller that wrote over
        an existing override and then had to roll back destroyed the tenant's
        prior posture instead of restoring it (``main.provision_tenant_offering``
        did exactly that on every failed billing hop).

        It is captured HERE, inside the session that performs the write, on the
        row already fetched below. A caller doing read-then-write would span two
        pooled connections with no lock between them, and the value it read back
        would be whatever a concurrent writer left — a TOCTOU on the tenant's
        live posture.

        ``offering_code`` records WHICH offering produced this posture, and is
        ALWAYS written — including as ``None``. That is deliberate: a direct
        posture write (``PUT .../apps/{app_key}/byo-mode``) did not come from an
        offering, so leaving a previous code in place would attribute the new
        posture to an offering that no longer explains it. ``prior_offering_code``
        comes back for the same reason ``prior_byo_mode`` does — the provision
        rollback has to restore BOTH, or it repairs the posture while leaving the
        provenance of a failed provision behind.
        """
        if byo_mode not in BYO_MODES:
            raise ClientSafeError(f"invalid byo_mode: {byo_mode!r}")
        session = self.get_session()
        try:
            tenant = self._resolve_tenant(session, name_or_id)
            if tenant is None:
                return None
            app = session.query(App).filter(App.key == app_key).first()
            if app is None:
                return None
            row = (
                session.query(TenantAppByoMode)
                .filter(
                    TenantAppByoMode.tenant_id == tenant.id,
                    TenantAppByoMode.app_id == app.id,
                )
                .first()
            )
            if row is None:
                prior_byo_mode: str | None = None
                prior_offering_code: str | None = None
                row = TenantAppByoMode(
                    tenant_id=tenant.id,
                    app_id=app.id,
                    byo_mode=byo_mode,
                    offering_code=offering_code,
                )
                session.add(row)
            else:
                # Read BEFORE the overwrite — after ``setattr`` the old value is
                # gone from the instance as well as from the row.
                prior_byo_mode = str(row.byo_mode)
                prior_offering_code = (
                    str(row.offering_code) if row.offering_code is not None else None
                )
                setattr(row, "byo_mode", byo_mode)
                setattr(row, "offering_code", offering_code)
                setattr(row, "updated_at", utc_now())
            session.commit()
            return {
                "tenant_id": str(tenant.id),
                "tenant_name": tenant.name,
                "app_id": str(app.id),
                "app_key": app.key,
                "byo_mode": byo_mode,
                "offering_code": offering_code,
                "prior_byo_mode": prior_byo_mode,
                "prior_offering_code": prior_offering_code,
            }
        finally:
            session.close()

    def list_tenant_offerings(self, name_or_id: str) -> list[dict[str, Any]] | None:
        """Every per-product posture row this tenant carries, newest write first.

        This is the honest answer to "what does this tenant have". Provisioning
        an offering writes its EFFECTS to two stores — the posture row here and
        the markup rule + access charge in billing_service (S2S) — so this
        service can only report its own half. It reports it completely.

        ``offering_code`` is the offering that produced the posture, or None.
        **None does NOT mean "no offering".** It means the provenance is unknown:
        the posture was set directly via ``PUT .../apps/{app_key}/byo-mode``, or
        it predates migration 025, which added the column and could not backfill
        it because the code had never been written down anywhere. A caller that
        renders None as a blank cell is asserting "nothing provisioned" about
        rows that may well have been — render it as unknown, not as empty.

        Returns None when the tenant does not exist, so a caller can 404 rather
        than report an empty list for a tenant that is not there. A tenant with
        no overrides returns ``[]`` — every product then resolves to the
        tenant-wide ``Tenant.byo_mode``, which is NOT reported here because it is
        not a per-product fact; read it from the tenant itself.
        """
        session = self.get_session()
        try:
            tenant = self._resolve_tenant(session, name_or_id)
            if tenant is None:
                return None
            rows = (
                session.query(TenantAppByoMode, App)
                .join(App, App.id == TenantAppByoMode.app_id)
                .filter(TenantAppByoMode.tenant_id == tenant.id)
                .order_by(TenantAppByoMode.updated_at.desc())
                .all()
            )
            return [
                {
                    "app_key": app.key,
                    "app_name": app.name,
                    "byo_mode": row.byo_mode,
                    "offering_code": row.offering_code,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row, app in rows
            ]
        finally:
            session.close()

    def get_tenant_app_byo_mode(self, name_or_id: str, app_key: str) -> dict[str, Any] | None:
        """The per-``(tenant, app)`` override row, or None when there is no override.

        The read that completes the set. Set and clear have existed per-app since
        the posture rows were introduced (``set_tenant_app_byo_mode`` /
        ``clear_tenant_app_byo_mode``), and ``list_tenant_offerings`` reads them
        in bulk — but there was no way to ask about ONE product without pulling
        every row for the tenant and filtering client-side.

        DISTINGUISH THREE OUTCOMES, which callers previously could not:

          * ``None`` with an unknown tenant or app -> the caller is asking about
            something that does not exist. The route turns this into a 404.
          * ``None`` with both known -> no override; the product resolves to the
            tenant-wide ``Tenant.byo_mode``. Also a 404 at the route, but for a
            different reason, which is why the route re-checks existence itself
            rather than inferring from this None.
          * a dict -> an override exists, and ``offering_code`` carries its
            provenance under the same rule as ``list_tenant_offerings``: None
            means UNKNOWN, never "no offering".

        This does NOT resolve the effective posture. For "what applies to this
        product right now", use ``GET /tenants/{n}/byo-mode?app=<key>``, which
        falls back to the tenant-wide value. This one answers only "is there an
        override row, and what is in it" — the two questions have different
        answers whenever no override exists, and conflating them is how a caller
        ends up reporting ``byo_required`` as ``local_allowed``.
        """
        session = self.get_session()
        try:
            tenant = self._resolve_tenant(session, name_or_id)
            if tenant is None:
                return None
            app = session.query(App).filter(App.key == app_key).first()
            if app is None:
                return None
            row = (
                session.query(TenantAppByoMode)
                .filter(
                    TenantAppByoMode.tenant_id == tenant.id,
                    TenantAppByoMode.app_id == app.id,
                )
                .first()
            )
            if row is None:
                return None
            return {
                "app_key": app.key,
                "app_name": app.name,
                "byo_mode": row.byo_mode,
                "offering_code": row.offering_code,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        finally:
            session.close()

    def clear_tenant_app_byo_mode(self, name_or_id: str, app_key: str) -> bool:
        """Remove a per-product BYO posture override for ``(tenant, app)``.

        Reverts the product to the tenant-wide ``Tenant.byo_mode``. Returns True
        if an override row was deleted, False if none existed (or tenant/app
        unknown).
        """
        session = self.get_session()
        try:
            tenant = self._resolve_tenant(session, name_or_id)
            if tenant is None:
                return False
            app = session.query(App).filter(App.key == app_key).first()
            if app is None:
                return False
            deleted = (
                session.query(TenantAppByoMode)
                .filter(
                    TenantAppByoMode.tenant_id == tenant.id,
                    TenantAppByoMode.app_id == app.id,
                )
                .delete()
            )
            session.commit()
            return bool(deleted)
        finally:
            session.close()

    def _resolve_tenant(self, session: Session, name_or_id: str) -> Tenant | None:
        """Resolve a Tenant by UUID or human name within a caller's session."""
        try:
            return session.query(Tenant).filter(Tenant.id == UUID(str(name_or_id))).first()
        except ValueError, TypeError:
            return _tenant_by_name(session, name_or_id)

    # Offering operations (per-product usage-policy templates)
    _OFFERING_USAGE_FIELDS = (
        "markup_pct",
        "surcharge_input_per_1m_usd",
        "surcharge_output_per_1m_usd",
        "surcharge_cache_creation_per_1m_usd",
        "surcharge_cache_read_per_1m_usd",
        "surcharge_per_request_usd",
    )

    def create_offering(
        self,
        app_key: str,
        code: str,
        name: str,
        byo_mode: str,
        *,
        usage: dict[str, Any] | None = None,
        access_price_usd: float | None = None,
        access_cadence: str | None = None,
        included_allowance_usd: float | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Upsert an offering (usage-policy + access template) for ``(app, code)``.

        Returns the offering as a dict, or None if the app (by key) does not
        exist. Raises ``ClientSafeError`` on an invalid ``byo_mode`` /
        ``access_cadence``.
        """
        if byo_mode not in BYO_MODES:
            raise ClientSafeError(f"invalid byo_mode: {byo_mode!r}")
        if access_cadence is not None and access_cadence not in ("monthly", "one_time"):
            raise ClientSafeError(f"invalid access_cadence: {access_cadence!r}")
        usage = usage or {}
        session = self.get_session()
        try:
            app = session.query(App).filter(App.key == app_key).first()
            if app is None:
                return None
            row = (
                session.query(Offering)
                .filter(Offering.app_id == app.id, Offering.code == code)
                .first()
            )
            if row is None:
                row = Offering(app_id=app.id, code=code, name=name, byo_mode=byo_mode)
                session.add(row)
            else:
                setattr(row, "name", name)
                setattr(row, "byo_mode", byo_mode)
                setattr(row, "updated_at", utc_now())
            for field in self._OFFERING_USAGE_FIELDS:
                if field in usage:
                    setattr(row, field, usage[field])
            setattr(row, "access_price_usd", access_price_usd)
            if access_cadence is not None:
                setattr(row, "access_cadence", access_cadence)
            setattr(row, "included_allowance_usd", included_allowance_usd)
            setattr(row, "notes", notes)
            session.commit()
            session.refresh(row)
            return row.to_dict()
        finally:
            session.close()

    def get_offering(self, app_key: str, code: str) -> dict[str, Any] | None:
        """Get a single offering by ``(app_key, code)`` as a dict, or None."""
        session = self.get_session()
        try:
            app = session.query(App).filter(App.key == app_key).first()
            if app is None:
                return None
            row = (
                session.query(Offering)
                .options(joinedload(Offering.app))
                .filter(Offering.app_id == app.id, Offering.code == code)
                .first()
            )
            return row.to_dict() if row else None
        finally:
            session.close()

    def list_offerings(self, app_key: str | None = None) -> list[dict[str, Any]]:
        """List offerings, optionally filtered to one product (by app key)."""
        session = self.get_session()
        try:
            query = session.query(Offering).options(joinedload(Offering.app))
            if app_key is not None:
                app = session.query(App).filter(App.key == app_key).first()
                if app is None:
                    return []
                query = query.filter(Offering.app_id == app.id)
            rows = query.order_by(Offering.created_at).all()
            return [r.to_dict() for r in rows]
        finally:
            session.close()

    def delete_offering(self, app_key: str, code: str) -> bool:
        """Delete an offering by ``(app_key, code)``. True if a row was removed."""
        session = self.get_session()
        try:
            app = session.query(App).filter(App.key == app_key).first()
            if app is None:
                return False
            deleted = (
                session.query(Offering)
                .filter(Offering.app_id == app.id, Offering.code == code)
                .delete()
            )
            session.commit()
            return bool(deleted)
        finally:
            session.close()

    # App operations
    def create_app(
        self,
        key: str,
        name: str,
        description: str | None = None,
        audience: str = "platform",
        *,
        issuers: Sequence[str],
    ) -> App:
        """Create a new app AND its issuers (``app_issuer``) in ONE transaction.

        ``issuers`` is REQUIRED and must name at least one ``tenant_owner`` key
        (migration 050): an app with no issuer could have no licence minted or
        revoked by anyone but the break-glass client, so it may not exist at all.

        ``audience`` defaults to ``platform`` so an app registered by a caller that
        does not know about the field is NOT offered to people. The CHECK constraint
        rejects anything outside the three values; validate at the edge so the caller
        gets a 4xx rather than an IntegrityError.
        """
        owners = [str(o) for o in issuers] if not isinstance(issuers, str) else []
        if not owners:
            raise ClientSafeError(
                f"app {key!r} needs at least one issuer (a tenant_owner key); "
                "an app without an app_issuer row cannot exist"
            )
        session = self.get_session()
        try:
            app = App(key=key, name=name, description=description, audience=audience)
            session.add(app)
            session.flush()
            for owner in dict.fromkeys(owners):
                session.add(AppIssuer(app_id=app.id, owner_key=owner))
            session.commit()
            session.refresh(app)
            return app
        finally:
            session.close()

    def set_app_audience(self, key: str, audience: str) -> App | None:
        """Reclassify an app. Returns the app, or None if the key is unknown.

        Used by ``scripts/seed_apps.py`` to reconcile an already-registered app, so a
        wrong classification is fixable by re-seeding rather than by hand-written SQL.
        Validated here so the caller gets a ``ClientSafeError`` rather than the
        ``ck_app_audience`` IntegrityError.
        """
        if audience not in ("human", "platform", "automation"):
            raise ClientSafeError(f"invalid audience: {audience!r}")
        session = self.get_session()
        try:
            app = session.query(App).filter(App.key == key).first()
            if app is None:
                return None
            # setattr, matching ``set_tenant_byo_mode`` above: SQLAlchemy types the
            # attribute as ``Column[str]``, so a direct assignment fails ty.
            setattr(app, "audience", audience)
            session.commit()
            session.refresh(app)
            return app
        finally:
            session.close()

    def get_app(self, app_id: UUID) -> App | None:
        """Get app by ID"""
        session = self.get_session()
        try:
            return session.query(App).filter(App.id == app_id).first()
        finally:
            session.close()

    def get_app_by_key(self, key: str) -> App | None:
        """Get app by key"""
        session = self.get_session()
        try:
            return session.query(App).filter(App.key == key).first()
        finally:
            session.close()

    # Bundle operations (migrations 028 + 039)
    def get_bundle_by_key(self, key: str) -> Bundle | None:
        """Get a bundle by its key (e.g. ``non-agentic-ai``). Read-only."""
        session = self.get_session()
        try:
            return session.query(Bundle).filter(Bundle.key == key).first()
        finally:
            session.close()

    def create_bundle(
        self,
        key: str,
        name: str,
        description: str | None = None,
        *,
        ensurers: Sequence[str],
    ) -> Bundle:
        """Create a bundle AND its ensurers (``bundle_ensurer``) in ONE transaction.

        ``ensurers`` is REQUIRED and must name at least one ``tenant_owner`` key
        (migration 052, the ``create_app`` / ``app_issuer`` rule): a bundle nobody
        may ensure is reachable only through break-glass, so it may not be created.
        """
        owners = [str(o) for o in ensurers] if not isinstance(ensurers, str) else []
        if not owners:
            raise ClientSafeError(
                f"bundle {key!r} needs at least one ensurer (a tenant_owner key); "
                "a bundle without a bundle_ensurer row cannot be created"
            )
        session = self.get_session()
        try:
            bundle = Bundle(key=key, name=name, description=description)
            session.add(bundle)
            session.flush()
            for owner in dict.fromkeys(owners):
                session.add(BundleEnsurer(bundle_id=bundle.id, owner_key=owner))
            session.commit()
            session.refresh(bundle)
            return bundle
        finally:
            session.close()

    def bundle_ensurers(self, bundle_id: UUID) -> set[str]:
        """Owners allowed to ensure ``bundle_id`` (migration 052). Read-only."""
        session = self.get_session()
        try:
            return {
                str(r[0])
                for r in session.query(BundleEnsurer.owner_key)
                .filter(BundleEnsurer.bundle_id == UUID(str(bundle_id)))
                .all()
            }
        finally:
            session.close()

    def add_bundle_ensurers(self, bundle_id: UUID, owner_keys: Iterable[str]) -> int:
        """Declare ensurers for an existing bundle (idempotent). Returns rows inserted."""
        inserted = 0
        with self.engine.begin() as conn:
            for owner in owner_keys:
                result = conn.execute(
                    text(
                        "INSERT INTO bundle_ensurer (bundle_id, owner_key) VALUES (:b, :o) "
                        "ON CONFLICT (bundle_id, owner_key) DO NOTHING"
                    ),
                    {"b": str(bundle_id), "o": owner},
                )
                inserted += result.rowcount or 0
        return inserted

    def list_bundle_apps(self, bundle_id: UUID) -> list[App]:
        """The non-revoked apps in ``bundle_id``, ordered by key. Read-only."""
        session = self.get_session()
        try:
            return (
                session.query(App)
                .join(BundleApp, BundleApp.app_id == App.id)
                .filter(BundleApp.bundle_id == bundle_id, App.revoked.is_(False))
                .order_by(App.key)
                .all()
            )
        finally:
            session.close()

    def list_app_requirements(self) -> list[tuple[str, str, str]]:
        """Every ``app_requires`` edge between NON-revoked apps, as
        ``(app_key, required_app_key, caller_owner_key)`` (migration 059). Read-only;
        the table holds one row per evidenced call edge, so it is read whole."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.key, r.key, ar.caller_owner_key FROM app_requires ar "
                    "JOIN app a ON a.id = ar.app_id "
                    "JOIN app r ON r.id = ar.required_app_id "
                    "WHERE NOT a.revoked AND NOT r.revoked ORDER BY 1, 2"
                )
            ).fetchall()
        return [(str(a), str(r), str(c)) for a, r, c in rows]

    def list_bundle_callers(self, bundle_id: UUID) -> list[tuple[str, str]]:
        """The ``bundle_caller`` (owner, app key) pairs of ``bundle_id`` (migration 059),
        non-revoked apps only. Read-only."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.owner_key, a.key FROM bundle_caller c "
                    "JOIN app a ON a.id = c.app_id "
                    "WHERE c.bundle_id = :b AND NOT a.revoked ORDER BY 1, 2"
                ),
                {"b": str(bundle_id)},
            ).fetchall()
        return [(str(o), str(k)) for o, k in rows]

    def tenant_holds_bundle(self, tenant_id: str, bundle_id: UUID) -> bool:
        """True when a ``tenant_bundle`` row exists for (tenant, bundle). Read-only."""
        session = self.get_session()
        try:
            return (
                session.query(TenantBundle)
                .filter(
                    TenantBundle.tenant_id == UUID(str(tenant_id)),
                    TenantBundle.bundle_id == bundle_id,
                )
                .first()
                is not None
            )
        finally:
            session.close()

    def ensure_tenant_bundle(self, tenant_id: str, bundle_id: UUID) -> bool:
        """Record that ``tenant_id`` holds ``bundle_id``. Idempotent; True if inserted.

        Same IntegrityError-absorb idiom as ``ensure_tenant``: a concurrent second
        writer loses on the primary key and the row that won stands.
        """
        session = self.get_session()
        try:
            session.add(TenantBundle(tenant_id=UUID(str(tenant_id)), bundle_id=bundle_id))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True
        finally:
            session.close()

    def ensure_bundle_app(self, bundle_id: UUID, app_id: UUID) -> bool:
        """Put ``app_id`` in bundle ``bundle_id``. Idempotent; True if inserted.

        The one application-side writer of ``bundle_app`` (before this, membership was
        written only by migrations 028/039). Same IntegrityError-absorb idiom as
        ``ensure_tenant_bundle``: the composite primary key makes a repeat or a
        concurrent second writer a no-op. The caller validates that the app exists and
        is not revoked; the FK still refuses an unknown id.
        """
        session = self.get_session()
        try:
            session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if (
                    session.query(BundleApp)
                    .filter(BundleApp.bundle_id == bundle_id, BundleApp.app_id == app_id)
                    .first()
                    is None
                ):
                    raise  # not a duplicate: an FK violation (unknown bundle/app)
                return False
            return True
        finally:
            session.close()

    def list_tenant_app_licences(self, tenant_id: str, app_id: UUID) -> list[License]:
        """Every effectively-active, non-deleted licence for (tenant, app), oldest first.

        Read-only — deliberately NOT ``find_active_license`` (that lazily expires rows,
        a WRITE, and the provisioning dry-run must write nothing). Includes licences
        whose ``bundle_id`` is set, to ANY bundle: ``scripts/provision_tenant_bundle.py``
        needs the whole (tenant, app) candidate set to decide exists / adopt / insert /
        refuse, because billing resolves on (tenant_id, app) and drops a key with two
        candidates.
        """
        session = self.get_session()
        try:
            rows = (
                session.query(License)
                .options(joinedload(License.subject))
                .filter(
                    License.tenant_id == UUID(str(tenant_id)),
                    License.app_id == app_id,
                    License.status == "active",
                    License.deleted_at.is_(None),
                )
                .order_by(License.created_at, License.id)
                .all()
            )
            return [lic for lic in rows if lic.is_active()]
        finally:
            session.close()

    def adopt_license_into_bundle(self, license_id: UUID, bundle_id: UUID) -> bool:
        """Set ``bundle_id`` on an UNBUNDLED licence. Compare-and-set; True if it wrote.

        ``WHERE bundle_id IS NULL`` makes a concurrent second adopter (or one that
        raced a different bundle onto the row) a no-op that returns False instead of
        silently re-pointing a licence another bundle already owns.

        Adopting a DEPENDENCY licence (migration 059) PROMOTES it: a bundle that sells
        the app now holds it, so ``is_dependency`` is cleared in the same UPDATE (the
        licence stays the tenant's ONE licence for the app — no second row).
        """
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE license SET bundle_id = :b, is_dependency = false, "
                    "updated_at = now() WHERE id = :id AND bundle_id IS NULL"
                ),
                {"b": bundle_id, "id": license_id},
            )
            return result.rowcount == 1

    def list_bundle_holders(self, bundle_id: UUID) -> list[tuple[str, str | None, int]]:
        """Every tenant HOLDING ``bundle_id`` (``tenant_bundle``): (tenant id, name, n).

        ``n`` = the tenant's active, non-deleted licences carrying the bundle. Read-only.
        A change to the bundle's app set (``bundle_app``) changes what EVERY one of these
        tenants holds, which is why ``--extend-bundle`` lists them.
        """
        with self.engine.connect() as conn:
            return _bundle_holders(conn, bundle_id)

    def extend_bundle_apps(
        self, bundle_id: UUID, app_ids: list[UUID], expected_holders: int
    ) -> tuple[list[UUID] | None, int]:
        """Add ``app_ids`` to ``bundle_id`` iff it has exactly ``expected_holders`` holders.

        ONE transaction: the bundle row is locked ``FOR UPDATE`` (which conflicts with
        the ``FOR KEY SHARE`` a ``tenant_bundle`` / ``license.bundle_id`` FK insert takes,
        so no new holder can appear mid-transaction), the holder count is RE-READ, and
        only when it equals ``expected_holders`` are the ``bundle_app`` rows inserted.
        Returns (inserted app ids, or None when refused; the holder count read).
        """
        with self.engine.begin() as conn:
            conn.execute(text("SELECT id FROM bundle WHERE id = :b FOR UPDATE"), {"b": bundle_id})
            holders = len(_bundle_holders(conn, bundle_id))
            if holders != expected_holders:
                return None, holders
            inserted: list[UUID] = []
            for app_id in app_ids:
                res = conn.execute(
                    text(
                        "INSERT INTO bundle_app (bundle_id, app_id) VALUES (:b, :a) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"b": bundle_id, "a": app_id},
                )
                if res.rowcount == 1:
                    inserted.append(app_id)
            return inserted, holders

    def list_tenant_bundles(self, tenant_id: str) -> list[Bundle]:
        """The non-revoked bundles the tenant HOLDS (``tenant_bundle``), by key. Read-only."""
        session = self.get_session()
        try:
            return (
                session.query(Bundle)
                .join(TenantBundle, TenantBundle.bundle_id == Bundle.id)
                .filter(
                    TenantBundle.tenant_id == UUID(str(tenant_id)),
                    Bundle.revoked.is_(False),
                )
                .order_by(Bundle.key)
                .all()
            )
        finally:
            session.close()

    def find_bundle_app_licences(
        self, tenant_id: str, bundle_id: UUID, app_key: str
    ) -> list[License]:
        """The tenant's effectively-active per-app licence(s) for ``app_key`` in ``bundle_id``.

        Tenant ruling 2026-09-28: a bundle is the provisioning/billing object; runtime
        presents the SPECIFIC per-app licence, one per (tenant, app), carrying the
        bundle's id. Anything but exactly one row is a fault the caller must fail
        closed on. Read-only (no lazy expiry). The app must be non-revoked.
        """
        session = self.get_session()
        try:
            rows = (
                session.query(License)
                .join(App, App.id == License.app_id)
                .filter(
                    License.tenant_id == UUID(str(tenant_id)),
                    License.bundle_id == bundle_id,
                    License.status == "active",
                    License.deleted_at.is_(None),
                    App.key == app_key,
                    App.revoked.is_(False),
                )
                .order_by(License.created_at, License.id)
                .all()
            )
            return [lic for lic in rows if lic.is_active()]
        finally:
            session.close()

    def list_apps(self, *, include_revoked: bool = False) -> list[App]:
        """List apps — non-revoked only unless ``include_revoked``.

        ``include_revoked=True`` backs ``GET /internal/apps``, an IDENTITY MAP
        (billing resolves an ``X-App-Id`` key to the app uuid on rows it has
        already metered): a revoked app still owns its historical spend, so it
        must stay resolvable, flagged rather than dropped.
        """
        session = self.get_session()
        try:
            query = session.query(App)
            if not include_revoked:
                query = query.filter(App.revoked.is_(False))
            return query.order_by(App.key).all()
        finally:
            session.close()

    # Plan operations
    def create_plan(
        self,
        app_id: UUID,
        code: str,
        name: str,
        description: str | None = None,
        region_id: UUID | None = None,
        duration_days: int | None = None,
        rate_limit_rpm: int | None = None,
        rate_limit_burst: int | None = None,
        quota_monthly: int | None = None,
        token_budget_monthly: int | None = None,
    ) -> Plan:
        """Create a new plan"""
        session = self.get_session()
        try:
            kwargs: dict[str, Any] = {
                "app_id": app_id,
                "code": code,
                "name": name,
                "description": description,
                "region_id": region_id,
                "duration_days": duration_days,
                "token_budget_monthly": token_budget_monthly,
            }
            if rate_limit_rpm is not None:
                kwargs["rate_limit_rpm"] = rate_limit_rpm
            if rate_limit_burst is not None:
                kwargs["rate_limit_burst"] = rate_limit_burst
            if quota_monthly is not None:
                kwargs["quota_monthly"] = quota_monthly
            plan = Plan(**kwargs)
            session.add(plan)
            session.commit()
            session.refresh(plan)
            return plan
        finally:
            session.close()

    def update_plan(self, plan_id: UUID, **updates: Any) -> Plan | None:
        """Update an existing plan's mutable fields"""
        session = self.get_session()
        try:
            plan = session.query(Plan).filter(Plan.id == plan_id).first()
            if not plan:
                return None
            allowed = {
                "name",
                "description",
                "duration_days",
                "rate_limit_rpm",
                "rate_limit_burst",
                "quota_monthly",
                "token_budget_monthly",
                "revoked",
            }
            for key, value in updates.items():
                if key in allowed:
                    setattr(plan, key, value)
            session.commit()
            session.refresh(plan)
            return plan
        finally:
            session.close()

    def get_plan(self, plan_id: UUID) -> Plan | None:
        """Get plan by ID"""
        session = self.get_session()
        try:
            return session.query(Plan).filter(Plan.id == plan_id).first()
        finally:
            session.close()

    def list_plans(
        self,
        app_id: UUID | None = None,
        region_id: UUID | None = None,
        include_revoked: bool = False,
    ) -> list[Plan]:
        """List plans with optional filters; revoked plans only when ``include_revoked``."""
        session = self.get_session()
        try:
            query = session.query(Plan)
            if not include_revoked:
                query = query.filter(Plan.revoked.is_(False))
            if app_id:
                query = query.filter(Plan.app_id == app_id)
            if region_id:
                query = query.filter(Plan.region_id == region_id)
            return query.all()
        finally:
            session.close()

    # License operations
    def create_license(
        self,
        subject_id: UUID,
        app_id: UUID,
        plan_id: UUID,
        tenant_id: str,
        region_id: UUID | None = None,
        status: str = "active",
        starts_at: datetime | None = None,
        expires_at: datetime | None = None,
        license_metadata: dict[str, Any] | None = None,
        bundle_id: UUID | None = None,
        token_budget_override: int | None = None,
        quota_override: int | None = None,
        is_dependency: bool = False,
    ) -> License:
        """Create a new license

        ``is_dependency`` (migration 059) is passed only by bundle ensure, for a licence
        provisioned because a bundled app REQUIRES the app; it is never bundled
        (``ck_license_dependency_unbundled``) and never gets a user token.

        ``bundle_id`` (migration 039) is passed only by scripts/provision_tenant_bundle.py,
        for a per-app licence provisioned under a tenant's bundle.

        ``token_budget_override`` / ``quota_override`` (migration 041, #239): this
        licence's own budget / quota, stored in the SAME INSERT as the licence so no
        reader (provisioning push, reconcile sweep) can ever see the licence without
        them. None = the plan's value. Passed only by operator provisioning scripts.
        """
        session = self.get_session()
        try:
            license = License(
                subject_id=subject_id,
                app_id=app_id,
                plan_id=plan_id,
                tenant_id=tenant_id,
                bundle_id=bundle_id,
                is_dependency=is_dependency,
                token_budget_override=token_budget_override,
                quota_override=quota_override,
                region_id=region_id,
                status=status,
                starts_at=_as_utc(starts_at) or datetime.now(UTC),
                expires_at=_as_utc(expires_at),
                license_metadata=license_metadata,
            )
            session.add(license)
            session.commit()
            session.refresh(license)
            # Eagerly load app and plan for enriched response
            _ = license.app
            _ = license.plan
            return license
        finally:
            session.close()

    def get_license(self, license_id: UUID) -> License | None:
        """Get license by ID"""
        session = self.get_session()
        try:
            return (
                session.query(License)
                .options(joinedload(License.app), joinedload(License.plan))
                .filter(License.id == license_id)
                .first()
            )
        finally:
            session.close()

    def set_license_token_budget_override(
        self, license_id: UUID, token_budget_override: int | None
    ) -> License | None:
        """Set (or clear) a licence's own monthly token budget (migration 041).

        WHY THIS EXISTS. ``token_budget_override`` was INSERT-ONLY: the operator
        provisioning scripts wrote it at create time and every read published it,
        but no HTTP path updated it. Editing billing's ``token_budgets`` row
        instead does not stick — ``sync_from_licenses`` upserts that budget FROM
        this column (else the plan's ``token_budget_monthly``), so the licence is
        the durable lever and billing is the follower.

        ``None`` CLEARS the override, returning the licence to the plan-driven
        budget. That is a real operation rather than an omission, which is why
        the route's body makes the field REQUIRED: an absent key must not be
        indistinguishable from an explicit clear.

        UPDATE only, one column: it cannot create a licence, and the caller has
        already authorized the licence's tenant. ``updated_at`` advances via the
        column's ``onupdate``. Returns the updated row with app/plan loaded (the
        response builder reads them) or None when the id does not exist.
        """
        session = self.get_session()
        try:
            license = (
                session.query(License)
                .options(joinedload(License.app), joinedload(License.plan))
                .filter(License.id == license_id)
                .first()
            )
            if license is None:
                return None
            setattr(license, "token_budget_override", token_budget_override)
            session.commit()
            session.refresh(license)
            # Same eager load as create_license: the session closes here and the
            # route builds a response from these relationships afterwards.
            _ = license.app
            _ = license.plan
            return license
        finally:
            session.close()

    def expire_stale_licenses(self, tenant_id: str, app_id: UUID, subject_id: UUID) -> int:
        """Transition time-expired-but-still-``status='active'`` rows to ``'expired'``.

        Scoped to ONE ``(tenant_id, app_id, subject_id)`` key — the key being
        provisioned. Returns the number of rows transitioned.

        WHY THIS EXISTS (the renewal deadlock)
        --------------------------------------
        Two different notions of "active" coexist in this module:

        * EFFECTIVE validity is time-aware — ``License.is_active()`` checks
          ``status`` AND ``starts_at`` AND ``expires_at``; it backs introspection
          (``license_inactive``) and ``get_active_license`` (``/licenses/active``).
        * STRUCTURAL validity is status-only — the partial unique index
          ``uq_license_active_tenant_app_subject`` is ``WHERE status = 'active'``.

        NOTHING in this service (or any sibling: no CronJob, no scheduler, no
        sweep) ever transitions a license out of ``status='active'`` when its
        ``expires_at`` passes. So an expired row is simultaneously:

        1. invisible to ``/licenses/active`` (time-filtered) — the tenant is told
           it has NO license;
        2. still occupying the partial unique index slot — a replacement cannot
           be INSERTed;
        3. returned by ``create_user_license``'s idempotency branch as
           "returning it (idempotent)" — re-provisioning hands back the DEAD row.

        Net effect: the license is UNRENEWABLE through the normal path. Observed
        in production on a real customer tenant (app ``kag``, expired 2026-07-18).
        This method is the lazy expire-on-WRITE that breaks the deadlock: the
        next provisioning attempt retires the corpse and frees the index slot.

        WHY WRITE-PATH ONLY
        -------------------
        Deliberately NOT called from ``get_active_license`` / ``/licenses/active``.
        That is the ext_authz hot path behind a 30s cache; a row lock + WAL flush
        there would be a per-request write on the platform auth path. It already
        filters the time window, so expired rows are invisible to it regardless.

        CONCURRENCY (licenses runs ``replicas: 2``)
        -------------------------------------------
        The stale rows are taken with ``SELECT ... FOR UPDATE``. Under READ
        COMMITTED (postgres default), when replica B blocks on a row locked by
        replica A and A then commits ``status='expired'``, postgres does NOT hand
        B the stale snapshot: it re-evaluates the query's WHERE clause against
        the newly-committed row version (EvalPlanQual). ``status = 'active'`` no
        longer holds, so the row is FILTERED OUT of B's result set entirely.
        B therefore transitions nothing and returns 0 — it does not double-write
        and it cannot resurrect the row. The explicit ``!= "active"`` re-check in
        the loop below is belt-and-braces for the same invariant, so the
        behaviour does not silently depend on the isolation level staying READ
        COMMITTED.

        Both replicas then proceed to INSERT a replacement and the loser hits
        ``uq_license_active_tenant_app_subject``. That leg needs no new code:
        ``create_user_license`` already catches ``IntegrityError`` and re-runs
        ``find_active_license`` to return the winner's row (main.py, the
        ``except IntegrityError:`` block at the ``create_license`` call).

        Idempotent by construction: a second call finds nothing matching
        ``status='active' AND expires_at < now()`` and returns 0.

        PERPETUAL LICENSES ARE NEVER TOUCHED. ``expires_at IS NULL`` means
        perpetual (see the column comment on ``License.expires_at``) and NULL
        comparisons are never true, but the predicate is written EXPLICITLY
        because dropping it would expire every perpetual license in the estate —
        a far worse outage than the bug this fixes. Future-dated licenses are
        likewise untouched (``expires_at < now()`` is false for them).
        """
        session = self.get_session()
        try:
            now = datetime.now(UTC)
            stale = (
                session.query(License)
                .filter(
                    License.tenant_id == tenant_id,
                    License.app_id == app_id,
                    License.subject_id == subject_id,
                    License.status == "active",
                    License.expires_at.is_not(None),
                    License.expires_at < now,
                )
                .with_for_update()
                .all()
            )
            transitioned = 0
            for lic in stale:
                # Re-qualify under READ COMMITTED: if a concurrent replica won
                # the row and already retired it, do nothing (see docstring).
                if lic.status != "active":
                    continue
                # setattr(): the repo-wide idiom for writing to a Column-typed
                # attribute without tripping ty's invalid-assignment (see
                # revoke_license / introspect_token).
                setattr(lic, "status", "expired")
                setattr(lic, "updated_at", now)
                transitioned += 1
            session.commit()
            if transitioned:
                logger.info(
                    "Lazily expired %d stale active license(s) for tenant=%s app=%s subject=%s",
                    transitioned,
                    tenant_id,
                    app_id,
                    subject_id,
                )
            return transitioned
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def count_stale_licenses(self, now: datetime | None = None) -> int:
        """How many licences are STRUCTURALLY active but time-expired.

        The independent second opinion behind the sweep's anti-vacuity and
        over-fire guards (``scripts/expire_stale_licences.py``). Hand-written SQL
        ON PURPOSE: if this reused ``expire_all_stale_licenses``'s ORM query the
        guards could not fail, because any drift in that selection would move
        both numbers together and every comparison would be a tautology. This
        says, in its own words, "a row the estate still treats as occupying an
        active licence slot, whose validity window has closed".

        ``now`` is accepted so a caller can evaluate the count, the sweep and the
        recount at ONE instant. Without that, a licence crossing its
        ``expires_at`` between two calls — or clock skew between the application
        process and postgres — is indistinguishable from predicate drift.
        """
        session = self.get_session()
        try:
            row = session.execute(
                text(_STALE_LICENSE_COUNT_SQL), {"now": now or datetime.now(UTC)}
            ).first()
            return int(row[0]) if row else 0
        finally:
            session.close()

    def count_expired_unrevoked_tokens(self, now: datetime | None = None) -> int:
        """How many tokens are unrevoked but time-expired.

        The independent second opinion behind the sweep's anti-vacuity and
        over-fire guards (``scripts/revoke_expired_tokens.py``). Hand-written SQL
        ON PURPOSE: if this reused ``revoke_all_expired_tokens``'s ORM query the
        guards could not fail, because any drift in that selection would move both
        numbers together and every comparison would be a tautology.

        ``now`` is accepted so a caller can evaluate the count, the sweep and the
        recount at ONE instant — otherwise a token crossing its ``expires_at``
        mid-run, or clock skew between this process and postgres, is
        indistinguishable from predicate drift.
        """
        session = self.get_session()
        try:
            row = session.execute(
                text(_EXPIRED_TOKEN_COUNT_SQL), {"now": now or datetime.now(UTC)}
            ).first()
            return int(row[0]) if row else 0
        finally:
            session.close()

    def revoke_all_expired_tokens(
        self,
        *,
        limit: int = 500,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> list[str]:
        """Revoke every time-expired token in the estate. Returns ids, oldest first.

        WHY THIS EXISTS
        ---------------
        Nothing else revokes a token when it expires. ``revoke_tokens_for_license``
        is scoped to one licence and is only called on an explicit revoke or
        rotation, so expired-but-unrevoked rows accumulate for ever. That is the
        population #135 had to defend against: its ``max()`` over
        ``Token.revoked.is_(False)`` returned a PAST expiry, so a rotation minted a
        credential that was already dead AND destroyed the working ones. Prod held
        two such tokens as recently as 2026-08-17.

        The n8n workflow ``License Expiration Handler`` was supposed to do this. It
        cannot: it lists licences through ``GET /licenses?status=active``, which is
        TENANT-SCOPED by the ``X-Tenant-Id`` header, and its credential carries one
        tenant. An estate-wide sweep cannot be built on a tenant-scoped list call —
        no credential change fixes that. Hence a DAL-level sweep with no tenant
        concept at all.

        ``dry_run`` measures without writing. ``limit`` caps one sweep; the caller
        is responsible for noticing when the estate outgrows it (the script's
        truncation guard does).
        """
        session = self.get_session()
        try:
            at = now or datetime.now(UTC)
            rows = (
                session.query(Token)
                .filter(
                    Token.revoked.is_(False),
                    Token.expires_at.isnot(None),
                    Token.expires_at < at,
                )
                .order_by(Token.expires_at.asc())
                .limit(limit)
                .all()
            )
            ids = [str(t.id) for t in rows]
            if dry_run:
                return ids
            # Bulk UPDATE keyed on the ids just selected — the same idiom as
            # `revoke_tokens_for_license`. Per-row attribute assignment would write
            # the same rows but is not how this DAL expresses a set-update.
            session.query(Token).filter(Token.id.in_([t.id for t in rows])).update(
                {"revoked": True, "revoked_at": at}, synchronize_session=False
            )
            session.commit()
            return ids
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def expire_all_stale_licenses(
        self,
        *,
        limit: int = 500,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> list[str]:
        """UNSCOPED twin of ``expire_stale_licenses``: retire every stale licence
        in the estate. Returns the ids transitioned (as strings), oldest first.

        WHY AN UNSCOPED VARIANT EXISTS
        ------------------------------
        ``expire_stale_licenses`` is lazy expire-on-WRITE, scoped to the one
        ``(tenant_id, app_id, subject_id)`` being provisioned — so a corpse heals
        only if somebody happens to re-provision that exact key. Nothing else
        ever transitions a licence out of ``'active'``: on 2026-08-10 the prod
        estate held 144 ``active`` + 64 ``revoked`` licences and had NEVER, in its
        whole history, produced an ``expired`` row, while two rows sat
        ``status='active'`` with a past ``expires_at`` (one of them a real
        client's, app ``kag``, expired 2026-07-18). Each of those is invisible to
        ``/licenses/active`` yet still occupies the
        ``uq_license_active_tenant_app_subject`` slot — the renewal deadlock.

        The scoped method is deliberately left untouched: ``find_active_license``
        depends on its exact behaviour on the provisioning hot path.

        THE PREDICATE IS THE SCOPED METHOD'S, MINUS THE KEY
        ---------------------------------------------------
        ``status = 'active' AND expires_at IS NOT NULL AND expires_at < now``.

        * ``expires_at IS NOT NULL`` is written EXPLICITLY even though NULL
          comparisons are never true: ``expires_at IS NULL`` means PERPETUAL, and
          dropping this leg would expire every perpetual licence in the estate —
          an outage far worse than the bug this fixes. Estate-wide, that mistake
          would be unrecoverable by hand.
        * ``starts_at`` IS ABSENT, and must stay absent. A future-dated (pending)
          row is genuinely the licence that key will have AND it already occupies
          the unique-index slot; judging it dead would retire it and send the
          renewal that follows straight into a 409. Upstream excludes it for the
          same reason (see ``find_active_license``).
        * ``<``, not ``<=``, matching the scoped method and ``effective_status``
          (which reports a row as ``expired`` only once ``expires_at < now``). At
          exact equality ``effective_status`` still says ``active``, so ``<=``
          would let this sweep retire a row every reader in the service considers
          live. One microsecond of latency, in the only safe direction.

        CONCURRENCY (licenses runs ``replicas: 2``, and this may also run as a
        scheduled Job while the API is serving)
        -------------------------------------------------------------------
        Identical to the scoped method: rows are taken with
        ``SELECT ... FOR UPDATE``, and under READ COMMITTED a replica that blocks
        on a row another has just retired does NOT get the stale snapshot —
        postgres re-evaluates the WHERE clause against the newly-committed row
        version (EvalPlanQual), ``status = 'active'`` no longer holds, and the row
        is filtered out of the second reader's result set entirely. The explicit
        ``!= "active"`` re-check below is belt-and-braces for the same invariant
        so the behaviour does not silently depend on the isolation level.

        ``limit`` caps ONE sweep so a first run over a large backlog cannot hold
        thousands of row locks in a single transaction. Selection is oldest-first
        (``expires_at`` ASC, then ``id``) so repeated runs drain the backlog from
        the oldest end instead of re-measuring the same rows; the caller is
        responsible for noticing that a capped sweep left work behind (the CLI's
        TRUNCATED leg).

        ``dry_run`` returns the ids that WOULD be transitioned and writes
        nothing — no UPDATE, no commit, and no ``FOR UPDATE`` either, because a
        measurement must not block the provisioning path it is measuring.
        """
        session = self.get_session()
        try:
            now = now or datetime.now(UTC)
            query = (
                session.query(License)
                .filter(
                    License.status == "active",
                    License.expires_at.is_not(None),
                    License.expires_at < now,
                )
                .order_by(License.expires_at.asc(), License.id.asc())
                .limit(limit)
            )
            if dry_run:
                # Read-only: no locks taken, nothing written, and the read
                # transaction is explicitly discarded rather than committed.
                would_transition = [str(lic.id) for lic in query.all()]
                session.rollback()
                return would_transition

            stale = query.with_for_update().all()
            transitioned: list[str] = []
            for lic in stale:
                # Re-qualify under READ COMMITTED: if a concurrent writer won the
                # row and already retired it, do nothing (see docstring).
                if lic.status != "active":
                    continue
                # setattr(): the repo-wide idiom for writing to a Column-typed
                # attribute without tripping ty's invalid-assignment (see
                # expire_stale_licenses / revoke_license / introspect_token).
                setattr(lic, "status", "expired")
                setattr(lic, "updated_at", now)
                transitioned.append(str(lic.id))
            session.commit()
            if transitioned:
                logger.info(
                    "Swept %d stale active license(s) to 'expired' (limit=%d): %s",
                    len(transitioned),
                    limit,
                    transitioned,
                )
            return transitioned
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def count_reapable_licenses(self, cutoff: datetime) -> int:
        """How many licences are TERMINALLY expired and older than ``cutoff``.

        The independent second opinion behind the reaper's anti-vacuity and
        over-fire guards (``scripts/reap_expired_licences.py``). Hand-written SQL
        ON PURPOSE: if this reused ``reap_all_expired_licenses``' ORM query the
        guards could not fail, because any drift in that selection would move both
        numbers together and every comparison would be a tautology.

        ``cutoff`` is REQUIRED, not defaulted. The caller computes it once
        (``now - grace``) and passes the same value to the count, the sweep and the
        recount, so a licence crossing the cutoff mid-run — or clock skew between
        this process and postgres — can never be mistaken for predicate drift.
        There is deliberately no ``cutoff=None`` convenience default: a defaulted
        cutoff of ``now`` would silently reap the whole grace window.
        """
        session = self.get_session()
        try:
            row = session.execute(text(_REAPABLE_LICENSE_COUNT_SQL), {"cutoff": cutoff}).first()
            return int(row[0]) if row else 0
        finally:
            session.close()

    def reap_all_expired_licenses(
        self,
        *,
        cutoff: datetime,
        limit: int = 500,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> list[str]:
        """Archive terminally-expired licences to ``'revoked'``. Returns ids, oldest first.

        WHY THIS EXISTS — A GAUGE THAT CAN ONLY EVER GO UP IS NOT AN INSTRUMENT
        ----------------------------------------------------------------------
        ``services/expiry_metrics.py`` buckets the licence expiry gauge on
        ``(l.status = 'revoked')``, so a row with ``status='expired'`` and a past
        ``expires_at`` counts forever under ``revoked="false"`` — the series the
        ``LicenseAlreadyExpired`` alert reads. ``expire_stale_licences.py`` moves
        rows INTO ``'expired'``; ``revoke_expired_tokens.py`` reaps TOKENS only.
        Nothing moved a licence row out of ``'expired'``, so that gauge LATCHES:
        measured live 2026-09-07..09 it sat at a constant 2 from the metric's very
        first sample, with zero serving impact, and every future expiry would have
        accumulated on it permanently. An alert that cannot return to 0 is an alert
        that will be silenced.

        ``revoke_expired_tokens.py`` is the shape this mirrors: an expired token is
        already dead, and flipping the flag makes explicit what was already true.
        Same here — an ``'expired'`` licence is already unusable (``effective_status``
        returns the stored status verbatim for any non-``'active'`` row, and
        ``is_active()`` is false), so this is bookkeeping, not an entitlement change.

        ⚠️ THE GRACE WINDOW IS LOAD-BEARING, AND IT IS THE ONE DELIBERATE DIFFERENCE
        FROM THE TOKEN REAPER.
        ---------------------------------------------------------------------------
        ``'expired'`` and ``'revoked'`` are DIFFERENT facts to a reader: the first
        says "your licence lapsed, renew it", the second says "somebody killed it".
        ``effective_status`` returns the stored value verbatim, so this transition is
        LOSSY in a way the token reaper's is not — a token has one flag, a licence
        has a four-value state machine. Without a grace window the two CronJobs would
        chain (``expire-stale`` at 02:07Z, this at 04:37Z) and ``'expired'`` would
        exist for at most a few hours in the whole estate's history, which would
        quietly delete the distinction ``expire_stale_licences.py`` exists to create.

        So the caller passes ``cutoff = now - grace`` and only rows expired LONGER
        than the grace period are archived. A ``grace`` of 0 reduces this to exactly
        the token reaper's behaviour; the CronJob ships 30 days. The alert therefore
        still fires — and is actionable — for a licence that lapsed recently, and
        stops firing once the row is genuinely historical instead of never.

        ``revoked_at`` IS WRITTEN ONLY IF UNSET, the same rule ``revoke_license``
        follows: it is the audit record of when the credential was killed, and a
        second sweep must not rewrite it. That is also what makes this idempotent —
        a re-run selects nothing, because the predicate is scoped to ``'expired'``.

        ⛔ NOTHING IS DELETED. This is a status transition; the row, its tokens, its
        ``expires_at`` and its metadata all survive, and ``status`` can be set back
        by hand if the archive decision was wrong.

        CONCURRENCY: identical to ``expire_all_stale_licenses`` — rows are taken with
        ``SELECT ... FOR UPDATE`` and re-qualified under READ COMMITTED, so a replica
        that blocks on a row another has just archived filters it out rather than
        acting on a stale snapshot. ``dry_run`` takes NO locks and commits nothing.
        """
        session = self.get_session()
        try:
            at = now or datetime.now(UTC)
            query = (
                session.query(License)
                .filter(
                    License.status == "expired",
                    License.expires_at.is_not(None),
                    License.expires_at < cutoff,
                )
                .order_by(License.expires_at.asc(), License.id.asc())
                .limit(limit)
            )
            if dry_run:
                # Read-only: no locks taken, nothing written, and the read
                # transaction is explicitly discarded rather than committed.
                would_reap = [str(lic.id) for lic in query.all()]
                session.rollback()
                return would_reap

            reapable = query.with_for_update().all()
            reaped: list[str] = []
            for lic in reapable:
                # Re-qualify under READ COMMITTED: if a concurrent writer won the
                # row and already moved it, do nothing (see docstring).
                if lic.status != "expired":
                    continue
                # setattr(): the repo-wide idiom for writing to a Column-typed
                # attribute without tripping ty's invalid-assignment.
                setattr(lic, "status", "revoked")
                if not lic.revoked_at:
                    setattr(lic, "revoked_at", at)
                setattr(lic, "updated_at", at)
                reaped.append(str(lic.id))
            session.commit()
            if reaped:
                logger.info(
                    "Reaped %d terminally-expired license(s) to 'revoked' "
                    "(cutoff=%s, limit=%d): %s",
                    len(reaped),
                    cutoff.isoformat(),
                    limit,
                    reaped,
                )
            return reaped
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def tenants_for_licenses(self, license_ids: list[str]) -> dict[str, str]:
        """Map each licence id to the ``tenant_id`` STORED ON ITS OWN ROW.

        Exists for ``scripts/reap_expired_licences.py``, which must tear down each
        reaped licence's billing mirrors and therefore needs the tenant billing
        knows it by. The HTTP revoke path gets that tenant from the ``X-Tenant-Id``
        header its ownership check already validated; a CronJob has no header and no
        caller, so the licence ROW is the only source of truth available to it — and
        it is the right one, because it is the same column the create path mirrored
        into billing.

        ⛔ Deliberately a SEPARATE read rather than a wider return type on
        ``reap_all_expired_licenses``. That method's ``list[str]`` contract is
        asserted by the reaper's over-fire and truncation guards and by
        ``tests/unit/test_reap_expired_licences.py``; widening it to carry a tenant
        would change what those guards compare and is a bigger blast radius than the
        one fact this needs. Reaping is a STATUS TRANSITION — the row, and its
        ``tenant_id``, survive it — so reading afterwards is sound.

        A licence id with no row, or a row with no ``tenant_id``, is simply ABSENT
        from the result. The caller must treat that as "cannot tear this one down"
        and say so, never as "nothing to tear down": inventing or defaulting a tenant
        here would send billing a DELETE under a tenant that does not own the row.
        """
        if not license_ids:
            return {}
        wanted: list[UUID] = []
        for raw in license_ids:
            try:
                wanted.append(UUID(str(raw)))
            except TypeError, ValueError:
                # A malformed id cannot match a row; dropping it here keeps the
                # query well-formed and leaves the id unresolved for the caller.
                continue
        if not wanted:
            return {}
        session = self.get_session()
        try:
            rows = session.query(License.id, License.tenant_id).filter(License.id.in_(wanted)).all()
            return {str(row[0]): str(row[1]) for row in rows if row[1]}
        finally:
            session.close()

    def find_active_license(self, tenant_id: str, app_id: UUID, subject_id: UUID) -> License | None:
        """Return the existing ACTIVE, NON-EXPIRED license for (tenant, app, subject).

        Backs the idempotency of create_user_license: at most one active license
        per (tenant, app, subject) (enforced by uq_license_active_tenant_app_subject).
        Newest-first so a race that slipped two rows resolves deterministically.

        Two layers keep a time-expired row out of the idempotency branch:

        1. ``expire_stale_licenses`` runs FIRST and retires any
           ``status='active' AND expires_at < now()`` row for this key, freeing
           the partial-unique-index slot so a renewal can actually be minted.
           This is the write path (``create_user_license`` is the only caller),
           so the lazy transition belongs here and NOT on the read path.
        2. The ``expires_at`` window predicate below is belt-and-braces: even if
           the transition loses a race, an expired row can never be handed back
           to the caller as its "existing active license".

        The predicate deliberately covers ``expires_at`` ONLY, not ``starts_at``.
        A future-dated (pending) license is genuinely the license this key will
        have, and it still occupies the unique-index slot, so returning it from
        the idempotency branch is correct — filtering it out would make
        re-provisioning fall through to an INSERT that can only ever fail on the
        unique index. Expired rows are the opposite: they are dead AND their slot
        must be reclaimed.
        """
        # Lazy expire-on-write: retire corpses before deciding "does an active
        # license already exist?". Without this, an expired-but-never-transitioned
        # row makes the license permanently unrenewable (see expire_stale_licenses).
        self.expire_stale_licenses(tenant_id=tenant_id, app_id=app_id, subject_id=subject_id)

        session = self.get_session()
        try:
            now = datetime.now(UTC)
            return (
                session.query(License)
                .options(joinedload(License.app), joinedload(License.plan))
                .filter(
                    License.tenant_id == tenant_id,
                    License.app_id == app_id,
                    License.subject_id == subject_id,
                    License.status == "active",
                )
                .filter((License.expires_at.is_(None)) | (License.expires_at > now))
                .order_by(License.created_at.desc())
                .first()
            )
        finally:
            session.close()

    def tenant_has_licence_on_plan(self, tenant_id: str, app_id: UUID, plan_id: UUID) -> bool:
        """True when ``tenant_id`` holds, or ever held, a licence on ``plan_id`` for ``app_id``.

        Backs the ``plan_revoked`` mint gate in ``POST /licenses/user/{sub}``:
        retiring a plan closes it to NEW tenants only. A tenant already on it keeps
        onboarding members and keeps re-minting. ANY row counts (revoked, expired,
        soft-deleted): CommonPractice changes licence terms by revoking and then
        re-minting on the same plan, so at the re-mint the tenant may hold no
        active licence on it at all (#258).
        """
        session = self.get_session()
        try:
            return (
                session.query(License.id)
                .filter(
                    License.tenant_id == tenant_id,
                    License.app_id == app_id,
                    License.plan_id == plan_id,
                )
                .first()
                is not None
            )
        finally:
            session.close()

    def list_licenses(
        self,
        subject_id: UUID | None = None,
        app_id: UUID | None = None,
        plan_id: UUID | None = None,
        status: str | None = None,
        tenant_id: str | None = None,
        effective: bool = False,
        as_of: datetime | None = None,
        include_deleted: bool = False,
    ) -> list[License]:
        """List licenses with optional filters.

        ⭐ ``include_deleted`` (default False) IS THE PRECONDITION THAT LET
        ``LICENSE_SOFT_DELETE`` BE ARMED. While the flag defaulted off,
        ``deleted_at`` was NULL on every row and this parameter would have been
        decoration. Armed, a deleted licence is RETAINED — so without a filter
        here the two list surfaces would start returning rows that used to
        vanish, which is precisely the reappearance the flag's own comment named
        as the reason it could not be turned on. The write path and the read
        surfaces had to move together; this is the read half.

        ⚠️ IT IS NOT REDUNDANT WITH THE ``status`` FILTER, and that is the trap.
        A soft delete sets ``status='revoked'``, so ``?status=active`` already
        excludes a deleted row by accident. But an UNFILTERED list — which is
        what ``GET /licenses/admin/all`` and ``GET /internal/licenses`` serve by
        default — has no status predicate at all, and any consumer that counts
        rows from it would silently start counting deleted licences.

        ⛔ ``True`` IS AN AUDIT SURFACE, NOT A WIDER DEFAULT. The retained row IS
        this service's record of the deletion (there is no audit table), so an
        operator must be able to reach it; ``deleted_at``/``deleted_by``/
        ``delete_reason`` are unreadable if nothing can select the row. It is
        opt-in for the same reason ``effective=false`` is: the drift must be
        visible to whoever goes looking, and invisible to everyone else.

        ⚠️ ``effectively_active_clauses`` is NOT affected either way. It carries
        its own ``deleted_at`` arm (a row deleted AFTER ``as_of`` was still a seat
        AT ``as_of``), and stacking this filter on top of it would break exactly
        that — so with ``effective=True`` the two must not be combined. See the
        ordering in the body.

        ``effective`` (default False) changes what ``status="active"`` MEANS:

          * ``False`` — filter the STORED ``status`` column, verbatim. This is the
            historical behaviour and what the two ADMIN surfaces want: an operator
            debugging a licence must be able to see a row that says ``active``
            while its ``expires_at`` has passed. Hiding it is how that drift
            becomes invisible.
          * ``True``  — apply ``effectively_active_clauses(as_of)``, i.e. the same
            three-part rule ``count_tenant_seats`` uses. This is what a BILLING
            caller must use, so ``GET /internal/licenses?status=active`` and
            ``GET /internal/tenants/{id}/seat-count`` can no longer answer
            different numbers for one invoice.

        ⚠️ ``effective=True`` is only meaningful with ``status="active"``. The
        other stored statuses (``revoked``, ``expired``) are terminal and
        time-invariant, so there is nothing for the window to resolve; passing
        ``effective=True`` with one of them filters on the stored value exactly as
        before rather than silently returning nothing.

        ``as_of`` defaults to now and is ignored unless ``effective`` is set.
        """
        session = self.get_session()
        try:
            query = session.query(License).options(
                joinedload(License.app),
                joinedload(License.plan),
            )
            if tenant_id:
                query = query.filter(License.tenant_id == tenant_id)
            if subject_id:
                query = query.filter(License.subject_id == subject_id)
            if app_id:
                query = query.filter(License.app_id == app_id)
            if plan_id:
                query = query.filter(License.plan_id == plan_id)
            effective_active = bool(effective and status == "active")
            if status:
                if effective_active:
                    # ⛔ ONE predicate, shared with count_tenant_seats. Never
                    # restate the expression here — the two endpoints billing
                    # reads for one invoice have already drifted apart once.
                    query = query.filter(*effectively_active_clauses(as_of or datetime.now(UTC)))
                else:
                    query = query.filter(License.status == status)
            if not include_deleted and not effective_active:
                # ⛔ THE `effective_active` EXCLUSION IS LOAD-BEARING, NOT AN
                # OPTIMISATION. `effectively_active_clauses` already decides the
                # deleted case AS A FUNCTION OF `as_of`: a row deleted AFTER
                # `as_of` was still a seat at `as_of` and MUST be returned. A flat
                # `deleted_at IS NULL` stacked on top would drop it, silently
                # re-deriving a CLOSED period smaller than the invoice was written
                # from — the exact defect the soft delete exists to prevent,
                # reintroduced through the filter meant to hide deleted rows.
                #
                # Everywhere else the filter is right and unconditional: those
                # paths answer in the PRESENT tense, where a deleted licence is
                # simply gone, which is what every consumer saw before the flag
                # was armed.
                query = query.filter(License.deleted_at.is_(None))
            return query.all()
        finally:
            session.close()

    def get_active_license(self, tenant_id: str, app_key: str) -> list[License]:
        """Return active, non-expired licenses for (tenant_id, app_key), ORDERED.

        MORE THAN ONE ROW IS NORMAL. ``License.subject_id`` is NOT NULL and the
        partial unique index is on ``(tenant_id, app_id, subject_id)``, so
        provisioning mints one row per onboarded USER while fr/kanban/n8n are
        SOLD per-tenant (whole customer org). Two employees in one org therefore
        means two active rows for the same (tenant, app) — a normal state, not a
        data conflict. Callers must treat the list as a candidate set.

        The list is returned in ``ACTIVE_LICENSE_PICK_ORDER`` (see that constant
        for why): the best candidate is always element 0, deterministically and
        identically on every replica. Ordering in SQL rather than in Python is
        deliberate — it is the same clause for every caller and cannot drift.

        ``Subject`` is eagerly loaded because callers resolve a caller-supplied
        subject reference (Keycloak ``sub`` OR local ``Subject.id``) against this
        candidate set AFTER the session is closed.

        tenant_id is a UUID column since migration 016. This method is reached
        via /licenses/active with tenant_id from an UNTRUSTED query param, so a
        non-UUID value must resolve to "no match" (empty list → caller 404s),
        never a 500 from a uuid cast error.
        """
        try:
            UUID(str(tenant_id))
        except ValueError, AttributeError, TypeError:
            return []
        session = self.get_session()
        try:
            now = datetime.now(UTC)
            query = (
                session.query(License)
                .join(App, License.app_id == App.id)
                .options(
                    joinedload(License.app),
                    joinedload(License.plan),
                    joinedload(License.subject),
                )
                .filter(
                    License.tenant_id == tenant_id,
                    App.key == app_key,
                    License.status == "active",
                    License.starts_at <= now,
                )
                .filter((License.expires_at.is_(None)) | (License.expires_at > now))
                .order_by(*ACTIVE_LICENSE_PICK_ORDER)
            )
            return query.all()
        finally:
            session.close()

    def revoke_license(self, license_id: UUID) -> bool:
        """Revoke a license and all its tokens"""
        session = self.get_session()
        try:
            license = session.query(License).filter(License.id == license_id).first()
            if not license:
                return False

            # Revoke the license.
            #
            # ``revoked_at`` is written ONLY on the transition, for the same
            # reason the token UPDATE below filters on ``revoked.is_(False)``:
            # it is the audit record of when the credential was actually killed,
            # and re-revoking must not rewrite it.
            #
            # ⚠️ This was the half I missed. The first version of this change
            # filtered the TOKEN update three lines below and left the license
            # row's own timestamp writing itself over on every call — while the
            # commit rationale ("it took one retry to lose the forensic record of
            # when a leak was closed") was still true of the row above it, and
            # the new test class was named ...PreservesEarlierTimestamps while
            # asserting only the token stamps. Found by an adversarial reviewer.
            #
            # The retry is not hypothetical: `n8n-nodes-licenses` wraps this call
            # in `auth.executeWithRetry(...)` (automatic, not operator-driven) and
            # `provisioning`'s client documents it as an idempotent offboard
            # no-op. `updated_at` DOES advance every call — that is what it is
            # for.
            already_revoked = bool(license.revoked_at)
            setattr(license, "status", "revoked")
            if not already_revoked:
                setattr(license, "revoked_at", datetime.now(UTC))
            setattr(license, "updated_at", datetime.now(UTC))

            # Revoke all tokens for this license.
            #
            # ``revoked.is_(False)`` is load-bearing and its absence was an audit
            # bug: without it this UPDATE rewrote ``revoked_at`` on tokens that
            # were ALREADY revoked, so a license-wide revoke today destroyed the
            # timestamp of a credential individually killed months ago — and this
            # endpoint is callable repeatedly by any ``license-manager``, so it
            # took one retry to lose the forensic record of when a leak was
            # closed. Both sibling paths already filter this way
            # (``revoke_tokens_for_license``, ``regenerate_token_for_license``);
            # this one was the outlier.
            session.query(Token).filter(
                Token.license_id == license_id, Token.revoked.is_(False)
            ).update({"revoked": True, "revoked_at": datetime.now(UTC)})

            session.commit()
            return True
        finally:
            session.close()

    # ── Dependency release (licences#<orphan-deps>) ──────────────────────────
    # Bundle ensure (migration 059) provisions the ``app_requires`` closure of a
    # bundle as SHARED dependency licences (``is_dependency``, ``bundle_id`` NULL).
    # Nothing ever took them away again: revoking the bundle licences that needed
    # them left every dependency ACTIVE, and the owner could not revoke them
    # (``_bundle_owner_may_revoke`` refuses a bundle_id-NULL row). Measured live
    # 2026-10-21 on a w2v-offboarded tenant: kag, workers, evidence-review,
    # mlx-backend, mlx-ingestion-backend and inference-router still active.
    #
    # The rule: a dependency licence lives exactly as long as SOME active
    # non-dependency licence on the tenant reaches its app through
    # ``app_requires`` (transitively). Every licence revoke re-evaluates it in the
    # SAME transaction as the revoke, under the tenant's bundle-ensure advisory
    # lock (``services.bundle_ensure.tenant_lock_key``) so a concurrent ensure that
    # planned ``exists`` on a dependency cannot have it released underneath it.

    def _lock_tenant_for_dependency_release(self, session: Session, tenant_id: str) -> bool:
        """Take the tenant's bundle-ensure xact lock FIRST in ``session``. False on timeout.

        First, before any row lock: ensure holds this lock while it updates licence
        rows, so taking a row lock and then waiting here could deadlock.

        The bounded ``lock_timeout`` applies to the advisory-lock wait ONLY: it is
        set transaction-locally and restored to the session's previous value right
        after the lock is taken, so the row locks the revoke then takes wait as
        they would anywhere else. A 55P03 (``lock_not_available``) on the acquire
        rolls back and returns False — the caller's deferred path, never a 500.
        """
        try:
            previous = session.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
            session.execute(
                text("SELECT set_config('lock_timeout', :t, true)"),
                {"t": DEPENDENCY_RELEASE_LOCK_TIMEOUT},
            )
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"bundle_ensure:{tenant_id}"},
            )
            session.execute(
                text("SELECT set_config('lock_timeout', :t, true)"), {"t": str(previous)}
            )
        except DBAPIError as e:
            session.rollback()
            orig = getattr(e, "orig", None)
            code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
            if code == "55P03":  # lock_not_available
                return False
            raise
        return True

    @staticmethod
    def _dependency_roots(session: Session, tenant_id: str) -> list[Any]:
        """(licence id, app key, bundle id) of the tenant's ACTIVE NON-dependency
        licences — the roots of the dependency closure (``_dependency_requirements``)."""
        return list(
            session.execute(
                text(
                    "SELECT l.id, a.key, l.bundle_id FROM license l "
                    "JOIN app a ON a.id = l.app_id "
                    "WHERE l.tenant_id = :t AND l.status = 'active' AND l.deleted_at IS NULL "
                    "AND NOT l.is_dependency ORDER BY l.created_at, l.id"
                ),
                {"t": str(tenant_id)},
            ).fetchall()
        )

    @staticmethod
    def _app_requires_edges(session: Session) -> list[Any]:
        """Every ``app_requires`` edge between non-revoked apps, read in ``session``."""
        from services.app_requires import Edge

        return [
            Edge(str(a), str(r), str(c))
            for a, r, c in session.execute(
                text(
                    "SELECT a.key, r.key, ar.caller_owner_key FROM app_requires ar "
                    "JOIN app a ON a.id = ar.app_id "
                    "JOIN app r ON r.id = ar.required_app_id "
                    "WHERE NOT a.revoked AND NOT r.revoked ORDER BY 1, 2"
                )
            ).fetchall()
        ]

    def _licences_requiring_app(
        self, session: Session, tenant_id: str, app_key: str
    ) -> list[dict[str, str]]:
        """The tenant's active non-dependency licences whose ``app_requires`` closure
        reaches ``app_key``: ``[{license_id, tenant_id, app_key}]`` (``app_key`` is the
        REQUIRING licence's app). A root on a cycle counts as requiring it."""
        from services.app_requires import DependencyCycleError, closure

        edges = self._app_requires_edges(session)
        out: list[dict[str, str]] = []
        for lid, key, _bundle in self._dependency_roots(session, tenant_id):
            try:
                deps, _walked = closure([str(key)], edges)
            except DependencyCycleError:
                deps = [app_key]
            if app_key in deps:
                out.append(
                    {"license_id": str(lid), "tenant_id": str(tenant_id), "app_key": str(key)}
                )
        return out

    def _dependency_requirements(
        self, session: Session, tenant_id: str
    ) -> tuple[set[str], set[tuple[str, str]]] | None:
        """(required app keys, required (owner, app key) grants) for the tenant's
        remaining ACTIVE NON-dependency licences. None when ``app_requires`` holds a
        cycle reachable from them (the caller then releases NOTHING — fail safe).

        "Active" is the stored ``status = 'active'`` and not soft-deleted, without the
        time window: an expired-but-unrevoked licence may be renewed, so it keeps its
        dependencies (releasing too little is recoverable, too much is an outage).
        A dependency licence is never a root — otherwise kag-as-dependency would keep
        workers alive forever.
        """
        from services.app_requires import DependencyCycleError, closure

        rows = self._dependency_roots(session, tenant_id)
        roots = {str(r[1]) for r in rows}
        bundle_ids = sorted({str(r[2]) for r in rows if r[2] is not None})
        edges = self._app_requires_edges(session)
        try:
            deps, walked = closure(roots, edges)
        except DependencyCycleError as e:
            logger.error(
                "dependency_release_skipped: tenant=%s reason=dependency_cycle path=%s "
                "(no dependency licence released; fix app_requires)",
                tenant_id,
                " -> ".join(e.path),
            )
            return None
        grants = {(e.caller, e.required) for e in walked}
        if bundle_ids:
            grants |= {
                (str(o), str(k))
                for o, k in session.execute(
                    text(
                        "SELECT c.owner_key, a.key FROM bundle_caller c "
                        "JOIN app a ON a.id = c.app_id WHERE c.bundle_id = ANY(:b)"
                    ),
                    {"b": [UUID(b) for b in bundle_ids]},
                ).fetchall()
            }
        return roots | set(deps), grants

    def _release_orphaned_dependencies(
        self,
        session: Session,
        tenant_id: str,
        *,
        actor: str,
        trigger: str,
        now: datetime,
        explicit: License | None = None,
    ) -> list[str]:
        """Revoke every ACTIVE dependency licence on the tenant no remaining active
        non-dependency licence requires. Runs inside the caller's transaction (it does
        not commit). Returns the released licence ids, oldest first.

        ``explicit``: a dependency licence the caller has just revoked BY ID (already
        ``revoked`` in this transaction, so the candidate query no longer sees it). It
        gets the same audit stamp (``release_reason`` =
        ``DEPENDENCY_EXPLICIT_REVOKE_REASON``, ``released_by_revoke_of`` = its own id)
        and the same grant cleanup, but is NOT in the returned list — that list names
        only the licences released IN ADDITION to the one the caller revoked.

        THE AUDIT RECORD. This service has no audit table (see
        ``soft_delete_licenses_for_subject``); the retained row carries it in
        ``metadata`` (``released_by`` / ``release_reason`` / ``released_at`` /
        ``released_by_revoke_of``) and one ``dependency_released`` WARNING line per
        licence corroborates it. ``deleted_*`` is NOT used: a release is a revoke,
        not a deletion, and ``deleted_at`` would move seat counts.

        GRANTS. A ``tenant_dependency_grant`` row (owner, app) exists only so the
        owner's minter can mint that dependency for the tenant; once the licence is
        released the row is removed (removal IS the revocation, per the model),
        unless the remaining licences still walk that (caller, app) edge or a held
        bundle's ``bundle_caller`` names it. Only released apps' grant rows are
        touched.
        """
        if explicit is not None:
            meta_x: dict[str, Any] = dict(cast(dict[str, Any], explicit.license_metadata or {}))
            meta_x.update(
                {
                    "released_by": actor,
                    "release_reason": DEPENDENCY_EXPLICIT_REVOKE_REASON,
                    "released_at": now.isoformat(),
                    "released_by_revoke_of": str(explicit.id),
                }
            )
            setattr(explicit, "license_metadata", meta_x)
            logger.warning(
                "dependency_revoked_explicitly: tenant=%s licence=%s app=%s actor=%s",
                tenant_id,
                explicit.id,
                explicit.app.key,
                actor,
            )
        req = self._dependency_requirements(session, tenant_id)
        if req is None:
            return []
        required, required_grants = req
        candidates = (
            session.query(License)
            .join(App, App.id == License.app_id)
            .filter(
                License.tenant_id == UUID(str(tenant_id)),
                License.is_dependency.is_(True),
                License.status == "active",
                License.deleted_at.is_(None),
                App.key.notin_(sorted(required)) if required else text("true"),
            )
            .order_by(License.created_at, License.id)
            .all()
        )
        released: list[str] = []
        released_apps: dict[str, str] = {}
        if explicit is not None:
            released_apps[str(explicit.app_id)] = str(explicit.app.key)
        for lic in candidates:
            app_key = str(lic.app.key)
            meta: dict[str, Any] = dict(cast(dict[str, Any], lic.license_metadata or {}))
            meta.update(
                {
                    "released_by": actor,
                    "release_reason": DEPENDENCY_RELEASE_REASON,
                    "released_at": now.isoformat(),
                    "released_by_revoke_of": trigger,
                }
            )
            setattr(lic, "license_metadata", meta)
            setattr(lic, "status", "revoked")
            if lic.revoked_at is None:
                setattr(lic, "revoked_at", now)
            setattr(lic, "updated_at", now)
            released.append(str(lic.id))
            released_apps[str(lic.app_id)] = app_key
        if not released_apps:
            return []
        if released:
            session.query(Token).filter(
                Token.license_id.in_([UUID(i) for i in released]), Token.revoked.is_(False)
            ).update({"revoked": True, "revoked_at": now}, synchronize_session=False)
        removed_grants: list[str] = []
        for grant in (
            session.query(TenantDependencyGrant)
            .filter(
                TenantDependencyGrant.tenant_id == UUID(str(tenant_id)),
                TenantDependencyGrant.app_id.in_([UUID(a) for a in released_apps]),
            )
            .all()
        ):
            pair = (str(grant.owner_key), released_apps[str(grant.app_id)])
            if pair in required_grants:
                continue
            removed_grants.append(f"{pair[0]}:{pair[1]}")
            session.delete(grant)
        for lic in candidates:
            logger.warning(
                "dependency_released: tenant=%s licence=%s app=%s actor=%s trigger=%s reason=%r",
                tenant_id,
                lic.id,
                lic.app.key,
                actor,
                trigger,
                DEPENDENCY_RELEASE_REASON,
            )
        if removed_grants:
            logger.warning(
                "dependency_grants_removed: tenant=%s grants=%s actor=%s trigger=%s",
                tenant_id,
                ",".join(sorted(removed_grants)),
                actor,
                trigger,
            )
        return released

    def revoke_license_releasing_dependencies(
        self,
        license_id: UUID,
        *,
        actor: str,
        refuse_required_dependency: bool = False,
    ) -> tuple[str, list[str], list[dict[str, str]]]:
        """``revoke_license`` + ``_release_orphaned_dependencies`` in ONE transaction.

        Returns ``(outcome, released dependency licence ids, required_by)``;
        ``required_by`` (only for ``dependency_still_required``) lists the active
        licences that still require the dependency. Outcome is
        ``revoked`` | ``not_found`` | ``dependency_still_required`` (only with
        ``refuse_required_dependency``, which the route sets for every caller but the
        cross_owner break-glass: an explicit revoke of a dependency licence a
        remaining active licence still needs — nothing is written; without it the
        revoke is forced and logged ``dependency_force_revoked``) |
        ``ensure_in_progress`` (only with ``refuse_required_dependency``: the
        tenant's ensure lock could not be taken, so "still required" cannot be
        decided safely; nothing is written).

        An explicitly revoked dependency licence is audited and its grant rows
        cleaned up like a released one (``_release_orphaned_dependencies(explicit=)``).

        Idempotent: re-revoking an already-revoked licence keeps its ``revoked_at``
        and re-runs the release, so a release skipped earlier (lock timeout) is
        completed by the retry.

        LOCK TIMEOUT on a plain revoke: revoking is security-critical and must not
        be vetoed by a long ensure, so the licence is revoked WITHOUT the release
        (``revoke_license``) and a ``DEPENDENCY_RELEASE_DEFERRED`` line names the
        tenant; the next revoke on that tenant (or a retry) releases.
        """
        session = self.get_session()
        try:
            tenant_row = session.execute(
                text("SELECT tenant_id FROM license WHERE id = :l"), {"l": str(license_id)}
            ).first()
            if tenant_row is None:
                return "not_found", [], []
            tenant_id = str(tenant_row[0])
            if not self._lock_tenant_for_dependency_release(session, tenant_id):
                if refuse_required_dependency:
                    return "ensure_in_progress", [], []
                logger.error(
                    "DEPENDENCY_RELEASE_DEFERRED: tenant=%s licence=%s actor=%s — a bundle "
                    "ensure held the tenant lock past %s; the licence is revoked, its "
                    "orphaned dependency licences are NOT yet released (re-revoke to release)",
                    tenant_id,
                    license_id,
                    actor,
                    DEPENDENCY_RELEASE_LOCK_TIMEOUT,
                )
                session.close()
                return ("revoked" if self.revoke_license(license_id) else "not_found"), [], []
            lic = session.query(License).filter(License.id == license_id).first()
            if lic is None:
                return "not_found", [], []
            explicit_dependency = bool(lic.is_dependency) and lic.status == "active"
            if explicit_dependency:
                required_by = self._licences_requiring_app(session, tenant_id, str(lic.app.key))
                if required_by and refuse_required_dependency:
                    session.rollback()
                    return "dependency_still_required", [], required_by
                if required_by:
                    # Break-glass force (the route passes refuse=False only for
                    # cross_owner): allowed, but never silent.
                    logger.warning(
                        "dependency_force_revoked: tenant=%s licence=%s app=%s actor=%s "
                        "required_by=%s",
                        tenant_id,
                        lic.id,
                        lic.app.key,
                        actor,
                        ",".join(r["license_id"] for r in required_by),
                    )
            now = datetime.now(UTC)
            setattr(lic, "status", "revoked")
            if lic.revoked_at is None:
                setattr(lic, "revoked_at", now)
            setattr(lic, "updated_at", now)
            session.query(Token).filter(
                Token.license_id == license_id, Token.revoked.is_(False)
            ).update({"revoked": True, "revoked_at": now}, synchronize_session=False)
            session.flush()
            released = self._release_orphaned_dependencies(
                session,
                tenant_id,
                actor=actor,
                trigger=str(license_id),
                now=now,
                explicit=lic if explicit_dependency else None,
            )
            session.commit()
            return "revoked", released, []
        finally:
            session.close()

    def app_cascade_radius(self, app_id: UUID) -> dict[str, Any] | None:
        """Count exactly what deleting this app would destroy, BEFORE destroying it.

        ``app`` is a GLOBAL catalogue row: it has no ``tenant_id`` column and its
        ``key`` is globally unique, so one app row is shared by every tenant that
        holds a licence on it. That is why this returns a ``tenants`` list rather
        than a single tenant — the cascade is inherently cross-tenant and there is
        no "caller's tenant" to scope it to.

        Deliberately mirrors ``scripts/purge_test_apps.py::blast_radius``, which
        has guarded this exact cascade (dry-run -> radius -> allowlist re-check)
        since the 2026-07-17 catalogue audit. The HTTP route had none of it.

        Returns ``None`` if the app does not exist.
        """
        session = self.get_session()
        try:
            if session.query(App.id).filter(App.id == app_id).first() is None:
                return None
            lic_ids = [r[0] for r in session.query(License.id).filter(License.app_id == app_id)]
            tenants = sorted(
                # str(): tenant_id is a UUID column since migration 016 and this
                # set is rendered into a log line and a JSON error body.
                {
                    str(r[0])
                    for r in session.query(License.tenant_id)
                    .filter(License.app_id == app_id)
                    .distinct()
                    if r[0]
                }
            )
            return {
                "plans": session.query(Plan).filter(Plan.app_id == app_id).count(),
                "licenses": len(lic_ids),
                "tokens": (
                    session.query(Token).filter(Token.license_id.in_(lic_ids)).count()
                    if lic_ids
                    else 0
                ),
                "tenants": tenants,
            }
        finally:
            session.close()

    def delete_app(self, app_id: UUID) -> bool:
        """Hard-delete an app and cascade to licenses, plans, tokens.

        Deletes licenses first (avoids plan RESTRICT constraint), then deletes the app
        which cascades to plans.

        ⚠️ This is an IRREVERSIBLE, CROSS-TENANT cascade and this service has no
        audit table, so the log line below is the only record that it happened.
        It is WARNING, not INFO/DEBUG: ``LOG_LEVEL=INFO`` is production, so a
        DEBUG line here would be equivalent to deleting the record. Callers that
        expose this to a human must gate it on ``app_cascade_radius`` first — see
        the ``confirm_licenses`` gate on ``DELETE /apps/{app_id}``.

        Returns False if app not found.
        """
        session = self.get_session()
        try:
            app = session.query(App).filter(App.id == app_id).first()
            if not app:
                return False
            app_key = app.key
            # Delete licenses first to avoid plan ON DELETE RESTRICT
            deleted_licenses = session.query(License).filter(License.app_id == app_id).delete()
            session.delete(app)
            session.commit()
            logger.warning(
                "app_deleted: id=%s key=%s licenses_deleted=%d "
                "(tokens cascade via license_id FK; irreversible, no audit table)",
                app_id,
                app_key,
                deleted_licenses,
            )
            return True
        finally:
            session.close()

    def delete_plan(self, plan_id: UUID) -> tuple[bool, str | None]:
        """Hard-delete a plan.

        Returns:
            (True, None) on success
            (False, None) if plan not found
            (False, "conflict") if licenses still reference this plan
        """
        session = self.get_session()
        try:
            plan = session.query(Plan).filter(Plan.id == plan_id).first()
            if not plan:
                return False, None
            # Check for referencing licenses (RESTRICT constraint)
            count = session.query(License).filter(License.plan_id == plan_id).count()
            if count > 0:
                return False, "conflict"
            session.delete(plan)
            session.commit()
            return True, None
        finally:
            session.close()

    def subject_licence_delete_radius(self, subject_id: UUID, tenant_id: str) -> dict[str, Any]:
        """Count exactly what deleting this subject's licences would destroy.

        Mirrors ``app_cascade_radius`` — same purpose, same shape, one tenant
        instead of the whole catalogue — so the DELETE route can refuse an
        unconfirmed call and report the radius in the refusal, which is the only
        chance an operator gets to see it before it happens.

        ``licenses`` / ``tokens`` are what a HARD delete removes (``token`` cascades
        via ``token.license_id``). ``active_licences`` is called out separately
        because it is the number that moves a SEAT COUNT and therefore an invoice —
        deleting five revoked rows and deleting five active ones are the same
        ``licenses`` figure and completely different events.

        Returns zeros rather than ``None`` for a subject with nothing: unlike
        ``app_cascade_radius``, the route has already established that the subject
        exists (it resolves it to 404 first), so there is no absence to report and
        a ``None`` here would only add a branch that cannot be reached.
        """
        session = self.get_session()
        try:
            rows = (
                session.query(License.id, License.status, License.deleted_at)
                .filter(License.subject_id == subject_id, License.tenant_id == tenant_id)
                .all()
            )
            lic_ids = [r[0] for r in rows]
            return {
                "licenses": len(lic_ids),
                # Excludes rows already soft-deleted: those are not live licences
                # and counting them here would overstate the radius of a second
                # delete against the same subject.
                "active_licences": sum(
                    1 for _id, st, deleted_at in rows if st == "active" and deleted_at is None
                ),
                "tokens": (
                    session.query(Token).filter(Token.license_id.in_(lic_ids)).count()
                    if lic_ids
                    else 0
                ),
            }
        finally:
            session.close()

    def delete_licenses_for_subject(self, subject_id: UUID, tenant_id: str) -> int:
        """Hard-delete all licenses for a subject within a tenant. Tokens cascade via FK.

        Returns count of deleted licenses.

        ⛔ THIS DESTROYS BILLING EVIDENCE AND IS RETAINED ONLY AS THE
        ``LICENSE_SOFT_DELETE``-OFF PATH. The row goes, its tokens go with it
        (``token.license_id`` ON DELETE CASCADE), and nothing anywhere records that
        it existed — while ``count_tenant_seats(as_of=…)``, which billing bills
        from, can only filter rows that still exist. Prefer
        ``soft_delete_licenses_for_subject``; see migration 036.
        """
        session = self.get_session()
        try:
            count = (
                session.query(License)
                .filter(License.subject_id == subject_id, License.tenant_id == tenant_id)
                .delete()
            )
            session.commit()
            return count
        finally:
            session.close()

    def soft_delete_licenses_for_subject(
        self,
        subject_id: UUID,
        tenant_id: str,
        *,
        actor: str,
        reason: str | None = None,
        now: datetime | None = None,
        released_out: list[str] | None = None,
    ) -> int:
        """Mark this subject's licences deleted, retaining the rows. Returns the count.

        ``released_out``: when a list is passed, the tenant's dependency licences no
        remaining active licence requires are released in the SAME transaction
        (``_release_orphaned_dependencies``) and their ids appended to it.

        THE AUDITED ALTERNATIVE to ``delete_licenses_for_subject``. Nothing is
        removed, so a seat count at any instant BEFORE ``deleted_at`` still resolves
        to the number the invoice for that period was written from — which a hard
        delete makes permanently unrecoverable.

        ⛔ FOUR FIELDS IN ONE UPDATE, AND ALL FOUR MATTER:
          * ``deleted_at``          the instant, read by ``effectively_active_clauses``;
          * ``deleted_from_status`` the status it HELD, because the line below
            overwrites the live one;
          * ``status='revoked'`` + ``revoked_at`` — NOT cosmetic. A deleted licence
            must stop authorizing (``is_active`` / ``effective_status`` both key on
            the stored status) AND must release
            ``uq_license_active_tenant_app_subject``, the partial unique index
            ``WHERE status = 'active'``. Leave ``status`` alone and re-provisioning
            the same person for the same app fails on a row the operator believes
            is gone.
          * ``deleted_by`` / ``delete_reason`` — the audit. There is no audit TABLE
            in this service; the retained row IS the record, which is only possible
            because the row is no longer destroyed.

        ⚠️ ALREADY-SOFT-DELETED ROWS ARE SKIPPED, NOT RE-STAMPED. ``deleted_at`` is
        the FIRST deletion and must stay so — the same rule ``set_tenant_archived``
        follows for ``archived_at`` — otherwise a repeated call walks the timestamp
        forward and drags historical counts along with it, silently rewriting a
        closed period.

        ⛔ TOKENS ARE REVOKED, NOT DELETED. The hard path let the FK cascade destroy
        them; here they must be explicitly killed, or a soft-deleted licence keeps
        working — the row survives, so the cascade does not fire and the tokens stay
        valid. That would turn an audit improvement into an access-control
        regression, which is why it is done in the SAME transaction as the UPDATE
        rather than left to the caller.
        """
        stamp = now or datetime.now(UTC)
        session = self.get_session()
        try:
            release = released_out is not None
            if release and not self._lock_tenant_for_dependency_release(session, str(tenant_id)):
                release = False
                logger.error(
                    "DEPENDENCY_RELEASE_DEFERRED: tenant=%s subject=%s actor=%s — a bundle "
                    "ensure held the tenant lock past %s; the licences are deleted, orphaned "
                    "dependency licences are NOT yet released (re-revoke to release)",
                    tenant_id,
                    subject_id,
                    actor,
                    DEPENDENCY_RELEASE_LOCK_TIMEOUT,
                )
            live = (
                session.query(License)
                .filter(
                    License.subject_id == subject_id,
                    License.tenant_id == tenant_id,
                    License.deleted_at.is_(None),
                )
                .all()
            )
            if not live:
                return 0
            for lic in live:
                # setattr, not plain assignment: the ORM attributes type as
                # `Column[...]` and the type checker refuses a plain value. Same
                # workaround, for the same reason, as `ensure_tenant` and
                # `set_tenant_archived`.
                setattr(lic, "deleted_from_status", lic.status)
                setattr(lic, "deleted_at", stamp)
                setattr(lic, "deleted_by", actor)
                setattr(lic, "delete_reason", reason)
                setattr(lic, "status", "revoked")
                if lic.revoked_at is None:
                    # Preserve an EARLIER revocation: a licence revoked last month
                    # and deleted today was not revoked today, and `revoked_at` is
                    # read as the moment access stopped.
                    setattr(lic, "revoked_at", stamp)
            session.query(Token).filter(
                Token.license_id.in_([lic.id for lic in live]),
                Token.revoked.is_(False),
            ).update({"revoked": True, "revoked_at": stamp}, synchronize_session=False)
            if release and released_out is not None:
                session.flush()
                released_out.extend(
                    self._release_orphaned_dependencies(
                        session,
                        str(tenant_id),
                        actor=actor,
                        trigger=f"subject:{subject_id}",
                        now=stamp,
                    )
                )
            session.commit()
            return len(live)
        finally:
            session.close()

    def delete_orphan_licenses_by_tenant(self, tenant_id: str) -> int:
        """Hard-delete every license row whose tenant_id is this ORPHAN value.

        Guarded: refuses (raises ValueError) if a ``tenants`` row exists for
        ``tenant_id`` — this method may ONLY ever remove genuine orphans (a
        tenant_id with no registry entry), never a real tenant's licenses. Tokens
        cascade via the license_id FK (ON DELETE CASCADE). Used by the Stage-2
        orphan resolver to clear synthetic/ghost rows (no Keycloak user) that a
        soft revoke cannot clear (the FK counts all rows regardless of status).

        Returns the number of license rows deleted.
        """
        session = self.get_session()
        try:
            if session.query(Tenant).filter(Tenant.id == UUID(str(tenant_id))).first() is not None:
                raise ValueError(
                    f"refusing to delete: tenant_id {tenant_id} IS a registered tenant "
                    "(this method only deletes orphans)"
                )
            count = session.query(License).filter(License.tenant_id == tenant_id).delete()
            session.commit()
            return count
        finally:
            session.close()

    def revoke_tokens_for_license(self, license_id: UUID) -> int:
        """Revoke all tokens for a license (without revoking the license itself)"""
        session = self.get_session()
        try:
            count = (
                session.query(Token)
                .filter(Token.license_id == license_id, Token.revoked.is_(False))
                .update({"revoked": True, "revoked_at": datetime.now(UTC)})
            )
            session.commit()
            return count
        finally:
            session.close()

    @staticmethod
    def _inherited_rotation_expiry(replaced: list[datetime | None]) -> datetime | None:
        """Expiry a rotated token inherits from the live tokens it replaces.

        * Any replaced token perpetual (``expires_at IS NULL``) -> perpetual.
          A credential that was legitimately perpetual must stay perpetual; a
          rotation is not the place to impose an expiry that never existed.
        * Otherwise the LATEST replaced expiry. Rotation collapses N live
          credentials into one, so the replacement has to cover the longest-lived
          thing it destroys; taking the max can never outlive what the license
          already had, which is the invariant that matters (a rotation must never
          LENGTHEN a credential's life).
        * Nothing replaced -> ``None`` here; ``regenerate_token_for_license``
          treats a licence that never had a token as a NEW mint and applies
          ``resolve_new_token_expiry`` (licenses#244), so it is not perpetual.

        Note what this function deliberately does NOT decide: whether the expiry it
        returns is already in the past. It is a pure ``max``, and an all-expired
        input yields a past timestamp rather than an error, because the empty list
        it would otherwise collapse to already means something else here
        (perpetual). ``regenerate_token_for_license`` reads the result and refuses;
        see ``AllTokensExpiredError``.

        ⚠️ KNOWN, MEASURED, AND NOT API-REACHABLE. The third bullet can still launder
        a credential through the OTHER emptiness: revoke a license's only 1-day token
        by hand, rotate, and the replacement is PERPETUAL. Verified 2026-08-16 that no
        HTTP route can reach that state — ``/license/revoke`` by token requires the
        token to introspect ``active`` (so an expired one cannot be revoked), the
        by-license_id arm also sets ``status='revoked'`` (so the route's ``is_active()``
        gate 409s first), and there is no per-token DELETE and no license-reactivate
        route anywhere in the 22 write endpoints. It needs a direct DB write. Left as
        documentation rather than a guard: refusing on "no tokens at all" would break
        the documented fresh-mint fallback, which is a real and correct path.
        """
        if not replaced:
            return None
        if any(exp is None for exp in replaced):
            return None
        return max(exp for exp in replaced if exp is not None)

    def regenerate_token_for_license(
        self,
        license_id: UUID,
        token_type: str = "api",
    ) -> tuple[int, Token, str]:
        """Atomically revoke ALL live tokens for a license and mint a replacement.

        The bulk-revoke and the new-token insert happen in ONE session / ONE
        commit. This closes a non-atomic-rotation hole: the previous caller
        committed ``revoke_tokens_for_license`` and then SEPARATELY committed
        ``create_token``, so a crash between the two commits left the license
        with ZERO valid tokens. Perpetual license tokens have
        ``expires_at = NULL`` and are never auto-replaced, so that window is a
        PERMANENT ``token_not_found`` for the affected license — not a transient
        blip. Doing both in one transaction means a mid-operation failure rolls
        BOTH statements back: the pre-existing tokens survive intact.

        EXPIRY IS INHERITED, NEVER RESET (closed 2026-08-08). This used to take an
        ``expires_at`` argument defaulting to ``None``, and the route never passed
        one — so EVERY rotation minted a PERPETUAL replacement. Rotating a 1-day
        token silently converted it into a never-expiring bearer credential, i.e.
        any ``license-admin``/``license-manager`` could launder a short-lived
        token into a permanent one just by pressing "regenerate". On the mint
        route (``POST /licenses/{id}/tokens``) perpetuity is at least an EXPLICIT
        choice — the caller has to omit ``expires_days`` to get it. Rotation made
        it the silent default for every token it touched.

        The parameter is GONE rather than defaulted: the expiry is derived from
        the tokens being replaced (see ``_inherited_rotation_expiry``), so no
        caller can pass a longer one. "Rotate AND extend" is deliberately not
        expressible here — it would need its own explicit, clamped API.

        CONCURRENCY: the live-token SELECT is deliberately NOT ``FOR UPDATE``.
        Under READ COMMITTED a locking read would re-qualify after the concurrent
        winner commits, find ``revoked = false`` no longer true, and return an
        EMPTY set — which this method would read as "nothing to inherit from" and
        mint a PERPETUAL token. That is exactly the defect being fixed. With a
        plain snapshot read the loser sees either the original live token (winner
        not yet committed) or the winner's replacement (committed — revoke and
        insert are the same transaction, so they become visible together), and
        inherits correctly in both cases.

        REFUSES RATHER THAN MINTING A CORPSE (closed 2026-08-16). "Live" above
        used to mean *unrevoked* only, and nothing here ever revokes a token when
        it expires, so a license whose every unrevoked token had already expired
        inherited a PAST expiry — the replacement was rejected as ``token_invalid``
        on first use, while the tokens it destroyed were gone for good. Such a
        rotation now raises ``AllTokensExpiredError`` and writes nothing; see that
        class for why refusing beats both alternatives. The check lives HERE, in
        the same session as the revoke, not in the route: split across two sessions
        it would be a TOCTOU, and the whole point of this method is that the
        decision and the destruction are one transaction.

        Returns ``(revoked_count, new Token, raw token string)``.
        """
        session = self.get_session()
        try:
            # Read the expiries BEFORE revoking — the replacement inherits them.
            # The ``revoked`` filter is load-bearing, not just a mirror of the
            # UPDATE below: an ALREADY-revoked token is not a credential the
            # license still holds, so its expiry is not a lifetime to preserve.
            # Counting one would LENGTHEN the surviving credential — and a
            # revoked PERPETUAL token would make every future rotation on this
            # license perpetual, permanently resurrecting the defect.
            # cast(): Token.expires_at is a Column-typed attribute at the class
            # level; on an instance it is the plain value (same reason the writes
            # below/elsewhere go through setattr).
            live_tokens = (
                session.query(Token)
                .filter(Token.license_id == license_id, Token.revoked.is_(False))
                .all()
            )
            live_expiries: list[datetime | None] = [
                cast("datetime | None", tok.expires_at) for tok in live_tokens
            ]
            expires_at = self._inherited_rotation_expiry(live_expiries)
            # A REGISTERED long-lived predecessor stays registered across rotation
            # (licenses#244): the replacement is the same owned credential.
            inherited_registration: LongLivedRegistration | None = None
            if expires_at is None:
                for tok in live_tokens:
                    if tok.expires_at is None and tok.long_lived_owner is not None:
                        inherited_registration = LongLivedRegistration(
                            str(tok.long_lived_owner), str(tok.long_lived_reason)
                        )
                        break

            # An EXPIRED token is not a credential the license still holds either —
            # the same reasoning as ``revoked`` above, applied to the other way a
            # token stops being usable. Nothing in this service revokes a token when
            # it expires (there is no sweeper), so unrevoked-but-dead rows pile up
            # and the ``max()`` above returned a PAST timestamp: the replacement was
            # invalid the moment it was minted, while the tokens it replaced were
            # destroyed for good.
            #
            # This reads the RESULT rather than pre-filtering the input, and that is
            # deliberate. A filtered input makes ``[]`` ambiguous — no tokens at all
            # (perpetual is correct: no policy to preserve) versus every token
            # expired (perpetual is catastrophic: it launders a dead credential into
            # an unbounded one, the 2026-08-08 defect) — and the two then have to be
            # told apart by a second condition. Here the ambiguity never arises:
            # ``None`` means perpetual-or-nothing-to-inherit, and only a NON-None
            # result can be in the past. Pre-filtering would also be redundant, which
            # is measurable: a mutant passing the unfiltered list to
            # ``_inherited_rotation_expiry`` is EQUIVALENT, because whenever one live
            # token exists its expiry is larger than every expired one anyway.
            #
            # ``<``, not ``<=``: ``Token.is_valid`` treats ``expires_at == now`` as
            # still valid, and rotation must not disagree with introspection about
            # what "expired" means.
            if expires_at is not None and expires_at < datetime.now(UTC):
                raise AllTokensExpiredError(license_id, expires_at)

            # THE OTHER EMPTINESS. Everything above reasons about the expiry that
            # comes back; this reasons about the list being EMPTY, which reaches
            # the same "nothing replaced" arm and mints a PERPETUAL token.
            #
            # The comment above says that arm is safe because ``[]`` can only mean
            # "no tokens at all". That was true when it was written and is not true
            # now, in two ways — both MEASURED, not argued:
            #
            #   * `/license/revoke` by token requires the token to introspect
            #     ACTIVE. A live 30-day token IS active, so revoking it is an
            #     ordinary, correct operator action — and it empties the live set.
            #     Rotating afterwards then returned ``expires_at = None``: the
            #     incident response for a leaked bounded credential handed back an
            #     UNBOUNDED one. This needs no DB access and no special privilege
            #     beyond the rotate the operator already holds.
            #   * #159's `revoke_all_expired_tokens` sweep revokes every expired
            #     token estate-wide. Before it, such a license refused with
            #     ``AllTokensExpiredError`` (the expiries were in the past); after
            #     it, the live set is empty and the same rotation minted a
            #     perpetual token. The sweep silently converted a refusal into the
            #     exact credential-laundering that refusal existed to prevent.
            #
            # So the distinction has to be made explicitly rather than inferred
            # from the expiry. A license that has NEVER had a token is a NEW mint
            # and gets the new-token expiry policy (licenses#244), not perpetuity.
            if not live_expiries:
                revoked_total = session.query(Token).filter(Token.license_id == license_id).count()
                if revoked_total:
                    raise AllTokensRevokedError(license_id, revoked_total)
                # NEVER had a token: nothing to inherit, so this is a NEW mint and
                # the new-token policy applies (licenses#244) — `api` gets the
                # bounded default, anything else needs an explicit mint instead.
                expires_at = resolve_new_token_expiry(token_type, None, None)

            revoked_count = (
                session.query(Token)
                .filter(Token.license_id == license_id, Token.revoked.is_(False))
                .update({"revoked": True, "revoked_at": datetime.now(UTC)})
            )
            raw_token = Token.generate_token()
            token = Token(
                license_id=license_id,
                token_hash=Token.hash_token(raw_token),
                token_type=token_type,
                expires_at=expires_at,
                long_lived_owner=inherited_registration.owner if inherited_registration else None,
                long_lived_reason=(
                    inherited_registration.reason if inherited_registration else None
                ),
            )
            session.add(token)
            session.commit()
            session.refresh(token)
            return revoked_count, token, raw_token
        except Exception:
            # A failure anywhere above (token generation, INSERT, commit) must
            # not leave the revoke committed on its own — roll the whole unit of
            # work back so the license keeps its existing tokens.
            session.rollback()
            raise
        finally:
            session.close()

    # Token operations
    def create_token(
        self,
        license_id: UUID,
        token_type: str = "api",
        expires_at: datetime | None = None,
        granted_roles: list[str] | None = None,
        long_lived: LongLivedRegistration | None = None,
    ) -> tuple[Token, str]:
        """
        Create a new token

        ``granted_roles`` is for ``POST /licenses/{id}/operator-token`` ONLY — the route
        that checks the caller's own Keycloak JWT for the role. The DB CHECKs refuse any
        role but ``platform-admin`` and any role-bearing token without a short expiry.

        EXPIRY POLICY (licenses#244, see ``resolve_new_token_expiry``): ``expires_at=None``
        no longer means perpetual. An ``api`` token gets TOKEN_API_DEFAULT_EXPIRY_DAYS;
        a perpetual token needs ``long_lived`` (owner + reason) and is recorded in the
        long-lived register; anything else raises ``TokenExpiryPolicyError`` before a
        row is written.

        Returns:
            Tuple of (Token model, raw token string)
        """
        session = self.get_session()
        try:
            tenant_keys: frozenset[str] = frozenset()
            if long_lived is not None and token_type == "api":
                # The n8n path is decided by the LICENCE's tenant row, never a header.
                row = (
                    session.query(Tenant.id, Tenant.name)
                    .join(License, License.tenant_id == Tenant.id)
                    .filter(License.id == license_id)
                    .first()
                )
                if row is not None:
                    tenant_keys = frozenset({str(row[0]), str(row[1])})
            expires_at = resolve_new_token_expiry(
                token_type, expires_at, long_lived, tenant_keys=tenant_keys
            )

            # Generate raw token
            raw_token = Token.generate_token()
            token_hash = Token.hash_token(raw_token)

            # Create token
            token = Token(
                license_id=license_id,
                token_hash=token_hash,
                token_type=token_type,
                expires_at=expires_at,
                granted_roles=list(granted_roles or []),
                long_lived_owner=long_lived.owner.strip() if long_lived else None,
                long_lived_reason=long_lived.reason.strip() if long_lived else None,
            )
            session.add(token)
            session.commit()
            session.refresh(token)
            return token, raw_token
        finally:
            session.close()

    def get_token_by_hash(self, token_hash: str) -> Token | None:
        """Get token by hash.

        Returns the row TOKEN_HASH_PICK_ORDER selects when the hash is on more
        than one licence — a stable answer, not heap order — and warns that the
        multiplicity exists.
        """
        session = self.get_session()
        try:
            rows = (
                session.query(Token)
                .filter(Token.token_hash == token_hash)
                .order_by(*TOKEN_HASH_PICK_ORDER)
                .all()
            )
            _warn_on_hash_multiplicity(rows, token_hash, site="get_token_by_hash")
            return rows[0] if rows else None
        finally:
            session.close()

    def licences_for_token_hash_in_tenant(self, token_hash: str, tenant_id: str) -> list[License]:
        """Every licence of ``tenant_id`` carrying ``token_hash`` — exactly the
        rows ``revoke_token_for_tenant`` would write, so an authorization decided
        over this list covers the whole revoke, twins included."""
        try:
            tid = UUID(str(tenant_id))
        except ValueError, TypeError:
            return []
        session = self.get_session()
        try:
            return (
                session.query(License)
                .join(Token, Token.license_id == License.id)
                .filter(Token.token_hash == token_hash, License.tenant_id == tid)
                .distinct()
                .all()
            )
        finally:
            session.close()

    def _tenant_posture_from_session(
        self, session: Session, tenant_id: str, app_key: str | None
    ) -> TenantPosture:
        """Resolve the posture using ONE session. Raises on any DB fault.

        Split out of ``_lookup_tenant_posture`` so its bounded retry can re-run
        EXACTLY this query on a fresh session without the two copies drifting
        apart. Deliberately does NOT swallow anything: deciding what an
        unresolvable posture means is ``_lookup_tenant_posture``'s job.

        Returns byo_mode AND archived state from the SAME row read — see
        ``TenantPosture`` for why they share one query rather than two.
        """
        tenant = None
        try:
            tenant_uuid = UUID(str(tenant_id))
            tenant = session.query(Tenant).filter(Tenant.id == tenant_uuid).first()
        except ValueError, TypeError:
            tenant = _tenant_by_name(session, tenant_id)
        if tenant is None:
            # Unresolved tenant → byo fails CLOSED (no platform GPU by default),
            # archived fails OPEN (a row that does not exist is not archived).
            # See _MISSING_ROW_TENANT_POSTURE for why those directions differ.
            #
            # `resolution=missing_row`, NOT `fault`: "this registry holds no such
            # tenant" is a durable, cacheable FACT that the database answered. It
            # is the one unresolved case a caller may safely freeze.
            return _MISSING_ROW_TENANT_POSTURE
        archived = tenant.archived_at is not None
        name = str(tenant.name) if tenant.name else None
        # Per-(tenant, product) override wins when present.
        #
        # ⚠️ The override governs byo_mode ONLY. There is no per-product archive:
        # `archived_at` is a property of the TENANT, so a product-level BYO grant
        # can never un-archive one. Keeping `archived` outside this branch is
        # what stops an override row from becoming an accidental exemption.
        if app_key:
            override = (
                session.query(TenantAppByoMode)
                .join(App, App.id == TenantAppByoMode.app_id)
                .filter(
                    TenantAppByoMode.tenant_id == tenant.id,
                    App.key == app_key,
                )
                .first()
            )
            if override is not None and override.byo_mode:
                return TenantPosture(str(override.byo_mode), archived, name)
        return TenantPosture(
            str(tenant.byo_mode) if tenant.byo_mode else "byo_required", archived, name
        )

    def _lookup_byo_mode(
        self, tenant_id: str, app_key: str | None = None, session: Session | None = None
    ) -> str:
        """Resolve a tenant's BYO entitlement, fail-CLOSED to 'byo_required'.

        Thin accessor over ``_lookup_tenant_posture`` — kept because byo_mode is
        the only field most callers want, and because narrowing to the string
        here means none of them had to change when the archived flag joined the
        same row read.

        ⛔ ``introspect_token`` deliberately does NOT call this. It needs the
        ARCHIVED flag from the same read, and going through this accessor would
        throw it away — reintroducing the very gap this file's archived gate
        closes. If you are adding a caller that authorizes anything, call
        ``_lookup_tenant_posture`` and honour ``.archived``.

        ⛔ AND IT THROWS AWAY ``.resolution`` TOO, WHICH IS THE SECOND REASON NOT
        TO USE IT. 'byo_required' from this function means one of three things —
        the row says so, there is no row, or the read FAULTED — and narrowing to
        the string makes those indistinguishable. That is harmless for a LIVE
        ACCESS GATE, which only ever needs "may this tenant use the platform GPU"
        and is correct to deny on all three; it is NOT harmless for a caller that
        records a commercial fact, because it turns an infrastructure blip into a
        durable statement that a tenant is commercially BYO.

        So the split is by PURPOSE, not by value:
          * an access gate (worker_management's trusted-network path, the
            ``introspect_token`` byo field) keeps the fail-closed string and is
            served correctly by this accessor;
          * anything that PERSISTS or BILLS the answer must call
            ``_lookup_tenant_posture`` and branch on ``.resolution`` — see
            ``main.get_tenant_byo_mode``, which turns ``RESOLUTION_FAULT`` into a
            503 so the answer is retried instead of frozen.
        """
        return self._lookup_tenant_posture(tenant_id, app_key=app_key, session=session).byo_mode

    def _lookup_tenant_posture(
        self, tenant_id: str, app_key: str | None = None, session: Session | None = None
    ) -> TenantPosture:
        """Resolve a tenant's BYO entitlement AND archived state in one row read.

        Returns ``TenantPosture``; byo fail-CLOSES to 'byo_required' and archived
        fail-OPENS to False (see ``_MISSING_ROW_TENANT_POSTURE`` — the directions
        differ on purpose).

        ``.resolution`` reports which of the three paths produced ``byo_mode``:
        ``row`` (a reading), ``missing_row`` (a durable fact — no such tenant) or
        ``fault`` (no answer at all; the read failed on both attempts). The
        VALUES of byo_mode/archived are identical across the last two, so no
        existing caller's behaviour changes by adding this; it exists so a caller
        that writes money can refuse to freeze a fabrication. See
        ``_lookup_byo_mode`` for which callers must branch on it.

        ``tenant_id`` is canonically a UUID (Tenant.id); for legacy string
        names we also try Tenant.name.

        ``app_key`` (optional): when given AND a ``tenant_app_byo_mode`` override
        row exists for ``(tenant, app)``, that PER-PRODUCT posture wins over the
        tenant-wide ``Tenant.byo_mode``. Absent app_key, no override row, or an
        unknown app_key → the tenant-wide value applies (backward compatible).

        Platform inference runs on OWNER GPU and is a privileged grant, so an
        UNRESOLVED tenant (no ``tenants`` row, or an unknown legacy name) fails
        CLOSED to 'byo_required' — an unprovisioned tenant gets no platform
        inference by default. This is safe because every active-license
        tenant_id has been backfilled with an explicit row
        (``scripts/backfill_tenants_byo_mode.py``, 2026-06-21); a missing row
        therefore means a genuinely new/unprovisioned tenant, not a known one
        relying on the old fail-open default.

        The ``except`` branch denies too, for the same reason: an *infra* fault
        (DB error, statement timeout, or an unmigrated ``tenant_app_byo_mode``)
        leaves the posture UNRESOLVABLE, and 'local_allowed' is the most
        permissive value in the enum — it would run inference on the owner's GPU
        billed to nobody. A grant that cannot be verified is not a grant. Both
        branches therefore deny; there is no policy/infra asymmetry here.

        DENYING IS NOT FREE, so the fault path gets ONE bounded retry first —
        see the comment on the ``except`` arm for why that retry is the right
        shape and why it cannot weaken the deny.

        ``session``: when the caller (``introspect_token``) passes its own live
        session, we reuse that connection instead of opening a SECOND one. Under
        NullPool + pgbouncer that second ``get_session()`` was a second (often
        cross-node) pooler checkout on every introspect; reusing the caller's
        session removes it. We do NOT close a borrowed session (the caller owns
        its lifecycle); a standalone call (session=None) still opens and closes
        its own. The retry on the fault path is the one exception: it ALWAYS
        opens (and closes) a session of its own, because the session that just
        raised is in an aborted transaction and cannot serve another statement.
        """
        own_session = session is None
        # NB: the ternary tests ``session is None`` (not ``own_session``) so the
        # type checker can narrow ``session`` to ``Session`` in the else branch.
        active: Session = self.get_session() if session is None else session
        try:
            return self._tenant_posture_from_session(active, tenant_id, app_key)
        except Exception as first_exc:
            # Only a session we OWN may be rolled back — a borrowed session
            # belongs to the caller's unit of work.
            if own_session:
                active.rollback()

            # ONE bounded retry, on a session THIS function owns.
            #
            # WHY A RETRY AT ALL. This arm does not "return an error", it
            # FABRICATES a posture, and the fabricated value is a hard denial:
            # worker_management raises BYORequiredError -> 402
            # (llm_config_resolver), the modality router turns 409 into 402
            # across every router, requirements_management deliberately excludes
            # 402 from its retryable statuses, and worker_management's
            # _tenant_byo_cache caches the answer for 300s. In prod only 23 of
            # 52 tenants are genuinely byo_required, so a single blip here turns
            # into up to five minutes of hard denial for the other 29.
            #
            # WHY IT IS SAFE. Every fault that reaches this arm while
            # introspect_token stays HEALTHY is transient and CONNECTION-SCOPED:
            # introspect_token runs the token->license->app->plan join FIRST and
            # this byo SELECT LAST, so if we got here with the caller still
            # holding a live result the database is up and what failed is this
            # statement or this connection — a statement timeout, a pgbouncer
            # server-connection kill, a mid-request reset, a failover. A
            # PERSISTENT fault (table genuinely missing, permissions revoked, DB
            # really down) raises on the fresh session too and still fails
            # CLOSED. The retry separates those two cases; it never softens the
            # deny.
            #
            # WHY A FRESH, OWNED SESSION. `active` has raised, so its
            # transaction is ABORTED — every further statement on it raises
            # InFailedSqlTransaction, and it may be the CALLER's session, which
            # we may neither roll back nor close. So the retry gets its own from
            # get_session() and must close it in `finally` (nobody else can).
            #
            # ONE attempt, no loop, no sleep: this is the platform-wide auth hot
            # path and the caller has an introspect timeout. And the retry
            # itself may never raise — an exception escaping it would 500
            # introspect, which is strictly worse than the denial we are trying
            # to avoid — so both the attempt AND its cleanup are contained.
            retry_answer: TenantPosture | None = None
            retry_exc: BaseException | None = None
            retry_session: Session | None = None
            try:
                retry_session = self.get_session()
                retry_answer = self._tenant_posture_from_session(retry_session, tenant_id, app_key)
            except Exception as exc:
                retry_exc = exc
            finally:
                if retry_session is not None:
                    # We opened it, so only we can close it — and it must be
                    # closed on BOTH the success and the failure path, hence
                    # `finally`. No explicit rollback: Session.close() already
                    # releases the transaction, and this connection has already
                    # misbehaved once, so the close is suppressed — a second
                    # fault during cleanup must not escape and 500 the caller.
                    with contextlib.suppress(Exception):
                        retry_session.close()

            if retry_answer is not None:
                # The operator's ONLY evidence this seam exists and is load
                # bearing. A rising rate here means connection-scoped faults on
                # the auth hot path, i.e. the blips that used to silently
                # fabricate a posture — alert on the RATE, not on one line.
                logger.info(
                    "byo_mode lookup for %s failed on the first attempt (%r) but a retry on a "
                    "fresh session resolved it to %s — treating the first failure as a "
                    "transient, connection-scoped fault, not an unresolvable posture",
                    tenant_id,
                    first_exc,
                    retry_answer.byo_mode,
                )
                return retry_answer

            logger.warning(
                "byo_mode lookup failed for %s on the first attempt AND on a retry with a "
                "fresh session (retry raised %r) (DB error / column not migrated?); "
                "failing CLOSED to byo_required — platform inference runs on OWNER "
                "GPU and is an explicit grant, so an unresolvable posture is a denial",
                tenant_id,
                retry_exc,
                exc_info=first_exc,
            )
            # byo denies; archived does NOT refuse. An unresolvable row is not
            # evidence of an archive — see _FAULTED_TENANT_POSTURE.
            #
            # `resolution=fault`, and this is the ONLY site that sets it. The
            # byo_mode returned here is IDENTICAL to the missing-row case, so
            # every access gate behaves exactly as it did before; what changes is
            # that a caller which writes money can now tell that this string is a
            # fabrication rather than a reading, and refuse to freeze it.
            return _FAULTED_TENANT_POSTURE
        finally:
            if own_session:
                active.close()

    def _introspect_from_session(self, session: Session, raw_token: str) -> dict[str, Any] | None:
        """Introspect ``raw_token`` using ONE caller-supplied session.

        Split out of ``introspect_token`` so its bounded retry can re-run
        EXACTLY this work on a fresh session without the two copies drifting
        apart — the same split, for the same reason, as
        ``_tenant_posture_from_session`` under ``_lookup_tenant_posture``.

        Deliberately does NOT swallow anything and does NOT own the session's
        lifecycle: deciding what is retryable, and closing the session, are
        ``introspect_token``'s job.
        """
        token_hash = Token.hash_token(raw_token)
        # One round-trip for token + its license + the license's app & plan.
        # All many-to-one, so joinedload emits a single LEFT OUTER JOIN (one
        # row) instead of the former 4 sequential SELECTs — every saved
        # statement is a saved (often cross-node) pgbouncer round-trip on the
        # auth hot path. expire_on_commit=False keeps these rows live across
        # the throttled last_used_at commit below.
        #
        # ``.all()`` under an explicit ORDER BY, not ``.first()``: the hash is not
        # unique on its own (see TOKEN_HASH_PICK_ORDER) so a bare ``.first()``
        # resolved in heap order, and the throttled ``last_used_at`` UPDATE below
        # churns that order. Still ONE statement — the joins are all many-to-one,
        # so this cannot fan out — and the row count is bounded by the number of
        # licences carrying the hash (1 in the healthy case, 3 at the worst
        # measured in prod).
        rows = (
            session.query(Token)
            .options(
                joinedload(Token.license).joinedload(License.app),
                joinedload(Token.license).joinedload(License.plan),
                # Many-to-one like the two above, so it rides the SAME single SELECT
                # (test_introspect_query_count pins the statement count).
                # ...and undefer the collision flag so it is a column of that SELECT too.
                joinedload(Token.license)
                .joinedload(License.subject)
                .undefer(Subject.keycloak_sub_names_a_subject_row),
            )
            .filter(Token.token_hash == token_hash)
            .order_by(*TOKEN_HASH_PICK_ORDER)
            .all()
        )
        _warn_on_hash_multiplicity(rows, token_hash, site="introspect")
        token = rows[0] if rows else None

        if not token:
            return {"active": False, "reason": "token_not_found"}

        # Check token validity
        if not token.is_valid():
            return {"active": False, "reason": "token_invalid"}

        # License (eager-loaded above — no extra query)
        license = token.license
        if not license:
            return {"active": False, "reason": "license_not_found"}

        # Check license validity
        if not license.is_active():
            return {"active": False, "reason": "license_inactive"}

        # App and plan (eager-loaded above — no extra query)
        app = license.app
        plan = license.plan

        # Update last_used_at, but only when it is stale (throttled) so the
        # common poll path stays a pure read — no row-lock/WAL/commit per
        # call. This is the change that keeps introspect under caller timeouts.
        now = datetime.now(UTC)
        touched = False
        if token.last_used_at is None or (now - token.last_used_at) >= _LAST_USED_THROTTLE:
            setattr(token, "last_used_at", now)
            touched = True

        # Materialize everything the response needs into plain locals BEFORE
        # the byo lookup. Load-bearing ordering: _lookup_tenant_posture does NOT
        # roll back a session it BORROWED (only one it owns, and its retry
        # runs on a fresh session of its own), so when the byo SELECT faults
        # THIS session is left in an ABORTED transaction until the
        # `finally: session.close()` below. A lazy re-SELECT on it after
        # that point raises InFailedSqlTransaction, turning a byo blip into
        # a 500 on the auth hot path; snapshotting here guarantees there is
        # nothing left to lazily load.
        #
        # (This comment used to justify the same ordering by "the byo
        # fail-OPEN path calls session.rollback(), which expires ORM
        # instances". Both halves are now false — the path fails CLOSED and
        # it does not roll back this session. The ordering still matters,
        # for the reason above; do not "simplify" it away on the strength of
        # the old rationale being gone.)
        result = {
            "active": True,
            # ext_authz injects this as X-Tenant-Id (must stay a string).
            # tenant_id is a UUID column since migration 016 → str() it.
            "tenant_id": str(license.tenant_id),
            "license_id": str(license.id),
            "subject_id": str(license.subject_id),
            # The subject's Keycloak ``sub`` — the id a consumer recorded when the same
            # person reached it with a Keycloak JWT instead of this licence. Lets that
            # consumer recognise its own rows on the licence path (ingestion's purge
            # owner check). Identification only; never a role or a scope.
            # ⛔ None unless the stored value can BE a Keycloak sub: UUID-shaped and not
            # equal to any subject.id (a subject minted from another subject's row id
            # would otherwise claim that subject's records). See keycloak_sub_as_identity.
            "subject_keycloak_sub": (
                keycloak_sub_as_identity(
                    license.subject.keycloak_sub,
                    license.subject.keycloak_sub_names_a_subject_row,
                )
                if license.subject is not None
                else None
            ),
            # WHO ACTS when this token is presented — the field license-authz stamps on
            # metering events as actor_id. ``subject_keycloak_sub`` when it is a trusted
            # Keycloak sub (a human's, or an SA that IS the subject), else the subject's
            # stated ``actor_sub`` (its service's own SA sub, migration 047), else None.
            # ``subject_keycloak_sub`` itself is unchanged: ingestion compares it to a
            # recorded owner, and many service subjects share one SA sub.
            "subject_actor_sub": (
                actor_identity(
                    keycloak_sub_as_identity(
                        license.subject.keycloak_sub,
                        license.subject.keycloak_sub_names_a_subject_row,
                    ),
                    license.subject.actor_sub,
                )
                if license.subject is not None
                else None
            ),
            # WHAT KIND of holder this licence is: "tenant" for a tenant-level holder
            # (a ``svc:<tenant>:<app>`` bundle / dependency licence, or a non-human
            # realm), "member" for a person's per-login licence (is_tenant_level_subject,
            # the same split bundle_ensure and seat counting use). Computed from the RAW
            # keycloak_sub + realm, so a member whose stored sub does not qualify as a
            # Keycloak identity (``subject_keycloak_sub`` null) is still "member" — the
            # two are never confused (MCP grants care-bus tool scope to "tenant" only).
            # None only when the licence has no subject row.
            "subject_kind": (
                (
                    "tenant"
                    if is_tenant_level_subject(
                        license.subject.keycloak_sub, license.subject.keycloak_realm
                    )
                    else "member"
                )
                if license.subject is not None
                else None
            ),
            "app": app.to_dict() if app else None,
            "plan": plan.to_dict() if plan else None,
            "status": license.status,
            "starts_at": (license.starts_at.isoformat() if license.starts_at else None),
            "expires_at": (license.expires_at.isoformat() if license.expires_at else None),
            # THIS TOKEN's own expiry (#240), independent of the licence-level
            # `expires_at` above. NULL column = the token never expires. Read off the
            # already-loaded row, so no extra statement (test_introspect_query_count).
            "token_expires_at": (token.expires_at.isoformat() if token.expires_at else None),
            "region_id": str(license.region_id) if license.region_id else None,
            "metadata": license.license_metadata,
            # ⭐ The roles THIS TOKEN carries (migration 038) — never the licence's, never
            # the subject's. `[]` for every ordinary token. Non-empty only for a short-lived
            # operator token minted by POST /licenses/{id}/operator-token, which checked the
            # caller's own Keycloak JWT for the role. Consumers (ingestion's operator purge)
            # read it as realm roles; an absent field on an older build reads the same as [].
            "roles": _granted_roles(token),
        }

        # ⛔ NO BUNDLE WIDENING (ruling 2026-09-28). A token authorises ONLY its own
        # licence's app, bundle or not: a bundle is the provisioning/billing object, and
        # runtime presents the specific per-app licence (one per tenant x app, carrying
        # bundle_id). The migration-039 `apps[]` / requested-app swap that stood here would
        # have let every per-app licence in a bundle claim all of the bundle's apps — and
        # no consumer read it (license-authz and kag_common post {"token"} only).
        # tests/unit/test_bundle_licence.py::TestIntrospectNeverWidens pins this.

        tenant_id_for_byo = str(license.tenant_id)

        # Only pay a COMMIT when we actually wrote last_used_at. Do it before
        # the byo lookup so a (rare) byo rollback can't discard the write.
        if touched:
            session.commit()

        # BYO LLM entitlement — per-tenant, reusing THIS session (no second
        # pooler checkout). An unresolved tenant (no row) and an unresolvable
        # one (DB fault) both fail CLOSED to 'byo_required' — see
        # _lookup_tenant_posture; every active-license tenant_id is backfilled, so
        # no row ⇒ unprovisioned ⇒ no platform GPU.
        #
        # SCOPE LIMIT (deliberate): we pass NO ``app_key``, so the per-product
        # ``tenant_app_byo_mode`` override is NOT consulted on the introspect
        # path — introspect always reports the TENANT-WIDE posture. That is
        # what makes the per-product override a no-op until a consumer opts in
        # by passing ``?app`` on the dedicated endpoint. Adding app_key here
        # would change the posture every ext_authz call sees AND cost a third
        # SELECT on the auth hot path, so it is a reviewed decision, not a
        # bugfix — pinned by
        # tests/unit/test_byo_mode.py::TestIntrospectByoMode::
        # test_introspect_uses_tenant_wide_posture_ignoring_per_app_override
        # and by tests/unit/test_introspect_query_count.py.
        #
        # ⭐ ONE call, TWO answers: this resolves byo_mode AND archived state from
        # a single tenant row read, which is why the archived gate below costs no
        # additional SELECT and the 2-statement pin still holds.
        posture = self._lookup_tenant_posture(tenant_id_for_byo, session=session)
        result["byo_mode"] = posture.byo_mode

        # ────────────────────────────────────────────────────────────────────
        # THE ARCHIVED GATE. An archived tenant's licence is not usable.
        # ────────────────────────────────────────────────────────────────────
        # ⭐ WHY HERE, AND NOWHERE DOWNSTREAM. This is the licence/auth boundary:
        # `/license/introspect` is what ext_authz and every on-behalf-of caller
        # ask before doing chargeable work, so a refusal here stops the work
        # BEFORE it happens. The alternative — discarding the cost row after the
        # fact — would convert an attribution fault into an UNMETERED one, which
        # is strictly worse: the compute is still spent, it is simply no longer
        # visible. Refuse upstream; never drop a billing row.
        #
        # ⛔ THIS DOES NOT CLEAR `TenantSpendOnArchivedRegistryRow`, AND ANYONE
        # SHIPPING IT MUST KNOW THAT. This is the enforcement half of gitops
        # #3795 (b5d1a0b9f), which built the DETECTOR and filed the missing
        # authorizer as licenses#197 — this gate. But the two halves do not
        # currently observe the same tenants, and the alert will stay RED after
        # this merges. Re-measured on the prod primary 2026-09-11 using the
        # emitter's own predicate (billing_cost_events, 7d, grouped by tenant_id):
        #
        #   * Exactly ONE archived tenant is spending — `rmt-e2e`
        #     (019fb1f4-7342-7bb7-8ac6-2ff2fcda645f), 405 events / $1.6018 — and
        #     ALL 405 of its rows carry **license_id = NULL**. They are posted by
        #     `inference-router` and `mlx-ingestion-backend` keyed on a bare
        #     tenant_id, on a path that asks this service nothing.
        #   * All 19 licences of all 17 archived tenants are `status='revoked'`,
        #     so every one ALREADY answered `license_inactive` above and cannot
        #     reach this line at all.
        #
        # The alert's population (archived + billing rows) and this gate's
        # population (archived + presents a licence) are therefore DISJOINT
        # today, and this gate's honest blast radius is ZERO refusals. Closing
        # the alert's population means requiring a resolved licence at the
        # cost-event writer, which is outside this repo. ⛔ Do not "fix" the
        # alert by dropping those rows: that converts an attribution fault into
        # an UNMETERED one — the compute is still spent, merely invisible.
        #
        # It is still here because the ABSENCE was real, not because it is
        # currently load-bearing: `set_tenant_archived` revokes nothing (it
        # writes the timestamp and stops), and those 19 revocations were the
        # nightly reaper on unrelated dates (2026-04-26 → 2026-09-06), NOT a
        # consequence of the 2026-09-03 archive. The next tenant archived while
        # holding a LIVE licence would have kept authorizing spend indefinitely.
        # Now it cannot.
        #
        # ⚠️ NO PROMETHEUS COUNTER HERE, DELIBERATELY. The obvious instinct is a
        # `…_presented_total{tenant_id,tenant_name}` counter, and gitops #3795
        # explicitly refused that shape: "The metric carries no tenant label on
        # purpose (a per-tenant series would put customer identity in Prometheus
        # and would grow unbounded)." Its triage instruction is to name the
        # tenant from the DATABASES, not from a label. A second, per-tenant
        # series here would silently reverse that decision and give the estate
        # two instruments for one question. The refusal is observable as the
        # greppable WARNING below — same idiom as METERING_FAILURE_MARKER and
        # `introspect_retry_recovered`, and it puts no identity in Prometheus.
        #
        # Ordering: this runs AFTER the throttled last_used_at commit, on
        # purpose. The presentation is real and worth recording even though it is
        # refused — `last_used_at` is how an operator sees an archived tenant's
        # credential is still in active circulation.
        if posture.archived:
            logger.warning(
                "ARCHIVED_TENANT_LICENSE_PRESENTED: refusing introspect for licence %s — "
                "tenant %s (%s) is ARCHIVED. The licence row itself is still %r; archiving "
                "the tenant is what makes it unusable. If this tenant should be able to "
                "spend, UNARCHIVE it deliberately — do not exempt it here.",
                result["license_id"],
                tenant_id_for_byo,
                posture.name,
                license.status,
            )
            # Same shape as every other refusal on this path: active=False plus a
            # discriminating reason. Callers already branch on `active`, so this
            # denies correctly even for a consumer that has never heard of
            # `tenant_archived`; the reason only makes the WHY greppable.
            return {"active": False, "reason": "tenant_archived"}

        return result

    def introspect_token(self, raw_token: str) -> dict[str, Any] | None:
        """
        Introspect a token and return license/app/plan info

        A token authorises only its own licence's ``app`` — a bundle licence included
        (no ``apps[]`` widening; ruling 2026-09-28).

        Returns:
            Dict with active status and license info, or None if invalid

        ONE bounded retry on a CONNECT-scoped fault. On 2026-08-11 a single
        introspect out of 595 died inside NullPool's connect with 'server
        closed the connection unexpectedly' — the traceback topped out in
        Connection.__init__, so NO statement had run. NullPool means every call
        dials pgbouncer afresh through a ClusterIP fronting two pooler pods, so
        a pod leaving Endpoints, a stale conntrack entry, or a mid-flight reset
        each surface here as a 500 on a PUBLIC endpoint that most services call
        directly — they have none of license-authz's stale-while-error net.

        WHY A RE-DIAL AND NOT A DELAY. The fault is an immediate FIN/RST, not
        backpressure — there is no queue to drain, so a sleep waits out nothing.
        What helps is that connect() is a FRESH ClusterIP decision: with two
        backends an instant re-dial already has a good chance of a different
        pgbouncer pod, and a certainty once the bad one leaves Endpoints. So:
        one attempt, no loop, no sleep — matching _lookup_tenant_posture, and
        required by the 3.0s budget the auth middleware gives introspect.

        WHY RE-RUNNING THE WHOLE CALL IS SAFE. The only write is the THROTTLED
        last_used_at (see _LAST_USED_THROTTLE): a monotone last-writer-wins
        timestamp — no counter, no INSERT, no row lock (this path deliberately
        takes no FOR UPDATE). If the first attempt's COMMIT is applied
        server-side but its ACK is lost, the retry re-reads a FRESH
        last_used_at, the throttle predicate is then False, and no second
        UPDATE is issued at all. Everything else here is a SELECT.

        WHY A FRESH SESSION, AND WHY THE FIRST IS CLOSED FIRST. The failed
        session's transaction is ABORTED, so any further statement on it raises
        25P02 — which psycopg maps to InternalError, NOT OperationalError, so
        it would escape the predicate below as a hard 500. Worse,
        expire_on_commit=False leaves the dirty last_used_at in its identity
        map, so a reused session would re-flush that UPDATE onto a dead
        connection.

        CONNECT BUDGET: three physical connects, worst case — this one, the
        retry, and _lookup_tenant_posture's own bounded retry nested inside the retry
        attempt (it re-arms because it is handed a fresh session). Do not add a
        fourth seam.
        """
        first_exc: BaseException
        session = self.get_session()
        try:
            return self._introspect_from_session(session, raw_token)
        except _INTROSPECT_RETRYABLE as exc:
            if not _is_retryable_db_fault(exc):
                raise
            first_exc = exc
        finally:
            # `finally` runs before the `return` above hands its value back, so
            # the SUCCESS path closes exactly as it did before this seam
            # existed — no extra session, no extra statement. On the fault path
            # it runs before we dial again, so we never hold a known-broken
            # connection open across a second connect. Suppressed because a
            # connection that has already misbehaved may misbehave again on
            # teardown, and a cleanup fault must neither mask first_exc nor
            # discard an answer we already have.
            with contextlib.suppress(Exception):
                session.close()

        retry_session = self.get_session()
        try:
            result = self._introspect_from_session(retry_session, raw_token)
        except Exception as retry_exc:
            # `Exception`, not _INTROSPECT_RETRYABLE — this arm does not retry
            # again, it only makes the exhaustion greppable before re-raising,
            # and that is worth doing for ANY second-attempt failure. Widening
            # HERE does not widen what gets RETRIED.
            #
            # And it re-raises rather than returning {"active": False}: a
            # fabricated 'token invalid' on an infrastructure fault is silent,
            # fleet-wide de-authorization. A 500 is loud, and correct.
            logger.warning(
                "introspect_retry_exhausted: first attempt failed (%r) AND the retry on a "
                "fresh connection failed (%r) — not a transient, connect-scoped blip; "
                "re-raising so the caller gets a 500 rather than a fabricated "
                "'token invalid'",
                first_exc,
                retry_exc,
                exc_info=first_exc,
            )
            raise
        finally:
            with contextlib.suppress(Exception):
                retry_session.close()

        # The operator's ONLY evidence this seam exists and is load bearing:
        # this service has no prometheus client and no /metrics route, so
        # observability here is a greppable line (same reasoning as
        # METERING_FAILURE_MARKER in main.py). Alert on the RATE, not on one
        # line — a rising rate means pgbouncer, or the ClusterIP in front of
        # it, is dropping connects on the platform auth hot path.
        logger.info(
            "introspect_retry_recovered: first attempt failed (%r) but a retry on a fresh "
            "connection succeeded — treating the first failure as a transient, "
            "connect-scoped fault (NullPool re-dials pgbouncer per call), not a token "
            "or database problem",
            first_exc,
        )
        return result

    def revoke_token(self, raw_token: str) -> bool:
        """Revoke a token, with NO ownership check. Test/setup helper only.

        ⚠️ Do not wire a route to this. It has no tenant predicate, so reaching it
        from a request would let any ``license-manager`` revoke any token in any
        tenant by presenting its raw value. ``/license/revoke`` uses
        ``revoke_token_for_tenant`` instead, which resolves ownership and writes
        in one transaction. As of this change there are zero production callers
        here; the remaining ones are tests using it to put a row into the revoked
        state cheaply.
        """
        session = self.get_session()
        try:
            token_hash = Token.hash_token(raw_token)
            rows = (
                session.query(Token)
                .filter(Token.token_hash == token_hash)
                .order_by(*TOKEN_HASH_PICK_ORDER)
                .all()
            )
            _warn_on_hash_multiplicity(rows, token_hash, site="revoke_token")
            token = rows[0] if rows else None

            if not token:
                return False

            setattr(token, "revoked", True)
            setattr(token, "revoked_at", datetime.now(UTC))
            session.commit()
            return True
        finally:
            session.close()

    def revoke_token_for_tenant(self, raw_token: str, tenant_id: str) -> tuple[str, int]:
        """Revoke EVERY row ``tenant_id`` owns that carries ``raw_token``'s hash.

        Returns ``(outcome, revoked_count)`` where outcome is ``"revoked"``,
        ``"already_revoked"`` or ``"not_found"``, and ``revoked_count`` is how
        many rows this call actually transitioned (0 for the latter two).

        WHY IT IS A SET AND NOT A ROW. A raw token does not uniquely identify a
        row: the only UNIQUE index is ``ux_token_license_hash (license_id,
        token_hash)`` and ``idx_token_hash`` is NON-unique, so one hash can sit on
        several licences — and in production it does (7 unrevoked perpetual rows
        over three hashes, measured 2026-08-19). This method used to select ONE
        row with ``.first()``, flip it, and answer ``"revoked"``. The operator was
        told the leaked credential was dead while its twin on another licence was
        untouched and still introspected ``active``. Running it again made that
        WORSE, not better: whenever ``.first()`` landed back on the killed row it
        answered ``"already_revoked"`` — an idempotent SUCCESS — so the operator
        was told twice that a live credential was dead. ``revoke_tokens_for_license``
        cannot close the gap either; it is scoped to one ``license_id`` and cannot
        reach a twin. So incident response silently failed, and the count is
        returned rather than a bare boolean precisely so the operator can see how
        much was actually killed.

        The revoke is still tenant-scoped: rows on the same hash belonging to
        ANOTHER tenant are deliberately left alone (see NON-DISCLOSURE below).

        WHY THIS EXISTS AND ``revoke_token`` IS NOT ENOUGH. ``/license/revoke``
        used to establish ownership by calling ``introspect_token`` and refusing
        unless the result was ``active``. But ``active`` is LICENSE-aware — it is
        false for ``license_inactive`` and for ``token_invalid`` — while the write
        it guarded is the only thing in this service that ever sets
        ``Token.revoked``. So a LEAKED token whose license had lapsed answered
        "Token not found or already invalid" and wrote NOTHING. Nothing here
        transitions a token when it expires (there is no sweeper), and
        ``Token.is_valid`` consults only ``revoked`` and the token's own expiry —
        so when the customer renewed, the leaked credential came back live at full
        entitlement. The operator had every reason to believe it was dead.

        Revocation is an ADMINISTRATIVE act on a row, not an assertion about
        whether the credential currently authenticates. It must therefore be
        reachable for every token the tenant owns, in any license state.

        ONE SESSION, ONE TRANSACTION, on purpose: resolving the owner and writing
        the revocation in separate sessions would be TOCTOU — a license could be
        re-tenanted between the two.

        NON-DISCLOSURE IS PRESERVED. "Wrong tenant" and "no such token" both
        return ``"not_found"``, so the caller cannot use this endpoint to probe
        whether a token exists in another tenant. That masking is deliberate and
        predates this change; only the license-state gate is gone.

        ⚠️ The collapsing here is belt; the braces are in the route, which maps
        EVERY outcome other than ``"revoked"`` / ``"already_revoked"`` to one
        answer. Measured, not assumed: a mutant returning a distinct
        ``"wrong_tenant"`` from this method SURVIVED the whole battery, because
        the string never reaches the wire. That makes the mutant equivalent
        rather than the tests thin — but it also means this return value is not
        the thing protecting the property, so do not "improve" the route by
        giving a new outcome its own branch without re-reading
        ``TestNonDisclosureSurvives``.

        IDEMPOTENT. Re-revoking an already-revoked token reports
        ``"already_revoked"`` rather than a failure, and does NOT overwrite the
        original ``revoked_at`` — that timestamp is the audit record of when the
        credential was actually killed. The sibling ``license_id`` arm of the same
        endpoint has always been idempotent (``revoke_license`` returns True
        whenever the row exists); the two arms disagreed.
        """
        session = self.get_session()
        try:
            token_hash = Token.hash_token(raw_token)
            # Join to License so ownership is decided by the same statement that
            # finds the tokens. Deliberately NOT filtered on license status.
            #
            # ALL rows for the hash, in every tenant, ordered — not ``.first()``.
            # Fetching the foreign ones too is what lets the warning below report
            # the true multiplicity, and the tenant predicate is applied in Python
            # immediately after so the boundary is one visible, testable line
            # rather than a bind-parameter type coercion (``tenant_id`` arrives as
            # a string; the column is a UUID).
            rows = (
                session.query(Token, License)
                .join(License, Token.license_id == License.id)
                .filter(Token.token_hash == token_hash)
                .order_by(*TOKEN_HASH_PICK_ORDER)
                .all()
            )
            _warn_on_hash_multiplicity(
                [token for token, _license in rows], token_hash, site="revoke_token_for_tenant"
            )
            if not rows:
                return "not_found", 0

            owned = [
                token for token, license_obj in rows if str(license_obj.tenant_id) == str(tenant_id)
            ]
            if not owned:
                # Same answer as "no such token" — see NON-DISCLOSURE above.
                return "not_found", 0

            # Only the LIVE ones are written, so an earlier ``revoked_at`` is
            # never overwritten (see IDEMPOTENT below) — that timestamp is the
            # audit record of when the credential was actually killed. A hash
            # partially revoked by the pre-fix code therefore reports
            # ``"revoked"`` with the number of twins this call finished off,
            # instead of the reassuring ``"already_revoked"`` that hid them.
            live = [token for token in owned if not bool(token.revoked)]
            if not live:
                return "already_revoked", 0

            now = datetime.now(UTC)
            for token in live:
                setattr(token, "revoked", True)
                setattr(token, "revoked_at", now)
            session.commit()
            return "revoked", len(live)
        finally:
            session.close()
