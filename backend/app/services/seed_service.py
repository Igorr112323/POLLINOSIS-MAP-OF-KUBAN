"""Reproducible simulated data, NOT KubGMU, Sentinel-2, or ERA5 observations."""
from datetime import date, timedelta
import math
import numpy as np
from sqlalchemy import select, func
from app.models.tables import Observation, WeeklyObservation, Hotspot
from app.core.geography import DISTRICTS
from app.core.water_mask import demo_water_exclusion
from shapely.geometry import shape

def seed(db):
    if db.scalar(select(func.count()).select_from(Observation)):
        return
    rng = np.random.default_rng(13579)
    rows = []
    for year in range(2023, 2027):
        d = date(year,1,1)
        while d.year == year:
            doy = d.timetuple().tm_yday
            temp = 14.5 + 12 * math.sin(2*math.pi*(doy-105)/365) + rng.normal(0,2.5)
            tmax, tmin = temp + 5, temp - 5
            # A modeled seasonal signal plus substantial day-to-day variability.
            peak = math.exp(-.5*((doy-242)/17)**2)
            start = 1/(1+math.exp(-max(-50,min(50,(doy-224)*1.0))))
            pollen = max(0, 440*peak*start * rng.lognormal(-.18,.60) + rng.normal(0, 2))
            o = Observation(date=d,pollen=round(pollen,2),tmax=round(tmax,2),tmin=round(tmin,2),dew=round(temp-rng.uniform(4,12),2),wind_speed=round(rng.uniform(1,5),2),wind_direction=float(rng.choice([45,90,135,315],p=[.35,.35,.1,.2])),precipitation=round(max(0,rng.exponential(1.2)-.7),2),radiation=round(max(0,160+100*math.sin(2*math.pi*(doy-80)/365)+rng.normal(0,20)),2),provenance='synthetic')
            rows.append(o); d += timedelta(days=1)
    db.add_all(rows)
    by_date = {r.date:r for r in rows}
    for year in range(2023,2027):
        # 18 summer/autumn weeks plus controls; 28 + 27*3 = 109.
        start = date(year,7,1)
        for i in range(-5, 23 if year == 2023 else 22):
            day = start + timedelta(days=7*i)
            values = [by_date[day+timedelta(days=j)].pollen for j in range(7)]
            db.add(WeeklyObservation(start_date=day, year=year, season_week=i+1 if 0 <= i < 18 else None, pollen=float(np.mean(values)),provenance='synthetic'))
    areas = rng.uniform(1.5,5,189); areas *= 589/areas.sum()
    # Illustrative locations, not mapped infestation boundaries. Squares are constructed
    # with local metric scaling so geometry agrees approximately with area.
    for i in range(189):
        # Northern inland envelopes avoid the Kuban river and reservoir at coarse scale.
        # These are synthetic squares, not verified land use or authoritative water masks.
        envelopes=[(45.105,45.130,38.915,38.965),(45.105,45.135,38.985,39.045),(45.075,45.100,39.065,39.110),(45.070,45.090,38.885,38.925)]
        lat_min,lat_max,lon_min,lon_max=envelopes[i%len(envelopes)]
        area = float(areas[i]); side = math.sqrt(area*10000)
        for _ in range(10000):
            lat=float(rng.uniform(lat_min,lat_max)); lon=float(rng.uniform(lon_min,lon_max))
            dy = side/111320/2; dx = side/(111320*math.cos(math.radians(lat)))/2
            geom = {'type':'Polygon','coordinates':[[[lon-dx,lat-dy],[lon+dx,lat-dy],[lon+dx,lat+dy],[lon-dx,lat+dy],[lon-dx,lat-dy]]]}
            if shape(geom).disjoint(demo_water_exclusion()): break
        else: raise RuntimeError('Could not generate a polygon outside the demo exclusion zone')
        b04 = float(rng.uniform(.08,.16)); b08 = float(rng.uniform(.35,.65))
        db.add(Hotspot(id=i+1,lat=lat,lon=lon,area_ha=area,geometry=geom,district=DISTRICTS[i%4],landmark=f'Условный участок {i+1}',land_type=['пустырь','промзона','полоса отвода','строительная площадка','пригород'][i%5],b02=float(rng.uniform(.04,.12)),b03=float(rng.uniform(.08,.16)),b04=b04,b08=b08,b11=float(rng.uniform(.15,.3)),status='detected',provenance='synthetic'))
    db.commit()
