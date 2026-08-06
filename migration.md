# Consumer migration

No changes to either consumer repository were made in this extraction.

Add the shared dependency to both repositories:

```powershell
uv add "sifi-streamer @ git+ssh://git@github.com/BLINCdev/sifi-streamer.git@v0.1.0"
```

Equivalent metadata:

```toml
[project]
dependencies = ["sifi-streamer"]

[tool.uv.sources]
sifi-streamer = { git = "ssh://git@github.com/BLINCdev/sifi-streamer.git", tag = "v0.1.0" }
```

## cognitive-load-validation

1. Add the dependency, then remove its local `sifi_streamer/` directory.
2. Replace `cognitive_platform.recording` imports with package-root imports or
   `sifi_streamer.controller`.
3. Replace `cognitive_platform.sifi` imports with
   `sifi_streamer.sifi_backend`.
4. Keep participant paths, manifests, task suites, task-specific runners, and
   Parquet policy in the cognitive repository. Its CLI becomes a thin wrapper
   around `create_sifi_capture` and the shared runner functions.
5. Replace trial/presentation controller helpers with consumer functions that
   call generic segments and markers. Segment boundaries no longer generate
   duplicate markers.
6. Run the consumer's full tests and conversion checks.

## sifi-data-acquisition

1. Add the dependency, then remove its local `sifi_streamer/` directory.
2. Existing package-root device, bridge, capture, `BackgroundHandle`, and
   `SharedMemoryReader` imports remain available.
3. Fix any positional marker calls relying on the old bug: arguments are
   consistently `marker_id, marker_kind`.
4. Remove runtime dependencies no longer used by that consumer, then run its
   inference and bridge integration tests.

Pin both consumers to the same immutable tag or commit, regenerate `uv.lock`,
and verify a short synthetic capture before hardware testing.
