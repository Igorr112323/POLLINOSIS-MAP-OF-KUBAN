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

def test_point_forecast_and_targeted_batch(client):
    point=client.get('/api/v1/gis/point-forecast',params={'latitude':45.09,'longitude':39.0,'mode':'simulation','target_date':'2026-08-24','scenario':'no_mowing'})
    assert point.status_code==200,point.text
    data=point.json(); assert len(data['days'])==14 and data['scenario']=='no_mowing' and 'не измерение' in data['note']
    assert client.get('/api/v1/gis/point-forecast',params={'latitude':90,'longitude':39}).status_code==422
    admin=login(client,'admin'); brigade=login(client,'brigade'); bid=client.get('/api/v1/auth/me',headers=brigade).json()['id']
    body={'mowing_deadline_date':'2026-08-05','work_start_date':'2026-07-01','brigade_ids':[bid],'daily_team_capacity_ha':10,'target_risk_capture_pct':100,'hotspot_ids':[1,2]}
    result=client.post('/api/v1/work-orders/generate-batch',headers=admin,json=body)
    assert result.status_code==201 and {o['hotspot_id'] for o in result.json()['orders']}=={1,2}
    body['hotspot_ids']=[999999]
    assert client.post('/api/v1/work-orders/generate-batch',headers=admin,json=body).status_code==422

def test_demo_water_exclusion_is_applied_to_entire_polygons(client):
    from shapely.geometry import shape, Point
    from app.core.water_mask import demo_water_exclusion
    mask=demo_water_exclusion()
    assert mask.covers(Point(38.92,45.025))  # conservative southern exclusion, includes river bend
    assert mask.covers(Point(39.10,45.00))  # conservative lake/reservoir exclusion
    features=client.get('/api/v1/gis/hotspots').json()['features']
    assert len(features)==189
    assert all(shape(f['geometry']).disjoint(mask) for f in features)

def _synthetic_tif():
    import numpy as np
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    red=np.full((20,20),.2,dtype='float32'); nir=np.full((20,20),.2,dtype='float32');nir[:,:10]=.6
    with MemoryFile() as mem:
        with mem.open(driver='GTiff',height=20,width=20,count=2,dtype='float32',crs='EPSG:4326',transform=from_origin(39.,45.10,.00012,.00009)) as ds:
            ds.write(red,1);ds.write(nir,2)
        return mem.read()

def test_geotiff_real_ndvi_upload_roles_and_idempotence(client):
    admin=login(client,'admin');patient=login(client,'patient'); data=_synthetic_tif()
    files={'b04_tif':('test.tif',data,'image/tiff')}
    assert client.post('/api/v1/satellite/detect-geotiff',files=files).status_code==401
    assert client.post('/api/v1/satellite/detect-geotiff',headers=patient,files=files).status_code==403
    r=client.post('/api/v1/satellite/detect-geotiff',headers=admin,files=files)
    assert r.status_code==200,r.text
    assert r.json()['detected']==1 and r.json()['inserted']==1 and 'не определены' in r.json()['mask_note']
    spots=client.get('/api/v1/gis/hotspots').json()['features']
    assert len(spots)==190
    p=next(f['properties'] for f in spots if f['properties']['data_provenance']=='uploaded_geotiff')
    assert p['ndvi']==pytest.approx(.5,abs=1e-5) and p['bands']['B04']==pytest.approx(.2) and p['bands']['B08']==pytest.approx(.6)
    assert p['bands']['B11'] is None and p['ndmi'] is None and p['scene_id'] is None
    r=client.post('/api/v1/satellite/detect-geotiff',headers=admin,files=files)
    assert r.status_code==200 and r.json()['inserted']==0 and r.json()['updated']==1
    assert len(client.get('/api/v1/gis/hotspots').json()['features'])==190
    assert client.post('/api/v1/satellite/detect-geotiff',headers=admin,files={'b04_tif':('bad.tif',b'not a tif')}).status_code==422
    off=client.post('/api/v1/satellite/scan-live',headers=admin)
    assert off.status_code==503 and 'OFFLINE=true' in off.json()['detail']

