from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool
from config import settings


# NullPool: SQLite connections are file handles — cheap to open, no benefit from
# pooling. The default QueuePool(size=5, overflow=10) exhausted in production
# because long-running scoring Tasks hold a Session across many-second LLM
# awaits, queueing up new EPG-ingest tasks behind 15 idle-but-checked-out
# connections.
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
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
    """Add columns introduced after the initial schema; drop removed tables.
    Existing columns are detected via PRAGMA instead of swallowing the ALTER
    error, so genuine DDL failures (locked DB, disk full) stay visible."""
    new_columns = [
        ("receivers", "location",     "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("receivers", "priority",     "INTEGER NOT NULL DEFAULT 99"),
        ("receivers", "power_method", "VARCHAR(16)  NOT NULL DEFAULT 'none'"),
        ("receivers", "wol_mac",               "VARCHAR(32)"),
        ("receivers", "has_genre",             "BOOLEAN NOT NULL DEFAULT 0"),
        ("receivers",   "intertechno_family",  "VARCHAR(4)  NOT NULL DEFAULT ''"),
        ("receivers",   "intertechno_device",  "INTEGER NOT NULL DEFAULT 1"),
        ("receivers",   "intertechno_url",     "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("receivers",   "standby_newstate",    "INTEGER NOT NULL DEFAULT 4"),
        ("user_likes",  "sentiment",           "VARCHAR(8) NOT NULL DEFAULT 'like'"),
    ]
    with engine.connect() as conn:
        existing: dict[str, set[str]] = {}
        for table in {t for t, _, _ in new_columns}:
            existing[table] = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
        for table, col, definition in new_columns:
            if col in existing[table]:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))
        # recommendation_cache backed the LLM slate cache removed in 2.3 —
        # recs are served from user_event_scores now.
        conn.execute(text("DROP TABLE IF EXISTS recommendation_cache"))
        conn.commit()
