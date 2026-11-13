"""Behavioural discriminator: no JWKS fetch may run on the event-loop thread.

Sibling of ``test_no_sql_on_event_loop.py``, for the other blocking I/O this
service performs on the request path — and the one no existing gate can see:

* ``test_event_loop_blocking_invariant.py`` iterates ``isinstance(route,
  APIRoute)`` only, so ``AuthMiddleware`` is out of its universe *by
  construction* — middleware is not a route.
* ``test_no_sql_on_event_loop.py`` listens on ``before_cursor_execute``, so it
  sees SQL and nothing else. A ``urllib`` fetch is invisible to it.
* ``test_jwt_signature_verification.py`` and ``tests/auth/test_header_trust_stage2.py``
  monkeypatch ``auth.jwt_verify._signing_key`` — the documented seam — so they
  can never reach the network layer at all. The autouse ``_trust_test_signing_key``
  fixture in ``tests/conftest.py`` does the same for every other test in the
  suite. **This module deliberately restores the real ``_signing_key`` and
  patches BELOW it**, at ``urllib.request.OpenerDirector.open``, which is the
  only place the blocking call is observable.

  ⚠️ Not ``urllib.request.urlopen``. pyjwt <=2.13 called ``urlopen``; 2.14
  switched ``fetch_data`` to ``build_opener(_NoRedirectHandler(), ...).open(...)``
  and never touches ``urlopen`` again, so a recorder there saw 0 fetches and the
  vacuity floors below (correctly) failed. ``OpenerDirector.open`` is the
  common sink of BOTH spellings (``urlopen`` delegates to a global
  ``OpenerDirector``), so the recorder is on the path whichever one pyjwt uses.

What is being measured
----------------------
``PyJWKClient.get_signing_key`` refreshes the JWK set *bypassing its cache*
whenever the token's ``kid`` is not in the cached set (pyjwt
``jwks_client.py``: ``if not signing_key: … get_signing_keys(refresh=True)``),
and ``fetch_data`` is a blocking ``urllib`` opener call. So an attacker who
sends an unsigned three-segment token carrying an allowlisted ``iss`` and a
random ``kid`` forces a synchronous HTTPS fetch **before any signature is
checked and before any role dependency runs**. If that fetch happens on the
loop thread, one unauthenticated request stalls every other request the worker
is serving, including the health probes.

Two independent properties are asserted, because fixing only one leaves the
service exploitable:

1. ``test_a_bogus_kid_never_fetches_jwks_on_the_loop_thread`` — the fetch is
   off the loop. Fails on the pre-fix tree because ``dispatch`` calls the
   synchronous ``_extract_user_from_jwt`` directly.
2. ``test_a_burst_of_bogus_kids_does_not_fetch_once_per_request`` — the forced
   refresh is *bounded*. Offloading alone converts a loop stall into executor
   exhaustion: the pod caps ``to_thread`` at 8 threads (``main.HANDLER_THREAD_LIMIT``
   / ``TO_THREAD_EXECUTOR_LIMIT``), so eight concurrent bogus-``kid`` requests
   would occupy the entire executor for the JWKS timeout.

Both assertions are on counts and thread identities, never on timings, so they
cannot flake on a loaded machine.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from functools import lru_cache
from types import SimpleNamespace

import httpx
import jwt as pyjwt
import pytest
import uvicorn
from jwt.algorithms import RSAAlgorithm

import auth.jwt_verify as jv
import main
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem, public_key

# Captured at IMPORT time, i.e. before the autouse `_trust_test_signing_key`
# fixture replaces the module attribute. This is the only handle on the real
# implementation once that fixture has run.
_REAL_SIGNING_KEY = jv._signing_key

TENANT_ID = "01000000-0000-7000-8000-000000000001"

# The `kid` the fake JWKS endpoint serves. Every probe token deliberately asks
# for a DIFFERENT one, so `match_kid` misses and pyjwt forces its refresh.
SERVED_KID = "served-key-never-requested"

# A protected prefix (auth/middleware.py `_is_protected`), so `dispatch` reaches
# the Bearer branch. The request is expected to 401 — we are measuring what the
# service DID on the way to that 401, not the status.
PROBE_PATH = "/licenses"


@lru_cache(maxsize=1)
def _served_jwks() -> bytes:
    """A well-formed JWK Set holding ONE key, under a ``kid`` no probe uses.

    ⚠️ It must not be empty. ``PyJWKSet`` raises ``PyJWKSetError("The JWK Set
    did not contain any keys")`` while *parsing* an empty set, i.e. before
    ``match_kid`` is ever consulted — so an empty response short-circuits the
    forced ``refresh=True`` retry and this module would measure exactly one
    fetch and conclude, wrongly, that there is no amplification. Measured: the
    first draft of this file did precisely that, and its burst test passed
    vacuously against the unfixed tree.
    """
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key()))
    jwk.update({"kid": SERVED_KID, "use": "sig", "alg": "RS256"})
    return json.dumps({"keys": [jwk]}).encode()


class _FetchRecorder:
    """Stands in for ``urllib.request.OpenerDirector.open`` under pyjwt's JWKS client.

    Installed as a plain (non-descriptor) class attribute, so it is called
    WITHOUT the opener instance: ``opener.open(req, data, timeout)`` arrives
    here as ``(req, data, timeout)``.

    Records the calling thread's ident and answers with a valid one-key set
    whose ``kid`` never matches a probe token, which is what makes the miss —
    and therefore pyjwt's cache-bypassing refresh — deterministic.
    """

    def __init__(self) -> None:
        self.idents: list[int] = []
        self.thread_names: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, request, data=None, timeout=None):  # noqa: ANN001 - urllib shape
        current = threading.current_thread()
        with self._lock:
            self.idents.append(current.ident or -1)
            self.thread_names.append(current.name)
        # pyjwt does `with opener.open(...) as response: json.load(response)`.
        # BytesIO already implements both halves of that protocol, so it stands in
        # for the HTTPResponse without subclassing (an overridden `__exit__` on a
        # BytesIO subclass is an LSP violation `ty` correctly rejects).
        return io.BytesIO(_served_jwks())

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.idents)


@pytest.fixture
def jwks_recorder(monkeypatch):
    """Restore the real ``_signing_key`` and record every JWKS fetch.

    The autouse ``_trust_test_signing_key`` fixture has already replaced
    ``jv._signing_key`` with a stub returning the test public key. Undoing that
    here is the whole point: with the stub in place the network layer is never
    reached and this file would assert nothing.
    """
    monkeypatch.setattr(jv, "_signing_key", _REAL_SIGNING_KEY)
    # A fresh client per test, so one test's cached JWK set cannot suppress
    # another's fetch and make a count assertion read low for the wrong reason.
    jv._jwks_client.cache_clear()
    # The refresh throttle is MODULE-level and would otherwise carry across
    # tests: the burst test would inherit the first test's stamp and record a
    # low count for a reason it is not measuring.
    jv._reset_jwks_refresh_throttle()

    recorder = _FetchRecorder()
    monkeypatch.setattr(pyjwt.jwks_client.urllib.request.OpenerDirector, "open", recorder)
    try:
        yield recorder
    finally:
        jv._jwks_client.cache_clear()
        jv._reset_jwks_refresh_throttle()


def _bogus_kid_token(kid: str) -> str:
    """A well-formed RS256 JWT with an allowlisted ``iss`` and an unknown ``kid``.

    It is genuinely signed, but that is incidental — verification never gets as
    far as the signature, because the key resolution fails first. An attacker
    does not need a signature at all; a syntactically valid token is enough.
    """
    payload = {
        "sub": "jwks-probe",
        "iss": TEST_ISSUER,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    return pyjwt.encode(payload, private_key_pem(), algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def live_server(db_manager, monkeypatch):
    """A real uvicorn serving ``main.app``; yields the port and the loop thread ident.

    Same construction as ``test_no_sql_on_event_loop.py``: ``uvicorn.Server.run()``
    calls ``asyncio.run(...)`` in the calling thread, so the thread we start the
    server in IS the loop thread. A ``TestClient`` cannot substitute here — it
    drives requests through an anyio portal and would return the same 401
    whether or not the loop was blocked.
    """
    monkeypatch.setattr(main, "db_manager", db_manager)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        main.app,
        log_level="warning",
        loop="asyncio",
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + 45
    while not server.started:
        if not thread.is_alive():
            pytest.fail("uvicorn thread died during startup")
        if time.monotonic() > deadline:
            server.should_exit = True
            pytest.fail("uvicorn did not start within 45s")
        time.sleep(0.02)

    try:
        yield SimpleNamespace(port=port, loop_thread_ident=thread.ident)
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _drive(port: int, kid: str) -> int:
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as http:
        response = http.get(
            PROBE_PATH,
            headers={
                "Authorization": f"Bearer {_bogus_kid_token(kid)}",
                "X-Tenant-Id": TENANT_ID,
            },
        )
    return response.status_code


class TestNoJwksFetchOnTheEventLoopThread:
    def test_a_bogus_kid_never_fetches_jwks_on_the_loop_thread(self, live_server, jwks_recorder):
        status = _drive(live_server.port, "bogus-kid-0")

        # The request MUST be refused. If this ever became a 200 the rest of the
        # assertion would be measuring an authenticated path, not the attack.
        assert status == 401, f"expected the unresolvable token to 401, got {status}"

        # VACUITY FLOOR, measured before the invariant is asserted: if no fetch
        # was recorded at all, "none of them ran on the loop thread" is
        # trivially true and proves nothing. Two are expected — the initial
        # lookup and pyjwt's forced `refresh=True` retry on the `kid` miss.
        assert jwks_recorder.count >= 2, (
            f"only {jwks_recorder.count} JWKS fetch(es) recorded — the recorder "
            "was not on the path pyjwt actually takes, so this test asserted nothing"
        )

        on_loop = [
            (ident, name)
            for ident, name in zip(jwks_recorder.idents, jwks_recorder.thread_names)
            if ident == live_server.loop_thread_ident
        ]
        assert not on_loop, (
            f"{len(on_loop)} of {jwks_recorder.count} JWKS fetches ran on the event-loop "
            f"thread {live_server.loop_thread_ident} ({on_loop}). An unauthenticated "
            "request can therefore stall every other request this worker is serving, "
            "for up to JWKS_TIMEOUT_SECONDS, before any signature is checked."
        )

    def test_a_burst_of_bogus_kids_does_not_fetch_once_per_request(
        self, live_server, jwks_recorder
    ):
        """A distinct unknown ``kid`` per request must not buy a fetch per request.

        Without a bound, N requests cost N forced refreshes: pyjwt's per-key
        ``lru_cache`` does not memoise the raising call, and ``refresh=True``
        bypasses the JWK-set cache by design. That is the amplification which
        makes offloading alone insufficient — it moves the stall from the loop
        onto the 8-thread executor rather than removing it.
        """
        requests = 10
        for i in range(requests):
            assert _drive(live_server.port, f"bogus-kid-{i}") == 401

        # Floor first — a run that fetched nothing would satisfy any ceiling.
        assert jwks_recorder.count >= 1, (
            "no JWKS fetch recorded across the burst — the recorder was not on "
            "the path, so the ceiling below asserts nothing"
        )
        assert jwks_recorder.count <= 3, (
            f"{jwks_recorder.count} JWKS fetches for {requests} bogus-kid requests: "
            "an unauthenticated caller can force one blocking outbound HTTPS request "
            "per inbound request, with no upper bound"
        )
