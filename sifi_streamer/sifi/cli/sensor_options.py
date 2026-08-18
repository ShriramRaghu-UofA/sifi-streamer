"""Shared argparse translation for complete SiFi sensor profiles."""

import argparse
from dataclasses import replace
from pathlib import Path

from sifi_streamer.capture.records import Scalar
from sifi_streamer.sifi.sensor_profile import (
    ECG_SAMPLE_RATES,
    EDA_SAMPLE_RATES,
    EMG_SAMPLE_RATES,
    IMU_SAMPLE_RATES,
    PPG_AVERAGING_FACTORS,
    PPG_SPS_VALUES,
    SENSOR_PRESETS,
    TEMPERATURE_SAMPLE_RATES,
    PpgConfiguration,
    SiFiSensorProfile,
    load_sensor_profile,
)

_OPTION_DESTINATIONS = (
    "sensor_profile",
    "sensor_preset",
    "ecg_state",
    "emg_state",
    "eda_state",
    "imu_state",
    "ppg_state",
    "ecg_sample_rate",
    "emg_sample_rate",
    "eda_sample_rate",
    "imu_sample_rate",
    "ppg_sps",
    "ppg_avg",
    "temperature_sample_rate",
)


def add_sensor_arguments(parser: argparse.ArgumentParser) -> None:
    """Add common profile selection and frequent overrides to ``parser``."""
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--sensor-profile", type=Path, metavar="PATH")
    source.add_argument("--sensor-preset", choices=tuple(SENSOR_PRESETS))
    for name in ("ecg", "emg", "eda", "imu", "ppg"):
        parser.add_argument(
            f"--{name}",
            dest=f"{name}_state",
            choices=("off", "on"),
            help=f"Override {name.upper()} enabled state",
        )
    parser.add_argument(
        "--ecg-fs", dest="ecg_sample_rate", type=int, choices=sorted(ECG_SAMPLE_RATES)
    )
    parser.add_argument(
        "--emg-fs", dest="emg_sample_rate", type=int, choices=sorted(EMG_SAMPLE_RATES)
    )
    parser.add_argument(
        "--eda-fs", dest="eda_sample_rate", type=int, choices=sorted(EDA_SAMPLE_RATES)
    )
    parser.add_argument(
        "--imu-fs", dest="imu_sample_rate", type=int, choices=sorted(IMU_SAMPLE_RATES)
    )
    parser.add_argument("--ppg-sps", type=int, choices=sorted(PPG_SPS_VALUES))
    parser.add_argument("--ppg-avg", type=int, choices=sorted(PPG_AVERAGING_FACTORS))
    parser.add_argument(
        "--temperature-fs",
        dest="temperature_sample_rate",
        type=float,
        choices=sorted(TEMPERATURE_SAMPLE_RATES),
    )


def sensor_options_used(args: argparse.Namespace) -> bool:
    """Return whether the invocation explicitly selected any sensor option."""
    return any(getattr(args, name) is not None for name in _OPTION_DESTINATIONS)


def resolve_sensor_profile(args: argparse.Namespace) -> SiFiSensorProfile:
    """Load a base profile and apply direct command-line overrides."""
    if args.sensor_profile is not None:
        profile = load_sensor_profile(args.sensor_profile)
    else:
        profile = SENSOR_PRESETS[args.sensor_preset or "all"]
    changes: dict[str, object] = {}
    for name in ("ecg", "emg", "eda", "imu", "ppg"):
        config = getattr(profile, name)
        state = getattr(args, f"{name}_state")
        rate = getattr(args, f"{name}_sample_rate", None)
        updates: dict[str, object] = {}
        if state is not None:
            updates["enabled"] = state == "on"
        if rate is not None:
            updates["sample_rate_hz"] = rate
        if updates:
            changes[name] = replace(config, **updates)
    ppg_updates: dict[str, object] = {}
    if args.ppg_sps is not None:
        ppg_updates["samples_per_second"] = args.ppg_sps
    if args.ppg_avg is not None:
        ppg_updates["averaging"] = args.ppg_avg
    if ppg_updates:
        changed_ppg = changes.get("ppg", profile.ppg)
        assert isinstance(changed_ppg, PpgConfiguration)
        changes["ppg"] = replace(changed_ppg, **ppg_updates)
    if args.temperature_sample_rate is not None:
        changes["temperature"] = replace(
            profile.temperature, sample_rate_hz=args.temperature_sample_rate
        )
    return replace(profile, **changes)


def sensor_profile_summary(profile: SiFiSensorProfile) -> dict[str, Scalar]:
    """Return every resolved profile field for the local web dashboard."""
    return {
        "ecg_enabled": profile.ecg.enabled,
        "ecg_fs_hz": profile.ecg.sample_rate_hz,
        "ecg_dc_notch": profile.ecg.filters.dc_notch,
        "ecg_mains_notch_hz": profile.ecg.filters.mains_notch.value,
        "ecg_bandpass": profile.ecg.filters.bandpass,
        "ecg_bandpass_low_hz": profile.ecg.filters.low_cutoff_hz,
        "ecg_bandpass_high_hz": profile.ecg.filters.high_cutoff_hz,
        "emg_enabled": profile.emg.enabled,
        "emg_fs_hz": profile.emg.sample_rate_hz,
        "emg_dc_notch": profile.emg.filters.dc_notch,
        "emg_mains_notch_hz": profile.emg.filters.mains_notch.value,
        "emg_bandpass": profile.emg.filters.bandpass,
        "emg_bandpass_low_hz": profile.emg.filters.low_cutoff_hz,
        "emg_bandpass_high_hz": profile.emg.filters.high_cutoff_hz,
        "eda_enabled": profile.eda.enabled,
        "eda_fs_hz": profile.eda.sample_rate_hz,
        "eda_dc_notch": profile.eda.filters.dc_notch,
        "eda_mains_notch_hz": profile.eda.filters.mains_notch.value,
        "eda_bandpass": profile.eda.filters.bandpass,
        "eda_bandpass_low_hz": profile.eda.filters.low_cutoff_hz,
        "eda_bandpass_high_hz": profile.eda.filters.high_cutoff_hz,
        "eda_frequency_hz": profile.eda.frequency_hz,
        "ppg_enabled": profile.ppg.enabled,
        "ppg_sps": profile.ppg.samples_per_second,
        "ppg_avg": profile.ppg.averaging,
        "ppg_effective_rate_hz": profile.ppg.effective_sample_rate_hz,
        "ppg_led_ir_ma": profile.ppg.led_ir_ma,
        "ppg_led_red_ma": profile.ppg.led_red_ma,
        "ppg_led_green_ma": profile.ppg.led_green_ma,
        "ppg_led_blue_ma": profile.ppg.led_blue_ma,
        "ppg_sensitivity": profile.ppg.sensitivity.value,
        "imu_enabled": profile.imu.enabled,
        "imu_fs_hz": profile.imu.sample_rate_hz,
        "imu_accelerometer_range_g": profile.imu.accelerometer_range_g,
        "imu_gyroscope_range_dps": profile.imu.gyroscope_range_dps,
        "temperature_fs_hz": profile.temperature.sample_rate_hz,
    }
