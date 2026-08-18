"""Managed lifecycle for the vendor ``sifibridge`` executable."""

import contextlib
import json
import logging
import math
import os
import platform
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from enum import StrEnum
from pathlib import Path

from sifi_streamer.acquisition.devices import SignalStreamSpec
from sifi_streamer.exceptions import DeviceError
from sifi_streamer.sifi.devices import (
    Modalities,
    Modality,
    ModalitySpec,
    PacketReader,
    SiFiBandDevice,
    SiFiPacket,
    modalities_from_device_info,
    packet_from_json_line,
    streams_from_modalities,
)
from sifi_streamer.sifi.sensor_profile import (
    ALL_SENSORS_PROFILE,
    SiFiSensorProfile,
    bridge_configuration_commands,
)

logger = logging.getLogger(__name__)


def bridge_executable_name(system: str | None = None) -> str:
    """Return the vendor bridge executable name for an operating system."""
    operating_system = (system or platform.system()).lower()
    return "sifibridge.exe" if operating_system == "windows" else "sifibridge"


DEFAULT_BRIDGE_EXECUTABLE = Path("bin") / bridge_executable_name()


class BridgeTransport(StrEnum):
    """Supported bridge packet-output transports.

    ``TCP`` connects to a bridge-hosted stream, ``UDP`` binds a local datagram
    receiver, and ``STDOUT`` parses packet documents from the managed process.
    """

    TCP = "tcp"
    UDP = "udp"
    STDOUT = "stdout"


