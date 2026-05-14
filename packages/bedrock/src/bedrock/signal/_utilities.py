# This file is derived from the Blinker library.
# Copyright 2010 Jason Kirtland
# Licensed under the MIT License. See LICENSE.txt for details.
# Original source: https://github.com/pallets-eco/blinker

from __future__ import annotations

import collections.abc as c
import inspect
import typing as t
from weakref import WeakMethod, ref


class Symbol:
    """A constant symbol, nicer than ``object()``.

    Repeated calls with the same name return the same instance.

    Examples:
        >>> Symbol("foo") is Symbol("foo")
        True
        >>> Symbol("foo")
        foo
    """

    symbols: t.ClassVar[dict[str, Symbol]] = {}

    def __new__(cls, name: str) -> Symbol:
        """Create or retrieve a Symbol singleton instance.

        Args:
            name: The name of the symbol.

        Returns:
            A Symbol instance for the given name.
        """
        if name in cls.symbols:
            return cls.symbols[name]

        obj = super().__new__(cls)
        cls.symbols[name] = obj
        return obj

    def __init__(self, name: str) -> None:
        """Initialize the Symbol with a name.

        Args:
            name: The name of the symbol.
        """
        self.name = name

    def __repr__(self) -> str:
        """Return the string representation of the symbol.

        Returns:
            The name of the symbol.
        """
        return self.name

    def __getnewargs__(self) -> tuple[t.Any, ...]:
        """Return arguments for pickling the Symbol.

        Returns:
            A tuple containing the symbol's name for unpickling.
        """
        return (self.name,)


def make_id(obj: object) -> c.Hashable:
    """Generate a stable hashable identifier for a receiver or sender.

    This function creates a consistent identifier that can be used as a
    dictionary key or in a set. It handles special cases like bound methods
    and primitive types to ensure stable identification.

    Args:
        obj: The object to generate an identifier for. Can be any type,
            including bound methods, strings, integers, or other objects.

    Returns:
        A hashable identifier for the object. For bound methods, returns a
        tuple of (function id, instance id). For strings and integers, returns
        the value itself. For other objects, returns the object's id.
    """
    if inspect.ismethod(obj):
        # The id of a bound method is not stable, but the id of the unbound
        # function and instance are.
        return id(obj.__func__), id(obj.__self__)

    if isinstance(obj, (str | int)):
        # Instances with the same value always compare equal and have the same
        # hash, even if the id may change.
        return obj

    # Assume other types are not hashable but will always be the same instance.
    return id(obj)


def make_ref[T](obj: T, callback: c.Callable[[ref[T]], None] | None = None) -> ref[T]:
    """Create a weak reference to an object, handling bound methods specially.

    If the object is a bound method, a :class:`weakref.WeakMethod` is created
    to properly track the method. Otherwise, a standard :class:`weakref.ref`
    is returned.

    Args:
        obj: The object to create a weak reference to.
        callback: Optional callable that will be invoked when the weak
            reference is about to be finalized.

    Returns:
        A weak reference to the object.
    """
    if inspect.ismethod(obj):
        return WeakMethod(obj, callback)  # type: ignore[arg-type, return-value]

    return ref(obj, callback)
