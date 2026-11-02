from datetime import date,timedelta
import math
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.tables import User,Clinic,PatientSubscription,Hotspot,NotificationLog
from app.services.auth_service import require
from app.services.phenology_ml_service import feature,predict
from app.services.gaussian_plume_service import concentration_at, source_vectors, plume_at, LAT, LON
import numpy as np
from app.services.weather_service import weather
from app.core.geography import NEIGHBORHOODS

router=APIRouter(prefix='/clinical',tags=['clinical'])
DISCLAIMER='Информационные данные и оповещения носят профилактический справочный характер и не заменяют назначения лечащего врача; модель не прошла клиническую валидацию.'

class SubscriptionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=200)
    neighborhood:str=Field(min_length=1,max_length=100)
    latitude:float=Field(ge=-90,le=90)
    longitude:float=Field(ge=-180,le=180)
    clinical_profile:Literal['mild_rhinitis','moderate_rhinoconjunctivitis','severe_asthma_risk','pediatric']
    threshold:float=Field(default=10,gt=0,le=100000)
    lead_days:int=Field(default=7,ge=0,le=14)
    phone:str|None=Field(default=None,max_length=40)
    telegram_chat_id:str|None=Field(default=None,max_length=64)
    clinic_id:int|None=None
    owner_id:int|None=None
    active:bool=True

def seed_clinics(db):
    if db.scalar(select(Clinic.id).limit(1)): return
    # Approximate demo reference points, not verified addresses or real patient counts.
    for name,district,lat,lon in [('ККБ №2 / аллергологический центр','Западный',45.057,38.942),('Городская поликлиника №1','Центральный',45.035,38.972),('Городская поликлиника №7','Прикубанский',45.085,39.009),('Городская поликлиника №15','Карасунский',45.015,39.085)]:
        db.add(Clinic(name=name,district=district,latitude=lat,longitude=lon,cohort_estimate=0,provenance='synthetic_demo'))
    db.commit()

def sub_item(s):
    return {'id':s.id,'owner_id':s.owner_id,'clinic_id':s.clinic_id,'name':s.name,'neighborhood':s.neighborhood,'latitude':s.latitude,'longitude':s.longitude,'clinical_profile':s.clinical_profile,'threshold':s.threshold,'lead_days':s.lead_days,'phone':s.phone,'telegram_chat_id':s.telegram_chat_id,'active':s.active}

def accessible(db,sub_id,user):
    s=db.get(PatientSubscription,sub_id)
    if not s: raise HTTPException(404,'Subscription not found')
    if user.role=='patient' and s.owner_id!=user.id: raise HTTPException(403,'Forbidden')
    return s

@router.get('/clinics')
def clinics(db:Session=Depends(get_db),user:User=Depends(require('admin','doctor','patient'))):
    return {'clinics':[{'id':c.id,'name':c.name,'district':c.district,'data_provenance':c.provenance} for c in db.scalars(select(Clinic).order_by(Clinic.id))], 'disclaimer':DISCLAIMER}

@router.post('/subscriptions',status_code=201)
def create(payload:SubscriptionInput,db:Session=Depends(get_db),user:User=Depends(require('admin','doctor','patient'))):
    if user.role=='patient' and payload.owner_id not in (None,user.id): raise HTTPException(403,'Cannot assign another owner')
    owner_id=payload.owner_id or user.id
    owner=db.get(User,owner_id)
    if not owner or owner.role!='patient': raise HTTPException(422,'owner_id must refer to a patient')
    if payload.clinic_id and not db.get(Clinic,payload.clinic_id): raise HTTPException(422,'Unknown clinic')
    s=PatientSubscription(**{**payload.model_dump(),'owner_id':owner_id}); db.add(s); db.commit(); db.refresh(s)
    return {'subscription':sub_item(s),'disclaimer':DISCLAIMER}

@router.get('/subscriptions')
def subscriptions(db:Session=Depends(get_db),user:User=Depends(require('admin','doctor','patient'))):
    q=select(PatientSubscription).order_by(PatientSubscription.id)
    if user.role=='patient': q=q.where(PatientSubscription.owner_id==user.id)
    return {'subscriptions':[sub_item(s) for s in db.scalars(q)],'disclaimer':DISCLAIMER}

async def forecasts(db,model,target,locations):
    w=await weather(db,'Краснодар','live' if target==date.today() or not 2023<=target.year<=2026 else 'simulation',target)
    gdd=w['gdd_10']; feats=[]; hs=list(db.scalars(select(Hotspot).order_by(Hotspot.id)))
    for i,d in enumerate(w['days']):
        day=date.fromisoformat(d['date'])
        if i and day.month>=4: gdd+=max(0,(d['tmax']+d['tmin'])/2-10)
        feats.append(feature(day,d['tmax'],d['tmin'],d['dew'],gdd))
    probs,background=predict(model,feats); results=[[] for _ in locations]
    for wd,prob,bg in zip(w['days'],probs,background):
        day=date.fromisoformat(wd['date']); doy=day.timetuple().tm_yday
        phenology=math.exp(-.5*((doy-242)/19)**2)/(1+math.exp(-max(-50,min(50,doy-224))))
        values=concentration_at(hs,day,wd['wind_speed'],wd['wind_direction'],phenology,locations,float(bg))
        for result,value in zip(results,values): result.append({'date':day.isoformat(),'concentration_grains_m3':round(float(value),2),'city_danger_probability':round(float(prob),4)})
    return results,w['source']

