#!/usr/bin/env python3
r"""
Rotate a FLEET bundle of licence tokens — a Kubernetes Secret key holding a JSON
map ``{tenant_id: raw_token}`` — without a hard cutover, and prove the new
credential is live INSIDE the consuming pods before destroying the one it
replaces.

⛔ TOKEN VALUES ARE NEVER PRINTED, LOGGED, OR WRITTEN TO A PLAINTEXT FILE BY
THIS SCRIPT. Every operator-facing mention of a token goes through
``_fingerprint`` (``sha256(value)[:12]``). The two places a value moves are (a)
into ``sops --set``'s stdin, which writes it back encrypted, and (b) into the
licences API over TLS. See ``tests/unit/test_rotate_fleet_license_tokens.py``.


WHY THIS EXISTS
===============
``CONTROL_SERVICE_LICENSE_TOKENS`` (marketing + sales) and
``VIDEO_W2V_LICENSE_TOKENS`` / ``SALES_W2V_LICENSE_TOKENS`` had NO rotation
procedure. A fleet sweep found exactly one adjacent tool, gitops-production's
``audit-db-reference-drift.sh``, and that is a drift AUDIT which mints nothing.
So the documented incident response for a leaked fleet credential was, in
practice, improvisation — under time pressure, against live prod, across two
namespaces.


FINDING 1 — ``provision_service_token.py --rotate`` IS A HARD CUTOVER
=====================================================================
The obvious tool to reach for is the sibling ``provision_service_token.py
--rotate``. **It must not be used on a fleet bundle.** It calls
``DatabaseManager.regenerate_token_for_license``, which revokes EVERY live token
on the licence and inserts the replacement **in one transaction**. There is no
overlap and no grace window: the instant that transaction commits the old token
stops introspecting ``active``.

That is correct for a single service identity — its own docstring explains the
reasoning, and explicitly refuses a ``--keep-old`` escape hatch: *"An overlap
window is a real operational need, but it is a MINT, not a rotation, and the
honest way to ask for it is the explicit route ``POST
/licenses/{license_id}/tokens`` where the expiry is an argument."*

It is the WRONG primitive here, because these bundles are read by Deployments
that hold the value in a process environment. A hard cutover would 402 both
namespaces from the moment of the commit until every pod had rolled AND come up
— and on this fleet that is minutes, not seconds (see FINDING 3).

**So this script does not rotate. It MINTS ALONGSIDE, then revokes.** The
overlap window is deliberate: a bounded widening of the exposure of a credential
already believed compromised, bought in exchange for not taking prod down. It is
bounded by the script refusing to finish without either confirming every old
token revoked or naming, by ``license_id``, each one it could not.


FINDING 2 — THE SECRETS ARE FLUX-OWNED AND SOPS-ENCRYPTED, SO ``kubectl patch``
IS AN OUTAGE, NOT A SHORTCUT
==============================================================================
``marketing-secrets`` and ``sales-secrets`` carry the **label**
``kustomize.toolkit.fluxcd.io/name: services`` — the ``flux-system/services``
Kustomization, ``path: ./clusters/strato/services``, ``prune: true``,
``interval: 5m``, not suspended. Their values live SOPS-encrypted in git
(``encrypted_regex: ^(data|stringData)$``).

⚠️ LABEL, not annotation: live-verified 2026-09-15, ``metadata.annotations`` on
both Secrets is **nil**. Looking only at annotations answers "not Flux-managed",
which is the dangerous direction — see ``Cluster.flux_owner``, which also
explains why the naive go-template for this check prints the entire Secret.

A ``kubectl patch secret`` therefore does not merely fail — it SUCCEEDS, this
script would verify it, revoke the old tokens, report green, and then Flux would
reconcile within five minutes and restore the previous bundle, which by then
contains nothing but REVOKED credentials. Both services would go down with no
human action in between to correlate the outage with. This script refuses to
patch a Flux-owned Secret (exit 4) and writes to git instead.


FINDING 3 — THE ROLL IS THE ANNOTATION, AND IT IS NOT FREE
===========================================================
Because Flux decrypts SOPS *after* kustomize builds, CI cannot derive a checksum
of the secret content, so no automatic pod-template hash exists. gitops-production
enforces the manual equivalent: ``scripts/validate-strato/config_drift.py`` fails
the build when a Secret changes without a matching ``secret-revision`` bump on
every consuming pod template. **That annotation bump IS the roll trigger** — it
changes the pod template, so Flux rolls the Deployment. ``kubectl rollout
restart`` is not used here: it would create a rollout that the next reconcile
undoes.

⚠️ ``envFrom.secretRef`` and ``env.valueFrom.secretKeyRef`` materialise ONCE, at
container start. And every consumer of these keys — verified live, all of them —
is ``envFrom``/``secretKeyRef``; there is not one projected volume among them.
Worse, every consuming application caches the parsed value for the process
lifetime (marketing: ``@lru_cache get_settings()``; sales: a module-level
``settings = Settings()`` singleton). So even a volume would not help. **A Secret
change without a pod-template bump is INERT**, and invisibly so: everything keeps
working on the OLD token right up until the revoke, at which point the namespace
402s.

⚠️ AND THE ROLL IS AN OUTAGE WINDOW. ``marketing/marketing`` and ``sales/sales``
are ``strategy.type: Recreate`` at ``replicas: 1``. Their pods go down before the
replacements come up. ``sales`` sets ``progressDeadlineSeconds: 1000``, so budget
up to ~17 minutes before ``rollout status`` would even declare failure. This
rotation is zero-CREDENTIAL-downtime; it is not zero-SERVICE-downtime, and
claiming otherwise would be a lie an operator schedules around. Only
``marketing-worker`` is ``RollingUpdate``.

⚠️ AND A MALFORMED BUNDLE CRASHLOOPS AT BOOT rather than degrading: marketing's
``parse_licence_tokens()`` and sales's ``@field_validator`` both raise on a
non-UUID key, an empty token, or bad JSON, during ``Settings`` construction. So
this script validates the bundle shape before it writes.


THE ORDER OF OPERATIONS
=======================
Because the write goes through git and a human merge, the run is split in two.
A failure anywhere before ``finalize``'s revoke leaves the OLD tokens live —
the fail-SAFE direction: the fleet keeps working on a credential that is merely
leaked, instead of stopping on one that is revoked.

``--phase plan`` (default, READ-ONLY, writes nothing anywhere)
  1. Read the live Secret key, base64-decode, parse the ``{tenant_id: token}`` map.
  2. Refuse if the Secret is Flux-owned and this is a patch-mode run.
  3. INTROSPECT every current token — resolving each tenant's ``license_id`` for
     the mint, and establishing that the old token is live RIGHT NOW. That second
     job is load-bearing: see ``resolve_and_prove_live``.

``--phase stage --apply``
  4. MINT a replacement on the SAME licence via ``POST /licenses/{id}/tokens``.
     The old token is untouched — this opens the overlap window.
  5. VERIFY each new token introspects ``active`` for the right tenant, and that
     the old one STILL does, before either goes near git.
  6. ``sops --set`` the new bundle into the gitops secrets file and bump
     ``secret-revision`` on every consuming pod template, in the working tree.
     Nothing is committed: the commit, the PR and the merge are the operator's.

``--phase finalize --apply --previous-ref <sha>``
  7. Recover the OLD bundle by ``sops -d``-ing the secrets file at ``<sha>``.
     The old values are NOT journalled to disk between phases — a rotation tool
     that writes credentials to an operator's laptop has recreated the leak it was
     called to fix. Git already holds them, already encrypted, under the same key.
  8. WAIT until the live Secret and the pod both carry the new bundle.
  9. VERIFY FROM INSIDE THE POD that the env var it actually holds hashes to the
     new bundle. This is the only check that distinguishes "Flux applied it" from
     "the consumer picked it up".
 10. REVOKE the old tokens.
 11. VERIFY THE CONTROL: re-introspect each OLD token and require ``active:
     false``. Paired with step 3 this measures a TRANSITION rather than asserting
     a state — see ``revoke_and_prove_dead``.


IDEMPOTENCY — STATED HONESTLY
=============================
Safe to re-run; **not** a no-op on the second run. A second ``stage`` mints a
second generation and rewrites the working tree; a second ``finalize`` re-revokes
(``/license/revoke`` on an already-revoked token is a no-op answering ``revoked:
false``) and re-asserts the control. It never creates a duplicate licence and
never leaves a licence with zero live tokens, because every mint precedes every
revoke.

The one state a crash strands is a crash after the merge and before step 10: the
fleet is UP on the new tokens and the OLD ones are still live. That is fail-safe
for availability and fail-OPEN for the leak, so the script prints a ``STRANDED``
block naming every affected ``license_id`` and exits 5.


USAGE
=====
    # 1. plan — read-only, writes nothing, proves every current token is live
    python scripts/rotate_fleet_license_tokens.py \
        --namespace marketing --secret marketing-secrets \
        --key CONTROL_SERVICE_LICENSE_TOKENS \
        --secrets-file clusters/strato/services/marketing/secrets.yaml \
        --roll deploy/marketing:api --roll deploy/marketing-worker:worker

    # 2. stage — mint + write the gitops working tree (add --apply)
    ... --phase stage --expires-days 365 --apply

    # 3. human: review `git diff`, commit, PR, merge; wait for Flux to reconcile.

    # 4. finalize — verify in-pod, then revoke the old tokens
    ... --phase finalize --previous-ref <pre-rotation-sha> --apply

Environment:
    LICENSES_BASE_URL     default http://localhost:8544
    LICENSES_ADMIN_TOKEN  a bearer with license-admin / license-manager. If unset,
                          one is fetched via this repo's sanctioned outbound path
                          (``service_auth.ServiceTokenProvider``, client-credentials
                          against SERVICE_TOKEN_URL).
    KUBECTL               default "kubectl". May carry a wrapper, e.g.
                          KUBECTL='kubectl --context production'
    GITOPS_REPO           default ./gitops
    SOPS_AGE_KEY_FILE     as usual for sops

Exit codes:
    0  plan completed, or the requested phase completed and verified
    1  a precondition or verification failed; NOTHING was revoked
    2  bad arguments / missing configuration
    3  the bundle is not a non-empty {tenant_id: token} JSON object
    4  the Secret is Flux-owned and a patch-mode write was requested
    5  rotation applied but OLD TOKENS ARE STRANDED LIVE (see the report)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Make the repo root importable when run from the repo root or from inside the
# licenses container (working dir = /app) — same shim as the sibling provision_*
# scripts.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("rotate_fleet_license_tokens")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

DEFAULT_LICENSES_BASE_URL = "http://localhost:8544"
DEFAULT_GITOPS_REPO = "./gitops"
DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
#: Flux's `services` Kustomization reconciles every 5m; a Recreate rollout on a
#: 1-replica Deployment then has to drain and start. 20 minutes covers both with
#: room, and `sales` sets progressDeadlineSeconds: 1000 (~17m) anyway.
DEFAULT_RECONCILE_TIMEOUT_SECONDS = 1200
RECONCILE_POLL_SECONDS = 15

#: The key Flux stamps on every object it owns. Its PRESENCE is the signal; the
#: value (the owning Kustomization, here "services") only makes the message
#: actionable. Live-verified 2026-09-15: on marketing-secrets and sales-secrets
#: this is a **label**, and metadata.annotations is nil — see Cluster.flux_owner.
FLUX_OWNER_LABEL = "kustomize.toolkit.fluxcd.io/name"

#: The pod-template annotation gitops-production's config_drift.py requires to be
#: bumped whenever a Secret changes. ⚠️ THE KEY IS NOT UNIFORM ACROSS THE FLEET:
#: marketing, marketing-worker and sales use `secret-revision`; the licences
#: Deployment itself uses `secret-rev`. Which one applies is a property of the
#: workload, so it is an argument (--revision-annotation), not a constant.
DEFAULT_REVISION_ANNOTATION = "secret-revision"


class RotationError(RuntimeError):
    """A precondition or verification failed. Nothing destructive has happened."""


class FluxManagedError(RotationError):
    """The Secret is Flux-owned and a patch-mode write was requested."""


class StrandedError(RuntimeError):
    """The new bundle is deployed but old tokens could not be confirmed revoked."""


def _fingerprint(value: str) -> str:
    """A short, stable, NON-REVERSIBLE handle for a token.

    Every operator-facing mention of a token goes through here. Twelve hex chars
    of SHA-256 is enough to correlate "the thing in the pod" with "the thing I
    minted" across a run, and is not itself a credential.

    Deliberately NOT a prefix of the token. A prefix leaks real key material: for
    the ``secrets.token_urlsafe(32)`` tokens this service mints, twelve characters
    of prefix is ~72 bits of the secret — enough to matter if a log is ever
    correlated with a partial disclosure from somewhere else.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def bundle_digest(bundle: dict[str, str]) -> str:
    """The digest an in-pod ``sha256sum`` of the env var must reproduce.

    ``sort_keys=True`` and ``separators`` are load-bearing, not style: this string
    is what gets written to the Secret AND what the pod is required to hash to. If
    the two spellings of the same map differed by one space the verification would
    fail for a reason that has nothing to do with the rotation.
    """
    return hashlib.sha256(serialise_bundle(bundle).encode("utf-8")).hexdigest()


