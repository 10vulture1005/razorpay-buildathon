from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.config as config
from app.config import DATABASE_URL
from app.db.tables import Base

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    future=True,
    # SQLite doesn't support pool tuning; Postgres needs it under load.
    **({} if _is_sqlite else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,   # detects stale connections after Postgres restart
        "pool_recycle": 1800,    # recycle connections every 30 min
    }),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
