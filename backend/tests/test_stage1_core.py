import os
import tempfile
from pathlib import Path
from datetime import date
_test_dir = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL']='sqlite:///' + str(Path(_test_dir.name) / 'stage1-pollinoz-test.db')
os.environ['MODEL_PATH']=str(Path(_test_dir.name) / 'test.joblib')
os.environ['UPLOAD_DIR']=str(Path(_test_dir.name) / 'uploads')
os.environ['OFFLINE']='true'
os.environ['DEMO_MODE']='true'
os.environ['DEMO_ACCOUNTS_ENABLED']='true'
from fastapi.testclient import TestClient
import numpy as np
import pytest
from sqlalchemy import select,func
from app.main import app
from app.db.session import SessionLocal
from app.models.tables import Hotspot,WeeklyObservation,Observation
from app.services.gaussian_plume_service import plume_at,calculate
from app.services.mowing_optimizer_service import optimize, vegetation
from app.services.phenology_ml_service import daylength,validation

@pytest.fixture(scope='module')
def client():
    with TestClient(app) as c: yield c

def test_database(client):
    health=client.get('/api/v1/health').json()
    assert health['hotspots']==189 and health['weekly_observations']==109
    assert health['daily_observations']==1461 and health['model_loaded']
    with SessionLocal() as db:
        assert db.scalar(select(func.sum(Hotspot.area_ha)))==pytest.approx(589,abs=.01)
        assert db.scalar(select(func.count()).select_from(WeeklyObservation).where(WeeklyObservation.season_week!=None))==72

def test_geometry_and_spectral_indices(client):
    obj=client.get('/api/v1/gis/hotspots').json()
    assert obj['type']=='FeatureCollection' and len(obj['features'])==189
    for f in obj['features']:
        ring=f['geometry']['coordinates'][0]; assert ring[0]==ring[-1]
        p=f['properties']; b=p['bands']
        assert p['ndvi']==pytest.approx((b['B08']-b['B04'])/(b['B08']+b['B04']))
        assert p['ndmi']==pytest.approx((b['B08']-b['B11'])/(b['B08']+b['B11']))
        assert .2<=p['biomass_density']<=1
        assert p['provenance']=='synthetic'
    assert all(f['properties']['district']=='Западный' for f in client.get('/api/v1/gis/hotspots',params={'district':'Западный'}).json()['features'])

def test_validation_is_real_and_no_leakage(client):
    report=client.get('/api/v1/validation/kubgmu-report').json()
    assert 'synthetic' in report['data_provenance']
    assert report['training_years']==[2023] and report['evaluation_years']==[2024,2025,2026]
    assert report['weekly_count']==109 and len(report['weekly_series'])==109
    assert -1<=report['spearman_rho']<=1 and 0<=report['p_value']<=1
    assert 0<=report['roc_auc']<=1 and 0<=report['recall']<=1 and 0<=report['fpr']<=1
    assert len(report['roc_curve'])>=2
    assert report['roc_curve'][0]['threshold'] is None
    assert daylength(date(2026,6,21))>daylength(date(2026,8,21))

def test_gaussian_downwind():
    vals=plume_at(np.array([0]),np.array([0]),np.array([1000.,-1000.]),np.array([0.,0.]),np.array([1e6]),2,270)
    assert vals[0]>0 and vals[1]==0

def test_scenarios_and_burst(client):
    with SessionLocal() as db:
        hs=list(db.scalars(select(Hotspot).order_by(Hotspot.id)))
        today=date(2026,8,24)
        baseline=calculate(hs,today,2,90,'no_mowing')
        opt=calculate(hs,today,2,90,'optimal_489ha')
        assert len(opt['features'])==841
        assert opt['summary']['population_weighted_concentration'] < baseline['summary']['population_weighted_concentration']
        hs[0].status='in_progress'; hs[0].mowing_date=today
        burst=calculate(hs,today,2,90,'late_mowing_august_burst')
        assert burst['summary']['burst_sources']==1
        assert sum(f['properties']['concentration'] for f in burst['features']) > sum(f['properties']['concentration'] for f in baseline['features'])