def serialise_bundle(bundle: dict[str, str]) -> str:
    """The single canonical spelling of a bundle. See ``bundle_digest``."""
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"))


def validate_bundle(bundle: object, where: str) -> dict[str, str]:
    """Reject anything the consumers would CrashLoop on — before it reaches git.

    marketing's ``parse_licence_tokens()`` and sales's ``@field_validator`` both
    raise during ``Settings`` construction on a non-UUID key, an empty token, or
    malformed JSON. That is a boot failure, not a runtime degradation: the pod
    never becomes ready, and on a ``Recreate``/1-replica Deployment the old pod is
    already gone. Catching the shape here costs nothing; catching it after the
    merge costs an outage.
    """
    if not isinstance(bundle, dict) or not bundle:
        raise RotationError(
            f"{where} decoded to {type(bundle).__name__}, not a non-empty object. "
            "An EMPTY bundle is refused rather than treated as 'nothing to do': every "
            "loop below would run zero times and the rotation would report success "
            "having verified nothing."
        )
    validated: dict[str, str] = {}
    for tenant_id, token in bundle.items():
        try:
            uuid.UUID(str(tenant_id))
        except ValueError as exc:
            raise RotationError(
                f"{where}: key {tenant_id!r} is not a UUID. Both marketing and sales "
                "validate this at Settings construction and CrashLoop the pod on a bad "
                "key — this must not reach git."
            ) from exc
        if not isinstance(token, str) or not token.strip():
            raise RotationError(
                f"{where}: the entry for tenant {tenant_id} is not a non-empty string. "
                "A blank token would introspect inactive and silently satisfy the "
                "post-revocation control without ever having been a credential."
            )
        validated[str(tenant_id)] = token
    return validated


