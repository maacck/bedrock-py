"""Cache-backed distributed lock primitives."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING

from .exc import CacheLockAcquisitionError, CacheLockError, CacheLockOwnershipError

if TYPE_CHECKING:
    from .service import CacheService


class CacheLock:
    """Distributed lock backed by the configured cache backend.

    The lock uses cache-level atomic add and compare-and-delete operations so
    it can coordinate work across multiple processes when the active backend
    supports those guarantees (for example, Redis).

    Args:
        cache_service: Cache service used to talk to the configured backend.
        name: Logical lock name within the configured prefix namespace.
        expire: Lock TTL in seconds. The lock is automatically released by the
            backend when this TTL expires.
        blocking: Whether :meth:`acquire` and :meth:`aacquire` should wait for
            the lock when it is already held.
        blocking_timeout: Maximum number of seconds to wait while acquiring the
            lock. ``None`` means to wait indefinitely when blocking is enabled.
        sleep: Delay between acquisition attempts while waiting for the lock.
        prefix: Namespace prefix used to build the internal cache key.

    Examples:
        Synchronous usage:

        >>> from bedrock.contrib.cache import cache
        >>> with cache.lock("jobs:sync", expire=30, blocking_timeout=5):
        ...     run_job()

        Asynchronous usage:

        >>> from bedrock.contrib.cache import cache
        >>> async with cache.lock(
        ...     "jobs:async", expire=30, blocking_timeout=5
        ... ):
        ...     await run_job()
    """

    def __init__(
        self,
        *,
        cache_service: CacheService,
        name: str,
        expire: int = 30,
        blocking: bool = True,
        blocking_timeout: float | None = None,
        sleep: float = 0.1,
        prefix: str = "lock",
    ) -> None:
        normalized_name = name.strip(":")
        normalized_prefix = prefix.strip(":")
        if not normalized_name:
            raise ValueError("Cache lock name cannot be empty.")
        if not normalized_prefix:
            raise ValueError("Cache lock prefix cannot be empty.")
        if expire <= 0:
            raise ValueError("Cache lock expire must be greater than zero.")
        if sleep <= 0:
            raise ValueError("Cache lock sleep must be greater than zero.")
        if blocking_timeout is not None and blocking_timeout < 0:
            raise ValueError("Cache lock blocking_timeout cannot be negative.")
        self._cache_service = cache_service
        self._expire = expire
        self._blocking = blocking
        self._blocking_timeout = blocking_timeout
        self._sleep = sleep
        self._key = cache_service.namespace(normalized_prefix).build_key(normalized_name)
        self._token: bytes | None = None

    @property
    def key(self) -> str:
        """Return the internal cache key used by this lock.

        Returns:
            The fully qualified cache key that stores the lock token.
        """
        return self._key

    def _new_token(self) -> bytes:
        return uuid.uuid4().hex.encode()

    @staticmethod
    def _deadline(blocking_timeout: float | None) -> float | None:
        if blocking_timeout is None:
            return None
        return time.monotonic() + blocking_timeout

    @staticmethod
    def _remaining_sleep(deadline: float | None, sleep: float) -> float:
        if deadline is None:
            return sleep
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0
        return min(sleep, remaining)

    def acquire(
        self,
        *,
        blocking: bool | None = None,
        blocking_timeout: float | None = None,
        sleep: float | None = None,
    ) -> bool:
        """Attempt to acquire the lock synchronously.

        Args:
            blocking: Override for the instance-level blocking behavior.
            blocking_timeout: Override for the maximum time spent waiting for
                the lock.
            sleep: Override for the delay between retries while waiting.

        Returns:
            ``True`` when the lock is acquired, otherwise ``False``.

        Raises:
            CacheLockError: If this lock instance already owns the lock.
            ValueError: If the effective wait configuration is invalid.
        """
        if self._token is not None:
            raise CacheLockError(f"Lock '{self._key}' is already acquired by this instance.")

        active_blocking = self._blocking if blocking is None else blocking
        active_timeout = self._blocking_timeout if blocking_timeout is None else blocking_timeout
        active_sleep = self._sleep if sleep is None else sleep
        if active_sleep <= 0:
            raise ValueError("Cache lock sleep must be greater than zero.")
        if active_timeout is not None and active_timeout < 0:
            raise ValueError("Cache lock blocking_timeout cannot be negative.")

        deadline = self._deadline(active_timeout)
        token = self._new_token()
        backend = self._cache_service.get_backend()

        while True:
            if backend.add(self._key, token, ex=self._expire):
                self._token = token
                return True
            if not active_blocking:
                return False
            delay = self._remaining_sleep(deadline, active_sleep)
            if delay <= 0:
                return False
            time.sleep(delay)

    async def aacquire(
        self,
        *,
        blocking: bool | None = None,
        blocking_timeout: float | None = None,
        sleep: float | None = None,
    ) -> bool:
        """Attempt to acquire the lock asynchronously.

        Args:
            blocking: Override for the instance-level blocking behavior.
            blocking_timeout: Override for the maximum time spent waiting for
                the lock.
            sleep: Override for the delay between retries while waiting.

        Returns:
            ``True`` when the lock is acquired, otherwise ``False``.

        Raises:
            CacheLockError: If this lock instance already owns the lock.
            ValueError: If the effective wait configuration is invalid.
        """
        if self._token is not None:
            raise CacheLockError(f"Lock '{self._key}' is already acquired by this instance.")

        active_blocking = self._blocking if blocking is None else blocking
        active_timeout = self._blocking_timeout if blocking_timeout is None else blocking_timeout
        active_sleep = self._sleep if sleep is None else sleep
        if active_sleep <= 0:
            raise ValueError("Cache lock sleep must be greater than zero.")
        if active_timeout is not None and active_timeout < 0:
            raise ValueError("Cache lock blocking_timeout cannot be negative.")

        deadline = self._deadline(active_timeout)
        token = self._new_token()
        backend = self._cache_service.get_backend()

        while True:
            if await backend.aadd(self._key, token, ex=self._expire):
                self._token = token
                return True
            if not active_blocking:
                return False
            delay = self._remaining_sleep(deadline, active_sleep)
            if delay <= 0:
                return False
            await asyncio.sleep(delay)

    def release(self) -> None:
        """Release the lock synchronously.

        Raises:
            CacheLockOwnershipError: If this lock instance does not currently
                own the lock key.
        """
        if self._token is None:
            raise CacheLockOwnershipError(f"Lock '{self._key}' is not currently acquired.")

        token = self._token
        self._token = None
        released = self._cache_service.get_backend().compare_and_delete(
            self._key,
            token,
        )
        if not released:
            raise CacheLockOwnershipError(f"Lock '{self._key}' is no longer owned by this instance.")

    async def arelease(self) -> None:
        """Release the lock asynchronously.

        Raises:
            CacheLockOwnershipError: If this lock instance does not currently
                own the lock key.
        """
        if self._token is None:
            raise CacheLockOwnershipError(f"Lock '{self._key}' is not currently acquired.")

        token = self._token
        self._token = None
        released = await self._cache_service.get_backend().acompare_and_delete(
            self._key,
            token,
        )
        if not released:
            raise CacheLockOwnershipError(f"Lock '{self._key}' is no longer owned by this instance.")

    def locked(self) -> bool:
        """Return whether the lock key currently exists.

        Returns:
            ``True`` when the backend still holds the lock key, otherwise
            ``False``.
        """
        return self._cache_service.exists(self._key)

    async def alocked(self) -> bool:
        """Return whether the lock key currently exists asynchronously.

        Returns:
            ``True`` when the backend still holds the lock key, otherwise
            ``False``.
        """
        return await self._cache_service.aexists(self._key)

    def __enter__(self) -> CacheLock:
        """Acquire the lock for ``with`` usage.

        Returns:
            The current lock instance.

        Raises:
            CacheLockAcquisitionError: If the lock could not be acquired.
        """
        if not self.acquire():
            raise CacheLockAcquisitionError(f"Failed to acquire lock '{self._key}'.")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """Release the lock when leaving a ``with`` block.

        Args:
            exc_type: Exception type raised inside the context, if any.
            exc: Exception instance raised inside the context, if any.
            tb: Traceback raised inside the context, if any.

        Returns:
            ``False`` so exceptions from the wrapped block are not suppressed.
        """
        self.release()
        return False

    async def __aenter__(self) -> CacheLock:
        """Acquire the lock for ``async with`` usage.

        Returns:
            The current lock instance.

        Raises:
            CacheLockAcquisitionError: If the lock could not be acquired.
        """
        if not await self.aacquire():
            raise CacheLockAcquisitionError(f"Failed to acquire lock '{self._key}'.")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Release the lock when leaving an ``async with`` block.

        Args:
            exc_type: Exception type raised inside the context, if any.
            exc: Exception instance raised inside the context, if any.
            tb: Traceback raised inside the context, if any.

        Returns:
            ``False`` so exceptions from the wrapped block are not suppressed.
        """
        await self.arelease()
        return False
