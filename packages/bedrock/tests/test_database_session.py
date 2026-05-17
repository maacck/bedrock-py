"""Tests for database session lifecycle and SessionFactory."""

from __future__ import annotations

import pytest
from bedrock.database import DatabaseManager, SessionFactory
from bedrock.database.manager import DatabaseNotConfiguredError
from bedrock.database.session_factory import _current_db_session
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for test models."""


class Item(Base):
    """Minimal model for session lifecycle tests."""

    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)


@pytest.fixture
def db_engine():
    """Create an in-memory SQLite engine with tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_maker(db_engine):
    """Return a sessionmaker bound to the test engine."""
    return sessionmaker(bind=db_engine)


@pytest.fixture
def factory(session_maker):
    """Return a SessionFactory wrapping the test sessionmaker."""
    return SessionFactory(session_maker)


@pytest.fixture
def db_manager():
    """Return a fresh DatabaseManager initialized with SQLite :memory:."""
    manager = DatabaseManager()
    manager.init("sqlite:///:memory:")
    yield manager
    manager.clear_session()


class TestDatabaseSessionFixtures:
    """Verify that all session fixtures are properly wired."""

    def test_fixture_works(self, db_engine, session_maker, factory, db_manager):
        """Smoke test: all fixtures inject without error."""
        assert db_engine is not None
        assert session_maker is not None
        assert factory is not None
        assert db_manager is not None

    def test_factory_session_returns_active_session(self, factory):
        """Factory.session should return a usable SQLAlchemy session."""
        session = factory.session
        assert session.is_active

    def test_factory_clear_session_resets_context_var(self, factory):
        """After clear_session the ContextVar should be None."""
        _ = factory.session  # populate the context var
        factory.clear_session()
        assert _current_db_session.get() is None


class TestSessionScope:
    """Red-phase tests for session_scope lifecycle behavior."""

    def test_session_scope_creates_and_yields_session(self, factory):
        """session_scope should yield a SQLAlchemy Session instance."""
        with factory.session_scope() as session:
            assert isinstance(session, Session)

    def test_session_scope_binds_contextvar(self, factory):
        """session_scope should bind the yielded session to the ContextVar."""
        with factory.session_scope() as session:
            assert _current_db_session.get() is session

    def test_session_scope_commits_on_success(self, factory, db_engine):
        """session_scope should commit persisted data after a clean exit."""
        with factory.session_scope() as session:
            session.add(Item(name="session-scope-commit"))

        with Session(db_engine) as verify_session:
            result = verify_session.query(Item).filter_by(name="session-scope-commit").first()

        assert result is not None

    def test_session_scope_rollback_on_exception(self, factory, db_engine):
        """session_scope should roll back data when an exception escapes the block."""
        with pytest.raises(ValueError, match="boom"):
            with factory.session_scope() as session:
                session.add(Item(name="session-scope-rollback"))
                raise ValueError("boom")

        with Session(db_engine) as verify_session:
            result = verify_session.query(Item).filter_by(name="session-scope-rollback").first()

        assert result is None

    def test_session_scope_closes_session(self, factory):
        """session_scope should close the yielded session on exit."""
        with factory.session_scope() as session:
            scoped_session = session

        assert not scoped_session.in_transaction()

    def test_session_scope_resets_contextvar(self, factory):
        """session_scope should reset the ContextVar after exit."""
        with factory.session_scope():
            pass

        assert _current_db_session.get() is None

    def test_session_scope_raises_on_nesting(self, factory):
        """session_scope should reject nested scope entry when already bound."""
        with factory.session_scope():
            with pytest.raises(RuntimeError):
                with factory.session_scope():
                    pass

    def test_session_scope_nesting_guard_preserves_existing(self, factory, session_maker):
        """session_scope nesting guard should preserve the existing binding."""
        existing_session = session_maker()
        _current_db_session.set(existing_session)

        try:
            with pytest.raises(RuntimeError):
                with factory.session_scope():
                    pass

            assert _current_db_session.get() is existing_session
        finally:
            existing_session.close()
            _current_db_session.set(None)


