import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.session import get_db
from app.models.tables import User

# If not configured, random per process; tokens are invalidated on restart.
SECRET = settings.jwt_secret_key or settings.jwt_secret or secrets.token_urlsafe(48)
ROLES=('admin','dispatcher','brigade','doctor','patient')
security=HTTPBearer(auto_error=False)

def hash_password(password):
    salt=os.urandom(16); digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,310000)
    return 'pbkdf2_sha256$310000$'+base64.b64encode(salt).decode()+'$'+base64.b64encode(digest).decode()

def verify_password(password,stored):
    try:
        algorithm,iterations,salt,digest=stored.split('$')
        if algorithm!='pbkdf2_sha256': return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),base64.b64decode(salt),int(iterations))
        return hmac.compare_digest(actual,base64.b64decode(digest))
    except (ValueError,TypeError): return False

def issue(user):
    now=datetime.now(timezone.utc)
    return jwt.encode({'sub':str(user.id),'iat':now,'exp':now+timedelta(hours=8)},SECRET,algorithm='HS256')

def current_user(credentials:HTTPAuthorizationCredentials|None=Depends(security),db:Session=Depends(get_db)):
    if not credentials: raise HTTPException(401,'Bearer token required',headers={'WWW-Authenticate':'Bearer'})
    try:
        payload=jwt.decode(credentials.credentials,SECRET,algorithms=['HS256'],options={'require':['sub','exp','iat']})
        user=db.get(User,int(payload['sub']))
        if not user or not user.is_active or ((not settings.demo_mode or not settings.demo_accounts_enabled) and user.username in {'demo_'+r for r in ROLES}): raise ValueError('inactive')
        return user
    except (jwt.InvalidTokenError,ValueError,KeyError):
        raise HTTPException(401,'Invalid token',headers={'WWW-Authenticate':'Bearer'})

def require(*roles):
    def dependency(user:User=Depends(current_user)):
        if user.role not in roles: raise HTTPException(403,'Insufficient role')
        return user
    return dependency

def seed_users(db):
    if not settings.demo_mode or not settings.demo_accounts_enabled: return
    for role in ROLES:
        username='demo_'+role
        if not db.scalar(select(User).where(User.username==username)):
            db.add(User(username=username,password_hash=hash_password('Demo-Only-2026!'),role=role,is_active=True))
    db.commit()
