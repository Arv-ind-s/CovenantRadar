"""Fixtures giving each integration test its own isolated transaction
against the PostgreSQL instance CI supplies (`RADAR_TEST_DATABASE_URL`),
so tests never see one another's writes and never depend on run order.

Uses SQLAlchemy's own documented recipe for joining a session to an
external transaction: the fixture opens one connection and one outer
transaction per test and always rolls that outer transaction back
afterwards. Every session handed to a test — including the fresh session
`SqlAlchemyUnitOfWork` opens on each `__enter__` — begins its own
`SAVEPOINT` nested inside that outer transaction, and restarts it
automatically the moment it ends, so a real `session.commit()` inside the
code under test only ends the `SAVEPOINT` and never touches the outer
transaction the fixture rolls back at teardown.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.session import SessionTransaction
from sqlalchemy.schema import CreateSchema, DropSchema

from covenant_radar.db.base import Base

_DATABASE_URL_ENV = "RADAR_TEST_DATABASE_URL"


def _required_database_url() -> str:
    database_url = os.environ.get(_DATABASE_URL_ENV)
    if not database_url:
        pytest.fail(f"{_DATABASE_URL_ENV} is required for integration tests.")
    return database_url


@pytest.fixture(scope="module")
def database_engine() -> Iterator[Engine]:
    """Use a private schema so ORM fixtures cannot alter migration-test tables."""
    database_url = _required_database_url()
    schema = f"test_integration_{uuid4().hex}"
    admin = create_engine(database_url, pool_pre_ping=True)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(
        database_url, pool_pre_ping=True, connect_args={"options": f"-csearch_path={schema}"}
    )
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def db_connection(database_engine: Engine) -> Iterator[Connection]:
    """One connection and one outer transaction for a single test, always
    rolled back at the end regardless of what the test did with it."""
    connection = database_engine.connect()
    outer_transaction = connection.begin()
    try:
        yield connection
    finally:
        outer_transaction.rollback()
        connection.close()


@pytest.fixture
def db_session_factory(db_connection: Connection) -> Callable[[], Session]:
    """A session factory bound to `db_connection`.

    Every session it produces opens its own `SAVEPOINT` and restarts it as
    soon as it ends, so code such as `SqlAlchemyUnitOfWork` — which opens a
    fresh session per `__enter__` and calls `session.commit()` on a clean
    exit — can commit, and even open a second unit of work in the same
    test, without ever reaching the connection's outer transaction.
    """
    session_factory = sessionmaker(bind=db_connection, autoflush=False, expire_on_commit=False)

    def make_session() -> Session:
        session = session_factory()
        nested_transaction = db_connection.begin_nested()

        @event.listens_for(session, "after_transaction_end")
        def _restart_savepoint(_session: Session, _transaction: SessionTransaction) -> None:
            nonlocal nested_transaction
            if not nested_transaction.is_active:
                nested_transaction = db_connection.begin_nested()

        return session

    return make_session


@pytest.fixture
def db_session(db_session_factory: Callable[[], Session]) -> Iterator[Session]:
    """One isolated session, for a test that reads and writes directly
    rather than going through a `UnitOfWork`."""
    session = db_session_factory()
    try:
        yield session
    finally:
        session.close()
