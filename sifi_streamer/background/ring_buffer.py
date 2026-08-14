"""Seqlock-guarded NumPy ring buffer backed by shared memory."""

from collections.abc import Buffer
from multiprocessing.shared_memory import SharedMemory
from typing import cast

import numpy as np
import numpy.typing as npt

_HEADER_BYTES = 16


class SeqlockRingBuffer:
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
        return _HEADER_BYTES + n_samples * n_channels * np.dtype(dtype).itemsize

    @property
    def n_samples(self) -> int:
        return self._n_samples

    @property
    def n_channels(self) -> int:
        return self._n_channels

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    @property
    def shm_name(self) -> str:
        return self._shm.name

    def write_samples(self, samples: np.ndarray) -> None:
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
        return int(self._counter[0])

    def read_head(self) -> int:
        return int(self._head[0])

    def copy_ring(self) -> np.ndarray:
        return self._ring.copy()

    def close(self) -> None:
        self._counter = np.empty(0, dtype=np.uint64)
        self._head = np.empty(0, dtype=np.uint32)
        self._ring = np.empty((0, 0), dtype=self._dtype)
