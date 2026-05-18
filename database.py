import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

SUPABASE_DATABASE_URL = os.environ["SUPABASE_DATABASE_URL"]

engine = create_engine(SUPABASE_DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from models import sanctions  # noqa: F401 — registers tables with Base
    Base.metadata.create_all(bind=engine)
    print("Database initialised.")


if __name__ == "__main__":
    init_db()