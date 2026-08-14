"""Live shared-memory reader for one SiFi modality."""

from dataclasses import dataclass
from multiprocessing.shared_memory import SharedMemory
from typing import Self

import numpy as np
import numpy.typing as npt

from sifi_streamer.background.ring_buffer import SeqlockRingBuffer
from sifi_streamer.exceptions import StaleDataError


@dataclass(frozen=True, slots=True)
class SignalWindow:
    """Copied, gap-aware rows from one live stream."""

    start_index: int
    end_index: int
    timestamps: np.ndarray
    samples: np.ndarray
    validity: np.ndarray
    overrun: bool = False


class SharedMemoryReader:
    """Read coherent recent windows from one worker-owned modality ring.

    The reader attaches to, but does not unlink, an existing shared-memory block.
    Each successful read advances this reader's freshness counter.

    Args:
        shm_name: Existing operating-system shared-memory name.
        n_samples: Ring capacity in rows.
        n_channels: Number of signal columns.
        dtype: NumPy-compatible payload dtype used by the writer.
    """

    def __init__(
        self,
        shm_name: str,
        n_samples: int,
        n_channels: int,
        *,
        dtype: npt.DTypeLike = np.float32,
    ) -> None:
        self._shm = SharedMemory(name=shm_name, create=False)
        self._ring = SeqlockRingBuffer(
            n_samples, n_channels, self._shm, dtype=dtype, is_owner=False
        )
        self._last_counter = -1

    @property
    def n_samples(self) -> int:
        """Return the ring capacity in samples."""
        return self._ring.n_samples

    @property
    def n_channels(self) -> int:
        """Return the number of columns in each sample."""
        return self._ring.n_channels

    @property
    def dtype(self) -> np.dtype:
        """Return the shared payload dtype."""
        return self._ring.dtype

    @property
    def has_new_data(self) -> bool:
        """Return whether a completed write occurred since the last window read."""
        counter = self._ring.read_counter()
        return counter != self._last_counter and counter % 2 == 0

    def read_window(
        self, n_samples: int, *, raise_on_stale: bool = False
    ) -> np.ndarray | None:
        """Copy the newest ``n_samples`` rows in chronological order.

        The method retries a bounded number of times if a concurrent writer makes
        a snapshot inconsistent. ``None`` means no fresh coherent window was
        available.

        Args:
            n_samples: Positive window length no larger than ring capacity.
            raise_on_stale: Raise instead of returning ``None`` when the ring has
                not changed since this reader's previous successful call.

        Returns:
            A copied ``(n_samples, n_channels)`` array, or ``None``.

        Raises:
            ValueError: If the requested window is non-positive or too large.
            StaleDataError: If data is unchanged and ``raise_on_stale`` is true.
        """
        if n_samples <= 0:
            raise ValueError("n_samples must be positive")
        capacity = self._ring.n_samples
        if n_samples > capacity:
            raise ValueError(
                f"n_samples={n_samples} exceeds ring buffer capacity {capacity}"
            )
        for _ in range(16):
            first = self._ring.read_counter()
            if first % 2:
                continue
            head, ring, second = (
                self._ring.read_head(),
                self._ring.copy_ring(),
                self._ring.read_counter(),
            )
            if first != second:
                continue
            if first == self._last_counter:
                if raise_on_stale:
                    raise StaleDataError(
                        "No new samples since the last read_window() call"
                    )
                return None
            self._last_counter = first
            tail = (head - n_samples) % capacity
            return (
                ring[tail:head].copy()
                if tail < head
                else np.concatenate((ring[tail:], ring[:head]))
            )
        return None

    def _coherent_snapshot(
        self,
    ) -> tuple[int, int, int, np.ndarray, np.ndarray, np.ndarray] | None:
        for _ in range(16):
            first = self._ring.read_counter()
            if first % 2:
                continue
            head = self._ring.read_head()
            total = self._ring.read_total()
            samples = self._ring.copy_ring()
            timestamps = self._ring.copy_timestamps()
            validity = self._ring.copy_validity()
            second = self._ring.read_counter()
            if first == second:
                return first, head, total, samples, timestamps, validity
        return None

    @staticmethod
    def _ordered(array: np.ndarray, head: int, count: int) -> np.ndarray:
        if count == 0:
            return array[:0].copy()
        tail = (head - count) % len(array)
        return (
            array[tail:head].copy()
            if tail < head
            else np.concatenate((array[tail:], array[:head]))
        )

    def read_signal_window(self, n_samples: int) -> SignalWindow | None:
        """Return up to the newest ``n_samples`` rows with timestamps and validity."""
        if n_samples <= 0 or n_samples > self.n_samples:
            raise ValueError(f"n_samples must be between 1 and {self.n_samples}")
        snapshot = self._coherent_snapshot()
        if snapshot is None:
            return None
        counter, head, total, samples, timestamps, validity = snapshot
        if total == 0 or counter == self._last_counter:
            return None
        count = min(n_samples, total, self.n_samples)
        self._last_counter = counter
        return SignalWindow(
            total - count,
            total,
            self._ordered(timestamps, head, count),
            self._ordered(samples, head, count),
            self._ordered(validity, head, count),
        )

    def read_since(
        self, cursor: int, *, max_samples: int | None = None
    ) -> SignalWindow | None:
        """Return rows newer than an absolute cursor, reporting ring overruns."""
        if cursor < 0:
            raise ValueError("cursor must be non-negative")
        if max_samples is not None and max_samples <= 0:
            raise ValueError("max_samples must be positive")
        snapshot = self._coherent_snapshot()
        if snapshot is None:
            return None
        counter, head, total, samples, timestamps, validity = snapshot
        if cursor >= total:
            return None
        retained = min(total, self.n_samples)
        oldest = total - retained
        start = max(cursor, oldest)
        if max_samples is not None:
            start = max(start, total - max_samples)
        ordered_samples = self._ordered(samples, head, retained)
        ordered_timestamps = self._ordered(timestamps, head, retained)
        ordered_validity = self._ordered(validity, head, retained)
        offset = start - oldest
        self._last_counter = counter
        return SignalWindow(
            start,
            total,
            ordered_timestamps[offset:],
            ordered_samples[offset:],
            ordered_validity[offset:],
            cursor < oldest,
        )

    def close(self) -> None:
        """Release local NumPy views and close this shared-memory attachment."""
        self._ring.close()
        self._shm.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
