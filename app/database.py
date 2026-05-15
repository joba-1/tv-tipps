from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from config import settings


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401 — registers models with Base
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Add columns that were introduced after the initial schema."""
    new_columns = [
        ("receivers", "location",     "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("receivers", "priority",     "INTEGER NOT NULL DEFAULT 99"),
        ("receivers", "power_method", "VARCHAR(16)  NOT NULL DEFAULT 'none'"),
        ("receivers", "wol_mac",      "VARCHAR(32)"),
        ("receivers", "has_genre",    "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    with engine.connect() as conn:
        for table, col, definition in new_columns:
            try:
                conn.execute(__import__("sqlalchemy").text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {definition}"
                ))
                conn.commit()
            except Exception:
                pass  # column already exists
