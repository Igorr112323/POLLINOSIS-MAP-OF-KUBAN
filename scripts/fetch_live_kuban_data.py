#!/usr/bin/env python3
"""Fetch public source snapshots; fail closed, never write synthetic substitutes.

The resulting JSON/GeoJSON files are designed for GitHub Actions artifacts and
can be imported with scripts/import_live_kuban_data.py. This script never
writes to the application database.
"""
import argparse
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
import httpx
from shapely.geometry import mapping
from app.services.satellite_service import BBOX, SatelliteError, find_scene, osm_parcels, read_scene, classify_parcels

FORECAST='https://api.open-meteo.com/v1/forecast'
ARCHIVE='https://archive-api.open-meteo.com/v1/archive'
LAT,LON=45.035,38.975
DAILY=('temperature_2m_max','temperature_2m_min','precipitation_sum','wind_speed_10m_max','wind_direction_10m_dominant','shortwave_radiation_sum')
HOURLY=('dew_point_2m',)

def _http_get(url, params):
    failure=None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=httpx.Timeout(90,connect=15),follow_redirects=False) as client:
                response=client.get(url,params=params); response.raise_for_status()
                if len(response.content)>10_000_000: raise ValueError('Open-Meteo response exceeds 10 MB')
                return response.json()
        except (httpx.HTTPError, ValueError) as e:
            failure=e
            if attempt<2:time.sleep(2**attempt)
    raise RuntimeError(f'Open-Meteo request failed: {type(failure).__name__}: {failure}') from failure

def _days(payload, expected_dates):
    """Validate provider values and convert them to the app's 14-day format."""
    daily=payload['daily']; hourly=payload['hourly']
    if daily['time']!=expected_dates: raise ValueError('Open-Meteo returned unexpected dates')
    times=hourly['time']; dew=hourly['dew_point_2m']
    if len(times)!=len(dew): raise ValueError('Hourly dew-point length mismatch')
    if any(len(daily[k])!=len(expected_dates) for k in DAILY): raise ValueError('Daily field length mismatch')
    result=[]
    for i,day in enumerate(expected_dates):
        samples=[float(v) for t,v in zip(times,dew) if t.startswith(day) and v is not None]
        values=[daily[k][i] for k in DAILY]
        if not samples or any(v is None or not math.isfinite(float(v)) for v in values) or any(not math.isfinite(v) for v in samples):
            raise ValueError(f'Incomplete or invalid weather on {day}')
        result.append({'date':day,'tmax':float(values[0]),'tmin':float(values[1]),'precipitation':float(values[2]),'wind_speed':float(values[3]),'wind_direction':float(values[4]),'radiation':float(values[5]),'dew':sum(samples)/len(samples)})
    return result

def fetch_weather(today=None):
    today=today or datetime.now(ZoneInfo('Europe/Moscow')).date()
    # Use the latest completed Jul-Aug season, leaving time for ERA5 publication.
    year=today.year if today>=date(today.year,9,7) else today.year-1
    start,end=date(year,7,1),date(year,8,31)
    common={'latitude':LAT,'longitude':LON,'daily':','.join(DAILY),'hourly':','.join(HOURLY),'timezone':'Europe/Moscow','wind_speed_unit':'ms'}
    forecast_params={**common,'start_date':today.isoformat(),'end_date':(today+timedelta(days=13)).isoformat()}
    archive_params={**common,'start_date':start.isoformat(),'end_date':end.isoformat(),'models':'era5'}
    forecast=_http_get(FORECAST,forecast_params); archive=_http_get(ARCHIVE,archive_params)
    forecast_dates=[(today+timedelta(days=i)).isoformat() for i in range(14)]
    archive_dates=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    return {'schema_version':1,'retrieved_at':datetime.now(timezone.utc).isoformat(),'coordinates':{'latitude':LAT,'longitude':LON},'timezone':'Europe/Moscow',
        'forecast':{'source':'open-meteo-forecast','endpoint':FORECAST,'parameters':forecast_params,'provider_response':forecast,'days':_days(forecast,forecast_dates)},
        'archive':{'source':'open-meteo-era5','endpoint':ARCHIVE,'parameters':archive_params,'provider_response':archive,'days':_days(archive,archive_dates)},
        'note':'Прогноз и ERA5 — модельные данные Open-Meteo, а не непосредственные измерения на месте.'}

def fetch_satellite():
    scene,urls=find_scene(); parcels,osm_endpoint=osm_parcels(return_endpoint=True); arrays,trans,crs=read_scene(urls)
    candidates=classify_parcels(parcels,arrays,trans,crs,scene['calibration'])
    features=[]
    for c in candidates:
        features.append({'type':'Feature','geometry':mapping(c['geometry']),'properties':{
            'osm_id':c['osm_id'],'land_type':c['land_type'],'name':c['name'],'area_ha':c['area_ha'],'ndvi':c['ndvi'],
            'bands':{'B04':c['b04'],'B08':c['b08'],'B11':c['b11']},'valid_pixels':c['valid_pixels'],
            'scene_id':scene['scene_id'],'data_provenance':'sentinel2_osm_live'}})
    return {'type':'FeatureCollection','schema_version':1,'retrieved_at':datetime.now(timezone.utc).isoformat(),
        'scene':scene,'asset_urls':urls,'bbox':list(BBOX),'osm_parcels':len(parcels),'features':features,
        'osm_endpoint':osm_endpoint,'sources':{'geometry':'OpenStreetMap via Overpass API; © OpenStreetMap contributors, ODbL','imagery':'Copernicus Sentinel-2 L2A via Earth Search'},
        'note':'NDVI показывает растительность; вид растения и присутствие амброзии не установлены. Нужна полевая проверка.'}

def atomic_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(path)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'backend/data/real_snapshots')
    args=parser.parse_args(); out=args.output_dir;out.mkdir(parents=True,exist_ok=True)
    tasks=[('sentinel2_osm','krasnodar_sentinel2_hotspots.geojson',fetch_satellite),('openmeteo','krasnodar_openmeteo.json',fetch_weather)]
    manifest={'schema_version':1,'started_at':datetime.now(timezone.utc).isoformat(),'results':{}}
    for key,name,task in tasks:
        # Never leave an old successful file masquerading as this run's output.
        destination=out/name
        if destination.exists():destination.unlink()
        try:
            data=task();atomic_json(destination,data)
            manifest['results'][key]={'status':'success','file':name,'features':len(data['features']) if 'features' in data else None}
            print(f'{key}: success → {destination}',flush=True)
        except Exception as e:
            manifest['results'][key]={'status':'error','error':f'{type(e).__name__}: {str(e)[:500]}'}
            print(f'{key}: {manifest["results"][key]["error"]}',file=sys.stderr,flush=True)
    manifest['finished_at']=datetime.now(timezone.utc).isoformat();atomic_json(out/'manifest.json',manifest)
    return 0 if all(x['status']=='success' for x in manifest['results'].values()) else 1
if __name__=='__main__':sys.exit(main())
