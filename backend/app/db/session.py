from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.core.config import settings

class Base(DeclarativeBase):
    pass

def make_engine(url):
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg://' + url[len('postgresql://'):]
    if url.startswith('sqlite:///') and not url.endswith(':memory:'):
        Path(url[len('sqlite:///'):]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={'check_same_thread': False} if url.startswith('sqlite') else {}, pool_pre_ping=True)
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def configure(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute('PRAGMA journal_mode=WAL')
            cur.execute('PRAGMA foreign_keys=ON')
            cur.close()
    return engine

engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_db():
    with SessionLocal() as db:
        yield db


def migrate_stage2(engine):
    # Stage 1 installations already have these tables; create_all does not alter them.
    from sqlalchemy import inspect, text
    with engine.begin() as conn:
        inspector = inspect(conn)
        for table in ('observations', 'weekly_observations', 'hotspots'):
            if table in inspector.get_table_names() and 'data_provenance' not in {c['name'] for c in inspector.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN data_provenance VARCHAR NOT NULL DEFAULT 'synthetic_demo'"))
                if table == 'hotspots':
                    conn.execute(text("UPDATE hotspots SET data_provenance='uploaded_dataset' WHERE provenance LIKE 'uploaded_%'"))


def migrate_satellite(engine):
    """Add nullable observation metadata without changing existing datasets."""
    from sqlalchemy import inspect, text
    with engine.begin() as conn:
        inspector = inspect(conn)
        if 'hotspots' not in inspector.get_table_names():
            return
        columns = {c['name'] for c in inspector.get_columns('hotspots')}
        for name, sqltype in (('scene_id', 'VARCHAR'), ('source_key', 'VARCHAR'), ('observed_ndvi', 'FLOAT')):
            if name not in columns:
                conn.execute(text(f'ALTER TABLE hotspots ADD COLUMN {name} {sqltype}'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_hotspots_source_key ON hotspots (source_key)'))
        if 'weather_cache' in inspector.get_table_names() and 'source_origin' not in {c['name'] for c in inspector.get_columns('weather_cache')}:
            conn.execute(text('ALTER TABLE weather_cache ADD COLUMN source_origin VARCHAR'))
        if 'satellite_scenes' in inspector.get_table_names() and 'band_calibration' not in {c['name'] for c in inspector.get_columns('satellite_scenes')}:
            conn.execute(text('ALTER TABLE satellite_scenes ADD COLUMN band_calibration JSON'))
