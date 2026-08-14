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

Consumers with non-SiFi acquisition devices may inject an `AcquisitionDevice`
with fixed `SignalStreamSpec` declarations. Prefer dynamic `streams` and
`stream_readers`; fixed `Modalities` remains a SiFi compatibility view. Web
consumer launchers pass a runtime factory to `serve_capture_web` rather than
moving lifecycle ownership into browser code.

## Sensor configuration migration

Replace `emg_sample_rate=...` with a complete `sensor_profile=...`, normally a
built-in `ALL_SENSORS_PROFILE`, `EMG_ONLY_PROFILE`, or `EMG_IMU_PROFILE`, or a
profile loaded with `load_sensor_profile()`. The default hardware path uses the
all-sensors profile. Replace `--emg-sample-rate` with `--emg-fs`, optionally
combined with `--sensor-preset` or `--sensor-profile`.

Profiles configure every supported option on every startup. PPG uses raw SPS
and averaging rather than `fs`; the SiFiBand default `200 / 4` produces a 50 Hz
effective stream rate. Profile options are rejected for synthetic capture.
