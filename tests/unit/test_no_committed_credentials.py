"""No live credential may be a hardcoded default in this repo.

On 2026-08-11 `tests/integration/conftest.py` was found to carry, as the
fallback of `os.environ.get("LICENSE_TOKEN", ...)`, a **live, unrevoked,
perpetual production api token** for the `kag` app on tenant `n8n-dev`. It was
created 2026-05-16 and was still being used the day it was found, so it had sat
in the repo — and in every clone's git history — for nearly three months.

`detect-secrets` did not catch it. The value is a bare high-entropy string in
the second argument of `os.environ.get`, which its keyword plugin does not read
as an assignment; it flagged the DATABASE_URL five lines below and missed this.
So the secret gate cannot be the only thing standing here — hence this test.

It is deliberately a UNIT test (it runs in CI, on every push) rather than an
integration test: the 58 `-m integration` tests are excluded by `addopts` and
by the archetype, so a guard living there would never execute.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Names whose value is a credential, not configuration.
_CREDENTIAL_NAMES = re.compile(
    r"(token|secret|password|passwd|api_?key|private_?key|credential)",
    re.IGNORECASE,
)

# A UUID is configuration (tenant ids, app ids) and is allowed as a default.
_UUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Obvious placeholders that are not real credentials.
_PLACEHOLDER = re.compile(
    r"\A(changeme|placeholder|example|dummy|test|fake|xxx+|\*+|<[^>]*>|)\Z",
    re.IGNORECASE,
)

# Long enough to be a real credential rather than a flag or a short word.
_MIN_CREDENTIAL_LEN = 16

#: SHA-256 of the two credentials removed from ``KEYCLOAK_SETUP.md``.
#: Hashes, NEVER values — see ``test_the_text_guard_can_actually_fire``.
_REMOVED_CREDENTIAL_SHA256 = frozenset(
    {
        "8413766de7457acaab94349d0ac8216350fae8f302069246ada9424af31b4c54",  # pragma: allowlist secret
        "be9a8b401e4d55bc83414ac051d8ed678619060c110f0f8e46ca844071fcc5b1",  # pragma: allowlist secret
    }
)

#: Credential-shaped runs, used to tokenise a file before hashing.
#:
#: The charset is broad (it must cover base64, including `/` and `=` padding), so
#: a raw match can span a whole `KEY=value` pair. `_candidate_tokens` therefore
#: also splits on the separators — without that, `KEYCLOAK_CLIENT_SECRET=<value>`
#: hashed as ONE token and the value was never hashed on its own, so the check
#: would have missed the exact shape at `KEYCLOAK_SETUP.md:308`. Caught by
#: `test_the_hash_check_can_actually_fire`, which is why that floor exists.
_CANDIDATE_TOKEN = re.compile(r"[A-Za-z0-9_+./~=-]{16,}")
_TOKEN_SEPARATORS = re.compile(r"[=:,;'\"`\s]+")


def _candidate_tokens(text: str) -> set[str]:
    """Every credential-shaped run in ``text``, plus its separator-split parts."""
    out: set[str] = set()
    for raw in _CANDIDATE_TOKEN.findall(text):
        out.add(raw)
        for part in _TOKEN_SEPARATORS.split(raw):
            if len(part) >= _MIN_CREDENTIAL_LEN:
                out.add(part)
    return out


def _tracked_files() -> list[Path]:
    """Every file git TRACKS in this checkout.

    ⚠️ Load-bearing, and it replaced an `rglob` walk. This module is named
    "no COMMITTED credentials", so the corpus must be what git tracks — not what
    happens to sit on disk. `rglob` matched dotfiles, so `*.env` matched the
    repo's **gitignored** `.env`, the sanctioned local secret store: the text
    guard found four real developer secrets there and went RED on the primary
    checkout while CI, which has no `.env`, stayed green. That is the "cries
    wolf" failure the comment above `_TEXT_ASSIGNMENT` predicts, aimed at the
    person least able to ignore it. Found by an adversarial reviewer.

    Using git also means `.venv`, build output and other people's worktrees are
    excluded by construction rather than by a hand-maintained skip list.
    """
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [REPO_ROOT / rel for rel in out.split("\0") if rel]


def _python_files() -> list[Path]:
    """Every .py file in THIS checkout.

    The skip list is matched against the path RELATIVE to the repo root, never
    the absolute path. Matching absolute parts made this scanner return an
    empty list whenever the checkout itself lived under a skipped directory —
    and `.worktrees` is in the list, so running from a git worktree (the normal
    way work happens here) silently scanned NOTHING. Every assertion below then
    passed vacuously, including against the exact file that had the live token
    in it. Caught by re-introducing the defect and watching the guard stay green.
    """
    return [p for p in _tracked_files() if p.suffix == ".py" and p.is_file()]


#: Text files that are NOT Python but can carry a credential just as well.
#: ⚠️ This list exists because the scanner above was `.py`-only, and a Keycloak
#: client secret plus a master-realm `admin` password sat in a committed
#: `KEYCLOAK_SETUP.md` from the repo's INITIAL COMMIT until 2026-08-17 — out of
#: scope by construction, so the guard was green the entire time. A credential
#: does not care what extension it is written under.
_TEXT_SUFFIXES = (
    ".md",
    ".yaml",
    ".yml",
    ".sh",
    ".toml",
    ".json",
    ".txt",
    # ⚠️ `.sql` matters most of the group: 23 tracked files, including the
    # `migrations/*_seed_*.sql` provisioning scripts. A service token in seed SQL
    # is entirely plausible here — `scripts/provision_service_token.py` exists.
    ".sql",
    ".ini",
    ".cfg",
    ".conf",
    ".properties",
    ".env",
    ".example",
    ".tf",
    ".tfvars",
)

#: Files selected by NAME, because they have no usable suffix.
#: `Path(".env").suffix == ""` — so listing `.env` in `_TEXT_SUFFIXES` selected
#: NOTHING, and a *tracked* `.env` (the very file this module is nominally about)
#: was invisible. Dockerfiles are the same shape and `ENV`/`ARG` secrets are a
#: classic. Found by an adversarial reviewer.
_TEXT_NAMES = frozenset({".env", "Dockerfile", "Dockerfile.authcopy", "Makefile"})


def _text_files() -> list[Path]:
    """Every non-Python text file in THIS checkout.

    Same relative-path skip logic as `_python_files`, for the same reason: a
    checkout living under a skipped directory name (`.worktrees`, which is how
    work normally happens in this repo) must not silently scan nothing.
    """
    skip_names = {"uv.lock", "package-lock.json", ".secrets.baseline"}
    return [
        p
        for p in _tracked_files()
        if (p.suffix in _TEXT_SUFFIXES or p.name in _TEXT_NAMES or p.name.startswith("Dockerfile"))
        and p.name not in skip_names
        and p.is_file()
    ]


#: Two shapes, deliberately narrow:
#:   * ``NAME: value`` / ``NAME=value`` — YAML, .env, Markdown (`**Secret:** `v``)
#:   * ``--password value``            — a CLI flag, which takes no separator
#: An explicit separator is REQUIRED for the first form. Allowing a bare space
#: matched English prose — measured, not guessed: it flagged a CI comment
#: ("...nor a LICENSE_TOKEN: tests/integration/...") and three ``openapi.json``
#: descriptions containing the word "token". A guard that cries wolf gets
#: deleted by the first person it annoys, which is how you end up with no guard.
_TEXT_ASSIGNMENT = re.compile(
    r"""(?P<flag>--)?                          # CLI flag form
        (?P<name>[A-Za-z_][A-Za-z0-9_\-]*)     # the key
        \*{0,2}["']?                           # Markdown emphasis / JSON closing quote
        \s*[:=]\s*                             # the separator, REQUIRED
        [*`'"\s]*                              # emphasis / quoting / padding
        (?P<value>[A-Za-z0-9_+./~=-]{16,})     # the value
        """,
    re.VERBOSE,
)

#: The same, for a CLI flag, which takes a SPACE rather than a separator:
#: ``--password 'value'``. Kept apart from the assignment form so the assignment
#: form can REQUIRE its separator — allowing a bare space there matched English
#: prose (a CI comment "...nor a LICENSE_TOKEN: tests/integration/..." and three
#: openapi.json descriptions containing the word "token").
_TEXT_FLAG = re.compile(
    r"""--(?P<name>[A-Za-z_][A-Za-z0-9_\-]*)
        \s+["']?
        (?P<value>[A-Za-z0-9_+./~=-]{16,})
        """,
    re.VERBOSE,
)

#: A generated credential mixes character classes. Natural-language prose and
#: dotted identifiers do not, and neither do the placeholder words already
#: covered by `_PLACEHOLDER`.
_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA = re.compile(r"[A-Za-z]")


def _text_credential_findings(text: str) -> list[tuple[int, str, str]]:
    """(lineno, key, value) for credential-named assignments in a text file.

    ⚠️ Scans with BOTH patterns and keeps every non-overlapping match of each,
    because `finditer` on a single combined pattern resumed PAST the end of a
    match — so in ``-d "client_secret=<secret>"`` the leading ``-d`` matched
    first (name ``d``, value ``client_secret=<secret>``), was rejected as
    non-credential-named, and the scan resumed beyond the secret entirely. That
    shape is `KEYCLOAK_SETUP.md:68` — the exact line the old detect-secrets
    baseline recorded — so the guard missed the one occurrence its predecessor
    caught. Four `curl -d` blocks in that file were invisible. Found by an
    adversarial reviewer.
    """
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        seen: set[tuple[str, str]] = set()
        for pattern in (_TEXT_ASSIGNMENT, _TEXT_FLAG):
            # Restart the scan after each key, not after each whole match, so an
            # earlier short token cannot consume a later credential.
            for match in pattern.finditer(line):
                name, value = match.group("name"), match.group("value")
                if not _CREDENTIAL_NAMES.search(name):
                    continue
                if not _credential_shaped(value):
                    # `_credential_shaped` rejects EVERY UUID as configuration.
                    # Right for `tenant_id`/`app_id`; wrong here — Keycloak's admin
                    # console issues UUID-format client secrets, so a UUID under a
                    # credential-NAMED key is a credential. The secret removed in
                    # this change was 32 chars and escaped that hole by luck.
                    # Found by an adversarial reviewer.
                    if not _UUID.match(value):
                        continue
                # `${VAR}` / `$VAR` / `{{ template }}` are references, not values.
                if value.startswith(("$", "{{", "%")):
                    continue
                # Mixed character classes: a real secret has both, prose rarely does.
                if not (_HAS_DIGIT.search(value) and _HAS_ALPHA.search(value)):
                    continue
                if (name, value) in seen:
                    continue
                seen.add((name, value))
                found.append((lineno, name, value))
    return found


def test_no_credential_shaped_literal_is_committed_in_a_text_file():
    """A Keycloak secret in a Markdown file is exactly as leaked as one in a .py.

    This is the assertion that would have caught `KEYCLOAK_SETUP.md` on the day
    it was committed, rather than 9 months later.
    """
    offenders: list[str] = []
    for path in _text_files():
        if path == Path(__file__):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, name, _value in _text_credential_findings(text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno} {name}")

    assert not offenders, (
        "a credential-shaped literal is committed in a non-Python file — it must "
        "live in SOPS or the environment, never in the tree:\n  " + "\n  ".join(offenders)
    )


def test_the_text_scanner_is_not_empty():
    """Vacuity floor for the scanner above, mirroring the .py one.

    Without it, a broken glob makes `test_no_credential_shaped_literal_is_committed_in_a_text_file`
    pass by scanning zero files — the precise failure mode this repo has already
    shipped once, with the relative-path bug in `_python_files`.
    """
    files = _text_files()
    assert len(files) > 5, f"expected several text files, scanned {len(files)}"
    names = {p.name for p in files}
    assert "README.md" in names, "README.md missing from the scan — the walker is broken"
    assert "pyproject.toml" in names, "pyproject.toml missing from the scan"


def test_the_text_guard_can_actually_fire():
    """POSITIVE CONTROL — the real SHAPES, with INVENTED values.

    The shapes are the ones that actually sat in ``KEYCLOAK_SETUP.md``: a shell
    ``--password '<v>'``, a ``KEY=value`` env line, and a Markdown
    ``**Client Secret:** `<v>` ``.

    ⚠️ The first version used the REAL literals, split at construction so THIS
    guard would not flag itself. That was wrong twice over: it re-committed the
    secrets into a different tracked file, and ``detect-secrets`` scans raw
    content and does not care about string concatenation — it reported them as
    NEW findings and failed the CI gate. **A guard that re-introduces the secret
    it removes is not a guard.** The value-keyed check below uses SHA-256 instead.
    """
    admin_pw = "Synthetic0Admin1Value2Here3"  # pragma: allowlist secret
    client_secret = "Fabricated4Client5Secret6Value7"  # pragma: allowlist secret

    shell = f"kcadm.sh config credentials --user admin --password '{admin_pw}'"
    assert _text_credential_findings(shell), "the shell --password shape is not detected"

    envline = f"KEYCLOAK_CLIENT_SECRET={client_secret}"
    assert _text_credential_findings(envline), "the KEY=value shape is not detected"

    markdown = f"- **Client Secret:** `{client_secret}`"
    assert _text_credential_findings(markdown), "the Markdown `Secret: value` shape is not detected"

    # And the converse — configuration must NOT trip it, or the guard is noise
    # and will be disabled by the first person it annoys.
    assert not _text_credential_findings("KEYCLOAK_URL=https://auth.nonagentic.ai")
    assert not _text_credential_findings("client_secret=${KEYCLOAK_CLIENT_SECRET}")
    assert not _text_credential_findings("tenant_id: 0195f48b-d000-7000-8000-000000000001")


def test_the_shapes_an_adversarial_reviewer_found_it_missing():
    """Each of these returned [] in the first version and is now caught.

    They are not hypothetical: the `curl -d` form is `KEYCLOAK_SETUP.md:68`, the
    ONE line the old detect-secrets baseline had recorded, and the JSON form is
    the reason `.json` is in `_TEXT_SUFFIXES` at all — without it that suffix was
    decorative.
    """
    secret = "Fabricated4Client5Secret6Value7"  # pragma: allowlist secret

    curl = f'  -d "client_secret={secret}"'
    assert _text_credential_findings(curl), (
        "the curl -d form is missed: a leading `-d` matched first and finditer "
        "resumed past the whole secret"
    )

    js = f'  "client_secret": "{secret}",'
    assert _text_credential_findings(js), 'JSON `"key": "value"` is missed'

    yaml_quoted = f'  client_secret: "{secret}"'
    assert _text_credential_findings(yaml_quoted), "quoted YAML is missed"

    # A base64-shaped value containing `/` — the AWS secret-key shape.
    b64 = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY1EXAMPLEKEY"  # pragma: allowlist secret
    assert _text_credential_findings(b64), "a value containing `/` is missed"


def test_the_corpus_comes_from_git_not_from_the_filesystem():
    """DISCRIMINATING control for the `git ls-files` corpus.

    The test this replaces asserted `".env" not in scanned` with `"README.md" in
    scanned` as its control. Both halves were satisfied by an `rglob` walk too, so
    it could not detect the regression it was named for. Worse, `.env` has an
    EMPTY suffix, so the suffix filter excluded it regardless of the walk — and in
    CI there is no `.env` at all. Three independent reasons it proved nothing.

    This asserts on something ONLY `git ls-files` excludes: untracked and ignored
    paths. `.venv/` always exists after `uv sync` and is always ignored, so an
    `rglob` corpus would contain it and a git corpus cannot.
    """
    scanned = _tracked_files()
    assert scanned, "the tracked corpus is empty"

    # The discriminator: `.venv` exists on disk and is gitignored.
    venv = REPO_ROOT / ".venv"
    assert venv.is_dir(), (
        "`.venv` does not exist, so this control cannot discriminate — run `uv sync` "
        "or pick another always-ignored path"
    )
    from_venv = [p for p in scanned if ".venv" in p.relative_to(REPO_ROOT).parts]
    assert not from_venv, (
        f"{len(from_venv)} gitignored .venv paths are in the corpus — the walk is "
        "reading the filesystem, not the git index"
    )

    # Positive half: real tracked files ARE present, so "no .venv" is not "nothing".
    names = {p.name for p in scanned}
    assert {"README.md", "pyproject.toml", "main.py"} <= names, (
        f"the tracked corpus is missing expected files: {sorted(names)[:12]}"
    )


def test_the_removed_keycloak_credentials_stay_out_of_the_working_tree():
    """Keyed on the SHA-256 of each removed value, so a rename cannot bring it back.

    Tokenises every scanned file and hashes each credential-shaped run, rather
    than substring-matching a literal this repo must no longer contain.

    NOTE this checks the WORKING TREE only. Both values remain in git history
    (present from ``c919fb3``, the initial commit) and in every existing clone,
    which no code change can undo.
    """
    hits: list[str] = []
    for path in _python_files() + _text_files():
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(
            hashlib.sha256(t.encode()).hexdigest() in _REMOVED_CREDENTIAL_SHA256
            for t in _candidate_tokens(body)
        ):
            hits.append(str(path.relative_to(REPO_ROOT)))

    assert not hits, f"a removed Keycloak credential is back in the tree: {sorted(set(hits))}"


def test_the_hash_check_can_actually_fire():
    """VACUITY FLOOR: a typo in a hash or in the tokeniser leaves the check above
    permanently green while searching for something that can never be found.

    Uses a throwaway value hashed at runtime, so proving the mechanism needs no
    real secret in the tree.
    """
    probe = "Throwaway8Probe9Value0"  # pragma: allowlist secret
    probe_hash = hashlib.sha256(probe.encode()).hexdigest()

    # Every shape the removed credentials were actually written in.
    for line in (
        f"KEYCLOAK_CLIENT_SECRET={probe}",
        f'  -d "client_secret={probe}"',
        f"- **Client Secret:** `{probe}`",
        f"kcadm.sh config credentials --user admin --password '{probe}'",
        f'  "client_secret": "{probe}",',
    ):
        tokens = _candidate_tokens(line)
        assert probe in tokens, f"the tokeniser did not extract the value from {line!r}: {tokens}"
        assert any(hashlib.sha256(t.encode()).hexdigest() == probe_hash for t in tokens), (
            f"the hash path does not match a value the tokeniser extracted from {line!r}"
        )

    # The pinned hashes are well-formed, so a truncated paste is caught.
    assert len(_REMOVED_CREDENTIAL_SHA256) == 2
    assert all(len(h) == 64 for h in _REMOVED_CREDENTIAL_SHA256), _REMOVED_CREDENTIAL_SHA256


def test_the_file_scanner_is_not_empty():
    """The scanner must actually see this repo's files.

    Without this, every other assertion in the file is vacuous the moment the
    path logic breaks — which is precisely what happened.
    """
    files = _python_files()
    assert len(files) > 50, f"expected the whole repo, scanned {len(files)} files"
    names = {p.name for p in files}
    for expected in ("models.py", "main.py", "conftest.py"):
        assert expected in names, f"{expected} missing from the scan — the walker is broken"


def _credential_shaped(value: str) -> bool:
    if len(value) < _MIN_CREDENTIAL_LEN:
        return False
    if _UUID.match(value) or _PLACEHOLDER.match(value):
        return False
    # A URL or a filesystem path is configuration, not a bare credential.
    # (A DSN *containing* a password is a separate concern, already covered by
    # redact_dsn's tests and by the detect-secrets baseline.)
    return not value.startswith(("http://", "https://", "postgresql", "/", "./"))


def _env_lookup_defaults(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Yield (lineno, env_var_name, default_literal) for env lookups with defaults."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            # os.getenv(...) / os.environ.get(...)
            if func.attr in {"getenv", "get"}:
                name = func.attr
        if name is None:
            continue
        key, default = node.args[0], node.args[1]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if not isinstance(default, ast.Constant) or not isinstance(default.value, str):
            continue
        found.append((node.lineno, key.value, default.value))
    return found


def test_no_credential_shaped_env_default_is_committed():
    """An env var named like a credential must not carry a real default.

    This is the exact shape of the defect:

        LICENSE_TOKEN = os.environ.get("LICENSE_TOKEN", "<a live token>")
    """
    offenders: list[str] = []

    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError, UnicodeDecodeError:
            continue
        for lineno, env_name, default in _env_lookup_defaults(tree):
            if _CREDENTIAL_NAMES.search(env_name) and _credential_shaped(default):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno} {env_name} has a hardcoded default")

    assert not offenders, (
        "a credential-shaped environment default is committed to the repo — "
        "it must come from the environment with NO fallback:\n  " + "\n  ".join(offenders)
    )


def test_the_specific_leaked_token_is_gone_from_the_working_tree():
    """The exact literal that leaked must never come back.

    Belt and braces alongside the AST rule above: that rule keys on the env
    var NAME, so a rename would slip past it. This keys on the value.

    NOTE this checks the WORKING TREE only. The token remains in git history
    and in every existing clone, which no code change can undo — rotation is a
    separate, operator-gated action.
    """
    leaked = (
        "LbHo0Wwa" + "jEZbvvgncafLjXvZVwGTiNxqDbzqwxyrHto"
    )  # split so this file is not itself a hit
    # `_python_files() + _text_files()`, not `_python_files()`. This check was
    # still .py-only inside the very PR whose subject is that a .py-only scanner
    # stayed green for nine months — so the live token could be re-committed into
    # a .md/.json/.yaml/.sh and this stayed green. Found by an adversarial reviewer.
    hits = [
        str(p.relative_to(REPO_ROOT))
        for p in _python_files() + _text_files()
        if p != Path(__file__) and leaked in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not hits, f"the revoked/leaked integration token is present in: {hits}"


def test_the_guard_can_actually_fire():
    """A gate that cannot fire proves nothing — assert against synthetic input.

    Without this, every assertion above passes trivially on a clean corpus and
    nobody learns whether the detector discriminates.
    """
    src = 'import os\nAPI_TOKEN = os.environ.get("API_TOKEN", "kQ8vN2xW9pLm4sYr7tB3zC")\n'
    findings = _env_lookup_defaults(ast.parse(src))
    assert findings, "the AST walker found nothing in text that plainly contains the pattern"
    lineno, name, default = findings[0]
    assert _CREDENTIAL_NAMES.search(name), name
    assert _credential_shaped(default), default

    # And the converse: configuration must NOT trip it.
    ok = 'import os\nTENANT = os.environ.get("TENANT_ID", "0195f48b-d000-7000-8000-000000000001")\n'
    lineno, name, default = _env_lookup_defaults(ast.parse(ok))[0]
    assert not _credential_shaped(default), "a UUID is configuration and must not be flagged"
