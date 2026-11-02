import hmac
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from app.services.auth_service import security, current_user
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.geography import DISTRICTS
from app.db.session import get_db
from app.models.tables import Hotspot,WorkOrder
from app.services.mowing_optimizer_service import rank

router=APIRouter(prefix='/integrations',tags=['integrations'])
@router.get('/edds-mis-feed')
def feed(x_api_key:str|None=Header(default=None),credentials:HTTPAuthorizationCredentials|None=Depends(security),db:Session=Depends(get_db)):
    key_ok=bool(settings.edds_mis_api_key and x_api_key and hmac.compare_digest(x_api_key.encode(),settings.edds_mis_api_key.encode()))
    if not key_ok:
        if credentials:
            user=current_user(credentials,db)
            if user.role not in ('admin','dispatcher'): raise HTTPException(403,'Insufficient role')
        elif not settings.edds_mis_api_key:
            raise HTTPException(503,'Integration API key is not configured')
        else:
            raise HTTPException(401,'Invalid API key')
    hotspots=list(db.scalars(select(Hotspot))); orders=list(db.scalars(select(WorkOrder)))
    scores={r['id']:r for r in rank(hotspots)}
    active=[h for h in hotspots if h.status not in ('mowed_verified','completed_late_bloom')]
    complete=[o for o in orders if o.status in ('completed_verified','completed_late_bloom')]
    districts=sorted(set(DISTRICTS)|{h.district for h in hotspots})
    return {'active_hotspots':len(active),'active_area_ha':sum(h.area_ha for h in active),'orders_total':len(orders),'orders_completed':len(complete),'mowed_area_ha':sum(o.area_mowed_ha or 0 for o in complete),'districts':[{'name':d,'active_hotspots':sum(h.district==d for h in active),'exposure_risk':sum(scores[h.id]['exposure_risk'] for h in active if h.district==d)} for d in districts], 'data_provenance':sorted({h.data_provenance for h in hotspots}), 'notice':'Модельный индекс риска и ориентировочные данные; не использовать для принятия медицинских решений.'}
