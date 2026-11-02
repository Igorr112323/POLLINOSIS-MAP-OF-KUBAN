# Configure isolated resources before importing the application; running this file
# alone must never drop the operator's default database.
import os, tempfile
from pathlib import Path
_test_dir = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL']='sqlite:///' + str(Path(_test_dir.name) / 'stage2-pollinoz-test.db')
os.environ['MODEL_PATH']=str(Path(_test_dir.name) / 'model.joblib')
os.environ['UPLOAD_DIR']=str(Path(_test_dir.name) / 'uploads')
os.environ['OFFLINE']='true'
os.environ['DEMO_MODE']='true'
os.environ['DEMO_ACCOUNTS_ENABLED']='true'
import io, json
from datetime import date
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from app.main import app
from app.db.session import Base,engine,SessionLocal
from app.models.tables import Hotspot,User,WorkOrder

@pytest.fixture
def client():
    database = Path(engine.url.database).resolve()
    assert database.is_relative_to(Path(tempfile.gettempdir()).resolve())
    assert database.name in {'stage1-pollinoz-test.db', 'stage2-pollinoz-test.db'}
    Base.metadata.drop_all(engine)
    with TestClient(app) as c: yield c

def login(c,role):
    response=c.post('/api/v1/auth/login',json={'username':'demo_'+role,'password':'Demo-Only-2026!'})
    assert response.status_code==200, response.text
    return {'Authorization':'Bearer '+response.json()['access_token']}

def test_auth_roles_and_private_data(client):
    assert client.get('/api/v1/auth/me').status_code==401
    admin=login(client,'admin'); patient=login(client,'patient'); brigade=login(client,'brigade')
    assert client.get('/api/v1/auth/me',headers=admin).json()['role']=='admin'
    assert client.get('/api/v1/work-orders',headers=patient).status_code==403
    assert client.post('/api/v1/datasets/reset-demo',headers=brigade).status_code==403
    assert client.post('/api/v1/auth/register',json={'username':'other_patient','password':'LongPass123!','role':'admin'}).status_code==422
    assert client.post('/api/v1/auth/register',json={'username':'other_patient','password':'LongPass123!'}).status_code==201
    assert client.post('/api/v1/auth/register',json={'username':'other_patient','password':'LongPass123!'}).status_code==409
    assert client.post('/api/v1/auth/login',json={'username':'other_patient','password':'badpass1'}).status_code==401

def test_observation_import_retrain_reset_and_rollback(client):
    admin=login(client,'admin')
    template=client.get('/api/v1/datasets/templates/observations_csv')
    response=client.post('/api/v1/datasets/observations/import-csv',headers=admin,files={'file':('observations.csv',template.content,'text/csv')})
    assert response.status_code==200,response.text
    assert response.json()['data_provenance']=='uploaded_dataset'
    report=client.get('/api/v1/validation/kubgmu-report').json()
    assert report['training_years']==[2023] and report['evaluation_years']==[]
    assert report['roc_auc'] is None and report['data_provenance']=='uploaded_dataset'
    invalid=b'date,pollen_count\n2023-08-01,-5\n'
    assert client.post('/api/v1/datasets/observations/import-csv',headers=admin,files={'file':('x.csv',invalid)}).status_code==422
    assert client.get('/api/v1/validation/kubgmu-report').json()['data_provenance']=='uploaded_dataset'
    assert client.post('/api/v1/datasets/reset-demo',headers=admin).json()['data_provenance']=='synthetic_demo'
    assert client.get('/api/v1/health').json()['hotspots']==189

def test_weekly_import_does_not_invent_days(client):
    admin=login(client,'admin')
    content='year,week,pollen_count\n'+''.join(f'{year},{week},{1 if week<30 else 40}\n' for year in (2023,2024) for week in range(25,37))
    r=client.post('/api/v1/datasets/observations/import-csv',headers=admin,files={'file':('w.csv',content)})
    assert r.status_code==200,r.text
    report=client.get('/api/v1/validation/kubgmu-report').json()
    assert report['weekly_count']==24 and report['start_window_accuracy'] is None
    assert report['blind_2024_accuracy'] is not None