@dataclass
class TenantRotation:
    """One tenant's slot in the bundle, carried through the phases."""

    tenant_id: str
    old_token: str
    license_id: str | None = None
    new_token: str | None = None
    #: Only True once the old token has been confirmed REFUSED after revocation.
    #: Until then a failure must report this tenant as stranded.
    old_revoked: bool = False

    @property
    def old_fp(self) -> str:
        return _fingerprint(self.old_token)

    @property
    def new_fp(self) -> str:
        return _fingerprint(self.new_token) if self.new_token else "(not minted)"


@dataclass
class RollTarget:
    """A workload to bump, plus the container to exec into for verification.

    The container matters. ``marketing/Deployment/marketing`` has an init container
    (``migrate``) and an app container (``api``) both taking ``envFrom:
    marketing-secrets``; a ``kubectl exec`` with no ``-c`` picks the default
    container, and verifying the wrong one is a check that passes for the wrong
    reason.
    """

    workload: str  # e.g. "deploy/marketing"
    container: str | None = None

    @classmethod
    def parse(cls, spec: str) -> RollTarget:
        workload, _, container = spec.partition(":")
        if "/" not in workload:
            raise RotationError(
                f"--roll {spec!r} must be KIND/NAME[:CONTAINER], e.g. deploy/marketing:api"
            )
        return cls(workload=workload, container=container or None)

    @property
    def name(self) -> str:
        return self.workload.split("/", 1)[1]


@dataclass
class Runner:
    """An injectable process runner.

    Injectable because the tests must exercise the ORDER of operations — mint
    before write, write before verify, verify before revoke — and that order is
    the thing most likely to be wrong. A test that could only mock the HTTP layer
    would prove nothing about it.
    """

    dry_run: bool = True
    #: Every command run, in order, with dry-run skips recorded. Tests assert on it.
    calls: list[list[str]] = field(default_factory=list)

    def _exec(self, argv: list[str], stdin: str | None = None, cwd: str | None = None) -> str:
        proc = subprocess.run(  # noqa: S603 - argv is built here, never shell-interpolated
            argv, input=stdin, capture_output=True, text=True, check=False, cwd=cwd
        )
        if proc.returncode != 0:
            raise RotationError(
                f"{' '.join(argv[:4])}... failed ({proc.returncode}): {proc.stderr.strip()[:400]}"
            )
        return proc.stdout

    def read(self, argv: list[str], cwd: str | None = None) -> str:
        """Always executed, even in a dry run: a dry run that reads nothing verifies nothing."""
        self.calls.append(argv)
        return self._exec(argv, cwd=cwd)

    def write(self, argv: list[str], stdin: str | None = None, cwd: str | None = None) -> str:
        """Skipped in a dry run, and the skip is recorded so tests can see it."""
        if self.dry_run:
            self.calls.append(["<dry-run-skipped>", *argv])
            return ""
        self.calls.append(argv)
        return self._exec(argv, stdin=stdin, cwd=cwd)


