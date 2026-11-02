from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.tables import User
from app.services.auth_service import require
from app.services.weather_service import weather
router=APIRouter(prefix='/weather',tags=['weather'])
class SyncRequest(BaseModel):
    municipality:str='Краснодар'
@router.post('/sync')
async def sync(payload:SyncRequest,db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher'))):
    try: result=await weather(db,payload.municipality,'live',date.today())
    except ValueError as e: raise HTTPException(422,str(e)) from e
    return {'status':'synced_live' if result['source']=='open-meteo-live' else 'offline_cache_used','weather':result}
