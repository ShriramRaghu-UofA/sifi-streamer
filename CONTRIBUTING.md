# Contributing

This package targets Python 3.14 or newer and uses `uv` for Python dependency
management, commands, and builds. The local capture dashboard uses Node.js 24,
Svelte 5, Tailwind CSS, daisyUI, and uPlot.

Read [AGENTS.md](AGENTS.md), [architecture.md](architecture.md), and
[compatibility.md](compatibility.md) before changing public APIs, capture
records, acquisition lifecycle, process ownership, or compatibility behavior.

## Python environment

Create or update the locked development environment from the repository root:

```powershell
uv sync --locked --dev
```

Add Python dependencies with `uv add`; do not edit `uv.lock` manually. Keep
runtime dependencies minimal. Heavy conversion packages belong behind the
existing `parquet` extra.

Run Python validation with:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run python -m unittest discover -s tests -v
```

Ruff owns lint and formatting checks. `ty` owns static type checking. New or
changed public APIs and non-obvious boundaries should have precise type hints.

## Dashboard development

Normal package installation does not require Node.js. The compiled dashboard
is stored in `sifi_streamer/web_assets` and is included in wheels and Git-based
installs. Node.js is required only when editing the frontend source.

Install exactly the locked frontend dependencies and validate the source:

```powershell
Push-Location frontend
npm ci
npm run check
npm run build
Pop-Location
```

`npm run build` writes the deployable files directly to
`sifi_streamer/web_assets`. Commit those regenerated files with the frontend
source and `frontend/package-lock.json`. Never commit `frontend/node_modules`.
Do not hand-edit generated files in `sifi_streamer/web_assets`.

When changing a `.svelte` file, follow the repository's Svelte skill guidance
and keep the Svelte checker clean. Rebuilding is required even when a source
change appears to affect only styling.

## Full release check

Before handoff or release, run the Python and dashboard checks above followed
by:

```powershell
git diff --check
uv build
```

Install the wheel into a clean Python 3.14 environment and confirm that:

- `import sifi_streamer` resolves from the installed wheel;
- `sifi-capture --help` and `sifi-capture-web --help` succeed;
- the packaged dashboard assets are present;
- a short synthetic capture can be written and read back.

Use temporary directories for captures and installation smoke tests. Do not
commit build output under `dist`, temporary captures, virtual environments, or
Node modules.

## Commits and compatibility

Add regression coverage for meaningful behavior changes. Preserve the
authoritative append-only schema-v2 capture format unless a schema correction
is explicitly approved and documented. Keep application-specific vocabulary
and artifact policy in consumer repositories.

Do not create commits unless the task explicitly requests one. Release tags
should be immutable and match the version declared in `pyproject.toml`.
