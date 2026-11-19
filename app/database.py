from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def make_database(url: str):
    options = {"check_same_thread": False, "timeout": 20} if url.startswith("sqlite:") else {}
    engine = create_engine(url, connect_args=options, pool_pre_ping=True)
    return engine, sessionmaker(engine, expire_on_commit=False)