@router.get('/subscriptions/{sub_id}/personal-forecast')
async def personal(sub_id:int,request:Request,target_date:date|None=None,db:Session=Depends(get_db),user:User=Depends(require('admin','doctor','patient'))):
    s=accessible(db,sub_id,user); target=target_date or date.today()
    calendars,source=await forecasts(db,request.app.state.model,target,[(s.latitude,s.longitude)])
    days=calendars[0]; exceed=next((d['date'] for d in days if d['concentration_grains_m3']>=s.threshold),None)
    # Rank actual point contributions for the first threshold day (or target day).
    evaluation_date=date.fromisoformat(exceed) if exceed else target
    meteo=await weather(db,'Краснодар','live' if target==date.today() or not 2023<=target.year<=2026 else 'simulation',target)
    wd=meteo['days'][(evaluation_date-target).days]; doy=evaluation_date.timetuple().tm_yday
    factor=math.exp(-.5*((doy-242)/19)**2)/(1+math.exp(-max(-50,min(50,doy-224))))
    hs=list(db.scalars(select(Hotspot))); sx,sy,q,_=source_vectors(hs,evaluation_date,'current',factor)
    rx=np.array([(s.longitude-LON)*111320*math.cos(math.radians(LAT))]); ry=np.array([(s.latitude-LAT)*111320])
    contributing=[]
    for i,h in enumerate(hs):
        if h.status in ('mowed_verified','completed_late_bloom') and h.mowing_date and h.mowing_date.year==evaluation_date.year and h.mowing_date<=evaluation_date: continue
        value=float(plume_at(sx[i:i+1],sy[i:i+1],rx,ry,q[i:i+1],wd['wind_speed'],wd['wind_direction'])[0])
        if value>1e-12:
            dist=math.hypot(sx[i]-rx[0],sy[i]-ry[0])
            contributing.append({'hotspot_id':h.id,'distance_m':round(dist,1),'contribution_grains_m3':value,'status':h.status,'data_provenance':h.data_provenance})
    contributing.sort(key=lambda x:x['distance_m'])
    return {'hotspot_data_provenance':sorted({h.data_provenance for h in hs}),'contributing_hotspots':contributing[:10],'contribution_date':evaluation_date.isoformat(),'lead_days':s.lead_days,'advisory_end_date':exceed,'subscription_id':s.id,'days':days,'first_threshold_date':exceed,'advisory_start_date':(date.fromisoformat(exceed)-timedelta(days=s.lead_days)).isoformat() if exceed else None,'data_provenance':request.app.state.model['data_provenance'],'weather_source':source,'model_note':'Uncalibrated sum of city ML background and local Gaussian plume; city probability is not personalized.','disclaimer':DISCLAIMER}

@router.get('/dashboard')
async def dashboard(request:Request,target_date:date|None=None,db:Session=Depends(get_db),user:User=Depends(require('admin','doctor'))):
    target=target_date or date.today(); clinics=list(db.scalars(select(Clinic))); subs=list(db.scalars(select(PatientSubscription).where(PatientSubscription.active==True)))
    locations=[(n[2],n[3]) for n in NEIGHBORHOODS]+[(s.latitude,s.longitude) for s in subs]
    calendars,source=await forecasts(db,request.app.state.model,target,locations)
    neighborhoods=[]
    for n,days in zip(NEIGHBORHOODS,calendars):
        neighborhoods.append({'name':n[0],'district':n[1],'first_threshold_date':next((d['date'] for d in days if d['concentration_grains_m3']>=10),None),'peak_grains_m3':max(d['concentration_grains_m3'] for d in days)})
    due=[]
    for s,days in zip(subs,calendars[len(NEIGHBORHOODS):]):
        first=next((date.fromisoformat(d['date']) for d in days if d['concentration_grains_m3']>=s.threshold),None)
        if first and 0<=(first-target).days<=s.lead_days: due.append(s)
    return {'clinics':[{'id':c.id,'name':c.name,'district':c.district,'latitude':c.latitude,'longitude':c.longitude,'cohort_estimate':c.cohort_estimate,'active_subscriptions':sum(s.clinic_id==c.id for s in subs),'notifications_due':sum(s.clinic_id==c.id for s in due),'data_provenance':c.provenance} for c in clinics],'neighborhoods':neighborhoods,'notifications_due':len(due),'weather_source':source,'data_provenance':request.app.state.model['data_provenance'],'disclaimer':DISCLAIMER}
