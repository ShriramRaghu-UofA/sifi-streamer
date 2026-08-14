"""Seqlock-guarded NumPy ring buffer backed by shared memory."""

from collections.abc import Buffer
from multiprocessing.shared_memory import SharedMemory
from typing import cast

import numpy as np
import numpy.typing as npt

_HEADER_BYTES = 16


class SeqlockRingBuffer:
    """Expose a fixed-size shared-memory sample ring guarded by a sequence lock.

    The 16-byte header stores a write counter and next-write head. Odd counters
    indicate an in-progress write; equal even counters around a copy identify a
    coherent snapshot. Writes larger than capacity retain their newest rows.

    Args:
        n_samples: Positive row capacity.
        n_channels: Positive column count.
        shm: Existing block large enough for the header and payload.
        dtype: Non-object scalar NumPy dtype.
        is_owner: Initialize header and payload when true; attach unchanged when
            false.
    """

    def __init__(
        self,
        n_samples: int,
        n_channels: int,
        shm: SharedMemory,
        *,
        dtype: npt.DTypeLike = np.float32,
        is_owner: bool,
    ) -> None:
        if n_samples <= 0 or n_channels <= 0:
            raise ValueError("ring dimensions must be positive")
        payload_dtype = np.dtype(dtype)
        if payload_dtype.hasobject or payload_dtype.fields is not None:
            raise ValueError("Ring-buffer dtype must be a non-object scalar dtype")
        required = self.required_bytes(n_samples, n_channels, dtype=payload_dtype)
        if shm.size < required:
            raise ValueError(
                f"SharedMemory block is {shm.size} bytes; need at least {required}"
            )
        self._n_samples, self._n_channels, self._dtype, self._shm = (
            n_samples,
            n_channels,
            payload_dtype,
            shm,
        )
        buffer = cast(Buffer, shm.buf)
        self._counter = np.frombuffer(buffer, dtype=np.uint64, count=1)
        self._head = np.frombuffer(buffer, dtype=np.uint32, count=1, offset=8)
        self._ring = np.frombuffer(
            buffer,
            dtype=payload_dtype,
            count=n_samples * n_channels,
            offset=_HEADER_BYTES,
        ).reshape(n_samples, n_channels)
        if is_owner:
            self._counter[0] = 0
            self._head[0] = 0
            self._ring.fill(0)

    @staticmethod
    def required_bytes(
        n_samples: int, n_channels: int, *, dtype: npt.DTypeLike = np.float32
    ) -> int:
        """Return shared-memory bytes required for a layout and dtype."""
        return _HEADER_BYTES + n_samples * n_channels * np.dtype(dtype).itemsize

    @property
    def n_samples(self) -> int:
        """Return ring capacity in rows."""
        return self._n_samples

    @property
    def n_channels(self) -> int:
        """Return sample column count."""
        return self._n_channels

    @property
    def dtype(self) -> np.dtype:
        """Return the normalized payload dtype."""
        return self._dtype

    @property
    def shm_name(self) -> str:
        """Return the backing shared-memory block name."""
        return self._shm.name

    def write_samples(self, samples: np.ndarray) -> None:
        """Append a 2-D sample matrix, retaining newest rows on overflow.

        Raises:
            ValueError: If ``samples`` is not shaped ``(time, n_channels)``.
        """
        if samples.ndim != 2 or samples.shape[1] != self._n_channels:
            raise ValueError(
                f"Expected shape (T, {self._n_channels}); got {samples.shape!r}"
            )
        data = samples.astype(self._dtype, copy=False)[-self._n_samples :]
        n, capacity, head = len(data), self._n_samples, int(self._head[0])
        self._counter[0] += 1
        end = head + n
        if end <= capacity:
            self._ring[head:end] = data
        else:
            split = capacity - head
            self._ring[head:] = data[:split]
            self._ring[: n - split] = data[split:]
        self._head[0] = end % capacity
        self._counter[0] += 1

    def read_counter(self) -> int:
        """Return the current sequence-lock counter."""
        return int(self._counter[0])

    def read_head(self) -> int:
        """Return the index at which the next row will be written."""
        return int(self._head[0])

    def copy_ring(self) -> np.ndarray:
        """Return an un-ordered physical copy of the ring payload."""
        return self._ring.copy()

    def close(self) -> None:
        """Release NumPy views so the shared-memory owner can close the block."""
        self._counter = np.empty(0, dtype=np.uint64)
        self._head = np.empty(0, dtype=np.uint32)
        self._ring = np.empty((0, 0), dtype=self._dtype)
