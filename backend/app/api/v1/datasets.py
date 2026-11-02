import csv, io, json, math
from datetime import date
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from shapely.geometry import shape
from shapely.errors import GEOSException
from sqlalchemy import delete, select, func
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.tables import ImportedSample,Hotspot,WorkOrder,Observation,WeeklyObservation,User
from app.services.auth_service import require
from app.services.seed_service import seed
from app.services.phenology_ml_service import initialize
from app.core.geography import NEIGHBORHOODS

router=APIRouter(prefix='/datasets',tags=['datasets'])
ADMIN=Depends(require('admin'))
MAX_BYTES=5_000_000

async def read_limited(file):
    b=await file.read(MAX_BYTES+1)
    if len(b)>MAX_BYTES: raise HTTPException(413,'File exceeds 5 MB')
    return b

def number(value,name,low,high,optional=False):
    if value is None or value=='':
        if optional: return None
        raise ValueError(f'{name} required')
    n=float(value)
    if not math.isfinite(n) or not low<=n<=high: raise ValueError(f'{name} out of range')
    return n

@router.post('/observations/import-csv')
async def import_observations(request:Request,file:UploadFile=File(...),db:Session=Depends(get_db),user:User=ADMIN):
    try:
        raw=(await read_limited(file)).decode('utf-8-sig')
        reader=csv.DictReader(io.StringIO(raw)); rows=[]; seen=set(); granularities=set()
        if not reader.fieldnames or 'pollen_count' not in reader.fieldnames: raise ValueError('pollen_count column required')
        for row in reader:
            if len(rows)>=10000: raise ValueError('Too many rows')
            if row.get('date'):
                day=date.fromisoformat(row['date']); granularity='daily'
            elif row.get('year') and row.get('week'):
                day=date.fromisocalendar(int(row['year']),int(row['week']),1); granularity='weekly'
            else: raise ValueError('date or ISO year and week required')
            if day in seen: raise ValueError('Duplicate date')
            seen.add(day); granularities.add(granularity)
            tmax=number(row.get('tmax'),'tmax',-60,60,True); tmin=number(row.get('tmin'),'tmin',-60,60,True)
            if tmax is not None and tmin is not None and tmin>tmax: raise ValueError('tmin exceeds tmax')
            rows.append(ImportedSample(date=day,granularity=granularity,pollen=number(row.get('pollen_count'),'pollen_count',0,1e7),tmax=tmax,tmin=tmin,dew=number(row.get('dew'),'dew',-80,60,True),precipitation=number(row.get('precipitation'),'precipitation',0,2000,True),wind_speed=number(row.get('wind_speed'),'wind_speed',0,100,True),provenance='uploaded_csv'))
        if not rows or len(granularities)!=1: raise ValueError('Provide non-empty rows of one granularity')
        db.execute(delete(ImportedSample)); db.add_all(rows); db.flush()
        model=initialize(db)
        db.commit(); request.app.state.model=model
    except (ValueError,UnicodeError,csv.Error,TypeError) as e:
        db.rollback(); raise HTTPException(422,str(e)) from e
    return {'status':'imported','records':len(rows),'granularity':next(iter(granularities)),'data_provenance':'uploaded_dataset','source_format':'uploaded_csv','weather_note':'Missing weather is imputed from synthetic climatology; weekly samples are not expanded into daily observations.','training_year':model['training_year']}

