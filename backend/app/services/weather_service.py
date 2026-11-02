from datetime import date, datetime, timedelta, timezone
import math
import httpx
from sqlalchemy import select
from app.core.config import settings
from app.core.geography import MUNICIPALITIES
from app.models.tables import Observation, WeatherCache

async def weather(db, municipality, mode, target):
    if municipality not in MUNICIPALITIES:
        raise ValueError('Неизвестный муниципалитет')
    end = target + timedelta(days=13)
    lat, lon = MUNICIPALITIES[municipality]
    key = f'{municipality}:{mode}:{target}'
    cached = db.scalar(select(WeatherCache).where(WeatherCache.key == key))
    if not settings.offline:
        url = settings.forecast_url if mode == 'live' else settings.archive_url
        params = {'latitude':lat,'longitude':lon,'start_date':target.isoformat(),'end_date':end.isoformat(), 'daily':'temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,wind_direction_10m_dominant,shortwave_radiation_sum', 'hourly':'temperature_2m,dew_point_2m,shortwave_radiation,precipitation,wind_speed_10m,wind_direction_10m', 'timezone':'Europe/Moscow','wind_speed_unit':'ms'}
        if mode == 'live':
            params['forecast_days'] = 14
        try:
            async with httpx.AsyncClient(timeout=3.5) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                daily = payload['daily']; hourly = payload['hourly']
                days = []
                for i, ds in enumerate(daily['time']):
                    values = [v for j,v in enumerate(hourly['dew_point_2m']) if hourly['time'][j].startswith(ds) and v is not None]
                    days.append({'date':ds,'tmax':daily['temperature_2m_max'][i], 'tmin':daily['temperature_2m_min'][i], 'dew':sum(values)/len(values) if values else None, 'wind_speed':daily['wind_speed_10m_max'][i], 'wind_direction':daily['wind_direction_10m_dominant'][i], 'precipitation':daily['precipitation_sum'][i], 'radiation':daily['shortwave_radiation_sum'][i]})
                if len(days) != 14 or any(any(day[k] is None for k in ('tmax','tmin','dew','wind_speed','wind_direction')) for day in days):
                    raise ValueError('incomplete weather response')
                if cached: cached.payload=days; cached.updated_at=datetime.now(timezone.utc)
                else: db.add(WeatherCache(key=key,payload=days,updated_at=datetime.now(timezone.utc)))
                db.commit()
                return finish(db,days,'open-meteo-live',target,municipality)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            db.rollback()
    if cached:
        return finish(db,cached.payload,'open-meteo-cache',target,municipality)
    days=[]
    for i in range(14):
        d=target+timedelta(days=i)
        # For other years, use the corresponding day of a synthetic reference year.
        try: ref=d.replace(year=2024)
        except ValueError: ref=date(2024,2,28)
        obs=db.get(Observation,d) or db.get(Observation,ref)
        days.append({'date':d.isoformat(),'tmax':obs.tmax,'tmin':obs.tmin,'dew':obs.dew,'wind_speed':obs.wind_speed,'wind_direction':obs.wind_direction,'precipitation':obs.precipitation,'radiation':obs.radiation})
    return finish(db,days,'synthetic-climatology',target,municipality)

def finish(db, days, source, target, municipality):
    # The historical thermal sum is modeled, not live weather; this is made explicit.
    refyear = target.year if 2023 <= target.year <= 2026 else 2024
    try: refdate=target.replace(year=refyear)
    except ValueError: refdate=date(refyear,2,28)
    start=date(refyear,4,1)
    gdd=sum(max(0,(o.tmax+o.tmin)/2-10) for o in db.scalars(select(Observation).where(Observation.date>=start,Observation.date<=refdate))) if refdate >= start else 0
    rose={str(k):0 for k in range(0,360,45)}
    for day in days: rose[str(int((day['wind_direction']+22.5)//45*45)%360)]+=1
    return {'municipality':municipality,'source':source,'gdd_source':'synthetic-climatology','days':days,'gdd_10':round(gdd,2),'wind_rose':rose}
