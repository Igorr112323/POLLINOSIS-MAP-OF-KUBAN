import os,tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as t:
 os.environ['XDG_RUNTIME_DIR']=t
 import pgserver
 s=pgserver.PostgresServer(Path(t)/'pg')
 os.environ.update(DATABASE_URL=s.get_uri(),OFFLINE='true',DEMO_MODE='false',JWT_SECRET_KEY='postgres-test-secret-no-production',EDDS_MIS_API_KEY='postgres-test-key',MODEL_PATH=t+'/model',UPLOAD_DIR=t+'/uploads')
 from fastapi.testclient import TestClient
 from app.main import app
 from app.db.session import SessionLocal
 from app.models.tables import User,Hotspot
 from app.services.auth_service import hash_password
 from sqlalchemy import select
 from PIL import Image
 from io import BytesIO
 with TestClient(app) as c:
  assert c.get('/api/v1/health').status_code==200
  assert c.get('/api/v1/auth/demo-accounts').status_code==404
  with SessionLocal() as db:
   db.add_all([User(username='operator',password_hash=hash_password('very-strong-test-pass'),role='admin',is_active=True),User(username='team',password_hash=hash_password('very-strong-test-pass'),role='brigade',is_active=True)]);db.commit()
  def login(u):
   r=c.post('/api/v1/auth/login',json={'username':u,'password':'very-strong-test-pass'}); assert r.status_code==200,r.text; return {'Authorization':'Bearer '+r.json()['access_token']},r.json()['profile']['id']
  a,_=login('operator');b,uid=login('team')
  r=c.post('/api/v1/work-orders/generate-batch',headers=a,json={'work_start_date':'2026-07-01','mowing_deadline_date':'2026-08-05','brigade_ids':[uid],'daily_team_capacity_ha':10,'target_risk_capture_pct':1}); assert r.status_code==201,r.text
  o=r.json()['orders'][0]; assert c.patch(f'/api/v1/work-orders/{o["id"]}/status',headers=b,json={'status':'in_progress'}).status_code==200
  with SessionLocal() as db:
   h=db.get(Hotspot,o['hotspot_id']); lat,lon=h.lat,h.lon
  image=BytesIO();Image.new('RGB',(2,2),'green').save(image,'PNG');img=image.getvalue()
  r=c.post(f'/api/v1/work-orders/{o["id"]}/complete',headers=b,data={'gps_lat':lat,'gps_lon':lon,'completed_date':'2026-07-30'},files={'photo_before':('before.png',img,'image/png'),'photo_after':('after.png',img,'image/png')});assert r.status_code==200,r.text
  url=r.json()['order']['photo_before'];assert c.get(url).status_code==401 and c.get(url,headers=b).content==img
  csv=c.get('/api/v1/datasets/templates/observations_csv').content
  r=c.post('/api/v1/datasets/observations/import-csv',headers=a,files={'file':('in.csv',csv,'text/csv')});assert r.status_code==200,r.text
  r=c.post('/api/v1/datasets/reset-demo?confirm_delete_work_orders=true',headers=a);assert r.status_code==200,r.text
  geo=c.get('/api/v1/datasets/templates/hotspots_geojson').content
  r=c.post('/api/v1/datasets/hotspots/import-geojson',headers=a,files={'file':('g.json',geo,'application/json')});assert r.status_code==200,r.text
  print('PostgreSQL 16.2 PASS: startup, login, batch, status, completion, secured photo, CSV, reset, GeoJSON')
 s.cleanup()
