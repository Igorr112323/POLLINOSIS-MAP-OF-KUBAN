#!/usr/bin/env python3
"""Validate and add a GitHub Actions snapshot to the local DB; never replace demo data.

Use --apply to modify the configured database. Files are operator-supplied;
structural validation is not proof of their authenticity.
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
INITIAL_CWD=Path.cwd()
os.chdir(ROOT/'backend')
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT/'scripts'))
from sqlalchemy import select
from shapely.geometry import shape, box
from app.db.session import SessionLocal, engine, Base, migrate_stage2, migrate_satellite
from app.models.tables import WeatherCache, Hotspot, WorkOrder, Observation
from app.services.seed_service import seed
from app.services.satellite_service import BBOX, MAX_CANDIDATES, _area_ha, _trusted_url, COG_HOSTS, THUMB_HOSTS, SatelliteError, save_candidates
from fetch_live_kuban_data import _days, FORECAST, ARCHIVE, LAT, LON

def _timestamp(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('В файле нет часового пояса retrieved_at')
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def _finite(value,low,high,name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError(f'Некорректно: {name}')
    return float(value)

def validate_satellite(doc):
    version=doc.get('schema_version')
    if doc.get('type')!='FeatureCollection' or version not in (1,2) or doc.get('bbox')!=list(BBOX):raise ValueError('Неверный формат спутникового снимка')
    _timestamp(doc['retrieved_at']);scene=dict(doc['scene']);sid=scene['scene_id']
    scene['snapshot_origin']='sentinel2_raster_live' if version==2 else 'sentinel2_osm_live'
    if not isinstance(sid,str) or not 1<=len(sid)<=250:raise ValueError('Некорректный scene_id')
    datetime.fromisoformat(scene['acquired_at'].replace('Z','+00:00'))
    _finite(scene['cloud_cover'],0,14.999999 if version==2 else 19.999999,'cloud_cover')
    if scene.get('coverage_pct') is not None:_finite(scene['coverage_pct'],0,100,'coverage_pct')
    if scene.get('thumbnail'):_trusted_url(scene['thumbnail'],THUMB_HOSTS)
    for key in ('red','nir','swir16','scl'):_trusted_url(doc['asset_urls'][key],COG_HOSTS)
    for key in ('red','nir','swir16'):
        _finite(scene['calibration'][key]['scale'],1e-12,1,'scale');_finite(scene['calibration'][key]['offset'],-1,1,'offset')
    features=doc['features']
    if not isinstance(features,list) or len(features)>MAX_CANDIDATES:raise ValueError('Слишком много объектов')
    entries=[];seen=set();bounds=box(*BBOX)
    for f in features:
        p=f['properties'];geo=shape(f['geometry']);osm=p['candidate_id'] if version==2 else p['osm_id'];bands=p['bands']
        if f.get('type')!='Feature' or geo.geom_type!='Polygon' or not geo.is_valid or geo.is_empty or not bounds.covers(geo):raise ValueError('Некорректный полигон OSM')
        if not isinstance(osm,str) or not re.fullmatch(r'raster/[0-9a-f]{24}' if version==2 else r'(way|relation)/[1-9][0-9]*/[0-9]+',osm) or osm in seen:raise ValueError('Некорректный или повторный OSM ID')
        seen.add(osm)
        if p['scene_id']!=sid or p['data_provenance']!=scene['snapshot_origin'] or (version==2 and p.get('acquired_at')!=scene['acquired_at']):raise ValueError('Источники сцены и объекта не совпадают')
        area=_finite(p['area_ha'],.3 if version==2 else .2,25,'area_ha'); real_area=_area_ha(geo)
        if abs(area-real_area)>max(.001,real_area*.001):raise ValueError('Площадь не соответствует геометрии')
        ndvi=_finite(p['ndvi'],.48 if version==2 else .38,1,'ndvi');b04=_finite(bands['B04'],0,1,'B04');b08=_finite(bands['B08'],0,1,'B08');b11=_finite(bands['B11'],0,1,'B11')
        if version==2:_finite(p['ndmi'],-.1,.6,'ndmi')
        if not isinstance(p['valid_pixels'],int) or isinstance(p['valid_pixels'],bool) or p['valid_pixels']<1:raise ValueError('Некорректное число пикселей')
        if not isinstance(p['name'],str) or not isinstance(p['land_type'],str):raise ValueError('Некорректные метаданные участка')
        entries.append({'geometry':geo,'osm_id':osm,'area_ha':area,'name':p['name'][:200],'land_type':p['land_type'][:100],'ndvi':ndvi,'b04':b04,'b08':b08,'b11':b11,'valid_pixels':p['valid_pixels']})
    return scene,entries

def validate_water(doc, satellite_doc):
    if doc.get('type')!='FeatureCollection' or doc.get('schema_version')!=1 or doc.get('bbox')!=list(BBOX) or doc.get('scene_id')!=satellite_doc['scene']['scene_id'] or doc.get('retrieved_at')!=satellite_doc['retrieved_at']:
        raise ValueError('Водная маска не соответствует спутниковому снимку')
    features=doc['features']
    if not isinstance(features,list) or len(features)>100000:raise ValueError('Некорректная водная маска')
    bounds=box(*BBOX);polygons=[]
    for f in features:
        g=shape(f['geometry'])
        if f.get('type')!='Feature' or g.geom_type!='Polygon' or g.is_empty or not g.is_valid or not bounds.covers(g):raise ValueError('Некорректная геометрия водной маски')
        polygons.append(g)
    return polygons

def remove_unreserved_demo_over_water(db,water):
    if not water:return {'removed':0,'protected':0}
    from shapely.strtree import STRtree
    tree=STRtree(water);removed=protected=0
    for h in db.scalars(select(Hotspot).where(Hotspot.data_provenance=='synthetic_demo')).all():
        g=shape(h.geometry)
        if not any(g.intersection(water[i]).area>1e-14 for i in tree.query(g,predicate='intersects')):continue
        if h.status!='detected' or h.mowing_date is not None or db.scalar(select(WorkOrder.id).where(WorkOrder.hotspot_id==h.id).limit(1)) is not None:
            protected+=1;continue
        db.delete(h);removed+=1
    return {'removed':removed,'protected':protected}

def validate_weather(doc):
    if doc.get('schema_version')!=1 or doc.get('coordinates')!={'latitude':LAT,'longitude':LON} or doc.get('timezone')!='Europe/Moscow':raise ValueError('Неверный формат или координаты Open-Meteo')
    retrieved=_timestamp(doc['retrieved_at']);windows=[]
    for key,endpoint,source,mode in (('forecast',FORECAST,'open-meteo-forecast','live'),('archive',ARCHIVE,'open-meteo-era5','simulation')):
        part=doc[key];params=part['parameters']
        if part['source']!=source or part['endpoint']!=endpoint or params.get('latitude')!=LAT or params.get('longitude')!=LON or params.get('timezone')!='Europe/Moscow' or params.get('wind_speed_unit')!='ms':raise ValueError('Не совпадают параметры источника погоды')
        if key=='archive' and params.get('models')!='era5':raise ValueError('Архив не обозначен как ERA5')
        start=date.fromisoformat(params['start_date']);end=date.fromisoformat(params['end_date']);count=(end-start).days+1
        if count<14 or count>100 or (key=='forecast' and count!=14):raise ValueError('Недопустимый диапазон погоды')
        dates=[(start+timedelta(days=i)).isoformat() for i in range(count)]
        days=_days(part['provider_response'],dates)
        if days!=part['days']:raise ValueError('Нормализованные данные погоды не совпадают с ответом провайдера')
        # Each cache entry must contain 14 consecutive days, as required by the API.
        for i in range(count-13):windows.append((f'Краснодар:{mode}:{dates[i]}',days[i:i+14],source,retrieved))
    return windows

def import_data(db,scene,candidates,weather_windows,water=None):
    try:
        result=save_candidates(db,candidates,scene.get('snapshot_origin','sentinel2_osm_live'),scene,commit=False) if scene is not None else None
        cleanup=remove_unreserved_demo_over_water(db,water or [])
        added=updated=skipped=0
        for key,days,origin,retrieved in weather_windows:
            cached=db.scalar(select(WeatherCache).where(WeatherCache.key==key))
            if cached:
                existing=cached.updated_at
                if existing.tzinfo:existing=existing.astimezone(timezone.utc).replace(tzinfo=None)
                if existing>=retrieved:skipped+=1;continue
                cached.payload=days;cached.updated_at=retrieved;cached.source_origin=origin;updated+=1
            else:
                db.add(WeatherCache(key=key,payload=days,updated_at=retrieved,source_origin=origin));added+=1
        db.commit()
        return {'satellite':result,'demo_water_cleanup':cleanup,'weather_cache':{'inserted':added,'updated':updated,'newer_existing_skipped':skipped}}
    except Exception:
        db.rollback();raise

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('snapshot_dir',type=Path);p.add_argument('--apply',action='store_true',help='Actually write to DATABASE_URL');p.add_argument('--allow-partial',action='store_true',help='Import successful source even if the other is absent')
    args=p.parse_args();folder=(INITIAL_CWD/args.snapshot_dir).resolve()
    try:
        docs=[]
        for name in ('krasnodar_sentinel2_hotspots.geojson','krasnodar_openmeteo.json'):
            path=folder/name
            if not path.exists() and args.allow_partial:
                docs.append(None);continue
            if path.stat().st_size>30_000_000:raise ValueError('Файл слишком большой: '+name)
            docs.append(json.loads(path.read_text(encoding='utf-8')))
        if all(doc is None for doc in docs):raise ValueError('Не найдено ни одного файла снимка')
        scene,candidates=validate_satellite(docs[0]) if docs[0] is not None else (None,[])
        windows=validate_weather(docs[1]) if docs[1] is not None else []
        water=[]
        if docs[0] is not None and docs[0].get('schema_version')==2:
            path=folder/'krasnodar_sentinel2_water_mask.geojson'
            if path.stat().st_size>30_000_000:raise ValueError('Файл водной маски слишком большой')
            water=validate_water(json.loads(path.read_text(encoding='utf-8')),docs[0])
            if water:
                from shapely.strtree import STRtree
                tree=STRtree(water)
                for c in candidates:
                    if any(c['geometry'].intersection(water[i]).area>1e-14 for i in tree.query(c['geometry'],predicate='intersects')):
                        raise ValueError('Спутниковый кандидат пересекает маску SCL 6')
        print(f'Проверены файлы: сцена {scene["scene_id"] if scene else "нет"}, участков {len(candidates)}, погодных окон {len(windows)}. Подлинность файлов структурная проверка не подтверждает.')
        if not args.apply:print('Без --apply база не изменена.');return 0
        migrate_stage2(engine);migrate_satellite(engine);Base.metadata.create_all(engine)
        with SessionLocal() as db:
            # A new installation needs demo observations before the app starts;
            # do not overwrite an existing operator hotspot database.
            if db.scalar(select(Observation.date).limit(1)) is None and db.scalar(select(Hotspot.id).limit(1)) is None:seed(db)
            result=import_data(db,scene,candidates,windows,water)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,TypeError,KeyError,SatelliteError) as e:
        print(f'Импорт отклонён: {type(e).__name__}: {e}',file=sys.stderr);return 1
if __name__=='__main__':sys.exit(main())