class _UdpPacketReader:
    def __init__(self, host: str, port: int) -> None:
        self._host, self._port, self._sock, self._pending = host, port, None, deque()

    def connect(self) -> None:
        """Bind the configured UDP endpoint."""
        if self._sock is not None:
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind((self._host, self._port))
        except OSError as exc:
            self.disconnect()
            raise DeviceError(
                f"UDP packet reader: cannot bind to {self._host}:{self._port}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the socket and discard queued packets."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock = None
        self._pending.clear()

    def read_packet(self) -> SiFiPacket:
        """Return the next valid packet from one or more datagrams."""
        if self._sock is None:
            raise DeviceError("UDP packet reader.read_packet() called before connect()")
        while True:
            if self._pending:
                return self._pending.popleft()
            try:
                datagram, _ = self._sock.recvfrom(65535)
            except OSError as exc:
                raise DeviceError(f"UDP packet reader: receive failed: {exc}") from exc
            for line in datagram.splitlines():
                if packet := packet_from_json_line(line.strip()):
                    self._pending.append(packet)


class _StdoutPacketReader:
    def __init__(self, packets: queue.Queue[SiFiPacket | None]) -> None:
        self._packets, self._connected = packets, False

    def connect(self) -> None:
        """Enable reads from the bridge stdout queue."""
        self._connected = True

    def disconnect(self) -> None:
        """Disable reads and wake a blocked reader."""
        self._connected = False
        self._packets.put(None)

    def read_packet(self) -> SiFiPacket:
        """Block for the next parsed stdout packet."""
        if not self._connected:
            raise DeviceError(
                "stdout packet reader.read_packet() called before connect()"
            )
        if (packet := self._packets.get()) is None:
            raise DeviceError("stdout packet reader closed")
        return packet


class SiFiBridgeDevice:
    """Launch, configure, stream from, and orderly stop one bridge process.

    The executable must already be installed; device startup never downloads or
    updates it. The process is placed in a separate Windows process group or
    POSIX session so the foreground launcher retains Ctrl+C ownership.

    Args:
        host: TCP destination host or local UDP bind interface.
        port: TCP or UDP packet port.
        executable: Explicit path to the vendor bridge executable.
        startup_timeout_s: Maximum wait for bridge info and TCP readiness.
        transport: Packet transport name or :class:`BridgeTransport` member.
        sensor_profile: Complete sensor state sent before every acquisition.

    Raises:
        ValueError: If ``transport`` is unsupported.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        executable: str | Path = DEFAULT_BRIDGE_EXECUTABLE,
        startup_timeout_s: float = 20.0,
        transport: BridgeTransport | str = BridgeTransport.TCP,
        sensor_profile: SiFiSensorProfile = ALL_SENSORS_PROFILE,
    ) -> None:
        (
            self._host,
            self._port,
            self._transport,
            self._executable,
            self._startup_timeout_s,
            self._sensor_profile,
        ) = (
            host,
            port,
            BridgeTransport(transport),
            Path(executable),
            startup_timeout_s,
            sensor_profile,
        )
        self._process: subprocess.Popen[str] | None = None
        self._control: queue.Queue[dict[str, object]] = queue.Queue()
        self._stdout_packets: queue.Queue[SiFiPacket | None] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._reader: PacketReader | None = None
        self._modalities: Modalities[ModalitySpec] | None = None
        self._device_info: dict[str, object] | None = None

    @property
    def modalities(self) -> Modalities[ModalitySpec]:
        """Return modality layouts reported by the connected bridge.

        Raises:
            DeviceError: If accessed before :meth:`connect` completes.
        """
        if self._modalities is None:
            raise DeviceError("SiFiBridgeDevice.modalities accessed before connect()")
        return self._modalities

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        """Return the connected bridge's generic signal stream registry."""
        return streams_from_modalities(self.modalities)

    @property
    def device_info(self) -> dict[str, object] | None:
        """Return the complete bridge info document, if connection supplied one."""
        return self._device_info

    def connect(self) -> None:
        """Launch and configure the bridge, then connect its packet reader.

        Repeated calls after success are no-ops. Partial startup is cleaned up
        before an error is propagated.

        Raises:
            DeviceError: If the executable is absent, the process or transport
                fails, bridge metadata is invalid, or startup times out.
        """
        if self._process is not None:
            return
        if not self._executable.exists():
            raise DeviceError(
                f"sifibridge executable not found: {self._executable}; "
                "download it explicitly with "
                "'uv run sifi-download-bridge --tested --output-directory bin' "
                "or visit https://github.com/SiFiLabs/sifi-bridge-pub/"
            )
        self._control, self._stdout_packets, self._reader = (
            queue.Queue(),
            queue.Queue(),
            self._make_reader(),
        )
        if self._transport is BridgeTransport.UDP:
            self._reader.connect()
        try:
            logger.info(
                "Connecting SiFi bridge via %s at %s:%d",
                self._transport,
                self._host,
                self._port,
            )
            self._launch()
            self._send("connect")
            for command in bridge_configuration_commands(self._sensor_profile):
                self._send(command)
            self._send("info")
            self._device_info = self._wait_for_info()
            self._modalities = modalities_from_device_info(self._device_info)
            self._validate_configured_modalities()
            self._send("start")
            if self._transport is BridgeTransport.TCP:
                self._connect_tcp_when_ready()
            elif self._transport is BridgeTransport.STDOUT:
                self._reader.connect()
            logger.info("SiFi bridge connected")
        except DeviceError, OSError, ValueError:
            logger.exception("SiFi bridge connection failed")
            self.disconnect()
            raise

    def _validate_configured_modalities(self) -> None:
        """Require bridge-reported enabled states and rates to match the profile."""
        expected = (
            (
                Modality.ECG,
                self._sensor_profile.ecg.enabled,
                self._sensor_profile.ecg.sample_rate_hz,
            ),
            (
                Modality.EMG,
                self._sensor_profile.emg.enabled,
                self._sensor_profile.emg.sample_rate_hz,
            ),
            (
                Modality.EDA,
                self._sensor_profile.eda.enabled,
                self._sensor_profile.eda.sample_rate_hz,
            ),
            (
                Modality.IMU,
                self._sensor_profile.imu.enabled,
                self._sensor_profile.imu.sample_rate_hz,
            ),
            (
                Modality.PPG,
                self._sensor_profile.ppg.enabled,
                self._sensor_profile.ppg.effective_sample_rate_hz,
            ),
        )
        for modality, enabled, rate in expected:
            actual = self.modalities.get(modality)
            if (actual is not None) != enabled:
                state = "enabled" if enabled else "disabled"
                raise DeviceError(
                    f"Bridge reported {modality.value} in the wrong state; "
                    f"expected {state}"
                )
            if actual is not None and not math.isclose(actual.sample_rate, rate):
                raise DeviceError(
                    f"Bridge reported {modality.value} at {actual.sample_rate} Hz; "
                    f"expected {rate:g} Hz"
                )
        temperature = self.modalities.temperature
        if temperature is not None and not math.isclose(
            temperature.sample_rate, self._sensor_profile.temperature.sample_rate_hz
        ):
            raise DeviceError(
                f"Bridge reported temperature at {temperature.sample_rate} Hz; "
                "expected "
                f"{self._sensor_profile.temperature.sample_rate_hz:g} Hz"
            )

    def disconnect(self) -> None:
        """Orderly stop the reader and bridge, escalating to termination if needed.

        Standard input, output, and error streams are closed during teardown.
        Calling this method when disconnected is safe.
        """
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.disconnect()
        process, self._process = self._process, None
        if process is None:
            return
        logger.info("Stopping SiFi bridge process")
        if process.stdin is not None:
            try:
                process.stdin.write("stop\nexit\n")
                process.stdin.flush()
                process.stdin.close()
            except OSError, ValueError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning("SiFi bridge did not terminate; killing it")
                process.kill()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
        logger.info("SiFi bridge disconnected")

    def read_packet(self) -> SiFiPacket:
        """Return the next packet from the selected transport.

        Raises:
            DeviceError: If called before connection or packet I/O fails.
        """
        if self._reader is None:
            raise DeviceError("SiFiBridgeDevice.read_packet() called before connect()")
        return self._reader.read_packet()

    def _make_reader(self) -> PacketReader:
        match self._transport:
            case BridgeTransport.TCP:
                return SiFiBandDevice(self._host, self._port)
            case BridgeTransport.UDP:
                return _UdpPacketReader(self._host, self._port)
            case BridgeTransport.STDOUT:
                return _StdoutPacketReader(self._stdout_packets)

    def _launch(self) -> None:
        arguments = [str(self._executable)]
        if self._transport is BridgeTransport.TCP:
            arguments += ["--tcp-out", f"{self._host}:{self._port}", "--no-stdout-data"]
        elif self._transport is BridgeTransport.UDP:
            arguments += ["--udp-out", f"{self._host}:{self._port}", "--no-stdout-data"]
        try:
            windows = os.name == "nt"
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
                start_new_session=not windows,
            )
            logger.info(
                "Launched SiFi bridge process (pid=%s)",
                getattr(self._process, "pid", "unknown"),
            )
        except OSError as exc:
            raise DeviceError(f"Unable to launch sifibridge: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(document, dict):
                    continue
                if isinstance(document.get("info"), dict):
                    self._control.put(document)
                elif (
                    self._transport is BridgeTransport.STDOUT
                    and "packet_type" in document
                ) and (packet := packet_from_json_line(line)):
                    self._stdout_packets.put(packet)
        finally:
            if self._transport is BridgeTransport.STDOUT:
                self._stdout_packets.put(None)

    def _read_stderr(self) -> None:
        process = self._process
        if process is not None and process.stderr is not None:
            for line in process.stderr:
                message = line.rstrip()
                self._stderr_lines.append(message)
                logger.warning("sifibridge: %s", message)

    def _send(self, command: str) -> None:
        if self._process is None or self._process.stdin is None:
            raise DeviceError("sifibridge process is not running")
        self._process.stdin.write(command + "\n")
        self._process.stdin.flush()
        logger.debug("Sent sifibridge command: %s", command)

    def _wait_for_info(self) -> dict[str, object]:
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                details = " ".join(self._stderr_lines)
                raise DeviceError(
                    f"sifibridge exited with code {self._process.returncode}: {details}"
                )
            try:
                return self._control.get(timeout=0.1)
            except queue.Empty:
                pass
        raise DeviceError(
            f"Timed out after {self._startup_timeout_s:.1f}s waiting for bridge info"
        )

    def _connect_tcp_when_ready(self) -> None:
        assert self._reader is not None
        deadline, last_error = time.monotonic() + self._startup_timeout_s, None
        while time.monotonic() < deadline:
            try:
                self._reader.connect()
                return
            except DeviceError as exc:
                last_error = exc
                time.sleep(0.1)
        raise last_error or DeviceError("Timed out waiting for sifibridge TCP output")