def test_scene_classification_and_live_import(client, monkeypatch):
    import numpy as np
    from shapely.geometry import box
    from rasterio.transform import from_origin
    from app.core.config import settings
    from app.services.satellite_service import classify_parcels
    from app.api.v1 import satellite
    from rasterio.crs import CRS
    trans=from_origin(39.,45.1,.00012,.00009)
    parcel={'geometry':box(39.,45.0982,39.0012,45.1),'area_ha':2.,'osm_id':'way/123/0','land_type':'grass','name':'OSM'}
    arrays={'red':np.full((20,20),2000,dtype='float32'),'nir':np.full((20,20),6000,dtype='float32'),'swir16':np.full((20,20),3000,dtype='float32'),'scl':np.full((20,20),4,dtype='float32')}
    cal={k:{'scale':.0001,'offset':0} for k in ('red','nir','swir16')}
    matches=classify_parcels([parcel],arrays,trans,CRS.from_epsg(4326),cal)
    assert len(matches)==1 and matches[0]['ndvi']==pytest.approx(.5) and matches[0]['b11']==pytest.approx(.3)
    shifted={k:{'scale':.0001,'offset':-.1} for k in ('red','nir','swir16')}
    assert classify_parcels([parcel],arrays,trans,CRS.from_epsg(4326),shifted)[0]['ndvi']==pytest.approx(2/3,rel=1e-6)
    arrays['scl'][:]=6
    assert classify_parcels([parcel],arrays,trans,CRS.from_epsg(4326),cal)==[]
    arrays['scl'][:]=4
    scene={'scene_id':'S2A_TEST','acquired_at':'2026-08-24T00:00:00Z','cloud_cover':4.,'thumbnail':None,'coverage_pct':100.,'season':'test','calibration':cal}
    monkeypatch.setattr(settings,'offline',False)
    monkeypatch.setattr(satellite,'find_scene',lambda:(scene,{}))
    monkeypatch.setattr(satellite,'osm_parcels',lambda:[parcel])
    monkeypatch.setattr(satellite,'read_scene',lambda urls:(arrays,trans,CRS.from_epsg(4326)))
    r=client.post('/api/v1/satellite/scan-live',headers=login(client,'admin'))
    assert r.status_code==200,r.text
    assert r.json()['scene']['scene_id']=='S2A_TEST' and r.json()['inserted']==1
    p=next(f['properties'] for f in client.get('/api/v1/gis/hotspots').json()['features'] if f['properties']['data_provenance']=='sentinel2_osm_live')
    assert p['scene_id']=='S2A_TEST' and p['source_key'].endswith('way/123/0') and p['ndvi']==pytest.approx(.5)
    assert client.get('/api/v1/satellite/latest-scene').json()['scene']['scene_id']=='S2A_TEST'

