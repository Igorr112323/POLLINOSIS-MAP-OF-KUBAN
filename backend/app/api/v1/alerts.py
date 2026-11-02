"""Explicit manual alert dispatch. No automatic scheduling and no configured SMS provider."""
from datetime import date
import httpx
from fastapi import APIRouter,Depends,HTTPException,Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.tables import User,PatientSubscription,NotificationLog
from app.services.auth_service import require
from app.api.v1.clinical import DISCLAIMER,forecasts
from app.core.config import settings

router=APIRouter(prefix='/alerts',tags=['alerts'])
class AlertRequest(BaseModel):
    subscription_id:int
    target_date:date|None=None

@router.post('/send')
async def send(payload:AlertRequest,request:Request,db:Session=Depends(get_db),user:User=Depends(require('admin','doctor'))):
    s=db.get(PatientSubscription,payload.subscription_id)
    if not s or not s.active: raise HTTPException(404,'Active subscription not found')
    target=payload.target_date or date.today()
    # Never send real messages based on an artificial future/past simulation.
    if target!=date.today(): raise HTTPException(422,'Dispatch is only allowed for today; use personal forecast for simulation')
    calendars,source=await forecasts(db,request.app.state.model,target,[(s.latitude,s.longitude)])
    days=calendars[0]; threshold=next((d for d in days if d['concentration_grains_m3']>=s.threshold),None)
    if not threshold or (date.fromisoformat(threshold['date'])-target).days>s.lead_days:
        return {'status':'not_due','disclaimer':DISCLAIMER,'weather_source':source}
    when=date.fromisoformat(threshold['date']); peak=max(d['concentration_grains_m3'] for d in days)
    message=f'Справочный прогноз: возможно превышение индивидуального порога {when.isoformat()}. {DISCLAIMER}'
    logs=[]
    for channel,destination in [('telegram',s.telegram_chat_id),('sms',s.phone)]:
        if not destination: continue
        existing=db.scalar(select(NotificationLog).where(NotificationLog.subscription_id==s.id,NotificationLog.channel==channel,NotificationLog.threshold_date==when,NotificationLog.status=='sent'))
        if existing:
            logs.append({'channel':channel,'status':'already_sent','log_id':existing.id}); continue
        status='not_configured'
        if channel=='telegram' and settings.telegram_bot_token and settings.enable_live_telegram:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    response=await client.post(f'https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage',json={'chat_id':destination,'text':message})
                    status='sent' if response.is_success and response.json().get('ok') is True else 'failed'
            except (httpx.HTTPError,ValueError): status='failed'
        log=NotificationLog(subscription_id=s.id,channel=channel,status=status,message=message,threshold_date=when,peak=peak)
        db.add(log); db.flush(); logs.append({'channel':channel,'status':status,'log_id':log.id})
    db.commit()
    return {'status':'processed' if logs else 'no_channel','deliveries':logs,'threshold_date':when.isoformat(),'peak':peak,'weather_source':source,'disclaimer':DISCLAIMER}

@router.get('/logs')
def logs(db:Session=Depends(get_db),user:User=Depends(require('admin','doctor'))):
    rows=db.scalars(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(1000))
    return {'logs':[{'id':r.id,'subscription_id':r.subscription_id,'channel':r.channel,'status':r.status,'message':r.message,'threshold_date':r.threshold_date.isoformat(),'peak':r.peak,'created_at':r.created_at.isoformat()} for r in rows],'disclaimer':DISCLAIMER}

class DispatchRequest(BaseModel):
    target_date:date|None=None

@router.post('/dispatch')
async def dispatch(payload:DispatchRequest,request:Request,db:Session=Depends(get_db),user:User=Depends(require('admin','doctor'))):
    target=payload.target_date or date.today()
    subs=list(db.scalars(select(PatientSubscription).where(PatientSubscription.active==True).order_by(PatientSubscription.id)))
    if not subs:
        return {'checked':0,'generated':0,'deliveries':[],'disclaimer':DISCLAIMER}
    calendars,source=await forecasts(db,request.app.state.model,target,[(s.latitude,s.longitude) for s in subs])
    deliveries=[]; generated=0
    for sub,days in zip(subs,calendars):
        first=next((d for d in days if d['concentration_grains_m3']>=sub.threshold),None)
        if not first: continue
        threshold_date=date.fromisoformat(first['date'])
        if not 0<=(threshold_date-target).days<=sub.lead_days: continue
        generated+=1; peak=max(d['concentration_grains_m3'] for d in days)
        simulation_note=f'Расчёт на {target.isoformat()}; данные: {request.app.state.model["data_provenance"]}; погода: {source}. ' if target!=date.today() else f'Данные: {request.app.state.model["data_provenance"]}; погода: {source}. '
        message=simulation_note+f'Справочный прогноз: возможно превышение индивидуального порога {threshold_date.isoformat()}, пик {peak:.1f} пз/м³. {DISCLAIMER}'
        channels=[]
        if sub.telegram_chat_id: channels.append(('telegram',sub.telegram_chat_id))
        if sub.phone: channels.append(('sms',sub.phone))
        if not channels: channels.append(('internal',None))
        for channel,destination in channels:
            status='logged_offline_demo'
            if channel=='telegram' and settings.telegram_bot_token and settings.enable_live_telegram and target==date.today():
                existing=db.scalar(select(NotificationLog).where(NotificationLog.subscription_id==sub.id,NotificationLog.channel==channel,NotificationLog.threshold_date==threshold_date,NotificationLog.status=='sent'))
                if existing:
                    log=NotificationLog(subscription_id=sub.id,channel=channel,status='already_sent',message=message,threshold_date=threshold_date,peak=peak)
                    db.add(log); db.flush()
                    deliveries.append({'subscription_id':sub.id,'channel':channel,'status':'already_sent','log_id':log.id}); continue
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        response=await client.post(f'https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage',json={'chat_id':destination,'text':message})
                        status='sent' if response.is_success and response.json().get('ok') is True else 'failed'
                except (httpx.HTTPError,ValueError): status='failed'
            log=NotificationLog(subscription_id=sub.id,channel=channel,status=status,message=message,threshold_date=threshold_date,peak=peak)
            db.add(log); db.flush(); deliveries.append({'subscription_id':sub.id,'channel':channel,'status':status,'log_id':log.id})
    db.commit()
    return {'checked':len(subs),'generated':generated,'deliveries':deliveries,'weather_source':source,'data_provenance':request.app.state.model['data_provenance'],'disclaimer':DISCLAIMER}