def test_hotspot_import_validation_and_ndvi(client):
    admin=login(client,'admin')
    data=client.get('/api/v1/datasets/templates/hotspots_geojson').json()
    r=client.post('/api/v1/datasets/hotspots/import-geojson',headers=admin,files={'file':('s.json',json.dumps(data))})
    assert r.status_code==200,r.text
    p=client.get('/api/v1/gis/hotspots').json()['features'][0]['properties']
    assert p['data_provenance']=='uploaded_dataset' and p['ndmi']==pytest.approx(3/7)
    props=data['features'][0]['properties'];props.clear();props.update({'area_ha':1,'NDVI':.7})
    assert client.post('/api/v1/datasets/hotspots/import-geojson',headers=admin,files={'file':('s.json',json.dumps(data))}).status_code==200
    p=client.get('/api/v1/gis/hotspots').json()['features'][0]['properties']
    assert p['ndvi']==pytest.approx(.7) and p['ndmi'] is None and p['bands']['B08'] is None
    props['area_ha']=-1
    assert client.post('/api/v1/datasets/hotspots/import-geojson',headers=admin,files={'file':('s.json',json.dumps(data))}).status_code==422
    assert len(client.get('/api/v1/gis/hotspots').json()['features'])==1

def test_optimizer_pareto_strategies(client):
    cap=client.post('/api/v1/optimizer/calculate',json={'strategy':'area_or_budget_cap','max_area_ha':100,'mowing_deadline_date':'2026-08-05'}).json()
    assert cap['selected_area_ha']<=100
    assert 20<=len(cap['pareto_curve'])<=22 and cap['pareto_curve'][0]['area_pct']==0 and cap['pareto_curve'][-1]['area_pct']==100
    assert all(a['risk_capture_pct']<=b['risk_capture_pct'] for a,b in zip(cap['pareto_curve'],cap['pareto_curve'][1:]))
    constrained=client.post('/api/v1/optimizer/calculate',json={'strategy':'brigade_capacity','brigades_count':1,'daily_norm_ha':1,'work_start_date':'2026-08-01','mowing_deadline_date':'2026-08-05'}).json()
    assert constrained['capacity_ha']==5 and constrained['selected_area_ha']<=5

def png():
    out=io.BytesIO();Image.new('RGB',(2,2),'green').save(out,'PNG');return out.getvalue()

