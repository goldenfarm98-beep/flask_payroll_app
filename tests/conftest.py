import os
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL or not DATABASE_URL.startswith("postgres"):
    pytest.skip("DATABASE_URL PostgreSQL diperlukan untuk test", allow_module_level=True)

SCHEMA_NAME = f"test_schema_{uuid.uuid4().hex}"


@pytest.fixture(scope="session")
def app_instance():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    engine = sa.create_engine(DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{SCHEMA_NAME}"'))
        conn.commit()

    try:
        os.environ["DB_SEARCH_PATH"] = SCHEMA_NAME
        os.environ["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
        if "app" in sys.modules:
            del sys.modules["app"]

        from app import app, db

        app.config.update(TESTING=True)
        with app.app_context():
            db.create_all()

        yield app
    finally:
        if "app" in locals():
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        with engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA_NAME}" CASCADE'))
            conn.commit()


@pytest.fixture()
def client(app_instance):
    return app_instance.test_client()


@pytest.fixture(autouse=True)
def clean_db(app_instance):
    from app import db

    with app_instance.app_context():
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()
    yield