class TestIndependentSession:
    """Red-phase tests for independent_session lifecycle behavior."""

    def test_independent_session_creates_and_yields(self, factory):
        """independent_session should yield a SQLAlchemy Session instance."""
        with factory.independent_session() as session:
            assert isinstance(session, Session)

    def test_independent_session_no_contextvar_bind(self, factory):
        """independent_session should not bind the yielded session to the ContextVar."""
        _current_db_session.set(None)

        with factory.independent_session() as session:
            assert isinstance(session, Session)
            assert _current_db_session.get() is None

    def test_independent_session_preserves_existing_binding(self, factory, session_maker):
        """independent_session should preserve any pre-existing ContextVar binding."""
        outer_session = session_maker()
        _current_db_session.set(outer_session)

        try:
            with factory.independent_session() as session:
                assert session is not outer_session
                assert _current_db_session.get() is outer_session

            assert _current_db_session.get() is outer_session
        finally:
            outer_session.close()
            _current_db_session.set(None)

    def test_independent_session_commits_on_success(self, factory, db_engine):
        """independent_session should commit persisted data after a clean exit."""
        with factory.independent_session() as session:
            session.add(Item(name="independent-session-commit"))

        with Session(db_engine) as verify_session:
            result = verify_session.query(Item).filter_by(name="independent-session-commit").first()

        assert result is not None

    def test_independent_session_rollback_on_exception(self, factory, db_engine):
        """independent_session should roll back data when an exception escapes the block."""
        with pytest.raises(ValueError, match="boom"):
            with factory.independent_session() as session:
                session.add(Item(name="independent-session-rollback"))
                raise ValueError("boom")

        with Session(db_engine) as verify_session:
            result = verify_session.query(Item).filter_by(name="independent-session-rollback").first()

        assert result is None

    def test_independent_session_closes_session(self, factory):
        """independent_session should close the yielded session on exit."""
        with factory.independent_session() as session:
            independent_session = session

        assert not independent_session.in_transaction()


class TestClearSessionFix:
    """Red-phase tests for the clear_session close behavior fix."""

    def test_clear_session_closes_session(self, factory, session_maker):
        """clear_session should close the previously bound session."""
        session = session_maker()
        factory.set_session(session)

        try:
            factory.clear_session()
            assert not session.in_transaction()
        finally:
            session.close()
            _current_db_session.set(None)

    def test_clear_session_clears_contextvar(self, factory, session_maker):
        """clear_session should clear the ContextVar binding."""
        session = session_maker()
        factory.set_session(session)

        try:
            factory.clear_session()
            assert _current_db_session.get() is None
        finally:
            session.close()
            _current_db_session.set(None)

    def test_clear_session_noop_when_no_session(self, factory):
        """clear_session should be a no-op when no session is currently bound."""
        _current_db_session.set(None)
        factory.clear_session()
        assert _current_db_session.get() is None


class TestDatabaseManagerProxy:
    """Red-phase tests for DatabaseManager session context helpers."""

    def test_db_session_scope_proxy(self, db_manager):
        """DatabaseManager.session_scope should proxy session_scope end-to-end."""
        Base.metadata.create_all(db_manager.engine)

        with db_manager.session_scope() as session:
            session.add(Item(name="db-manager-session-scope"))

        with Session(db_manager.engine) as verify_session:
            result = verify_session.query(Item).filter_by(name="db-manager-session-scope").first()

        assert result is not None

    def test_db_independent_session_proxy(self, db_manager):
        """DatabaseManager.independent_session should proxy independent_session end-to-end."""
        Base.metadata.create_all(db_manager.engine)

        with db_manager.independent_session() as session:
            session.add(Item(name="db-manager-independent-session"))

        with Session(db_manager.engine) as verify_session:
            result = verify_session.query(Item).filter_by(name="db-manager-independent-session").first()

        assert result is not None

    def test_db_session_scope_raises_not_configured(self):
        """Uninitialized DatabaseManager.session_scope should raise a configuration error."""
        manager = DatabaseManager()

        with pytest.raises(DatabaseNotConfiguredError):
            with manager.session_scope():
                pass


class TestBackwardCompatibility:
    """Coverage for currently supported session behavior that must keep passing."""

    def test_db_session_auto_vivifies(self, db_manager):
        """DatabaseManager.session should create and return a session on first access."""
        session = db_manager.session

        try:
            assert session is not None
            assert session.is_active
        finally:
            session.close()
            db_manager.clear_session()

    def test_session_factory_call_bypasses_cache(self, factory):
        """SessionFactory.__call__ should bypass the cached context-local session."""
        cached_session = factory.session
        fresh_session_one = factory()
        fresh_session_two = factory()

        try:
            assert fresh_session_one is not cached_session
            assert fresh_session_two is not cached_session
            assert fresh_session_two is not fresh_session_one
        finally:
            fresh_session_one.close()
            fresh_session_two.close()
            cached_session.close()
            _current_db_session.set(None)

    def test_set_session_and_clear_roundtrip(self, factory, session_maker):
        """set_session and clear_session should round-trip an explicit session binding."""
        mock_session = session_maker()

        try:
            factory.set_session(mock_session)
            assert factory.session is mock_session

            factory.clear_session()

            assert _current_db_session.get() is None
        finally:
            mock_session.close()
            _current_db_session.set(None)

