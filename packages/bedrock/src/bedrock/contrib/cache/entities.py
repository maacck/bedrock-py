"""Entity models for the Bedrock cache system."""

from __future__ import annotations

import time

from pydantic import BaseModel


class CacheEntry(BaseModel):
    """Internal representation of a cached value with TTL tracking.

    Used primarily by the in-memory backend to track expiry timestamps.

    Attributes:
        value: The cached Python object.
        expires_at: Monotonic-clock deadline when this entry expires, or ``None`` for no expiry.
    """

    value: bytes
    expires_at: float | None = None

    @property
    def is_expired(self) -> bool:
        """Return whether this entry has passed its expiry time."""
        if self.expires_at is None:
            return False
        return time.monotonic() > self.expires_at
