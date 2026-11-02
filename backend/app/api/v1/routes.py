from datetime import date,timedelta
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.core.config import settings
from app.core.geography import DISTRICTS, NEIGHBORHOODS, MUNICIPALITIES
from app.models.tables import Hotspot, Observation, WeeklyObservation, WeatherCache, ImportedSample
from app.schemas.contracts import OptimizerRequest, FeatureCollection
from app.services.weather_service import weather
from app.services.phenology_ml_service import feature,predict,validation
from app.services.gaussian_plume_service import calculate,risk
from app.services.mowing_optimizer_service import rank,optimize

router=APIRouter()
Mode=Literal['live','simulation']
Scenario=Literal['current','no_mowing','optimal_489ha','late_mowing_august_burst']

def spots(db): return list(db.scalars(select(Hotspot).order_by(Hotspot.id)))

def day_for(mode, target_date):
    d=target_date or date.today()
    if mode=='simulation' and not 2023<=d.year<=2026:
        raise HTTPException(422,'Для симуляции доступны 2023–2026 годы')
    return d

async def get_weather(db, municipality, mode, target):
    try: return await weather(db,municipality,mode,target)
    except ValueError as e: raise HTTPException(422,str(e)) from e

@router.get('/health')
def health(request:Request,db:Session=Depends(get_db)):
    db.execute(text('SELECT 1'))
    return {'status':'ok','database':'ok','weather_cache_entries':db.scalar(select(func.count()).select_from(WeatherCache)),'model_loaded':bool(getattr(request.app.state,'model',None)),'hotspots':db.scalar(select(func.count()).select_from(Hotspot)),'weekly_observations':db.scalar(select(func.count()).select_from(WeeklyObservation)),'daily_observations':db.scalar(select(func.count()).select_from(Observation)),'data_provenance':request.app.state.model['data_provenance'],'uploaded_observation_records':db.scalar(select(func.count()).select_from(ImportedSample))}

@router.get('/system/passport')
def passport():
    return {'name':'ПОЛЛИНОЗ-КАРТА КУБАНИ','primary_area':'МО город Краснодар','reference_point':{'name':'КубГМУ, ул. им. Митрофана Седина, 4','latitude':45.0185,'longitude':38.9723},'districts':DISTRICTS,'neighborhoods':[{'name':n[0],'district':n[1],'latitude':n[2],'longitude':n[3],'population_estimate':n[4]} for n in NEIGHBORHOODS],'municipalities':[{'name':k,'latitude':v[0],'longitude':v[1]} for k,v in MUNICIPALITIES.items()],'clinical_threshold_grains_m3':settings.clinical_threshold,'population_note':'Координаты и численность микрорайонов приблизительные; требуется сверка.'}

@router.get('/weather/forecast')
async def weather_forecast(municipality:str='Краснодар',mode:Mode='live',target_date:date|None=None,db:Session=Depends(get_db)):
    return await get_weather(db,municipality,mode,day_for(mode,target_date))

@router.get('/gis/hotspots',response_model=FeatureCollection)
def hotspots(district:str|None=None,status:Literal['detected','assigned','in_progress','mowed_verified','completed_late_bloom']|None=None,min_risk:Annotated[float,Query(ge=0)]=0,db:Session=Depends(get_db)):
    allspots=spots(db); scores={r['id']:r for r in rank(allspots)}; features=[]
    for h in allspots:
        r=scores[h.id]
        if district and district!=h.district or status and status!=h.status or r['risk_index']<min_risk: continue
        features.append({'type':'Feature','geometry':h.geometry,'properties':{'id':h.id,'area_ha':h.area_ha,'district':h.district,'landmark':h.landmark,'land_type':h.land_type,'bands':({'B02':None,'B03':None,'B04':None,'B08':None,'B11':None} if h.provenance.endswith('ndvi_only') else {'B02':None if '_missing_b02' in h.provenance else h.b02,'B03':None if '_missing_b03' in h.provenance else h.b03,'B04':h.b04,'B08':h.b08,'B11':None if '_missing_b11' in h.provenance else h.b11}),**r,'status':h.status,'mowing_date':h.mowing_date.isoformat() if h.mowing_date else None,'provenance':h.provenance,'data_provenance':h.data_provenance,'source_format':'sentinel2_l2a_osm' if h.data_provenance=='sentinel2_osm_live' else 'uploaded_geotiff' if h.data_provenance=='uploaded_geotiff' else 'uploaded_geojson' if h.provenance.startswith('uploaded_geojson') else 'synthetic_demo','scene_id':h.scene_id,'source_key':h.source_key,'bands_note':'B04/B08 reconstructed from NDVI; B02/B03/B11 unavailable' if h.provenance.endswith('ndvi_only') else 'Missing optional bands are null' }})
    return {'type':'FeatureCollection','features':features}

@router.get('/gis/plume-grid')
async def plume_grid(mode:Mode='live',target_date:date|None=None,wind_speed_override:Annotated[float|None,Query(ge=.1,le=60)]=None,wind_dir_override:Annotated[float|None,Query(ge=0,le=360)]=None,scenario:Scenario='current',db:Session=Depends(get_db)):
    target=day_for(mode,target_date); w=await get_weather(db,'Краснодар',mode,target); day=w['days'][0]
    # Seasonality is a simple modeled emission curve, not pollen measurements.
    import math
    doy=target.timetuple().tm_yday
    phenology=math.exp(-.5*((doy-242)/19)**2)/(1+math.exp(-max(-50,min(50,doy-224))))
    result=calculate(spots(db),target,wind_speed_override if wind_speed_override is not None else day['wind_speed'],wind_dir_override if wind_dir_override is not None else day['wind_direction'],scenario,phenology)
    result['summary'].update({'hotspot_data_provenance':sorted({h.data_provenance for h in spots(db)}),'date':target.isoformat(),'scenario':scenario,'weather_source':w['source'],'phenology_factor':phenology})
    return result

