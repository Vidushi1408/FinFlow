from sqlalchemy.orm import sessionmaker

from etl.load.db import get_engine

engine = get_engine(pool_size=10, max_overflow=20, pool_timeout=30, pool_recycle=1800)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
