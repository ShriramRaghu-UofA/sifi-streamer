"""Seqlock-guarded NumPy ring buffer backed by shared memory."""

from collections.abc import Buffer
from multiprocessing.shared_memory import SharedMemory
from typing import cast

import numpy as np
import numpy.typing as npt

_HEADER_BYTES = 32


class SeqlockRingBuffer:
    """Expose a fixed-size shared-memory sample ring guarded by a sequence lock.

    The header stores a write counter, absolute sample count, and next-write
    head. Odd counters indicate an in-progress write; equal even counters around
    a copy identify a coherent snapshot. Oversized writes retain newest rows.

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
        self._total = np.frombuffer(buffer, dtype=np.uint64, count=1, offset=8)
        self._head = np.frombuffer(buffer, dtype=np.uint32, count=1, offset=16)
        payload_bytes = n_samples * n_channels * payload_dtype.itemsize
        self._ring = np.frombuffer(
            buffer,
            dtype=payload_dtype,
            count=n_samples * n_channels,
            offset=_HEADER_BYTES,
        ).reshape(n_samples, n_channels)
        self._timestamps = np.frombuffer(
            buffer,
            dtype=np.float64,
            count=n_samples,
            offset=_HEADER_BYTES + payload_bytes,
        )
        self._validity = np.frombuffer(
            buffer,
            dtype=np.uint8,
            count=n_samples * n_channels,
            offset=_HEADER_BYTES + payload_bytes + n_samples * 8,
        ).reshape(n_samples, n_channels)
        if is_owner:
            self._counter[0] = 0
            self._total[0] = 0
            self._head[0] = 0
            self._ring.fill(0)
            self._timestamps.fill(0)
            self._validity.fill(0)

    @staticmethod
    def required_bytes(
        n_samples: int, n_channels: int, *, dtype: npt.DTypeLike = np.float32
    ) -> int:
        """Return shared-memory bytes required for a layout and dtype."""
        payload = n_samples * n_channels * np.dtype(dtype).itemsize
        return _HEADER_BYTES + payload + n_samples * 8 + n_samples * n_channels

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

    def write_samples(
        self,
        samples: np.ndarray,
        *,
        timestamps: np.ndarray | None = None,
        validity: np.ndarray | None = None,
    ) -> None:
        """Append a 2-D sample matrix, retaining newest rows on overflow.

        Raises:
            ValueError: If ``samples`` is not shaped ``(time, n_channels)``.
        """
        if samples.ndim != 2 or samples.shape[1] != self._n_channels:
            raise ValueError(
                f"Expected shape (T, {self._n_channels}); got {samples.shape!r}"
            )
        original_length = len(samples)
        if timestamps is None:
            timestamps = np.arange(
                int(self._total[0]),
                int(self._total[0]) + original_length,
                dtype=np.float64,
            )
        if validity is None:
            validity = np.ones(samples.shape, dtype=np.uint8)
        if timestamps.ndim != 1 or len(timestamps) != original_length:
            raise ValueError("timestamps must have shape (T,)")
        if validity.shape != samples.shape:
            raise ValueError("validity must have the same shape as samples")
        data = samples.astype(self._dtype, copy=False)[-self._n_samples :]
        times = timestamps.astype(np.float64, copy=False)[-self._n_samples :]
        valid = validity.astype(np.uint8, copy=False)[-self._n_samples :]
        n, capacity, head = len(data), self._n_samples, int(self._head[0])
        self._counter[0] += 1
        end = head + n
        if end <= capacity:
            self._ring[head:end] = data
            self._timestamps[head:end] = times
            self._validity[head:end] = valid
        else:
            split = capacity - head
            self._ring[head:] = data[:split]
            self._ring[: n - split] = data[split:]
            self._timestamps[head:] = times[:split]
            self._timestamps[: n - split] = times[split:]
            self._validity[head:] = valid[:split]
            self._validity[: n - split] = valid[split:]
        self._head[0] = end % capacity
        self._total[0] += original_length
        self._counter[0] += 1

    def read_counter(self) -> int:
        """Return the current sequence-lock counter."""
        return int(self._counter[0])

    def read_head(self) -> int:
        """Return the index at which the next row will be written."""
        return int(self._head[0])

    def read_total(self) -> int:
        """Return the absolute number of rows offered to this ring."""
        return int(self._total[0])

    def copy_ring(self) -> np.ndarray:
        """Return an un-ordered physical copy of the ring payload."""
        return self._ring.copy()

    def copy_timestamps(self) -> np.ndarray:
        return self._timestamps.copy()

    def copy_validity(self) -> np.ndarray:
        return self._validity.astype(np.bool_, copy=True)

    def close(self) -> None:
        """Release NumPy views so the shared-memory owner can close the block."""
        self._counter = np.empty(0, dtype=np.uint64)
        self._total = np.empty(0, dtype=np.uint64)
        self._head = np.empty(0, dtype=np.uint32)
        self._ring = np.empty((0, 0), dtype=self._dtype)
        self._timestamps = np.empty(0, dtype=np.float64)
        self._validity = np.empty((0, 0), dtype=np.uint8)