@router.post('/hotspots/import-geojson')
async def import_hotspots(file:UploadFile=File(...),db:Session=Depends(get_db),user:User=ADMIN):
    if db.scalar(select(func.count()).select_from(WorkOrder)): raise HTTPException(409,'Cannot replace hotspots while work orders exist')
    try:
        doc=json.loads((await read_limited(file)).decode('utf-8-sig'))
        if doc.get('type')!='FeatureCollection' or not isinstance(doc.get('features'),list) or not 1<=len(doc['features'])<=10000: raise ValueError('Expected non-empty FeatureCollection')
        rows=[]
        for i,f in enumerate(doc['features'],1):
            if f.get('type')!='Feature' or f.get('geometry',{}).get('type')!='Polygon': raise ValueError('Expected Polygon Features')
            geom=shape(f['geometry'])
            if not geom.is_valid or geom.is_empty or geom.area<=0: raise ValueError('Invalid polygon')
            x1,y1,x2,y2=geom.bounds
            if not (-180<=x1<=x2<=180 and -90<=y1<=y2<=90): raise ValueError('Invalid WGS84 coordinates')
            p=f.get('properties') or {}; c=geom.centroid
            area=number(p.get('area_ha'),'area_ha',.001,100000)
            bands=p.get('bands') or p
            ndvi_only = bands.get('B04') is None or bands.get('B08') is None
            if ndvi_only:
                ndvi=number(p.get('NDVI',p.get('ndvi')),'NDVI',-.99,.99); b08=(1+ndvi)/2; b04=(1-ndvi)/2; b11=0.
            else:
                b04=number(bands.get('B04'),'B04',0,1); b08=number(bands.get('B08'),'B08',0,1)
                if b04+b08==0: raise ValueError('B04 + B08 must be positive')
                b11=number(bands.get('B11'),'B11',0,1)
            nearest=min(NEIGHBORHOODS,key=lambda n:(n[2]-c.y)**2+(n[3]-c.x)**2)
            rows.append(Hotspot(id=i,lat=c.y,lon=c.x,area_ha=area,geometry=f['geometry'],district=str(p.get('district') or nearest[1])[:100],landmark=str(p.get('landmark') or f'Импортированный участок {i}')[:200],land_type=str(p.get('land_type') or 'не указан')[:100],b02=number(bands.get('B02'),'B02',0,1,True) or 0,b03=number(bands.get('B03'),'B03',0,1,True) or 0,b04=b04,b08=b08,b11=b11,status='detected',data_provenance='uploaded_dataset',provenance=('uploaded_geojson_ndvi_only' if ndvi_only else 'uploaded_geojson' + ('_missing_b02' if bands.get('B02') is None else '') + ('_missing_b03' if bands.get('B03') is None else ''))))
        db.execute(delete(Hotspot)); db.add_all(rows); db.commit()
    except (ValueError,TypeError,KeyError,UnicodeError,GEOSException) as e:
        db.rollback(); raise HTTPException(422,str(e)) from e
    return {'status':'imported','records':len(rows),'data_provenance':'uploaded_dataset','source_format':'uploaded_geojson','area_note':'Area supplied by operator, not independently verified.'}

@router.post('/reset-demo')
def reset(request:Request,confirm_delete_work_orders:bool=False,db:Session=Depends(get_db),user:User=ADMIN):
    count=db.scalar(select(func.count()).select_from(WorkOrder))
    if count and not confirm_delete_work_orders: raise HTTPException(409,'Reset would delete work orders. Set confirm_delete_work_orders=true to confirm.')
    if count: db.execute(delete(WorkOrder))
    db.execute(delete(ImportedSample)); db.execute(delete(WeeklyObservation)); db.execute(delete(Observation)); db.execute(delete(Hotspot)); db.flush()
    seed(db); request.app.state.model=initialize(db)
    return {'status':'reset','data_provenance':'synthetic_demo','deleted_work_orders':count,'note':'Photo files are not deleted automatically.'}

@router.get('/templates/{kind}')
def template(kind:str):
    if kind=='observations_csv':
        text='date,pollen_count,tmax,tmin,dew,precipitation,wind_speed\n'+''.join(f'2023-08-{i:02d},{2 if i<6 else 20+i},30,20,15,0,2\n' for i in range(1,15))
        return Response(text,media_type='text/csv',headers={'Content-Disposition':'attachment; filename="observations.csv"'})
    if kind=='hotspots_geojson':
        doc={'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Polygon','coordinates':[[[39.0,45.05],[39.001,45.05],[39.001,45.051],[39.0,45.051],[39.0,45.05]]]},'properties':{'area_ha':0.87,'district':'Прикубанский','landmark':'Пример','B04':0.1,'B08':0.5,'B11':0.2}}]}
        return Response(json.dumps(doc,ensure_ascii=False),media_type='application/geo+json',headers={'Content-Disposition':'attachment; filename="hotspots.geojson"'})
    raise HTTPException(404,'Unknown template')
