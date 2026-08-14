"""Complete, reproducible SiFi sensor configuration profiles."""

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

ECG_SAMPLE_RATES = frozenset((250, 500, 1000, 2000))
EMG_SAMPLE_RATES = frozenset((500, 1000, 1600, 2000))
EDA_SAMPLE_RATES = frozenset((4, 8, 16, 32, 50))
PPG_SPS_VALUES = frozenset((50, 100, 200, 400, 800))
PPG_AVERAGING_FACTORS = frozenset((1, 2, 4, 8, 16, 32))
IMU_SAMPLE_RATES = frozenset((25, 50, 100, 200))
ACCELEROMETER_RANGES = frozenset((2, 4, 8, 16))
GYROSCOPE_RANGES = frozenset((16, 31, 63, 125, 250, 500, 1000, 2000))
TEMPERATURE_SAMPLE_RATES = frozenset((0.1, 1.0, 2.0, 10.0))
SENSOR_PROFILE_VERSION = 1


class MainsNotch(StrEnum):
    """Supported mains-frequency notch settings."""

    OFF = "off"
    HZ_50 = "50"
    HZ_60 = "60"


class PpgSensitivity(StrEnum):
    """Supported PPG photodiode sensitivity settings."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


def _require_bool(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")


def _require_choice(name: str, value: object, choices: frozenset[int | float]) -> None:
    if isinstance(value, bool) or value not in choices:
        rendered = ", ".join(map(str, sorted(choices)))
        raise ValueError(f"{name} must be one of: {rendered}")


def _require_int_choice(name: str, value: object, choices: frozenset[int]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    _require_choice(name, value, choices)


def _require_finite(name: str, value: object, *, minimum: float = 0.0) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum:g}")


@dataclass(frozen=True, slots=True)
class FilterConfiguration:
    """Complete onboard filter settings for one biopotential sensor."""

    dc_notch: bool
    mains_notch: MainsNotch
    bandpass: bool
    low_cutoff_hz: float
    high_cutoff_hz: float

    def __post_init__(self) -> None:
        _require_bool("dc_notch", self.dc_notch)
        if not isinstance(self.mains_notch, MainsNotch):
            raise TypeError("mains_notch must be a MainsNotch")
        _require_bool("bandpass", self.bandpass)
        _require_finite("low_cutoff_hz", self.low_cutoff_hz)
        _require_finite("high_cutoff_hz", self.high_cutoff_hz)
        if self.low_cutoff_hz >= self.high_cutoff_hz:
            raise ValueError("low_cutoff_hz must be lower than high_cutoff_hz")


@dataclass(frozen=True, slots=True)
class EcgConfiguration:
    enabled: bool = True
    sample_rate_hz: int = 500
    filters: FilterConfiguration = FilterConfiguration(
        True, MainsNotch.HZ_60, True, 0, 30
    )

    def __post_init__(self) -> None:
        _require_bool("ecg.enabled", self.enabled)
        _require_int_choice("ecg.sample_rate_hz", self.sample_rate_hz, ECG_SAMPLE_RATES)


@dataclass(frozen=True, slots=True)
class EmgConfiguration:
    enabled: bool = True
    sample_rate_hz: int = 1600
    filters: FilterConfiguration = FilterConfiguration(
        True, MainsNotch.HZ_60, True, 20, 450
    )

    def __post_init__(self) -> None:
        _require_bool("emg.enabled", self.enabled)
        _require_int_choice("emg.sample_rate_hz", self.sample_rate_hz, EMG_SAMPLE_RATES)


@dataclass(frozen=True, slots=True)
class EdaConfiguration:
    enabled: bool = True
    sample_rate_hz: int = 50
    filters: FilterConfiguration = FilterConfiguration(
        True, MainsNotch.HZ_60, True, 0, 5
    )
    frequency_hz: float = 0

    def __post_init__(self) -> None:
        _require_bool("eda.enabled", self.enabled)
        _require_int_choice("eda.sample_rate_hz", self.sample_rate_hz, EDA_SAMPLE_RATES)
        _require_finite("eda.frequency_hz", self.frequency_hz)


@dataclass(frozen=True, slots=True)
class PpgConfiguration:
    enabled: bool = True
    samples_per_second: int = 200
    led_ir_ma: int = 9
    led_red_ma: int = 9
    led_green_ma: int = 9
    led_blue_ma: int = 9
    sensitivity: PpgSensitivity = PpgSensitivity.MEDIUM
    averaging: int = 4

    def __post_init__(self) -> None:
        _require_bool("ppg.enabled", self.enabled)
        _require_int_choice(
            "ppg.samples_per_second", self.samples_per_second, PPG_SPS_VALUES
        )
        for name, value in (
            ("led_ir_ma", self.led_ir_ma),
            ("led_red_ma", self.led_red_ma),
            ("led_green_ma", self.led_green_ma),
            ("led_blue_ma", self.led_blue_ma),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 50
            ):
                raise ValueError(f"ppg.{name} must be an integer from 1 through 50")
        if not isinstance(self.sensitivity, PpgSensitivity):
            raise TypeError("ppg.sensitivity must be a PpgSensitivity")
        _require_int_choice("ppg.averaging", self.averaging, PPG_AVERAGING_FACTORS)

    @property
    def effective_sample_rate_hz(self) -> float:
        """Return the output rate after hardware averaging."""
        return self.samples_per_second / self.averaging


@dataclass(frozen=True, slots=True)
class ImuConfiguration:
    enabled: bool = True
    sample_rate_hz: int = 100
    accelerometer_range_g: int = 2
    gyroscope_range_dps: int = 16

    def __post_init__(self) -> None:
        _require_bool("imu.enabled", self.enabled)
        _require_int_choice("imu.sample_rate_hz", self.sample_rate_hz, IMU_SAMPLE_RATES)
        _require_int_choice(
            "imu.accelerometer_range_g",
            self.accelerometer_range_g,
            ACCELEROMETER_RANGES,
        )
        _require_int_choice(
            "imu.gyroscope_range_dps", self.gyroscope_range_dps, GYROSCOPE_RANGES
        )


@dataclass(frozen=True, slots=True)
class TemperatureConfiguration:
    sample_rate_hz: float = 1

    def __post_init__(self) -> None:
        _require_choice(
            "temperature.sample_rate_hz",
            self.sample_rate_hz,
            TEMPERATURE_SAMPLE_RATES,
        )


@dataclass(frozen=True, slots=True)
class SiFiSensorProfile:
    """A complete desired SiFi sensor state with no inherited settings."""

    ecg: EcgConfiguration = EcgConfiguration()
    emg: EmgConfiguration = EmgConfiguration()
    eda: EdaConfiguration = EdaConfiguration()
    imu: ImuConfiguration = ImuConfiguration()
    ppg: PpgConfiguration = PpgConfiguration()
    temperature: TemperatureConfiguration = TemperatureConfiguration()

    def __post_init__(self) -> None:
        for name, value, expected in (
            ("ecg", self.ecg, EcgConfiguration),
            ("emg", self.emg, EmgConfiguration),
            ("eda", self.eda, EdaConfiguration),
            ("imu", self.imu, ImuConfiguration),
            ("ppg", self.ppg, PpgConfiguration),
            ("temperature", self.temperature, TemperatureConfiguration),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} must be a {expected.__name__}")


ALL_SENSORS_PROFILE = SiFiSensorProfile()
EMG_ONLY_PROFILE = replace(
    ALL_SENSORS_PROFILE,
    ecg=replace(ALL_SENSORS_PROFILE.ecg, enabled=False),
    eda=replace(ALL_SENSORS_PROFILE.eda, enabled=False),
    imu=replace(ALL_SENSORS_PROFILE.imu, enabled=False),
    ppg=replace(ALL_SENSORS_PROFILE.ppg, enabled=False),
)
EMG_IMU_PROFILE = replace(
    EMG_ONLY_PROFILE, imu=replace(EMG_ONLY_PROFILE.imu, enabled=True)
)
SENSOR_PRESETS: Mapping[str, SiFiSensorProfile] = MappingProxyType(
    {
        "all": ALL_SENSORS_PROFILE,
        "emg-only": EMG_ONLY_PROFILE,
        "emg-imu": EMG_IMU_PROFILE,
    }
)


def _toggle(value: bool) -> str:
    return "on" if value else "off"


def _number(value: int | float) -> str:
    return f"{value:g}"


def _filtered_command(name: str, config: object) -> str:
    if not isinstance(config, EcgConfiguration | EmgConfiguration | EdaConfiguration):
        raise TypeError("filtered sensor configuration is invalid")
    filters = config.filters
    command = (
        f"configure {name} --fs {config.sample_rate_hz}"
        f" --dc-notch {_toggle(filters.dc_notch)}"
        f" --mains-notch {filters.mains_notch.value}"
        f" --bandpass {_toggle(filters.bandpass)}"
        f" --bandpass-low {_number(filters.low_cutoff_hz)}"
        f" --bandpass-high {_number(filters.high_cutoff_hz)}"
    )
    if isinstance(config, EdaConfiguration):
        command += f" --freq {_number(config.frequency_hz)}"
    return command


def bridge_configuration_commands(profile: SiFiSensorProfile) -> tuple[str, ...]:
    """Render every supported bridge setting, with sensor states applied last."""
    ppg, imu = profile.ppg, profile.imu
    return (
        _filtered_command("ecg", profile.ecg),
        _filtered_command("emg", profile.emg),
        _filtered_command("eda", profile.eda),
        (
            f"configure ppg --sps {ppg.samples_per_second}"
            f" --led-ir {ppg.led_ir_ma} --led-red {ppg.led_red_ma}"
            f" --led-green {ppg.led_green_ma} --led-blue {ppg.led_blue_ma}"
            f" --sens {ppg.sensitivity.value} --avg {ppg.averaging}"
        ),
        (
            f"configure imu --fs {imu.sample_rate_hz}"
            f" --acc-range {imu.accelerometer_range_g}"
            f" --gyro-range {imu.gyroscope_range_dps}"
        ),
        f"configure temperature --fs {_number(profile.temperature.sample_rate_hz)}",
        (
            f"configure sensors --ecg {_toggle(profile.ecg.enabled)}"
            f" --emg {_toggle(profile.emg.enabled)}"
            f" --eda {_toggle(profile.eda.enabled)}"
            f" --imu {_toggle(profile.imu.enabled)}"
            f" --ppg {_toggle(profile.ppg.enabled)}"
        ),
    )


def sensor_profile_to_dict(profile: SiFiSensorProfile) -> dict[str, object]:
    """Return the stable versioned JSON representation of a profile."""
    value = asdict(profile)
    value["version"] = SENSOR_PROFILE_VERSION
    for name in ("ecg", "emg", "eda"):
        sensor = value[name]
        assert isinstance(sensor, dict)
        filters = sensor.pop("filters")
        assert isinstance(filters, dict)
        sensor.update(filters)
        sensor["mains_notch"] = str(sensor["mains_notch"])
    ppg = value["ppg"]
    assert isinstance(ppg, dict)
    ppg["sensitivity"] = str(ppg["sensitivity"])
    return {"version": value.pop("version"), **value}


def _object(value: object, name: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    actual = set(value)
    if actual != keys:
        missing, unknown = sorted(keys - actual), sorted(actual - keys)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ValueError(f"{name} has {'; '.join(details)} fields")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _floating(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


_FILTER_KEYS = {
    "enabled",
    "sample_rate_hz",
    "dc_notch",
    "mains_notch",
    "bandpass",
    "low_cutoff_hz",
    "high_cutoff_hz",
}


def _filters(value: Mapping[str, object], name: str) -> FilterConfiguration:
    try:
        mains = MainsNotch(value["mains_notch"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}.mains_notch must be off, 50, or 60") from exc
    return FilterConfiguration(
        _boolean(value["dc_notch"], f"{name}.dc_notch"),
        mains,
        _boolean(value["bandpass"], f"{name}.bandpass"),
        _floating(value["low_cutoff_hz"], f"{name}.low_cutoff_hz"),
        _floating(value["high_cutoff_hz"], f"{name}.high_cutoff_hz"),
    )


def sensor_profile_from_dict(document: object) -> SiFiSensorProfile:
    """Decode a strict, complete version-1 profile mapping."""
    root = _object(
        document,
        "profile",
        {"version", "ecg", "emg", "eda", "imu", "ppg", "temperature"},
    )
    if root["version"] != SENSOR_PROFILE_VERSION:
        raise ValueError(f"unsupported sensor profile version: {root['version']!r}")
    ecg = _object(root["ecg"], "ecg", _FILTER_KEYS)
    emg = _object(root["emg"], "emg", _FILTER_KEYS)
    eda = _object(root["eda"], "eda", _FILTER_KEYS | {"frequency_hz"})
    imu = _object(
        root["imu"],
        "imu",
        {"enabled", "sample_rate_hz", "accelerometer_range_g", "gyroscope_range_dps"},
    )
    ppg = _object(
        root["ppg"],
        "ppg",
        {
            "enabled",
            "samples_per_second",
            "led_ir_ma",
            "led_red_ma",
            "led_green_ma",
            "led_blue_ma",
            "sensitivity",
            "averaging",
        },
    )
    temperature = _object(root["temperature"], "temperature", {"sample_rate_hz"})
    try:
        sensitivity = PpgSensitivity(ppg["sensitivity"])
    except (TypeError, ValueError) as exc:
        raise ValueError("ppg.sensitivity must be low, medium, high, or max") from exc
    return SiFiSensorProfile(
        EcgConfiguration(
            _boolean(ecg["enabled"], "ecg.enabled"),
            _integer(ecg["sample_rate_hz"], "ecg.sample_rate_hz"),
            _filters(ecg, "ecg"),
        ),
        EmgConfiguration(
            _boolean(emg["enabled"], "emg.enabled"),
            _integer(emg["sample_rate_hz"], "emg.sample_rate_hz"),
            _filters(emg, "emg"),
        ),
        EdaConfiguration(
            _boolean(eda["enabled"], "eda.enabled"),
            _integer(eda["sample_rate_hz"], "eda.sample_rate_hz"),
            _filters(eda, "eda"),
            _floating(eda["frequency_hz"], "eda.frequency_hz"),
        ),
        ImuConfiguration(
            _boolean(imu["enabled"], "imu.enabled"),
            _integer(imu["sample_rate_hz"], "imu.sample_rate_hz"),
            _integer(imu["accelerometer_range_g"], "imu.accelerometer_range_g"),
            _integer(imu["gyroscope_range_dps"], "imu.gyroscope_range_dps"),
        ),
        PpgConfiguration(
            _boolean(ppg["enabled"], "ppg.enabled"),
            _integer(ppg["samples_per_second"], "ppg.samples_per_second"),
            _integer(ppg["led_ir_ma"], "ppg.led_ir_ma"),
            _integer(ppg["led_red_ma"], "ppg.led_red_ma"),
            _integer(ppg["led_green_ma"], "ppg.led_green_ma"),
            _integer(ppg["led_blue_ma"], "ppg.led_blue_ma"),
            sensitivity,
            _integer(ppg["averaging"], "ppg.averaging"),
        ),
        TemperatureConfiguration(
            _floating(temperature["sample_rate_hz"], "temperature.sample_rate_hz")
        ),
    )


def load_sensor_profile(path: str | Path) -> SiFiSensorProfile:
    """Load and validate a complete JSON sensor profile."""

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    document = json.loads(
        Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant
    )
    return sensor_profile_from_dict(document)


def write_sensor_profile(
    path: str | Path, profile: SiFiSensorProfile, *, overwrite: bool = False
) -> Path:
    """Write a complete JSON profile, refusing replacement by default."""
    destination = Path(path)
    with destination.open(
        "w" if overwrite else "x", encoding="utf-8", newline="\n"
    ) as file:
        json.dump(sensor_profile_to_dict(profile), file, indent=2, allow_nan=False)
        file.write("\n")
    return destination
