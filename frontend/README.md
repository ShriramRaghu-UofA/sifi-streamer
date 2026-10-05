# Capture Studio frontend

Svelte 5 and Vite build a static dashboard served by the existing Python HTTP
server. SvelteKit is unnecessary for this single local workspace: acquisition,
authentication, commands, and capture ownership stay in Python. Installed wheels
contain all browser assets and need neither Node.js nor a network connection.

## Component choices

Skeleton 5 supplies App Bar, Tabs, Dialog, Accordion, Popover, Segmented Control,
Switch, Progress, and Toast components. Buttons, cards, inputs, selects, tables,
and badges use Skeleton's Tailwind components and theme presets. Custom CSS is
limited to page composition, plot integration, and accessibility. Use Skeleton
components for new interactions before introducing bespoke behavior.

uPlot supplies the signal charts, which Skeleton does not provide. Null values
remain gaps; clock discontinuities switch to an explicitly labelled sample-index
axis. The browser keeps a bounded ten-second preview without changing capture
recording. Changing views never stops acquisition.

`src/lib/types.ts` describes the Python API. The per-app `Dashboard` instance in
`src/lib/dashboard.svelte.ts` owns commands, polling, cursors, and traces. Polling
is serialized; responses from before a command are discarded. Commands are not
retried automatically. Components own presentation and short-lived form state.

Eight bundled Skeleton themes are selected in Appearance, separately from the
light/dark/system color mode. Preferences use browser storage; no assets are
loaded from a CDN. Fonts come from the selected theme's system font stack.

## Build and check

From this directory:

```powershell
npm ci
npm run check
npm run format:check
npm run build
npx playwright install chromium
npm test
```

The build writes `index.html`, `app.js`, `app.css`, and `favicon.svg` into
`../sifi_streamer/web/assets`. Include regenerated files with source changes;
never hand-edit them. Fixed asset names match the Python server's explicit
allowlist. Additional runtime assets require a corresponding server route.
Terser is used to escape whitespace literals in generated JavaScript so Git's
whitespace checks remain meaningful.

Dependencies use the latest compatible releases. TypeScript stays on major 6
because the current svelte-check peer range excludes TypeScript 7. Upgrade it
when that checker supports the new major; do not force incompatible peers.

Browser tests cover scalar metadata, kind editing, generated annotation IDs,
nested segment closure, health-rule updates, appearance, mobile overflow,
failed commands, and stale polling responses. An integration test launches the
Python CLI with a temporary synthetic capture, drives the bundled frontend,
and reads the recording back to verify saved annotations and lifecycle records.

The integration test requires `uv` on PATH. If needed:

```powershell
$env:UV_EXE = 'C:\path\to\uv.exe'
npm test
```

To use an already installed Chrome instead of downloading Chromium:

```powershell
$env:PLAYWRIGHT_CHANNEL = 'chrome'
npm test
```

Test screenshots and traces are under `test-results` and are ignored by Git.

## Live development

First start the Python backend from the repository root with a fresh output path:

```powershell
uv run sifi-capture-web dev.capture.jsonl.zst --synthetic --no-open --web-port 8080
```

In another terminal, from `frontend`:

```powershell
$env:SIFI_WEB_ORIGIN = 'http://127.0.0.1:8080'
npm run dev
```

Open the Vite URL with the **same hash token** printed by the Python launcher:
`http://127.0.0.1:5173/#TOKEN`. The development proxy forwards `/api` requests
to that loopback origin with the matching Origin header; the session token is
still required. Production assets make same-origin requests directly to Python.
Start/stop in the browser uses real capture controls even in development.