def test_workflow_geofence_burst_and_export(client):
    dispatcher=login(client,'dispatcher'); brigade=login(client,'brigade'); admin=login(client,'admin')
    brigade_id=client.get('/api/v1/auth/me',headers=brigade).json()['id']
    batch={'mowing_deadline_date':'2026-08-05','work_start_date':'2026-07-01','brigade_ids':[brigade_id],'daily_team_capacity_ha':10,'target_risk_capture_pct':10}
    response=client.post('/api/v1/work-orders/generate-batch',headers=dispatcher,json=batch)
    assert response.status_code==201,response.text
    order=response.json()['orders'][0]; oid=order['id']
    assert client.patch(f'/api/v1/work-orders/{oid}/status',headers=brigade,json={'status':'in_progress'}).status_code==200
    with SessionLocal() as db:
        h=db.get(Hotspot,order['hotspot_id']); lat=h.lat;lon=h.lon
    files={'photo_before':('before.png',png(),'image/png'),'photo_after':('after.png',png(),'image/png')}
    form={'gps_lat':lat+1,'gps_lon':lon,'completed_date':'2026-07-30','notes':'test'}
    assert client.post(f'/api/v1/work-orders/{oid}/complete',headers=brigade,data=form,files=files).status_code==422
    form['gps_lat']=lat
    import base64
    broken=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4N0AAAAASUVORK5CYII=')
    invalid_files={'photo_before':('before.png',broken,'image/png'),'photo_after':('after.png',png(),'image/png')}
    assert client.post(f'/api/v1/work-orders/{oid}/complete',headers=brigade,data=form,files=invalid_files).status_code==422
    completed=client.post(f'/api/v1/work-orders/{oid}/complete',headers=brigade,data=form,files=files)
    assert completed.status_code==200,completed.text
    result=completed.json(); assert result['order']['status']=='completed_verified' and result['order']['geofence_ok']
    assert result['delta_effect']['reduction_grains_m3']>=0
    photo=result['order']['photo_before']
    assert client.get(photo).status_code==401
    assert client.get(photo,headers=login(client,'patient')).status_code==403
    assert client.get(photo,headers=dispatcher).content==png()
    assert client.get(photo,headers=brigade).status_code==200
    assert client.get('/uploads/work_orders/anything.png').status_code==404
    with SessionLocal() as db:
        other=User(username='other_brigade',password_hash='unused',role='brigade',is_active=True);db.add(other);db.commit()
        from app.services.auth_service import issue
        other_auth={'Authorization':'Bearer '+issue(other)}
    assert client.get(photo,headers=other_auth).status_code==403
    assert client.post(f'/api/v1/work-orders/{oid}/complete',headers=brigade,data=form,files=files).status_code==409
    assert client.post('/api/v1/datasets/reset-demo',headers=admin).status_code==409
    assert client.get('/api/v1/work-orders/export/csv',headers=dispatcher).content.startswith(b'\xef\xbb\xbf')
    reset=client.post('/api/v1/datasets/reset-demo?confirm_delete_work_orders=true',headers=admin)
    assert reset.status_code==200 and reset.json()['deleted_work_orders']>0
    assert client.get('/api/v1/health').json()['hotspots']==189

def test_patient_forecast_privacy_dashboard_and_alerts(client):
    patient=login(client,'patient');doctor=login(client,'doctor')
    sub={'name':'Demo','neighborhood':'Юбилейный','latitude':45.025,'longitude':38.921,'clinical_profile':'severe_asthma_risk','threshold':.01,'phone':'+70000000000'}
    res=client.post('/api/v1/clinical/subscriptions',headers=patient,json=sub)
    assert res.status_code==201,res.text
    sid=res.json()['subscription']['id']
    fc=client.get(f'/api/v1/clinical/subscriptions/{sid}/personal-forecast',headers=patient,params={'target_date':'2026-08-24'})
    assert fc.status_code==200 and len(fc.json()['days'])==14 and 'disclaimer' in fc.json()
    assert client.get('/api/v1/clinical/dashboard',headers=patient).status_code==403
    dashboard=client.get('/api/v1/clinical/dashboard',headers=doctor,params={'target_date':'2026-08-24'})
    assert dashboard.status_code==200 and len(dashboard.json()['clinics'])==4
    assert client.post('/api/v1/alerts/send',headers=patient,json={'subscription_id':sid}).status_code==403
    assert client.post('/api/v1/alerts/send',headers=doctor,json={'subscription_id':sid,'target_date':'2026-08-24'}).status_code==422
    assert client.post('/api/v1/weather/sync',headers=doctor,json={}).status_code==403
    assert client.post('/api/v1/weather/sync',headers=login(client,'admin'),json={}).json()['status']=='offline_cache_used'

