# Contributing to Licenses

Thanks for your interest in Licenses. Contributions of every size are welcome —
bug reports, documentation fixes, and code.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

- **Open an issue before a large change.** It saves you writing code that will not be merged.
- **One concern per pull request.** A feature and a refactor belong in two PRs.
- **Keep the tree green.** If a check fails, say so in the PR rather than working around it.
- **No credentials, ever.** Not in code, tests, fixtures, CI, or commit messages.

## Development setup

```bash
git clone https://github.com/nonagenticai/licenses.git
cd licenses
uv sync --frozen
```

## Tests and lint

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

Both must pass before a pull request is ready for review. CI runs the same commands on
GitHub-hosted runners; a local pass and a green CI run are the same gate.

**The suite needs a real PostgreSQL 18.** It does not use SQLite and does not fall back to one.
The easiest route is the same throwaway the workflow uses:

```bash
docker run -d --rm --name licenses-pg -e POSTGRES_PASSWORD=postgres \
  --shm-size=1g -p 127.0.0.1:5432:5432 postgres:18 -c fsync=off
export TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres'
export DATABASE_URL="$TEST_DATABASE_URL"
uv run pytest -q
```

## Branches and commits

Branch names: `feat/`, `fix/`, `docs/`, `chore/`, `ci/` — for example `fix/retry-on-502`.

Commit messages follow `type(scope): summary` in the imperative mood, for example
`fix(api): reject an empty tenant header`.

This project uses the [Developer Certificate of Origin](https://developercertificate.org).
Sign off every commit:

```bash
git commit -s -m "fix(api): reject an empty tenant header"
```

## Pull requests

- Describe the problem before the solution.
- Link the issue it closes, if there is one.
- Add or update tests for behaviour changes.
- A maintainer reviews every PR. First-time contributors and fork PRs need approval
  before CI runs — that is a GitHub default, not a comment on your patch.

## Reporting a security issue

Do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under Apache-2.0, as
described in [LICENSE](LICENSE).
