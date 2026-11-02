from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.tables import User
from app.services.auth_service import hash_password,verify_password,issue,current_user,require,ROLES
from app.core.config import settings

router=APIRouter(prefix='/auth',tags=['auth'])
class Credentials(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username: str=Field(min_length=3,max_length=120,pattern=r'^[A-Za-z0-9_.-]+$')
    password: str=Field(min_length=8,max_length=128)

def profile(user): return {'id':user.id,'username':user.username,'role':user.role}

@router.post('/register',status_code=201)
def register(payload:Credentials,db:Session=Depends(get_db)):
    user=User(username=payload.username,password_hash=hash_password(payload.password),role='patient',is_active=True)
    db.add(user)
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409,'Username already exists')
    db.refresh(user); return profile(user)

@router.post('/login')
def login(payload:Credentials,db:Session=Depends(get_db)):
    user=db.scalar(select(User).where(User.username==payload.username))
    if (not settings.demo_mode or not settings.demo_accounts_enabled) and payload.username in {'demo_'+r for r in ROLES}: raise HTTPException(401,'Invalid credentials')
    if not user or not user.is_active or not verify_password(payload.password,user.password_hash):
        raise HTTPException(401,'Invalid credentials')
    return {'access_token':issue(user),'token_type':'bearer','profile':profile(user)}

@router.get('/me')
def me(user:User=Depends(current_user)): return profile(user)

@router.get('/demo-accounts')
def demo_accounts():
    if not settings.demo_mode or not settings.demo_accounts_enabled: raise HTTPException(404,'Demo accounts disabled')
    return {'warning':'Public demo credentials. Disable demo accounts in any non-demo deployment.','accounts':[{'username':'demo_'+r,'password':'Demo-Only-2026!','role':r} for r in ROLES]}

@router.get('/brigades')
def brigades(db:Session=Depends(get_db),user:User=Depends(require('admin','dispatcher'))):
    return {'brigades':[{'id':b.id,'username':b.username} for b in db.scalars(select(User).where(User.role=='brigade',User.is_active==True).order_by(User.id))]}
