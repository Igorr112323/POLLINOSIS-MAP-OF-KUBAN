import csv, io, math, uuid
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from typing import Literal
from PIL import Image, UnidentifiedImageError
from pyproj import Transformer
from shapely.geometry import Point,shape
from shapely.ops import transform
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.session import get_db
from app.models.tables import Hotspot,User,WorkOrder
from app.services.auth_service import require
from app.services.mowing_optimizer_service import optimize,rank
from app.services.gaussian_plume_service import calculate
from app.services.weather_service import weather

router=APIRouter(prefix='/work-orders',tags=['work-orders'])
class BatchRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mowing_deadline_date:date
    work_start_date:date
    brigade_ids:list[int]=Field(min_length=1,max_length=185)
    daily_team_capacity_ha:float=Field(default=5,gt=0,le=100)
    max_area_ha:float|None=Field(default=None,ge=0)
    target_risk_capture_pct:float=Field(default=99,ge=0,le=100)

class StatusRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status:Literal['assigned','in_progress']
    brigade_id:int|None=None

def item(o,h):
    return {'id':o.id,'order_number':f'WO-{o.id:06d}','hotspot_id':o.hotspot_id,'district':h.district,'area_ha':h.area_ha,'priority_rank':o.priority_rank,'brigade_id':o.brigade_id,'planned_date':o.planned_date.isoformat(),'pre_flowering':o.planned_date<=date(o.planned_date.year,8,5),'status':o.status,'gps_lat':o.gps_lat,'gps_lon':o.gps_lon,'distance_m':o.distance_m,'geofence_ok':o.geofence_ok,'photo_before':f'/api/v1/work-orders/{o.id}/photos/before' if o.photo_before else None,'photo_after':f'/api/v1/work-orders/{o.id}/photos/after' if o.photo_after else None,'area_mowed_ha':o.area_mowed_ha,'completed_date':o.completed_date.isoformat() if o.completed_date else None,'notes':o.notes}

