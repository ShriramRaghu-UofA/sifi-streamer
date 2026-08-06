"""Managed lifecycle for the vendor ``sifibridge`` executable."""

import contextlib
import json
import os
import queue
import socket
import subprocess
import threading
import time
from collections import deque
from enum import StrEnum
from pathlib import Path

from sifi_streamer.devices import (
    Modalities,
    ModalitySpec,
    PacketReader,
    SiFiBandDevice,
    SiFiPacket,
    modalities_from_device_info,
    packet_from_json_line,
)
from sifi_streamer.exceptions import DeviceError

EMG_SAMPLE_RATES = frozenset((500, 1000, 1600, 2000))


class BridgeTransport(StrEnum):
    TCP = "tcp"
    UDP = "udp"
    STDOUT = "stdout"


class _UdpPacketReader:
    def __init__(self, host: str, port: int) -> None:
        self._host, self._port, self._sock, self._pending = host, port, None, deque()

    def connect(self) -> None:
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
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock = None
        self._pending.clear()

    def read_packet(self) -> SiFiPacket:
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
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._packets.put(None)

    def read_packet(self) -> SiFiPacket:
        if not self._connected:
            raise DeviceError(
                "stdout packet reader.read_packet() called before connect()"
            )
        if (packet := self._packets.get()) is None:
            raise DeviceError("stdout packet reader closed")
        return packet


class SiFiBridgeDevice:
    """Launch, configure, stream from, and orderly stop one bridge process."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        executable: str | Path = "bin/sifibridge.exe",
        startup_timeout_s: float = 20.0,
        transport: BridgeTransport | str = BridgeTransport.TCP,
        emg_sample_rate: int = 1600,
    ) -> None:
        if emg_sample_rate not in EMG_SAMPLE_RATES:
            choices = ", ".join(map(str, sorted(EMG_SAMPLE_RATES)))
            raise ValueError(
                f"emg_sample_rate must be one of: {choices}"
            )
        (
            self._host,
            self._port,
            self._transport,
            self._executable,
            self._startup_timeout_s,
            self._emg_sample_rate,
        ) = (
            host,
            port,
            BridgeTransport(transport),
            Path(executable),
            startup_timeout_s,
            emg_sample_rate,
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
        if self._modalities is None:
            raise DeviceError("SiFiBridgeDevice.modalities accessed before connect()")
        return self._modalities

    @property
    def device_info(self) -> dict[str, object] | None:
        return self._device_info

    def connect(self) -> None:
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
            self._launch()
            self._send("connect")
            self._send(f"configure emg --fs {self._emg_sample_rate}")
            self._send("info")
            self._device_info = self._wait_for_info()
            self._modalities = modalities_from_device_info(self._device_info)
            self._send("start")
            if self._transport is BridgeTransport.TCP:
                self._connect_tcp_when_ready()
            elif self._transport is BridgeTransport.STDOUT:
                self._reader.connect()
        except DeviceError, OSError, ValueError:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.disconnect()
        process, self._process = self._process, None
        if process is None:
            return
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
                process.kill()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()

    def read_packet(self) -> SiFiPacket:
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
                self._stderr_lines.append(line.rstrip())

    def _send(self, command: str) -> None:
        if self._process is None or self._process.stdin is None:
            raise DeviceError("sifibridge process is not running")
        self._process.stdin.write(command + "\n")
        self._process.stdin.flush()

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
