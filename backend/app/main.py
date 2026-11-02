from contextlib import asynccontextmanager
from app.core.config import settings
from app.api.v1 import auth, datasets, work_orders, clinical, weather_sync, alerts, integrations
from app.services.auth_service import seed_users
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.routes import router
from app.db.session import Base, engine, SessionLocal, migrate_stage2
from app.services.seed_service import seed
from app.services.phenology_ml_service import initialize

@asynccontextmanager
async def lifespan(app:FastAPI):
    migrate_stage2(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
        seed_users(db)
        clinical.seed_clinics(db)
        app.state.model=initialize(db)
    yield
    engine.dispose()

app=FastAPI(title='ПОЛЛИНОЗ-КАРТА КУБАНИ API',version='0.1.0',lifespan=lifespan,description='Экспериментальный backend: встроенные данные синтетические; операторские файлы не проверяются как реальные. Система не валидирована для принятия решений.')
app.add_middleware(CORSMiddleware,allow_origins=[x.strip() for x in settings.cors_allowed_origins.split(',') if x.strip()],allow_methods=['GET','POST','PATCH'],allow_headers=['Authorization','Content-Type','X-API-Key'])
app.include_router(router,prefix='/api/v1')

for r in (auth.router, datasets.router, work_orders.router, clinical.router, weather_sync.router, alerts.router, integrations.router):
    app.include_router(r,prefix='/api/v1')