@router.post('/generate-batch',status_code=201)
def generate_batch(payload:BatchRequest,db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher'))):
    if payload.work_start_date>payload.mowing_deadline_date: raise HTTPException(422,'Start after deadline')
    if len(set(payload.brigade_ids))!=len(payload.brigade_ids): raise HTTPException(422,'Duplicate brigade IDs')
    users=list(db.scalars(select(User).where(User.id.in_(payload.brigade_ids),User.role=='brigade',User.is_active==True)))
    if len(users)!=len(payload.brigade_ids): raise HTTPException(422,'Each brigade ID must refer to an active brigade user')
    hs=list(db.scalars(select(Hotspot).order_by(Hotspot.id)))
    # Existing orders reserve sites; teams' already scheduled work also reserves time.
    existing=list(db.scalars(select(WorkOrder)))
    reserved={o.hotspot_id for o in existing}
    hs=[h for h in hs if h.id not in reserved and h.status=='detected']
    result=optimize(hs,brigades_count=len(users),daily_norm_ha=payload.daily_team_capacity_ha,work_start_date=payload.work_start_date,mowing_deadline_date=payload.mowing_deadline_date,max_area_ha=payload.max_area_ha,target_risk_capture_pct=payload.target_risk_capture_pct)
    hmap={h.id:h for h in hs}; ranking={r['id']:r['rank'] for r in rank(hs)}
    # Do not overlap previous orders for a team; reserve conservatively through latest planned date.
    availability={u.id:payload.work_start_date for u in users}
    for o in existing:
        if o.brigade_id in availability and o.status not in ('completed_verified','completed_late_bloom'):
            availability[o.brigade_id]=max(availability[o.brigade_id],o.planned_date+timedelta(days=1))
    made=[]; unscheduled=[]
    for hid in result['selected_ids']:
        h=hmap[hid]; uid=min(availability,key=availability.get); start=availability[uid]
        days=math.ceil(h.area_ha/payload.daily_team_capacity_ha); finish=start+timedelta(days=days-1)
        if finish>payload.mowing_deadline_date: unscheduled.append(hid); continue
        order=WorkOrder(hotspot_id=hid,priority_rank=ranking[hid],brigade_id=uid,planned_date=finish,status='assigned')
        db.add(order); h.status='assigned'; availability[uid]=finish+timedelta(days=1); made.append(order)
    db.commit()
    return {'created':len(made),'unscheduled_ids':unscheduled,'orders':[item(o,hmap[o.hotspot_id]) for o in made],'assumptions':'Teams work every calendar day; site assigned to one team for whole days.'}

@router.get('/export/csv')
def export(db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher'))):
    pairs=db.execute(select(WorkOrder,Hotspot).join(Hotspot,WorkOrder.hotspot_id==Hotspot.id).order_by(WorkOrder.id)).all()
    out=io.StringIO(newline=''); fields=['id','order_number','hotspot_id','district','area_ha','priority_rank','brigade_id','planned_date','pre_flowering','status','gps_lat','gps_lon','distance_m','geofence_ok','photo_before','photo_after','area_mowed_ha','completed_date','notes']; writer=csv.DictWriter(out,fieldnames=fields); writer.writeheader()
    for o,h in pairs:
        row=item(o,h)
        # Prevent formulas being executed when the CSV is opened in Excel.
        for k,v in row.items():
            if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')): row[k]="'"+v
        writer.writerow(row)
    return Response(content=('\ufeff'+out.getvalue()).encode('utf-8'),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="work_orders.csv"'})

@router.get('')
def listing(status:str|None=None,district:str|None=None,brigade_id:int|None=None,urgent:bool|None=None,db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher','brigade'))):
    q=select(WorkOrder,Hotspot).join(Hotspot,WorkOrder.hotspot_id==Hotspot.id)
    if user.role=='brigade': q=q.where(WorkOrder.brigade_id==user.id)
    if status: q=q.where(WorkOrder.status==status)
    if district: q=q.where(Hotspot.district==district)
    if brigade_id is not None: q=q.where(WorkOrder.brigade_id==brigade_id)
    pairs=db.execute(q.order_by(WorkOrder.planned_date,WorkOrder.priority_rank)).all()
    if urgent is not None:
        pairs=[(o,h) for o,h in pairs if (o.status not in ('completed_verified','completed_late_bloom') and o.planned_date<=date.today()+timedelta(days=3))==urgent]
    return {'orders':[item(o,h) for o,h in pairs],'summary':{'planned_ha':round(sum(h.area_ha for o,h in pairs),2),'mowed_ha':round(sum(o.area_mowed_ha or 0 for o,h in pairs),2),'days_to_flowering':(date(date.today().year,8,10)-date.today()).days,'note':'Summary reflects applied filters; bloom date is an assumption.'}}

@router.get('/{order_id}/photos/{kind}')
def order_photo(order_id:int,kind:Literal['before','after'],db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher','brigade'))):
    order=db.get(WorkOrder,order_id)
    if not order: raise HTTPException(404,'Order not found')
    if user.role=='brigade' and order.brigade_id!=user.id: raise HTTPException(403,'Not assigned')
    stored=order.photo_before if kind=='before' else order.photo_after
    if not stored: raise HTTPException(404,'Photo not found')
    folder=(Path(settings.upload_dir)/'work_orders').resolve()
    path=(folder/Path(stored).name).resolve()
    if not path.is_relative_to(folder) or not path.is_file(): raise HTTPException(404,'Photo not found')
    return FileResponse(path,headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

@router.patch('/{order_id}/status')
def change(order_id:int,payload:StatusRequest,db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher','brigade'))):
    o=db.get(WorkOrder,order_id)
    if not o: raise HTTPException(404,'Order not found')
    if o.status in ('completed_verified','completed_late_bloom'): raise HTTPException(409,'Order already completed')
    if user.role=='brigade':
        if o.brigade_id!=user.id or payload.status!='in_progress' or payload.brigade_id is not None: raise HTTPException(403,'Not assigned to this brigade')
    else:
        if payload.brigade_id is not None:
            brigade=db.get(User,payload.brigade_id)
            if not brigade or brigade.role!='brigade' or not brigade.is_active: raise HTTPException(422,'Invalid brigade')
            o.brigade_id=brigade.id
    if o.brigade_id is None: raise HTTPException(422,'Assign a brigade first')
    o.status=payload.status; h=db.get(Hotspot,o.hotspot_id); h.status='in_progress' if o.status=='in_progress' else 'assigned'; db.commit()
    return item(o,h)

async def photo_bytes(f:UploadFile):
    data=await f.read(5_000_001)
    if len(data)>5_000_000: raise HTTPException(413,'Photo exceeds 5 MB')
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ('JPEG','PNG'): raise ValueError('Only JPEG and PNG are accepted')
            img.verify(); ext='jpg' if img.format=='JPEG' else 'png'
    except (UnidentifiedImageError,ValueError,SyntaxError,Image.DecompressionBombError,OSError): raise HTTPException(422,'Invalid image')
    return data,ext

@router.post('/{order_id}/complete')
async def complete(order_id:int,gps_lat:float=Form(ge=-90,le=90),gps_lon:float=Form(ge=-180,le=180),completed_date:date=Form(),notes:str=Form(default='',max_length=4000),area_mowed_ha:float|None=Form(default=None,gt=0),photo_before:UploadFile=File(),photo_after:UploadFile=File(),db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher','brigade'))):
    o=db.scalar(select(WorkOrder).where(WorkOrder.id==order_id).with_for_update())
    if not o: raise HTTPException(404,'Order not found')
    if user.role=='brigade' and o.brigade_id!=user.id: raise HTTPException(403,'Not assigned')
    if o.status!='in_progress': raise HTTPException(409,'Order must be in progress')
    if completed_date>date.today(): raise HTTPException(422,'Completion date cannot be in the future')
    h=db.get(Hotspot,o.hotspot_id); area=area_mowed_ha if area_mowed_ha is not None else h.area_ha
    if not .98*h.area_ha<=area<=1.02*h.area_ha: raise HTTPException(422,'Full-site completion requires mowed area within 2% of site area')
    # Local azimuthal equidistant projection centered on GPS for distance to polygon.
    projection=Transformer.from_crs('EPSG:4326',f'+proj=aeqd +lat_0={gps_lat} +lon_0={gps_lon} +datum=WGS84 +units=m',always_xy=True)
    distance=transform(projection.transform,shape(h.geometry)).distance(Point(0,0))
    if distance>500: raise HTTPException(422,f'Outside geofence: {distance:.1f} m')
    before,be=await photo_bytes(photo_before); after,ae=await photo_bytes(photo_after)
    evaldate=max(completed_date,date(completed_date.year,8,24)) if completed_date<date(completed_date.year,8,10) else completed_date
    w=await weather(db,'Краснодар','simulation',evaldate); wd=w['days'][0]
    # weather() may commit a cache entry; reacquire and refresh the row lock.
    o=db.scalar(select(WorkOrder).where(WorkOrder.id==order_id).with_for_update().execution_options(populate_existing=True))
    if not o or o.status!='in_progress': raise HTTPException(409,'Order no longer in progress')
    if user.role=='brigade' and o.brigade_id!=user.id: raise HTTPException(403,'Not assigned')
    allspots=list(db.scalars(select(Hotspot).order_by(Hotspot.id))); nearest=next(x['nearest_neighborhood'] for x in rank([h]))
    doy=evaldate.timetuple().tm_yday; phenology=math.exp(-.5*((doy-242)/19)**2)/(1+math.exp(-max(-50,min(50,doy-224))))
    a=calculate(allspots,evaldate,wd['wind_speed'],wd['wind_direction'],'current',phenology)
    old=next(x['concentration'] for x in a['summary']['neighborhoods'] if x['name']==nearest)
    late=completed_date>=date(completed_date.year,8,10)
    h.status='completed_late_bloom' if late else 'mowed_verified'; h.mowing_date=completed_date
    b=calculate(allspots,evaldate,wd['wind_speed'],wd['wind_direction'],'current',phenology)
    new=next(x['concentration'] for x in b['summary']['neighborhoods'] if x['name']==nearest)
    folder=Path(settings.upload_dir)/'work_orders'; folder.mkdir(parents=True,exist_ok=True)
    filenames=[f'{uuid.uuid4().hex}.{be}',f'{uuid.uuid4().hex}.{ae}']; paths=[folder/name for name in filenames]
    try:
        paths[0].write_bytes(before); paths[1].write_bytes(after)
        # Conditional update prevents duplicate completion if two requests race.
        updated=db.execute(update(WorkOrder).where(WorkOrder.id==order_id,WorkOrder.status=='in_progress',WorkOrder.brigade_id==o.brigade_id).values(
            status='completed_late_bloom' if late else 'completed_verified', gps_lat=gps_lat,gps_lon=gps_lon,distance_m=distance,geofence_ok=True,
            photo_before='/uploads/work_orders/'+filenames[0],photo_after='/uploads/work_orders/'+filenames[1],area_mowed_ha=area,completed_date=completed_date,completed_at=datetime.now(timezone.utc).replace(tzinfo=None),notes=notes).execution_options(synchronize_session=False))
        if updated.rowcount!=1: raise HTTPException(409,'Order changed during completion')
        db.commit(); db.refresh(o)
    except Exception:
        db.rollback()
        for path in paths: path.unlink(missing_ok=True)
        raise
    return {'order':item(o,h),'delta_effect':{'neighborhood':nearest,'evaluation_date':evaldate.isoformat(),'weather_source':w['source'],'before_grains_m3':old,'after_grains_m3':new,'reduction_grains_m3':round(old-new,2),'reduction_pct':round(100*(old-new)/old,2) if old else None,'note':'Uncalibrated illustrative model; negative reduction indicates increased exposure.'},'warning':'Late bloom mowing can cause a mechanical burst.' if late else None}