@router.get('/gis/point-forecast')
async def point_forecast(request:Request,latitude:Annotated[float,Query(ge=44.85,le=45.25)],longitude:Annotated[float,Query(ge=38.65,le=39.30)],mode:Mode='live',target_date:date|None=None,scenario:Scenario='current',db:Session=Depends(get_db)):
    """Illustrative city-model background plus local plume, evaluated at a clicked coordinate."""
    import math
    import numpy as np
    from app.services.gaussian_plume_service import LAT,LON,source_vectors,plume_at
    target=day_for(mode,target_date); w=await get_weather(db,'Краснодар',mode,target); allspots=spots(db)
    gdd=w['gdd_10']; features=[]
    for i,day in enumerate(w['days']):
        d=date.fromisoformat(day['date'])
        if i and d.month>=4: gdd+=max(0,(day['tmax']+day['tmin'])/2-10)
        features.append(feature(d,day['tmax'],day['tmin'],day['dew'],gdd))
    _,background=predict(request.app.state.model,features)
    rx=np.array([(longitude-LON)*111320*math.cos(math.radians(LAT))]); ry=np.array([(latitude-LAT)*111320])
    days=[]; contributors=[]
    for i,(wd,bg) in enumerate(zip(w['days'],background)):
        d=date.fromisoformat(wd['date']); doy=d.timetuple().tm_yday
        factor=math.exp(-.5*((doy-242)/19)**2)/(1+math.exp(-max(-50,min(50,doy-224))))
        sx,sy,q,_=source_vectors(allspots,d,scenario,factor)
        local=float(plume_at(sx,sy,rx,ry,q,wd['wind_speed'],wd['wind_direction'])[0])
        days.append({'date':d.isoformat(),'concentration_grains_m3':round(float(bg)+local,2),'local_plume_grains_m3':round(local,2)})
        if i==0:
            for n,h in enumerate(allspots):
                value=float(plume_at(sx[n:n+1],sy[n:n+1],rx,ry,q[n:n+1],wd['wind_speed'],wd['wind_direction'])[0])
                if value>1e-12:
                    contributors.append({'hotspot_id':h.id,'latitude':h.lat,'longitude':h.lon,'distance_m':round(math.hypot(sx[n]-rx[0],sy[n]-ry[0]),1),'contribution_grains_m3':value,'status':h.status,'data_provenance':h.data_provenance})
    contributors.sort(key=lambda c:c['distance_m'])
    return {'latitude':latitude,'longitude':longitude,'days':days,'contributing_hotspots':contributors[:10], 'scenario':scenario,'weather_source':w['source'],'data_provenance':request.app.state.model['data_provenance'],'hotspot_data_provenance':sorted({h.data_provenance for h in allspots}),'note':'Модельный городской фон и локальный шлейф могут учитывать источники дважды; результат не измерение и не медицинское заключение.'}

@router.post('/optimizer/calculate')
def optimizer(payload:OptimizerRequest,db:Session=Depends(get_db)):
    return optimize(spots(db),**payload.model_dump())

@router.get('/validation/kubgmu-report')
def report(request:Request,db:Session=Depends(get_db)):
    return validation(db,request.app.state.model)

@router.get('/forecast/clinical')
async def clinical(request:Request,municipality:str='Краснодар',mode:Mode='live',target_date:date|None=None,db:Session=Depends(get_db)):
    target=day_for(mode,target_date); w=await get_weather(db,municipality,mode,target); gdd=w['gdd_10']; features=[]
    for i,day in enumerate(w['days']):
        d=date.fromisoformat(day['date'])
        if i and d.month>=4: gdd+=max(0,(day['tmax']+day['tmin'])/2-10)
        features.append(feature(d,day['tmax'],day['tmin'],day['dew'],gdd))
    probabilities,concentrations=predict(request.app.state.model,features)
    days=[{'date':d['date'],'concentration_grains_m3':round(float(c),2),'danger_probability':round(float(p),4),'risk':risk(c)} for d,p,c in zip(w['days'],probabilities,concentrations)]
    # Neighborhood values are uncalibrated local-plume estimates, not the ML city prediction.
    neighborhoods=[]
    if municipality=='Краснодар':
        allspots=spots(db)
        import math
        for d, weather_day in zip(days,w['days']):
            day=date.fromisoformat(d['date']); doy=day.timetuple().tm_yday
            emission=math.exp(-.5*((doy-242)/19)**2)/(1+math.exp(-max(-50,min(50,doy-224))))
            local=calculate(allspots,day,weather_day['wind_speed'],weather_day['wind_direction'],'current',emission)
            for neighborhood in local['summary']['neighborhoods']:
                neighborhoods.append({'date':day.isoformat(),**neighborhood})
    return {'municipality':municipality,'weather_source':w['source'],'data_provenance':request.app.state.model['data_provenance'],'hotspot_data_provenance':sorted({h.data_provenance for h in spots(db)}),'days':days,'neighborhoods':neighborhoods,'neighborhoods_note':'Uncalibrated plume contributions, not clinical ML predictions; no regional background.','recommendation':'При симптомах или вопросах профилактической терапии обратитесь к врачу; не используйте эту модель для назначения лечения.'}