class Cluster:
    """Read-only-by-default access to the live cluster."""

    def __init__(self, runner: Runner):
        self._runner = runner
        self._prefix = shlex.split(os.getenv("KUBECTL", "kubectl"))

    def read_secret_key(self, namespace: str, secret: str, key: str) -> str:
        raw_b64 = self._runner.read(
            [
                *self._prefix,
                "get",
                "secret",
                secret,
                "-n",
                namespace,
                "-o",
                f"jsonpath={{.data.{key}}}",
            ]
        ).strip()
        if not raw_b64:
            raise RotationError(
                f"secret {namespace}/{secret} has no key {key!r} (or it is empty). List the "
                f"key NAMES safely with: kubectl get secret {secret} -n {namespace} "
                "-o go-template='{{range $k,$v := .data}}{{$k}}{{\"\\n\"}}{{end}}'"
            )
        try:
            return base64.b64decode(raw_b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RotationError(f"{namespace}/{secret}:{key} is not base64 UTF-8: {exc}") from exc

    def flux_owner(self, namespace: str, secret: str) -> str | None:
        """The owning Flux Kustomization, or None.

        ⚠️ IT IS A **LABEL**, NOT AN ANNOTATION — measured live 2026-09-15 on both
        ``marketing/marketing-secrets`` and ``sales/sales-secrets``:
        ``metadata.labels."kustomize.toolkit.fluxcd.io/name" = "services"`` and
        ``metadata.annotations`` is **nil**. Both are checked here anyway, because
        which one Flux stamps depends on how the object was applied and getting it
        wrong fails in the dangerous direction (reads as "not Flux-managed" →
        proceeds to patch → Flux reverts it five minutes later onto revoked
        credentials).

        ⚠️ THE GUARD CLAUSES ARE A DISCLOSURE CONTROL, NOT TIDINESS. The obvious
        spelling, ``{{index .metadata.annotations "…"}}``, raises ``index of
        untyped nil`` when the map is absent — and kubectl's template error path
        **prints the whole object first**. Measured: it dumped every base64 value
        in ``marketing-secrets`` to the terminal. In a tool whose entire premise is
        that token values are never printed, that is the bug that matters most on
        this line. ``{{with …}}`` yields nothing for a nil map instead of erroring,
        so the not-managed case is an empty string and never a dump.

        go-template rather than jsonpath because the key contains dots and a
        slash, and escaping those into a jsonpath that survives BOTH a local shell
        and an ssh-wrapped KUBECTL is a reliable source of a silently-empty result.
        """
        query = "".join(
            "{{with .metadata." + scope + "}}"
            '{{with index . "' + FLUX_OWNER_LABEL + '"}}{{.}}{{end}}'
            "{{end}}"
            for scope in ("labels", "annotations")
        )
        owner = self._runner.read(
            [*self._prefix, "get", "secret", secret, "-n", namespace, "-o", f"go-template={query}"]
        ).strip()
        return None if not owner or owner == "<no value>" else owner

    def in_pod_digest(self, namespace: str, target: RollTarget, env_var: str) -> str:
        """SHA-256 of the env var AS THE PROCESS SEES IT, computed inside the container.

        Only the 64-hex digest crosses the wire; the value never leaves the pod, so
        this verification does not itself become a disclosure. ``printf %s`` rather
        than ``echo`` so no trailing newline is folded into the hash.
        """
        argv = [*self._prefix, "exec", target.workload, "-n", namespace]
        if target.container:
            argv += ["-c", target.container]
        argv += ["--", "sh", "-c", f'printf %s "${env_var}" | sha256sum | cut -d" " -f1']
        return self._runner.read(argv).strip().split()[0] if not self._runner.dry_run else ""


class Gitops:
    """Writes to the SOPS-encrypted gitops working tree. Never commits."""

    def __init__(self, runner: Runner, repo: Path):
        self._runner = runner
        self.repo = repo
        if not (repo / ".git").exists():
            raise RotationError(f"{repo} is not a git checkout — pass --gitops-repo.")

    def decrypt_key_at_ref(self, ref: str, rel_path: str, key: str) -> str:
        """The plaintext value of ``stringData[key]`` from ``rel_path`` at git ``ref``.

        This is how ``finalize`` recovers the OLD tokens without any phase having
        written them to a plaintext file: git already holds them, already encrypted
        under the same age recipient. ``sops -d`` is fed the blob on stdin so no
        temporary plaintext copy exists on disk at any point.
        """
        blob = self._runner.read(["git", "show", f"{ref}:{rel_path}"], cwd=str(self.repo))
        decrypted = self._runner._exec(
            ["sops", "-d", "--input-type", "yaml", "--output-type", "json", "/dev/stdin"],
            stdin=blob,
            cwd=str(self.repo),
        )
        doc = json.loads(decrypted)
        value = (doc.get("stringData") or {}).get(key)
        if not value:
            raise RotationError(
                f"{rel_path} at {ref} has no stringData.{key}. Is --previous-ref really a "
                "commit from BEFORE the rotation?"
            )
        return str(value)

    def set_key(self, rel_path: str, key: str, value: str) -> None:
        """``sops --set`` the value back, in place and encrypted.

        The value goes through argv here because that is the only interface
        ``sops --set`` offers. It is the one place in this script a token-bearing
        string touches a command line, and it is a LOCAL sops process on the
        operator's own machine — not the ssh-wrapped KUBECTL, whose argv would land
        in a remote host's audit record. It is called out rather than hidden.
        """
        self._runner.write(
            ["sops", "--set", f'["stringData"]["{key}"] {json.dumps(value)}', rel_path],
            cwd=str(self.repo),
        )

    def bump_revision_annotation(self, rel_path: str, annotation: str, new_value: str) -> None:
        """Bump the pod-template annotation that makes Flux actually roll the pods.

        ⚠️ Without this the merge is INERT. Flux updates the Secret in etcd and
        nothing else; every running pod keeps the OLD value in its environment
        (``envFrom`` resolves once at container start, and both apps then cache the
        parsed settings for the process lifetime). Everything keeps working — on the
        old token — right up until ``finalize`` revokes it.

        gitops-production's ``scripts/validate-strato/config_drift.py`` fails CI on
        a Secret change without this bump, precisely because Flux decrypts SOPS
        after kustomize builds, so no derived checksum is possible.
        """
        path = self.repo / rel_path
        text = path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"^(\s*{re.escape(annotation)}:\s*)(\"[^\"]*\"|\S+)\s*$", re.MULTILINE
        )
        if not pattern.search(text):
            raise RotationError(
                f"{rel_path} has no {annotation!r} pod-template annotation to bump. "
                "Without it Flux applies the Secret and never rolls the pods, and the "
                "whole rotation is inert. Add the annotation (and check the spelling — "
                "the licences Deployment uses 'secret-rev', not 'secret-revision')."
            )
        updated, count = pattern.subn(rf'\g<1>"{new_value}"', text)
        if self._runner.dry_run:
            self._runner.calls.append(
                ["<dry-run-skipped>", "bump", rel_path, annotation, new_value]
            )
            return
        path.write_text(updated, encoding="utf-8")
        self._runner.calls.append(["bump", rel_path, annotation, new_value, f"occurrences={count}"])


