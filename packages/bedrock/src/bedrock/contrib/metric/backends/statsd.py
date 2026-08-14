"""StatsD provider — zero-dependency UDP protocol."""

import socket

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..events import MetricEvent


class StatsDSettings(BaseSettings):
    """Settings for the StatsD UDP backend."""

    model_config = SettingsConfigDict(env_prefix="STATSD_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8125
    prefix: str = ""


class StatsDProvider:
    """Emit metrics to a StatsD-compatible endpoint over UDP.

    Wire format (stable for a decade): ``name:value|c`` for counters,
    ``name:value|g`` for gauges, ``name:<ms>|ms`` for timers (converted from
    seconds). Fractional counter deltas are preserved (``2.5`` stays ``2.5``)
    while integer-valued floats serialize without a trailing ``.0``. Tags use
    Datadog syntax ``|#k:v,k2:v2`` with sorted keys. The UDP socket is created
    lazily on first emit; ``close`` releases it.
    """

    def __init__(self, settings: StatsDSettings | None = None) -> None:
        self._settings = settings or StatsDSettings()
        self._socket: socket.socket | None = None

    def _send(self, payload: str) -> None:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.sendto(payload.encode("utf-8"), (self._settings.host, self._settings.port))

    def emit(self, event: MetricEvent) -> None:
        """Serialize ``event`` to the StatsD wire format and send it."""
        name = f"{self._settings.prefix}{event.name}" if self._settings.prefix else event.name
        if event.type == "counter":
            body = f"{name}:{event.value:g}|c"
        elif event.type == "gauge":
            body = f"{name}:{event.value:g}|g"
        else:  # timer
            body = f"{name}:{int(round(event.value * 1000))}|ms"
        if event.tags:
            tag_str = ",".join(f"{k}:{v}" for k, v in sorted(event.tags.items()))
            body += f"|#{tag_str}"
        self._send(body)

    def close(self) -> None:
        """Close the UDP socket, if created."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None
