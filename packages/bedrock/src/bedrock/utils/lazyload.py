"""
Utilities for lazy loading, dynamic imports, and module introspection.

This module provides helpers for:

- Importing classes or attributes from dotted module paths.
- Checking whether a package contains a given submodule.
- Locating the directory that owns a module.
- Dynamically discovering subclasses within a module.

Credit
------
Most of the utilities in this module are adapted from the Django project
(https://www.djangoproject.com/), specifically from Django's ``django.utils.module_loading``
module. Django is licensed under the BSD License.

Original Django code used under the terms of Django's BSD license:

    Copyright (c) Django Software Foundation and individual contributors.
    All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the conditions in the Django license are met.

See https://github.com/django/django/blob/main/LICENSE for the full license text.
"""

from __future__ import annotations

import inspect
import os
import sys
import traceback
from collections.abc import Callable
from importlib import import_module
from importlib.util import find_spec as importlib_find
from typing import Any, TypeVar, cast

T = TypeVar("T")


def _split_ref(ref: str) -> tuple[str, str]:
    """Split a string reference into module and attribute names."""
    if not ref or not ref.strip():
        raise ValueError("Callable reference must not be empty.")

    if ":" in ref:
        module_name, attr_name = ref.split(":", 1)
    else:
        parts = ref.rsplit(".", 1)
        if len(parts) != 2:
            raise ValueError("Callable reference must use 'package.module:attribute' or 'package.module.attribute'.")
        module_name, attr_name = parts

    module_name = module_name.strip()
    attr_name = attr_name.strip()
    if not module_name or not attr_name:
        raise ValueError("Callable reference must include both a module path and attribute name.")

    return module_name, attr_name


def cached_import(module_path: str, class_name: str) -> object:
    """Import *class_name* from *module_path*, reusing an already-loaded module when safe.

    This function first checks whether the target module is already present in
    ``sys.modules`` **and** has finished its initialisation phase (``__spec__._initializing``
    is ``False``).  If both conditions are met the cached module is reused;
    otherwise a fresh ``import_module`` call is performed.

    Args:
        module_path: Fully qualified dotted path to the module
            (e.g. ``"myapp.services"``).
        class_name: Name of the attribute or class to retrieve from the module.

    Returns:
        The attribute / class identified by *class_name*.

    Raises:
        ImportError: Propagated from ``import_module`` when the module cannot
            be imported.
        AttributeError: Raised by ``getattr`` when *class_name* does not exist
            on the module.
    """
    # Check whether module is loaded and fully initialized.
    if not (
        (module := sys.modules.get(module_path))
        and (spec := getattr(module, "__spec__", None))
        and getattr(spec, "_initializing", False) is False
    ):
        module = import_module(module_path)
    return getattr(module, class_name)


def import_string(dotted_path: str) -> object:
    """Import a dotted module path and return the attribute designated by the last name.

    Example::

        >>> import_string("myapp.services.UserService")
        <class 'myapp.services.UserService'>

    Args:
        dotted_path: A fully qualified dotted path to the desired attribute
            (e.g. ``"myapp.services.UserService"``).

    Returns:
        The class or attribute referenced by the final segment of *dotted_path*.

    Raises:
        ImportError: If *dotted_path* does not contain at least one dot, or
            if the module does not define the requested attribute.
    """
    try:
        module_path, class_name = dotted_path.rsplit(".", 1)
    except ValueError as err:
        raise ImportError(f"{dotted_path} doesn't look like a module path") from err

    try:
        return cached_import(module_path, class_name)
    except AttributeError as err:
        raise ImportError(f'Module "{module_path}" does not define a "{class_name}" attribute/class') from err


def load_string(ref: str) -> object:
    """Import a dotted module path and return the attribute designated by the last name.

    Example::

        >>> load_string("myapp.services:UserService")
        <class 'myapp.services.UserService'>

    Args:
        ref: A fully qualified dotted path to the desired attribute
            (e.g. ``"myapp.services.UserService"``).

    Returns:
        The class or attribute referenced by the final segment of *path*.

    Raises:
        ImportError: If *path* does not contain at least one dot, or
            if the module does not define the requested attribute.
    """
    try:
        module_path, class_name = ref.split(":", 1)
    except ValueError as err:
        raise ImportError(f"{ref} doesn't look like a module path") from err

    try:
        return cached_import(module_path, class_name)
    except AttributeError as err:
        raise ImportError(f'Module "{module_path}" does not define a "{class_name}" attribute/class') from err