def test_late_completion_increases_emissions_on_mowing_day(client):
    from app.services.gaussian_plume_service import source_vectors
    admin=login(client,'admin'); brigade=login(client,'brigade')
    uid=client.get('/api/v1/auth/me',headers=brigade).json()['id']
    result=client.post('/api/v1/work-orders/generate-batch',headers=admin,json={'mowing_deadline_date':'2026-08-05','work_start_date':'2026-07-01','brigade_ids':[uid],'daily_team_capacity_ha':10,'target_risk_capture_pct':1}).json()
    order=result['orders'][0]; oid=order['id']
    client.patch(f'/api/v1/work-orders/{oid}/status',headers=brigade,json={'status':'in_progress'})
    with SessionLocal() as db:
        h=db.get(Hotspot,order['hotspot_id']);lat=h.lat;lon=h.lon
    files={'photo_before':('b.png',png(),'image/png'),'photo_after':('a.png',png(),'image/png')}
    r=client.post(f'/api/v1/work-orders/{oid}/complete',headers=brigade,data={'gps_lat':lat,'gps_lon':lon,'completed_date':'2026-08-24'},files=files)
    assert r.status_code==200,r.text
    assert r.json()['order']['status']=='completed_late_bloom' and r.json()['warning']
    with SessionLocal() as db:
        h=db.get(Hotspot,order['hotspot_id'])
        normal=source_vectors([h],date(2026,8,24),'no_mowing',1)[2][0]
        burst=source_vectors([h],date(2026,8,24),'current',1)[2][0]
        nextyear=source_vectors([h],date(2027,8,24),'current',1)[2][0]
        assert burst==pytest.approx(normal*1.8) and nextyear==pytest.approx(normal)

def test_budget_strategy_requires_cap(client):
    assert client.post('/api/v1/optimizer/calculate',json={'strategy':'area_or_budget_cap'}).status_code==422

def test_alert_records_not_configured_without_sending(client,monkeypatch):
    import app.api.v1.alerts as alert_module
    patient=login(client,'patient'); doctor=login(client,'doctor')
    res=client.post('/api/v1/clinical/subscriptions',headers=patient,json={'name':'Demo','neighborhood':'Юбилейный','latitude':45.025,'longitude':38.921,'clinical_profile':'pediatric','threshold':10,'phone':'+70000000000','telegram_chat_id':'123'})
    sid=res.json()['subscription']['id']
    async def fake_forecast(db,model,target,locations):
        return [[{'date':target.isoformat(),'concentration_grains_m3':50,'city_danger_probability':.9}]],'synthetic-climatology'
    monkeypatch.setattr(alert_module,'forecasts',fake_forecast)
    sent=client.post('/api/v1/alerts/send',headers=doctor,json={'subscription_id':sid})
    assert sent.status_code==200 and {x['status'] for x in sent.json()['deliveries']}=={'not_configured'}
    logs=client.get('/api/v1/alerts/logs',headers=doctor).json()['logs']
    assert len(logs)==2 and all(x['status']=='not_configured' for x in logs)

def test_dispatch_logs_and_keyed_feed(client, monkeypatch):
    import app.api.v1.alerts as alerts
    from app.core.config import settings
    patient=login(client,'patient'); doctor=login(client,'doctor')
    res=client.post('/api/v1/clinical/subscriptions',headers=patient,json={'name':'Demo','neighborhood':'Юбилейный','latitude':45.025,'longitude':38.921,'clinical_profile':'pediatric','threshold':10,'telegram_chat_id':'123'})
    assert res.status_code==201
    async def fake_forecast(db,model,target,locations):
        return [[{'date':target.isoformat(),'concentration_grains_m3':50,'city_danger_probability':.9}] for _ in locations],'synthetic-climatology'
    monkeypatch.setattr(alerts,'forecasts',fake_forecast)
    monkeypatch.setattr(settings,'telegram_bot_token','secret')
    monkeypatch.setattr(settings,'enable_live_telegram',False)
    r=client.post('/api/v1/alerts/dispatch',headers=doctor,json={'target_date':'2026-08-24'})
    assert r.status_code==200 and r.json()['generated']==1
    assert r.json()['deliveries'][0]['status']=='logged_offline_demo'
    assert len(client.get('/api/v1/alerts/logs',headers=doctor).json()['logs'])==1
    monkeypatch.setattr(settings,'edds_mis_api_key','correct')
    assert client.get('/api/v1/integrations/edds-mis-feed').status_code==401
    assert client.get('/api/v1/integrations/edds-mis-feed',headers={'X-API-Key':'wrong'}).status_code==401
    feed=client.get('/api/v1/integrations/edds-mis-feed',headers={'X-API-Key':'correct'}).json()
    assert feed['active_hotspots']==189 and len(feed['districts'])==4

