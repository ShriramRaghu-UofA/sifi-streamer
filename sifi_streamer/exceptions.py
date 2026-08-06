"""Domain exception hierarchy for :mod:`sifi_streamer`."""


class StreamerError(Exception):
    """Base class for streamer failures."""


class AckTimeoutError(StreamerError):
    """A background command was not acknowledged in time."""


class AckError(StreamerError):
    """The background worker rejected a command."""


class RecordingError(StreamerError):
    """A capture could not be started or stopped cleanly."""


class StaleDataError(StreamerError):
    """No new shared-memory samples are available."""


class DeviceError(StreamerError):
    """A SiFi connection, transport, or packet read failed."""


class CaptureInitializationError(StreamerError):
    """Capture backend startup failed."""