class LicensesClient:
    """The SANCTIONED issuance path: the licences HTTP API, not the database.

    ⛔ This client deliberately has no database handle. Minting with a direct
    ``INSERT INTO tokens`` would bypass every check the route owns — the
    ``license-admin``/``license-manager`` role gate, the tenant-ownership check
    that 404s a licence belonging to somebody else, and the archived-tenant
    refusal that stops a retired tenant being issued a credential outliving its
    retirement. That is the authz-bypass shape this fleet has banned, and "it was
    only a rotation script" is exactly how such a path gets introduced.
    """

    def __init__(self, base_url: str, bearer: str, timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS):
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {bearer}", "content-type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LicensesClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def introspect(self, raw_token: str) -> dict[str, Any]:
        """POST /license/introspect. Never raises on ``active: false`` — that is DATA.

        The post-revocation control depends on being able to read a refusal as a
        result rather than as an error.
        """
        resp = self._client.post(f"{self._base}/license/introspect", json={"token": raw_token})
        if resp.status_code != 200:
            raise RotationError(
                f"introspect returned HTTP {resp.status_code} for token "
                f"{_fingerprint(raw_token)}: {resp.text[:200]}"
            )
        return resp.json()

    def mint(
        self,
        license_id: str,
        tenant_id: str,
        token_type: str,
        expires_days: int | None,
        long_lived: tuple[str, str] | None = None,
    ) -> str:
        """POST /licenses/{license_id}/tokens — mint ALONGSIDE, leaving the old live.

        Requires ``license-admin`` or ``license-manager``; a ``long_lived`` mint
        (``--perpetual``) requires ``license-admin`` (403
        ``long_lived_requires_license_admin``). ``X-Tenant-Id`` must be
        the licence's own tenant or the route answers 404 — it will not confirm the
        licence exists, on purpose.

        A PERPETUAL token must be a REGISTERED long-lived one (licenses#244): pass
        ``long_lived=(owner, reason)``. The route refuses an unregistered perpetual
        internal/node mint and bounds an api mint with no expiry to 365 days. This
        script refuses to choose either implicitly; see ``--expires-days`` /
        ``--perpetual`` + ``--long-lived-owner`` / ``--long-lived-reason``.
        """
        params: dict[str, Any] = {"token_type": token_type}
        if expires_days is not None:
            params["expires_days"] = expires_days
        if long_lived is not None:
            params["long_lived"] = "true"
            params["long_lived_owner"], params["long_lived_reason"] = long_lived
        resp = self._client.post(
            f"{self._base}/licenses/{license_id}/tokens",
            params=params,
            headers={"X-Tenant-Id": tenant_id},
        )
        if resp.status_code != 201:
            raise RotationError(
                f"mint for licence {license_id} (tenant {tenant_id}) returned HTTP "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()["token"]

    def revoke(self, raw_token: str, tenant_id: str) -> bool:
        """POST /license/revoke. An already-revoked token answers ``revoked: false``."""
        resp = self._client.post(
            f"{self._base}/license/revoke",
            json={"token": raw_token},
            headers={"X-Tenant-Id": tenant_id},
        )
        if resp.status_code != 200:
            raise RotationError(
                f"revoke returned HTTP {resp.status_code} for token "
                f"{_fingerprint(raw_token)}: {resp.text[:200]}"
            )
        return bool(resp.json().get("revoked"))


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


def resolve_and_prove_live(
    client: LicensesClient, rotations: list[TenantRotation], app_key: str | None
) -> None:
    """Introspect every CURRENT token. Two jobs; the second is the one that gets skipped.

    * Resolve each tenant's ``license_id``, which the mint route needs.
    * Establish that the old token is live **now**.

    The second is what gives the post-revocation control its meaning. That control
    asserts the old token is REFUSED after revocation — but if the old token had
    already been dead before this script ran, it would pass having measured
    nothing at all. That is the check-with-no-discriminating-power shape this repo
    has been bitten by more than once. So an already-inactive entry is a hard
    failure here, never a skip.
    """
    for rot in rotations:
        body = client.introspect(rot.old_token)
        if not body.get("active"):
            raise RotationError(
                f"tenant {rot.tenant_id}: the CURRENT token ({rot.old_fp}) already introspects "
                f"INACTIVE (reason={body.get('reason')!r}). Refusing to continue — the "
                "post-revocation control can only prove something if the old token was "
                "provably ACCEPTED first. Fix or remove this bundle entry, then re-run."
            )
        if body.get("tenant_id") != rot.tenant_id:
            raise RotationError(
                f"bundle key {rot.tenant_id} holds a token that introspects for a DIFFERENT "
                f"tenant ({body.get('tenant_id')}). Rotating it would mint the replacement "
                "onto the wrong licence. Nothing was written."
            )
        license_id = body.get("license_id")
        if not license_id:
            raise RotationError(
                f"tenant {rot.tenant_id}: introspect returned active with no license_id; "
                "there is nothing to mint the replacement onto."
            )
        if app_key is not None and (body.get("app") or {}).get("key") != app_key:
            raise RotationError(
                f"tenant {rot.tenant_id}: token belongs to app "
                f"{(body.get('app') or {}).get('key')!r}, not the expected {app_key!r}."
            )
        rot.license_id = str(license_id)


def mint_replacements(
    client: LicensesClient,
    rotations: list[TenantRotation],
    token_type: str,
    expires_days: int | None,
    long_lived: tuple[str, str] | None = None,
) -> None:
    """Mint alongside, then verify each new token BEFORE it goes anywhere near git.

    A 201 from the mint route is not proof the credential works. This service has
    shipped a rotation that produced a token ``/license/introspect`` rejected on
    first use — the inherited-past-expiry defect ``AllTokensExpiredError`` now
    closes. Verifying at mint time means a bad credential never reaches a
    Deployment: the failure is a non-zero exit with the old tokens untouched,
    rather than a CrashLooping namespace after a merge.
    """
    for rot in rotations:
        if rot.license_id is None:
            raise RotationError(f"tenant {rot.tenant_id}: licence unresolved; run --phase plan.")
        rot.new_token = client.mint(
            rot.license_id, rot.tenant_id, token_type, expires_days, long_lived=long_lived
        )
        body = client.introspect(rot.new_token)
        if not body.get("active"):
            raise RotationError(
                f"tenant {rot.tenant_id}: the NEWLY MINTED token ({rot.new_fp}) does not "
                f"introspect active (reason={body.get('reason')!r}). It has NOT been written "
                "to git and the old token is untouched."
            )
        if body.get("tenant_id") != rot.tenant_id:
            raise RotationError(
                f"tenant {rot.tenant_id}: the newly minted token introspects for tenant "
                f"{body.get('tenant_id')}. It has NOT been written to git."
            )
        # The old token MUST still be live here. If minting killed it, this script is
        # calling a cutover endpoint, not the overlap one, and every assumption in
        # the module docstring is wrong.
        if not client.introspect(rot.old_token).get("active"):
            raise RotationError(
                f"tenant {rot.tenant_id}: minting the replacement KILLED the old token "
                f"({rot.old_fp}). That is a hard cutover, not an overlap. Stop and "
                "investigate — this script relies on POST /licenses/{license_id}/tokens "
                "minting ALONGSIDE, and prod is now one merge away from an outage."
            )


def wait_for_consumers(
    cluster: Cluster,
    namespace: str,
    targets: list[RollTarget],
    env_var: str,
    expected_digest: str,
    timeout_seconds: int,
) -> None:
    """Wait until EVERY consumer's process environment holds the new bundle, then stop.

    This is the step that distinguishes "Flux applied the Secret" from "the
    consumer picked it up", and it is the step a runbook written from memory
    always omits. It polls the in-pod digest rather than the Secret's
    ``resourceVersion``, because the Secret updating is precisely the thing that
    is NOT sufficient.

    On timeout it fails rather than proceeding. Proceeding would revoke a
    credential the pods are still using.
    """
    deadline = time.monotonic() + timeout_seconds
    pending = list(targets)
    last: dict[str, str] = {}
    # do-while, not while: at least ONE probe must happen. A plain `while
    # time.monotonic() < deadline` with a zero or already-elapsed timeout exits
    # having checked nothing and then reports "never picked it up" — a failure
    # that looks identical to a real one and is not.
    while True:
        still: list[RollTarget] = []
        for target in pending:
            try:
                digest = cluster.in_pod_digest(namespace, target, env_var)
            except RotationError as exc:
                # A pod mid-roll is not reachable; that is expected, not fatal yet.
                logger.info("%s not yet exec-able (%s)", target.workload, str(exc)[:120])
                still.append(target)
                continue
            last[target.workload] = digest
            if digest != expected_digest:
                still.append(target)
        pending = still
        if not pending or time.monotonic() >= deadline:
            break
        logger.info("waiting for %s to pick up the new bundle …", [t.workload for t in pending])
        time.sleep(RECONCILE_POLL_SECONDS)
    if pending:
        raise RotationError(
            "these consumers never picked up the new bundle within "
            f"{timeout_seconds}s: "
            + ", ".join(
                f"{t.workload} (holds {last.get(t.workload, '?')[:12]!r}, "
                f"expected {expected_digest[:12]!r})"
                for t in pending
            )
            + ".\nMost likely causes, in order: the PR is not merged; Flux has not "
            "reconciled (`flux get kustomization services -n flux-system`); the "
            f"pod-template revision annotation was not bumped, so the Secret changed and "
            "the pods never rolled; or another source (a ConfigMap literal, a hard-coded "
            f"default) shadows {env_var}. THE OLD TOKENS HAVE NOT BEEN REVOKED."
        )


def revoke_and_prove_dead(client: LicensesClient, rotations: list[TenantRotation]) -> None:
    """Revoke the old tokens, then PROVE they are refused.

    ⛔ THE CONTROL THAT CAN FAIL. A "verification" that cannot return a negative
    result verifies nothing. Here the control is genuinely two-sided, and it
    measures a TRANSITION rather than asserting a state:

    * ``resolve_and_prove_live`` required ``active: true`` on this exact token;
    * this step requires ``active: false`` on the same token after the revoke.

    That pairing is what makes it real. If ``/license/revoke`` silently wrote
    nothing — which it used to, when it gated the write on a licence-aware
    ``active`` check, see the route's own comment — this assertion goes red
    instead of the incident being closed on a credential that is still live.
    """
    stranded: list[TenantRotation] = []
    for rot in rotations:
        try:
            client.revoke(rot.old_token, rot.tenant_id)
            body = client.introspect(rot.old_token)
            if body.get("active"):
                raise RotationError(
                    f"tenant {rot.tenant_id}: the OLD token ({rot.old_fp}) STILL introspects "
                    "ACTIVE after /license/revoke reported success. The leaked credential is "
                    "live. Do not close this incident."
                )
            rot.old_revoked = True
        except RotationError as exc:
            logger.error("tenant %s: %s", rot.tenant_id, exc)
            stranded.append(rot)
    if stranded:
        raise StrandedError(
            "the new bundle is DEPLOYED and verified in-pod, but these old tokens could not "
            "be confirmed revoked:\n"
            + "\n".join(
                f"  tenant={r.tenant_id} licence={r.license_id} old=sha256:{r.old_fp}"
                for r in stranded
            )
            + "\nThe fleet is UP — it is running on the new tokens. The LEAK IS NOT CLOSED. "
            "Revoke these by hand via POST /license/revoke, or re-run --phase finalize."
        )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def _bearer() -> str:
    """The admin bearer: from env, else via this repo's own outbound SA path."""
    explicit = os.getenv("LICENSES_ADMIN_TOKEN")
    if explicit:
        return explicit
    try:
        from service_auth import ServiceTokenProvider
    except ImportError as exc:  # pragma: no cover - only outside the repo
        raise RotationError(
            "LICENSES_ADMIN_TOKEN is unset and service_auth could not be imported; run from "
            "the licenses repo root or set the bearer explicitly."
        ) from exc
    provider = ServiceTokenProvider()
    if not provider.configured:
        raise RotationError(
            "LICENSES_ADMIN_TOKEN is unset and the service-account credential is not "
            "configured (SERVICE_TOKEN_URL / SERVICE_CLIENT_ID / SERVICE_CLIENT_SECRET). "
            "Those live in the `licenses-env` Secret in namespace `licenses`."
        )
    token = asyncio.run(provider.get_token())
    if not token:
        raise RotationError(
            "client-credentials token fetch failed — see the warning logged above. A 401/403 "
            "here is a RESULT: the SA client lacks license-admin / license-manager."
        )
    return token


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Overlap-window rotation of a {tenant_id: token} licence-token bundle."
    )
    p.add_argument(
        "--phase",
        choices=["plan", "stage", "finalize"],
        default="plan",
        help="plan = read-only (default). stage = mint + write gitops. finalize = verify + revoke.",
    )
    p.add_argument("--namespace", required=True, help="Namespace holding the Secret.")
    p.add_argument("--secret", required=True, help="Secret name, e.g. marketing-secrets.")
    p.add_argument("--key", required=True, help="Secret key holding the JSON bundle.")
    p.add_argument(
        "--secrets-file",
        default=None,
        help="Path of the SOPS secrets.yaml inside the gitops repo, e.g. "
        "clusters/strato/services/marketing/secrets.yaml. Required for stage/finalize.",
    )
    p.add_argument(
        "--deployment-file",
        action="append",
        default=[],
        help="Gitops file whose pod-template revision annotation must be bumped. Repeatable. "
        "Required for --phase stage: without a bump Flux applies the Secret and never "
        "rolls the pods, and the whole rotation is inert.",
    )
    p.add_argument(
        "--revision-annotation",
        default=DEFAULT_REVISION_ANNOTATION,
        help="Pod-template annotation to bump (default secret-revision; the licences "
        "Deployment uses secret-rev).",
    )
    p.add_argument(
        "--revision-value", default=None, help="New annotation value (default: rotate-<UTC date>)."
    )
    p.add_argument(
        "--roll",
        action="append",
        default=[],
        metavar="KIND/NAME[:CONTAINER]",
        help="A consuming workload to verify in-pod, e.g. deploy/marketing:api. Repeatable.",
    )
    p.add_argument("--env-var", default=None, help="Env var inside the pod (default: --key).")
    p.add_argument("--app-key", default=None, help="Assert every token belongs to this app.")
    p.add_argument("--token-type", default="internal", choices=["api", "node", "internal"])
    expiry = p.add_mutually_exclusive_group()
    expiry.add_argument(
        "--expires-days", type=int, default=None, help="Replacement lifetime, days."
    )
    expiry.add_argument(
        "--perpetual",
        action="store_true",
        help="Mint PERPETUAL replacements. Explicit on purpose: a rotation that silently "
        "defaults to perpetual is how a bounded credential gets laundered into an "
        "unbounded one — a defect this service has already had to close once. Requires "
        "--long-lived-owner and --long-lived-reason: a perpetual token must be REGISTERED "
        "(licenses#244).",
    )
    p.add_argument("--long-lived-owner", default=None, help="Owner for the long-lived register.")
    p.add_argument("--long-lived-reason", default=None, help="Reason for the long-lived register.")
    p.add_argument(
        "--previous-ref",
        default=None,
        help="Git ref of the PRE-rotation secrets file, for --phase finalize. The old tokens "
        "are recovered from there rather than journalled to disk.",
    )
    p.add_argument("--gitops-repo", default=os.getenv("GITOPS_REPO", DEFAULT_GITOPS_REPO))
    p.add_argument("--reconcile-timeout", type=int, default=DEFAULT_RECONCILE_TIMEOUT_SECONDS)
    p.add_argument(
        "--flux-managed-ok",
        action="store_true",
        help="Unused by the git path; kept so a non-Flux Secret can be rotated in future "
        "without the refusal becoming a silent allow.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the phase's writes. Without it the run is read-only.",
    )
    return p.parse_args(argv)