def test_stac_search_and_osm_geometry_parsing(monkeypatch):
    from app.services import satellite_service as sat
    from shapely.geometry import mapping,box
    assets={k:{'href':f'https://sentinel-cogs.s3.us-west-2.amazonaws.com/test/{k}.tif','raster:bands':[{'scale':.0001,'offset':-.1}]} for k in ('red','nir','swir16','scl')}
    assets['thumbnail']={'href':'https://sentinel-cogs.s3.us-west-2.amazonaws.com/test/thumbnail.jpg'}
    feature={'type':'Feature','id':'scene-actual-id','geometry':mapping(box(38.8,44.9,39.2,45.2)),'properties':{'datetime':'2026-08-15T09:00:00Z','eo:cloud_cover':5},'assets':assets}
    calls=[]
    def fake(url,**kwargs):
        calls.append((url,kwargs))
        if url==sat.STAC:return {'features':[feature]}
        def ring(x,y,step):return [{'lon':x,'lat':y},{'lon':x+step,'lat':y},{'lon':x+step,'lat':y+step},{'lon':x,'lat':y+step},{'lon':x,'lat':y}]
        return {'elements':[{'type':'way','id':101,'tags':{'landuse':'grass'},'geometry':ring(39.,45.08,.001)}, {'type':'way','id':102,'tags':{'landuse':'forest'},'geometry':ring(39.01,45.08,.001)}, {'type':'way','id':103,'tags':{'natural':'scrub'},'geometry':ring(39.01,45.08,.001)}]}
    monkeypatch.setattr(sat,'_json_post',fake)
    scene,urls=sat.find_scene(date(2026,11,3));assert scene['scene_id']=='scene-actual-id' and scene['coverage_pct']==100 and urls['red'].endswith('red.tif') and scene['calibration']['red']['offset']==-.1
    search=calls[0][1]['json'];assert search['collections']==['sentinel-2-l2a'] and search['query']['eo:cloud_cover']['lt']==20 and search['datetime'].startswith('2026-07-25')
    parcels=sat.osm_parcels();assert len(parcels)==1 and parcels[0]['osm_id']=='way/101/0'
    assert all(term in calls[1][1]['data']['data'] for term in ('industrial','scrub','forest','44.98,38.88,45.14,39.13'))
    feature['assets']['red']['href']='https://untrusted.example/red.tif'
    with pytest.raises(sat.SatelliteError):sat.find_scene(date(2026,11,3))