def load_callable(ref: str) -> Callable[..., Any]:
    """Load a callable object from an import string.

    Args:
        ref: Import string in the form ``package.module:attribute``.

    Returns:
        Loaded callable.

    Raises:
        InvalidModuleCallableError: If the target is not callable or cannot be resolved.
    """
    target = load_string(ref)
    if not callable(target):
        raise ValueError(f"Resolved object '{ref}' is not callable.")
    return target


def load_optional_callable(ref: str) -> Callable[..., Any] | None:
    """Load a callable reference and allow missing bootstrap modules.

    Args:
        ref: Import string in the form ``package.module:attribute``.

    Returns:
        Callable target, or ``None`` when the module or attribute does not exist.

    Raises:
        InvalidModuleCallableError: If the reference is malformed or resolves to a non-callable.
    """
    try:
        return load_callable(ref)
    except ValueError as exc:
        message = str(exc)
        if "does not define attribute" in message or message.startswith("Failed to import module"):
            return None
        raise
    except ImportError:
        return None


def module_has_submodule(package: object, module_name: str) -> bool:
    """Check whether *package* contains a submodule called *module_name*.

    This is useful for optional feature detection — e.g. determining whether
    a ``management`` package exists inside an application.

    Args:
        package: A module object that is expected to be a package (i.e. has
            ``__name__`` and ``__path__`` attributes).
        module_name: The short name of the submodule to look for (not a
            fully qualified path).

    Returns:
        ``True`` if the submodule can be found, ``False`` otherwise.

    Note:
        This function does **not** import the submodule; it only checks
        whether a valid import spec exists.
    """
    try:
        package_name: str = package.__name__  # type: ignore[attr-defined]
        package_path: list[str] = package.__path__  # type: ignore[attr-defined]
    except AttributeError:
        # package isn't a package.
        return False

    full_module_name = package_name + "." + module_name
    try:
        return importlib_find(full_module_name, package_path) is not None
    except ModuleNotFoundError:
        # When module_name is an invalid dotted path, Python raises
        # ModuleNotFoundError.
        return False


def module_dir(module: object) -> str:
    """Return the filesystem directory that contains *module*.

    Works for both regular modules (single ``__file__``) and packages
    (single-element ``__path__``).

    Args:
        module: A module object.

    Returns:
        Absolute path to the directory containing the module source.

    Raises:
        ValueError: If the module has zero or multiple ``__path__`` entries
            (e.g. a namespace package spread across several directories)
            **and** has no ``__file__`` attribute.
    """
    # Convert to list because __path__ may not support indexing.
    paths: list[str] = list(getattr(module, "__path__", []))
    if len(paths) == 1:
        return paths[0]
    else:
        filename: str | None = getattr(module, "__file__", None)
        if filename is not None:
            return os.path.dirname(filename)
    raise ValueError(f"Cannot determine directory containing {module}")


def load_single_subclass[T](
    module_path: str,
    subclass: tuple[type[T], ...] | type[T],
    package: str | None = None,
) -> type[T] | None:
    """Import *module_path* and return the first concrete subclass of *subclass*.

    Scans all top-level attributes of the imported module and returns the
    first object that:

    1. Is a class (``isinstance(obj, type)``).
    2. Is a subclass of *subclass* (but is not *subclass* itself).
    3. Is **not** abstract (``inspect.isabstract`` returns ``False``).

    If multiple matching classes exist, **only the first one encountered**
    (in ``dir()`` order) is returned.

    Args:
        module_path: Dotted path to the module to scan.
        subclass: A single class or a tuple of classes used as the base type
            for discovery.
        package: Optional package name passed through to ``import_module``
            for relative imports.

    Returns:
        The discovered class, or ``None`` if no concrete subclass is found.

    Raises:
        ImportError: If the module cannot be imported (the original exception
            is chained and printed to stderr first).
    """
    try:
        root_module = import_module(module_path, package=package)
        subclass_tup: tuple[type[T], ...] = subclass if isinstance(subclass, tuple) else (subclass,)
        # Find the first concrete (non-abstract) subclass of the given type(s).
        for name in dir(root_module):
            obj = getattr(root_module, name)
            if inspect.isabstract(obj):
                continue
            if isinstance(obj, type) and issubclass(obj, subclass) and obj not in subclass_tup:
                return cast(type[T], obj)
        return None
    except (ImportError, AttributeError) as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        raise ImportError(f"Error importing class {subclass}: '{e}'") from e