def test_optimizer_budget(client):
    result=client.post('/api/v1/optimizer/calculate',json={'budget_share_pct':100,'mowing_deadline_date':'2026-08-05','brigades_count':185}).json()
    assert result['total_area_ha']==pytest.approx(589)
    assert 0<result['selected_area_ha']<=589 and result['risk_capture_pct']>=99
    assert result['estimated_gross_savings_rub']==pytest.approx(198000000*(1-result['selected_area_ha']/589),abs=2000)
    limited=client.post('/api/v1/optimizer/calculate',json={'budget_share_pct':10,'brigades_count':185,'mowing_deadline_date':'2026-08-05'}).json()
    assert limited['selected_area_ha']<=58.9
    assert client.post('/api/v1/optimizer/calculate',json={'budget_share_pct':101}).status_code==422

def test_offline_weather_and_endpoints(client):
    params={'mode':'simulation','target_date':'2026-08-24'}
    r=client.get('/api/v1/weather/forecast',params=params)
    assert r.status_code==200 and r.json()['source']=='synthetic-climatology'
    assert len(r.json()['days'])==14 and r.json()['gdd_10']>0
    assert client.get('/api/v1/weather/forecast',params={'municipality':'unknown'}).status_code==422
    assert client.get('/api/v1/weather/forecast',params={'mode':'simulation','target_date':'2030-01-01'}).status_code==422
    assert len(client.get('/api/v1/forecast/clinical',params=params).json()['days'])==14
    grid=client.get('/api/v1/gis/plume-grid',params={**params,'wind_dir_override':90,'wind_speed_override':2})
    assert grid.status_code==200 and len(grid.json()['features'])==841
    assert client.get('/api/v1/system/passport').status_code==200

def test_live_weather_cached_then_network_failure(client,monkeypatch):
    import httpx
    from datetime import timedelta
    from app.core.config import settings
    start=date(2026,7,25)
    dates=[(start+timedelta(days=i)).isoformat() for i in range(14)]
    payload={'daily':{'time':dates,'temperature_2m_max':[30]*14,'temperature_2m_min':[20]*14,'precipitation_sum':[0]*14,'wind_speed_10m_max':[2]*14,'wind_direction_10m_dominant':[90]*14,'shortwave_radiation_sum':[20]*14},'hourly':{'time':[d+'T12:00' for d in dates],'dew_point_2m':[15]*14}}
    async def successful(self,url,params):
        assert params['forecast_days']==14 and params['wind_speed_unit']=='ms'
        return httpx.Response(200,json=payload,request=httpx.Request('GET',url))
    monkeypatch.setattr(settings,'offline',False)
    monkeypatch.setattr(httpx.AsyncClient,'get',successful)
    params={'mode':'live','target_date':start.isoformat()}
    live=client.get('/api/v1/weather/forecast',params=params).json()
    assert live['source']=='open-meteo-live' and len(live['days'])==14
    async def failure(self,url,params): raise httpx.ConnectError('network unavailable')
    monkeypatch.setattr(httpx.AsyncClient,'get',failure)
    cached=client.get('/api/v1/weather/forecast',params=params).json()
    assert cached['source']=='open-meteo-cache' and cached['days']==live['days']

def test_simultaneous_live_cache_insert_is_safe(client, monkeypatch):
    """Another request may populate the same cache while Open-Meteo is in flight."""
    import httpx
    from datetime import timedelta, datetime, timezone
    from app.core.config import settings
    from app.models.tables import WeatherCache
    start=date(2026,7,26)
    dates=[(start+timedelta(days=i)).isoformat() for i in range(14)]
    payload={'daily':{'time':dates,'temperature_2m_max':[30]*14,'temperature_2m_min':[20]*14,'precipitation_sum':[0]*14,'wind_speed_10m_max':[2]*14,'wind_direction_10m_dominant':[90]*14,'shortwave_radiation_sum':[20]*14},'hourly':{'time':[d+'T12:00' for d in dates],'dew_point_2m':[15]*14}}
    async def competing_insert(self,url,params):
        with SessionLocal() as other:
            other.add(WeatherCache(key=f'Краснодар:live:{start}',payload=[],updated_at=datetime.now(timezone.utc)))
            other.commit()
        return httpx.Response(200,json=payload,request=httpx.Request('GET',url))
    monkeypatch.setattr(settings,'offline',False)
    monkeypatch.setattr(httpx.AsyncClient,'get',competing_insert)
    response=client.get('/api/v1/weather/forecast',params={'mode':'live','target_date':start.isoformat()})
    assert response.status_code==200 and response.json()['source']=='open-meteo-live'