def test_remote_window_reader_aligns_bands_on_common_grid(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from app.services.satellite_service import read_scene, BBOX
    urls={}
    for name,value in [('red',2000),('nir',6000),('swir16',3000),('scl',4)]:
        p=tmp_path/(name+'.tif');urls[name]=str(p)
        with rasterio.open(p,'w',driver='GTiff',height=80,width=100,count=1,dtype='uint16',crs='EPSG:4326',transform=from_bounds(*BBOX,100,80)) as ds:ds.write(np.full((80,100),value,dtype='uint16'),1)
    arrays,trans,crs=read_scene(urls)
    assert all(a.shape==(512,512) for a in arrays.values()) and str(crs)=='EPSG:4326'
    assert arrays['red'][256,256]==2000 and arrays['nir'][256,256]==6000 and arrays['scl'][256,256]==4

def test_two_geotiff_bands_and_reserved_candidate_is_not_overwritten(client):
    import numpy as np
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin
    def image(value, x=39.):
        with MemoryFile() as mem:
            with mem.open(driver='GTiff',height=20,width=20,count=1,dtype='uint16',crs='EPSG:4326',transform=from_origin(x,45.1,.00012,.00009)) as ds:
                ds.write(np.full((20,20),value,dtype='uint16'),1)
            return mem.read()
    admin=login(client,'admin');red=image(2000);nir=image(6000)
    files={'b04_tif':('red.tif',red,'image/tiff'),'b08_tif':('nir.tif',nir,'image/tiff')}
    data={'reflectance_scale':'0.0001','reflectance_offset':'-0.1'}
    r=client.post('/api/v1/satellite/detect-geotiff',headers=admin,files=files,data=data)
    assert r.status_code==200,r.text
    p=next(f['properties'] for f in client.get('/api/v1/gis/hotspots').json()['features'] if f['properties']['data_provenance']=='uploaded_geotiff')
    assert p['ndvi']==pytest.approx(2/3,rel=1e-6)
    with SessionLocal() as db:
        db.add(WorkOrder(hotspot_id=p['id'],priority_rank=1,planned_date=date(2026,8,1),status='planned'))
        db.commit()
    r=client.post('/api/v1/satellite/detect-geotiff',headers=admin,files=files,data=data)
    assert r.status_code==200 and r.json()['skipped_reserved']==1 and r.json()['updated']==0
    files['b08_tif']=('mismatch.tif',image(6000,39.01),'image/tiff')
    assert client.post('/api/v1/satellite/detect-geotiff',headers=admin,files=files,data=data).status_code==422

def test_satellite_migration_preserves_existing_rows(tmp_path):
    from sqlalchemy import create_engine, text, inspect
    from app.db.session import migrate_satellite
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE hotspots (id INTEGER PRIMARY KEY, data_provenance VARCHAR)'))
        conn.execute(text("INSERT INTO hotspots(id,data_provenance) VALUES(1,'uploaded_dataset')"))
    migrate_satellite(engine);migrate_satellite(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT id,data_provenance,scene_id,source_key,observed_ndvi FROM hotspots')).one()==(1,'uploaded_dataset',None,None,None)
    assert {'scene_id','source_key','observed_ndvi'} <= {c['name'] for c in inspect(engine).get_columns('hotspots')}
    engine.dispose()

def test_snapshot_import_is_validated_additive_and_labels_cache(client, monkeypatch):
    import sys
    import importlib
    import numpy as np
    from datetime import datetime, timedelta, timezone
    from shapely.geometry import box, mapping
    root=Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root/'scripts'))
    fetch=importlib.import_module('fetch_live_kuban_data')
    imp=importlib.import_module('import_live_kuban_data')
    def mock_weather(url,params):
        first=date.fromisoformat(params['start_date']);last=date.fromisoformat(params['end_date'])
        dates=[(first+timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
        daily={'time':dates};daily.update({key:[val]*len(dates) for key,val in zip(fetch.DAILY,[30,20,0,2,90,20])})
        return {'daily':daily,'hourly':{'time':[d+'T12:00' for d in dates],'dew_point_2m':[15]*len(dates)}}
    monkeypatch.setattr(fetch,'_http_get',mock_weather)
    weather=fetch.fetch_weather(date(2026,11,3));windows=imp.validate_weather(weather)
    assert weather['archive']['parameters']['models']=='era5' and len(windows)==50
    shape=box(39.0,45.08,39.001,45.081)
    scene={'scene_id':'S2_MOCK_TEST','acquired_at':'2026-08-24T10:00:00Z','cloud_cover':3.,'thumbnail':None,'calibration':{k:{'scale':.0001,'offset':-.1} for k in ('red','nir','swir16')}}
    doc={'type':'FeatureCollection','schema_version':1,'bbox':list(imp.BBOX),'retrieved_at':datetime.now(timezone.utc).isoformat(),'scene':scene,'asset_urls':{k:'https://sentinel-cogs.s3.us-west-2.amazonaws.com/test/'+k+'.tif' for k in ('red','nir','swir16','scl')},'features':[{'type':'Feature','geometry':mapping(shape),'properties':{'osm_id':'way/101/0','land_type':'grass','name':'test','area_ha':imp._area_ha(shape),'ndvi':.5,'bands':{'B04':.1,'B08':.3,'B11':.2},'valid_pixels':3,'scene_id':'S2_MOCK_TEST','data_provenance':'sentinel2_osm_live'}}]}
    validated,sites=imp.validate_satellite(doc)
    with SessionLocal() as db: result=imp.import_data(db,validated,sites,windows)
    assert result['satellite']['inserted']==1 and result['weather_cache']['inserted']==50
    entries=client.get('/api/v1/gis/hotspots').json()['features'];assert len(entries)==190
    assert any(f['properties']['scene_id']=='S2_MOCK_TEST' for f in entries)
    r=client.get('/api/v1/weather/forecast',params={'mode':'simulation','target_date':'2026-07-25'})
    assert r.status_code==200 and r.json()['source']=='open-meteo-cache' and r.json()['cache_origin']=='open-meteo-era5' and r.json()['days'][0]['tmax']==30
    doc['features'][0]['properties']['scene_id']='different'
    with pytest.raises(ValueError):imp.validate_satellite(doc)
    weather['archive']['parameters']['models']='other'
    with pytest.raises(ValueError):imp.validate_weather(weather)
