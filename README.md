# youtube-sort-playlist

Sorts and groups YouTube playlist entries by subscription.

## Development

Install `uv`, then sync the local environment and install hooks:

```bash
make venv
```

Run autofixes across the repository:

```bash
make fix
```

Run non-mutating validation checks:

```bash
make check
```

`pre-commit` is the primary autofix entrypoint. `make fix` is just a convenience wrapper around
`pre-commit run --all-files`.

## Application Commands

Add new videos to Watch Later:

```bash
make update
```

Sort videos in `Sort Watch Later`:

```bash
make sort
```

## Dependency Refresh

Refresh compatible dependencies and the lockfile with:

```bash
uv lock --upgrade
make venv
make fix
make check
```

This project intentionally keeps its current Python compatibility floor. Runtime dependencies should
only be upgraded as far as they can go without requiring application changes.
