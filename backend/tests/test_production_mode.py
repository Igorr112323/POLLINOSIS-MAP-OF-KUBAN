"""Test settings and startup in a fresh interpreter and isolated database."""
import os
import subprocess
import sys


def test_production_mode_requires_secrets_and_disables_demo(tmp_path):
    env = dict(os.environ, DEMO_MODE='false', OFFLINE='true', DATABASE_URL='sqlite:///'+str(tmp_path/'production.db'), MODEL_PATH=str(tmp_path/'model.joblib'), UPLOAD_DIR=str(tmp_path/'uploads'))
    env.pop('JWT_SECRET_KEY',None)
    env.pop('EDDS_MIS_API_KEY',None)
    bad = subprocess.run([sys.executable,'-c','from app.core.config import settings'], env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and 'JWT_SECRET_KEY' in bad.stderr
    env['JWT_SECRET_KEY']='test-secret-not-for-use'
    bad = subprocess.run([sys.executable,'-c','from app.core.config import settings'], env=env, capture_output=True, text=True)
    assert bad.returncode != 0 and 'EDDS_MIS_API_KEY' in bad.stderr
    env['EDDS_MIS_API_KEY']='test-api-key'
    code = '''
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app.main import app
from app.db.session import SessionLocal
from app.models.tables import User,WorkOrder,Hotspot
from datetime import date
from pathlib import Path
import os
from app.services.auth_service import hash_password,issue
with TestClient(app) as c:
    assert c.get('/api/v1/auth/demo-accounts').status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        u=User(username='real_admin',password_hash=hash_password('long-pass-1234'),role='admin',is_active=True)
        old=User(username='demo_admin',password_hash=hash_password('Demo-Only-2026!'),role='admin',is_active=True)
        team=User(username='team',password_hash=hash_password('long-pass-1234'),role='brigade',is_active=True)
        outsider=User(username='outsider',password_hash=hash_password('long-pass-1234'),role='brigade',is_active=True)
        db.add_all([u,old,team,outsider]); db.commit(); old_token=issue(old); team_token=issue(team); other_token=issue(outsider)
        h=db.scalar(select(Hotspot)); folder=Path(os.environ['UPLOAD_DIR'])/'work_orders'; folder.mkdir(parents=True,exist_ok=True)
        (folder/'photo.png').write_bytes(b'private photo')
        order=WorkOrder(hotspot_id=h.id,priority_rank=1,brigade_id=team.id,planned_date=date(2026,7,1),status='completed_verified',photo_before='/uploads/work_orders/photo.png')
        db.add(order);db.commit();photo=f'/api/v1/work-orders/{order.id}/photos/before'
    assert c.post('/api/v1/auth/login',json={'username':'demo_admin','password':'Demo-Only-2026!'}).status_code == 401
    assert c.get('/api/v1/auth/me',headers={'Authorization':'Bearer '+old_token}).status_code == 401
    r=c.post('/api/v1/auth/login',json={'username':'real_admin','password':'long-pass-1234'})
    assert r.status_code == 200
    assert c.get('/api/v1/auth/me',headers={'Authorization':'Bearer '+r.json()['access_token']}).status_code == 200
    assert c.get('/api/v1/integrations/edds-mis-feed',headers={'X-API-Key':'test-api-key'}).status_code == 200
    assert c.get('/uploads/work_orders/photo.png').status_code == 404
    assert c.get(photo).status_code == 401
    assert c.get(photo,headers={'Authorization':'Bearer '+other_token}).status_code == 403
    image=c.get(photo,headers={'Authorization':'Bearer '+team_token})
    assert image.status_code == 200 and image.content == b'private photo' and 'no-store' in image.headers['cache-control']
'''
    run = subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=90)
    assert run.returncode == 0, run.stderr
