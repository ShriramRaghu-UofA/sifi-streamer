"""Live shared-memory reader for one SiFi modality."""

from multiprocessing.shared_memory import SharedMemory
from typing import Self

import numpy as np
import numpy.typing as npt

from sifi_streamer.background.ring_buffer import SeqlockRingBuffer
from sifi_streamer.exceptions import StaleDataError


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

    def close(self) -> None:
        """Release local NumPy views and close this shared-memory attachment."""
        self._ring.close()
        self._shm.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
