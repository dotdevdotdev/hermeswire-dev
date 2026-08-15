# Releasing hermeswire-dev

This project publishes the CLI and web app to PyPI as `hermeswire-dev`. Packaging uses Hatchling via `pyproject.toml`.

Every release does **both** a PyPI publish and a GitHub release. The **GitHub release is the changelog** — there is no `CHANGELOG.md`; release notes are built from the commits since the last tag (step 5).

## Steps

### 1. Bump version

Edit `hermeswire/__init__.py` and update `__version__`.

### 2. Commit and push

```bash
git add hermeswire/__init__.py
git commit -m "chore: bump version to {VERSION}"
git push
```

### 3. Build artifacts

```bash
uv build
```

Produces `dist/hermeswire_dev-{VERSION}-py3-none-any.whl` and `dist/hermeswire_dev-{VERSION}.tar.gz`.

Optional sanity check — confirm `hermeswire/templates/` and `hermeswire/static/` are bundled:

```bash
unzip -l dist/hermeswire_dev-{VERSION}-py3-none-any.whl | grep -E "templates/|static/"
```

### 4. Publish to PyPI

The `PYPI_TOKEN` lives in `~/.hermeswire/.env`. Don't `source` the file — it can
contain unquoted `&` in other values (e.g. a URL with query params), which makes
the shell hit a parse error before `uv publish` ever runs. Grep the token out
directly:

```bash
uv publish --token "$(grep '^PYPI_TOKEN=' ~/.hermeswire/.env | cut -d= -f2-)" dist/hermeswire_dev-{VERSION}*
```

### 5. Create GitHub release

`gh release create` auto-creates the git tag — no separate `git tag` call needed. Build the changelog from commits since the last release:

```bash
git log --oneline v{LAST_VERSION}..HEAD
```

```bash
gh release create v{VERSION} --title "v{VERSION}" --notes "## Highlights
- ...

## New Features
- ...

## Fixes
- ...

Built by [dotdev.dev](https://dotdev.dev)"
```

## Notes

- Package name: `hermeswire-dev` (import package is `hermeswire`).
- Python: >=3.10 as declared in `pyproject.toml`.
- Build backend: Hatchling; no `setup.py` required.
- TestPyPI is available via `--publish-url https://test.pypi.org/legacy/` if you want to validate before a real publish.