def test_personal_forecast_includes_source_contributions(client):
    patient=login(client,'patient')
    sub=client.post('/api/v1/clinical/subscriptions',headers=patient,json={'name':'Demo','neighborhood':'Юбилейный','latitude':45.025,'longitude':38.921,'clinical_profile':'mild_rhinitis','threshold':10}).json()['subscription']
    forecast=client.get(f"/api/v1/clinical/subscriptions/{sub['id']}/personal-forecast",headers=patient,params={'target_date':'2026-08-24'}).json()
    assert len(forecast['days'])==14 and 'contributing_hotspots' in forecast
    assert all(h['contribution_grains_m3']>0 for h in forecast['contributing_hotspots'])
    assert 'disclaimer' in forecast and forecast['lead_days']==7

def test_dispatch_telegram_requires_explicit_flag_and_no_simulation_send(client, monkeypatch):
    import httpx
    import app.api.v1.alerts as alerts
    from app.core.config import settings
    patient=login(client,'patient'); doctor=login(client,'doctor')
    client.post('/api/v1/clinical/subscriptions',headers=patient,json={'name':'Demo','neighborhood':'Юбилейный','latitude':45.025,'longitude':38.921,'clinical_profile':'pediatric','threshold':10,'telegram_chat_id':'123'})
    async def fake_forecast(db,model,target,locations):
        return [[{'date':target.isoformat(),'concentration_grains_m3':50,'city_danger_probability':.9}] for _ in locations],'synthetic-climatology'
    monkeypatch.setattr(alerts,'forecasts',fake_forecast)
    calls=[]
    async def fake_post(self,url,json):
        calls.append((url,json)); return httpx.Response(200,json={'ok':True},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient,'post',fake_post)
    monkeypatch.setattr(settings,'telegram_bot_token','demo-token')
    monkeypatch.setattr(settings,'enable_live_telegram',True)
    sim=client.post('/api/v1/alerts/dispatch',headers=doctor,json={'target_date':'2026-08-24'}).json()
    assert sim['deliveries'][0]['status']=='logged_offline_demo' and not calls
    live=client.post('/api/v1/alerts/dispatch',headers=doctor,json={}).json()
    assert live['deliveries'][0]['status']=='sent' and len(calls)==1
    duplicate=client.post('/api/v1/alerts/dispatch',headers=doctor,json={}).json()
    assert duplicate['deliveries'][0]['status']=='already_sent' and len(calls)==1
    assert len(client.get('/api/v1/alerts/logs',headers=doctor).json()['logs'])==3

def test_brigade_directory_and_feed_role_access(client,monkeypatch):
    from app.core.config import settings
    admin=login(client,'admin'); dispatcher=login(client,'dispatcher'); patient=login(client,'patient')
    assert client.get('/api/v1/auth/brigades',headers=patient).status_code==403
    brigades=client.get('/api/v1/auth/brigades',headers=dispatcher).json()['brigades']
    assert len(brigades)>=1 and brigades[0]['username']=='demo_brigade'
    monkeypatch.setattr(settings,'edds_mis_api_key','')
    assert client.get('/api/v1/integrations/edds-mis-feed').status_code==503
    assert client.get('/api/v1/integrations/edds-mis-feed',headers=admin).status_code==200
    assert client.get('/api/v1/integrations/edds-mis-feed',headers=dispatcher).status_code==200
    assert client.get('/api/v1/integrations/edds-mis-feed',headers=patient).status_code==403

def test_patient_can_list_clinics_without_access_to_dashboard(client):
    patient=login(client,'patient')
    clinics=client.get('/api/v1/clinical/clinics',headers=patient)
    assert clinics.status_code==200 and len(clinics.json()['clinics'])==4
    assert 'disclaimer' in clinics.json()
    assert client.get('/api/v1/clinical/dashboard',headers=patient).status_code==403