def _report(
    rotations: list[TenantRotation], phase: str, dry_run: bool, targets: list[RollTarget]
) -> None:
    head = f"{phase.upper()} — DRY RUN, nothing written" if dry_run else f"{phase.upper()} COMPLETE"
    print(f"\n=== {head} ===")
    print(f"tenants in bundle : {len(rotations)}")
    print(f"consumers         : {', '.join(t.workload for t in targets) or '(none given)'}")
    print(f"{'tenant_id':38} {'licence':38} {'old':16} {'new':16} old_revoked")
    for rot in rotations:
        print(
            f"{rot.tenant_id:38} {str(rot.license_id):38} sha256:{rot.old_fp:9} "
            f"sha256:{rot.new_fp:9} {rot.old_revoked}"
        )
    print("\n(values shown are SHA-256 fingerprints, never tokens)")


def main(argv: list[str] | None = None) -> int:  # noqa: C901 - the phases are the structure
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    dry_run = not args.apply
    env_var = args.env_var or args.key
    expires_days = None if args.perpetual else args.expires_days

    try:
        targets = [RollTarget.parse(spec) for spec in args.roll]
    except RotationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.phase in ("stage", "finalize") and not args.secrets_file:
        print(f"ERROR: --phase {args.phase} requires --secrets-file.", file=sys.stderr)
        return 2
    if args.phase == "stage" and args.apply:
        if not args.deployment_file:
            print(
                "ERROR: --phase stage --apply requires at least one --deployment-file. The "
                "pod-template revision annotation IS the roll trigger: envFrom resolves once "
                "at container start and both apps cache the parsed settings for the process "
                "lifetime, so a Secret change without a bump is INERT — and invisibly so, "
                "until finalize revokes the old token and the namespace 402s.",
                file=sys.stderr,
            )
            return 2
        if expires_days is None and not args.perpetual:
            print(
                "ERROR: --phase stage --apply requires an explicit expiry decision: "
                "--expires-days N, or --perpetual.",
                file=sys.stderr,
            )
            return 2
        if expires_days is not None and expires_days < 1:
            print("ERROR: --expires-days must be >= 1.", file=sys.stderr)
            return 2
        if args.perpetual and not (args.long_lived_owner and args.long_lived_reason):
            print(
                "ERROR: --perpetual requires --long-lived-owner and --long-lived-reason: "
                "a perpetual token must be a REGISTERED long-lived one (licenses#244).",
                file=sys.stderr,
            )
            return 2
    if args.phase == "finalize":
        if not args.previous_ref:
            print(
                "ERROR: --phase finalize requires --previous-ref <sha>, the commit of the "
                "secrets file from BEFORE the rotation. The old tokens are recovered from "
                "git rather than journalled to disk.",
                file=sys.stderr,
            )
            return 2
        if not targets:
            print(
                "ERROR: --phase finalize requires --roll targets to verify in-pod before "
                "revoking. Revoking without that check is revoking blind.",
                file=sys.stderr,
            )
            return 2

    runner = Runner(dry_run=dry_run)
    cluster = Cluster(runner)

    try:
        owner = cluster.flux_owner(args.namespace, args.secret)
        if owner:
            logger.info(
                "secret %s/%s is owned by Flux Kustomization %r — writes go through git, "
                "not kubectl patch.",
                args.namespace,
                args.secret,
                owner,
            )
        elif args.phase in ("stage", "finalize"):
            raise FluxManagedError(
                f"secret {args.namespace}/{args.secret} carries no {FLUX_OWNER_LABEL} "
                "annotation, so it is NOT Flux-owned — but this script only knows how to "
                "write through the SOPS/gitops path. Rotating an unmanaged Secret needs a "
                "kubectl-patch mode that does not exist yet; adding one silently would be "
                "how the Flux refusal turns into a silent allow. Nothing was written."
            )

        live = cluster.read_secret_key(args.namespace, args.secret, args.key)
        try:
            parsed = json.loads(live)
        except json.JSONDecodeError as exc:
            print(
                f"ERROR: {args.namespace}/{args.secret}:{args.key} is not JSON ({exc}). This "
                "script rotates a {tenant_id: token} MAP; a singular scalar token (e.g. "
                "CONTROL_SERVICE_LICENSE_TOKEN) is a different shape and is NOT handled here.",
                file=sys.stderr,
            )
            return 3
        bundle = validate_bundle(parsed, f"{args.namespace}/{args.secret}:{args.key}")

        gitops = (
            Gitops(runner, Path(args.gitops_repo).expanduser())
            if args.phase in ("stage", "finalize")
            else None
        )

        with LicensesClient(
            os.getenv("LICENSES_BASE_URL", DEFAULT_LICENSES_BASE_URL), _bearer()
        ) as client:
            if args.phase == "finalize":
                assert gitops is not None  # noqa: S101 - guaranteed above
                old_raw = gitops.decrypt_key_at_ref(args.previous_ref, args.secrets_file, args.key)
                old_bundle = validate_bundle(
                    json.loads(old_raw), f"{args.secrets_file}@{args.previous_ref}:{args.key}"
                )
                rotations = [
                    TenantRotation(tenant_id=t, old_token=tok)
                    for t, tok in sorted(old_bundle.items())
                ]
                for rot in rotations:
                    rot.new_token = bundle.get(rot.tenant_id)
                    body = client.introspect(rot.old_token)
                    rot.license_id = body.get("license_id")
                    if not body.get("active"):
                        logger.info(
                            "tenant %s: old token %s is ALREADY inactive — nothing to revoke.",
                            rot.tenant_id,
                            rot.old_fp,
                        )
                        rot.old_revoked = True
                missing = [r.tenant_id for r in rotations if not r.new_token]
                if missing:
                    raise RotationError(
                        f"the LIVE bundle has no entry for tenants {missing} that the "
                        f"pre-rotation bundle at {args.previous_ref} had. Revoking now would "
                        "leave those tenants with no credential at all. Nothing was revoked."
                    )
                if {r.tenant_id for r in rotations if r.old_token == r.new_token}:
                    raise RotationError(
                        "the live bundle still equals the pre-rotation bundle for at least one "
                        "tenant — the new values have NOT reached the cluster. Is the PR "
                        "merged and reconciled? Nothing was revoked."
                    )
                wait_for_consumers(
                    cluster,
                    args.namespace,
                    targets,
                    env_var,
                    bundle_digest(bundle),
                    args.reconcile_timeout,
                )
                if dry_run:
                    print(
                        "\nDRY RUN: consumers verified in-pod; the revoke step was NOT run.\n"
                        "Re-run with --apply to revoke the old tokens and assert the control."
                    )
                    _report(rotations, args.phase, dry_run=True, targets=targets)
                    return 0
                revoke_and_prove_dead(client, [r for r in rotations if not r.old_revoked])
                _report(rotations, args.phase, dry_run=False, targets=targets)
                return 0

            rotations = [
                TenantRotation(tenant_id=t, old_token=tok) for t, tok in sorted(bundle.items())
            ]
            resolve_and_prove_live(client, rotations, args.app_key)

            if args.phase == "plan" or dry_run:
                _report(rotations, args.phase, dry_run=True, targets=targets)
                print(
                    "\nEvery current token above was introspected and is LIVE, and its licence "
                    "resolved. Next: --phase stage --apply (with --expires-days or --perpetual)."
                )
                return 0

            assert gitops is not None  # noqa: S101 - guaranteed above
            mint_replacements(
                client,
                rotations,
                args.token_type,
                expires_days,
                long_lived=(
                    (args.long_lived_owner, args.long_lived_reason) if args.perpetual else None
                ),
            )
            new_bundle = {r.tenant_id: r.new_token for r in rotations if r.new_token}
            validate_bundle(new_bundle, "the newly minted bundle")
            gitops.set_key(args.secrets_file, args.key, serialise_bundle(new_bundle))
            revision = (
                args.revision_value
                or f"rotate-{time.strftime('%Y-%m-%d', time.gmtime())}-{args.key.lower()}"
            )
            for dep_file in args.deployment_file:
                gitops.bump_revision_annotation(dep_file, args.revision_annotation, revision)

        _report(rotations, args.phase, dry_run=False, targets=targets)
        print(
            f"\nSTAGED in {args.gitops_repo}. The OLD tokens are STILL LIVE — the fleet is up.\n"
            "Next, by hand:\n"
            "  1. git -C <gitops> diff   (values are SOPS ciphertext; confirm only the intended\n"
            f"     keys and the {args.revision_annotation!r} bumps changed)\n"
            "  2. commit on a branch, open ONE PR, merge it\n"
            "  3. wait for Flux: flux get kustomization services -n flux-system\n"
            f"  4. re-run with --phase finalize --previous-ref <the pre-rotation sha> --apply\n"
            "     which verifies in-pod and only then revokes the old tokens."
        )
        return 0

    except FluxManagedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 4
    except StrandedError as exc:
        print(f"STRANDED: {exc}", file=sys.stderr)
        return 5
    except RotationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
