from datetime import date, datetime
from sqlalchemy import Date, DateTime, Float, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base

class Observation(Base):
    __tablename__ = 'observations'
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    pollen: Mapped[float] = mapped_column(Float)
    tmax: Mapped[float] = mapped_column(Float)
    tmin: Mapped[float] = mapped_column(Float)
    dew: Mapped[float] = mapped_column(Float)
    wind_speed: Mapped[float] = mapped_column(Float)
    wind_direction: Mapped[float] = mapped_column(Float)
    precipitation: Mapped[float] = mapped_column(Float)
    radiation: Mapped[float] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(String, default='synthetic')
    data_provenance: Mapped[str] = mapped_column(String, default='synthetic_demo')

class WeeklyObservation(Base):
    __tablename__ = 'weekly_observations'
    start_date: Mapped[date] = mapped_column(Date, primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    season_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pollen: Mapped[float] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(String, default='synthetic')
    data_provenance: Mapped[str] = mapped_column(String, default='synthetic_demo')

class Hotspot(Base):
    __tablename__ = 'hotspots'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    area_ha: Mapped[float] = mapped_column(Float)
    geometry: Mapped[dict] = mapped_column(JSON)
    district: Mapped[str] = mapped_column(String)
    landmark: Mapped[str] = mapped_column(String)
    land_type: Mapped[str] = mapped_column(String)
    b02: Mapped[float] = mapped_column(Float)
    b03: Mapped[float] = mapped_column(Float)
    b04: Mapped[float] = mapped_column(Float)
    b08: Mapped[float] = mapped_column(Float)
    b11: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default='detected')
    mowing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default='synthetic')
    data_provenance: Mapped[str] = mapped_column(String, default='synthetic_demo')
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    observed_ndvi: Mapped[float | None] = mapped_column(Float, nullable=True)

class SatelliteScene(Base):
    __tablename__ = 'satellite_scenes'
    scene_id: Mapped[str] = mapped_column(String, primary_key=True)
    acquired_at: Mapped[str] = mapped_column(String)
    cloud_cover: Mapped[float] = mapped_column(Float)
    thumbnail: Mapped[str | None] = mapped_column(String, nullable=True)
    searched_at: Mapped[datetime] = mapped_column(DateTime)
    band_calibration: Mapped[dict | None] = mapped_column(JSON, nullable=True)

class WeatherCache(Base):
    __tablename__ = 'weather_cache'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime)

from sqlalchemy import Boolean, ForeignKey, Text

class ImportedSample(Base):
    __tablename__ = 'imported_samples'
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    granularity: Mapped[str] = mapped_column(String)
    pollen: Mapped[float] = mapped_column(Float)
    tmax: Mapped[float | None] = mapped_column(Float, nullable=True)
    tmin: Mapped[float | None] = mapped_column(Float, nullable=True)
    dew: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[str] = mapped_column(String, default='uploaded_csv')
    data_provenance: Mapped[str] = mapped_column(String, default='uploaded_dataset')

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class WorkOrder(Base):
    __tablename__ = 'work_orders'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotspot_id: Mapped[int] = mapped_column(ForeignKey('hotspots.id', ondelete='RESTRICT'), index=True)
    priority_rank: Mapped[int] = mapped_column(Integer)
    brigade_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    planned_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String, default='planned')
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    geofence_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    photo_before: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_after: Mapped[str | None] = mapped_column(String, nullable=True)
    area_mowed_ha: Mapped[float | None] = mapped_column(Float, nullable=True)
    completed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

class Clinic(Base):
    __tablename__ = 'clinics'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    district: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    cohort_estimate: Mapped[int] = mapped_column(Integer, default=0)
    provenance: Mapped[str] = mapped_column(String, default='synthetic_demo')

class PatientSubscription(Base):
    __tablename__ = 'patient_subscriptions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    clinic_id: Mapped[int | None] = mapped_column(ForeignKey('clinics.id'), nullable=True)
    name: Mapped[str] = mapped_column(String)
    neighborhood: Mapped[str] = mapped_column(String)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    clinical_profile: Mapped[str] = mapped_column(String)
    threshold: Mapped[float] = mapped_column(Float)
    lead_days: Mapped[int] = mapped_column(Integer)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class NotificationLog(Base):
    __tablename__ = 'notification_logs'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey('patient_subscriptions.id'))
    channel: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    threshold_date: Mapped[date] = mapped_column(Date)
    peak: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
