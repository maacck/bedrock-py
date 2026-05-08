"""SQLAlchemy ORM model observer for insert, update, and delete events.

Provides a mechanism to listen for model lifecycle events by hooking into
SQLAlchemy's session ``before_flush`` event. Models can register observer
callbacks that are invoked when instances are inserted, updated, or deleted
within a session flush.

Inspired by ``sqlalchemy_utils.observes``.
"""

import typing
import weakref
from collections import defaultdict, namedtuple
from inspect import ismethod

from sqlalchemy import event as sa_event
from sqlalchemy.orm.session import Session

from bedrock.database.base import BedrockModel

Callback = namedtuple("Callback", ["func", "backref", "fullpath"])
"""Represents an observer callback with the callback function and metadata."""

ModelObserverIdentifier = typing.Literal["on_insert", "on_update", "on_delete"]
"""Valid event identifier for model lifecycle events."""

ListenerCallback = typing.Callable[[Session, BedrockModel, ModelObserverIdentifier], None]
"""Type alias for observer callback functions.

Signature: ``callback(session, target, identifier) -> None``
"""


class WeakMethod:
    """A weak reference wrapper around a bound method.

    Prevents memory leaks by not keeping strong references to objects
    when registering bound methods as observer callbacks.

    Args:
        object_dot_method: A bound method (e.g. ``instance.method``).
    """

    def __init__(self, object_dot_method):
        self.target = weakref.proxy(object_dot_method.__self__)
        self.method = weakref.proxy(object_dot_method.__func__)
        ###Older versions of Python can use 'im_self' and 'im_func' in place of '__self__' and '__func__' respectively

    def __call__(self, *args, **kwargs):
        """Call the wrapped method with the provided arguments."""
        return self.method(self.target, *args, **kwargs)


class ModelObserver:
    """Central observer that hooks into SQLAlchemy session flush events.

    Listens to the session ``before_flush`` event and invokes registered
    callbacks for models that have declared observers via ``@observes_model``.

    Callbacks are stored on the model class under ``__observers__`` as a
    dictionary mapping event identifiers to lists of weak-referenced functions.
    """

    def __init__(self):
        self.listener_args = [
            (Session, "before_flush", self.invoke_callbacks),
        ]
        self.callback_map = defaultdict(list)
        # TODO: make the registry a WeakKey dict
        self.generator_registry = defaultdict(list)

    def remove_sa_listeners(self) -> None:
        """Remove all registered SQLAlchemy event listeners."""
        for args in self.listener_args:
            sa_event.remove(*args)

    def register_sa_listeners(self) -> None:
        """Register the ``before_flush`` event listener if not already registered."""
        for args in self.listener_args:
            if not sa_event.contains(*args):
                sa_event.listen(*args)

    def register_listeners(
        self,
        model: type[BedrockModel],
        func: ListenerCallback,
        identifiers: list[ModelObserverIdentifier],
    ) -> None:
        """Register a callback function as an observer for the given model and events.

        Args:
            model: The SQLAlchemy model class to observe.
            func: The callback function or method to invoke on events.
            identifiers: A list of event identifiers to listen for
                (``on_insert``, ``on_update``, ``on_delete``).
        """
        if model.__observers__ is None:
            # observers is dict with weakref
            # it has event_identifier as key
            # and value is a list of functions
            model.__observers__ = defaultdict(list)
        for identifier in identifiers:
            if identifier not in model.__observers__:
                model.__observers__[identifier] = []
            # if is method
            if ismethod(func):
                # if is bound method
                model.__observers__[identifier].append(WeakMethod(func))
            else:
                model.__observers__[identifier].append(weakref.ref(func))

    def __repr__(self) -> str:
        return "<ModelObserver>"

    def invoke_callbacks(self, session: Session, ctx, instances) -> None:
        """Invoke registered observer callbacks during session flush.

        Maps session state categories (``new``, ``dirty``, ``deleted``) to
        observer event identifiers (``on_insert``, ``on_update``, ``on_delete``)
        and calls the appropriate callbacks for each affected instance.

        Args:
            session: The active SQLAlchemy session being flushed.
            ctx: Flush context (unused).
            instances: Instance set pending flush (unused).
        """
        identifier_map = {
            "new": "on_insert",
            "dirty": "on_update",
            "deleted": "on_delete",
        }
        for session_attr, identifier in identifier_map.items():
            for instance in getattr(session, session_attr):
                if not hasattr(instance, "__observers__"):
                    continue
                if not instance.__observers__ or identifier not in instance.__observers__:
                    continue
                for callback in instance.__observers__.get(identifier_map[session_attr], []):
                    if isinstance(callback, weakref.ref):
                        callback = callback()
                    callback(session, instance, identifier)


observer = ModelObserver()


def observes_model(
    model: type[BedrockModel],
    *identifiers: ModelObserverIdentifier,
    **observer_kw: typing.Any,
) -> typing.Callable[[ListenerCallback], ListenerCallback]:
    """Register a function as an observer callback for a SQLAlchemy model.

    The decorated function is invoked during the session ``before_flush`` phase,
    receiving all model instances that match the specified event identifiers
    within the current flush.

    Example::

        from bedrock.database import BedrockModel
        from bedrock.database.observer import (
            observes_model,
            observer,
        )


        class Catalog(BedrockModel):
            __tablename__ = "catalog"
            id = sa.Column(sa.Integer, primary_key=True)
            category_count = sa.Column(sa.Integer, default=0)


        @observes_model(Catalog, "on_insert", "on_delete")
        def log_catalog_changes(session, target, identifier):
            print(f"Catalog {target.id} was {identifier}")

    Args:
        model: The SQLAlchemy model class to observe.
        identifiers: One or more event identifiers to listen for
            (``on_insert``, ``on_update``, ``on_delete``).
        observer_kw: Optional keyword arguments. Pass ``observer=custom_observer``
            to use a different ``ModelObserver`` instance instead of the default
            module-level ``observer`` singleton.

    Returns:
        A decorator that registers the function as an observer callback.
    """
    observer_ = observer_kw.pop("observer", observer)
    observer_.register_sa_listeners()

    def wraps(func: ListenerCallback):
        observer_.register_listeners(model, func, identifiers, **observer_kw)

        return func

    return wraps
