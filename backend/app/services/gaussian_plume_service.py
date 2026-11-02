"""Steady-state ground-level Gaussian plume; illustrative assumptions, SI units."""
from datetime import date
import math
import numpy as np
from app.core.geography import NEIGHBORHOODS
from app.services.mowing_optimizer_service import vegetation,optimize

LAT=45.035; LON=38.975

def risk(c):
    if c<10: return 'safe'
    if c<50: return 'moderate'
    if c<100: return 'high'
    return 'extreme'

def plume_at(sx,sy,rx,ry,q,wind_speed,wind_from):
    # Positive x in direction TO; wind direction is meteorological FROM, clockwise from north.
    angle=np.deg2rad((wind_from+180)%360); ux=np.sin(angle); uy=np.cos(angle)
    dx=rx[:,None]-sx[None,:]; dy=ry[:,None]-sy[None,:]
    x=dx*ux+dy*uy; y=-dx*uy+dy*ux
    safe_x=np.maximum(x,1.)
    # Pasquill-Gifford class D, rural approximation.
    sigy=np.maximum(1.,.08*safe_x/(1+.0001*safe_x)**.5)
    sigz=np.maximum(1.,.06*safe_x/(1+.0015*safe_x)**.5)
    u=max(.5,wind_speed); height=1.0
    deposition=np.exp(-.015*safe_x/(u*np.maximum(sigz,1.)))
    concentration=q[None,:]/(math.pi*u*sigy*sigz)*np.exp(-.5*(y/sigy)**2)*np.exp(-.5*(height/sigz)**2)*deposition
    return np.where(x>0,concentration,0).sum(axis=1)

def source_vectors(hotspots,target,scenario,phenology):
    sx=np.array([(h.lon-LON)*111320*math.cos(math.radians(LAT)) for h in hotspots]); sy=np.array([(h.lat-LAT)*111320 for h in hotspots])
    selected=set(optimize(hotspots,mowing_deadline_date=date(target.year,8,5))['selected_ids']) if scenario=='optimal_489ha' else set()
    q=[]; burst_count=0
    for h in hotspots:
        # 10^6 grains/s/ha is an illustrative emission assumption, not a measured emission factor.
        factor=1.
        if scenario=='optimal_489ha' and target>=date(target.year,8,5) and h.id in selected: factor=.02
        elif scenario!='no_mowing' and h.status in ('mowed_verified','completed_late_bloom') and h.mowing_date and h.mowing_date.year==target.year and h.mowing_date<=target:
            factor=.02 if h.mowing_date<date(target.year,8,10) else .2
        if scenario in ('late_mowing_august_burst','current') and target>=date(target.year,8,10) and h.mowing_date==target and h.status in ('in_progress','mowed_verified','completed_late_bloom'):
            factor=1.8; burst_count+=1
        q.append(h.area_ha*vegetation(h)[2]*phenology*1_000_000*factor)
    return sx,sy,np.array(q),burst_count

def concentration_at(hotspots,target,wind_speed,wind_dir,phenology,locations,background=0.):
    sx,sy,q,_=source_vectors(hotspots,target,'current',phenology)
    rx=np.array([(lon-LON)*111320*math.cos(math.radians(LAT)) for lat,lon in locations]); ry=np.array([(lat-LAT)*111320 for lat,lon in locations])
    return plume_at(sx,sy,rx,ry,q,wind_speed,wind_dir)+background

def calculate(hotspots,target,wind_speed,wind_dir,scenario,phenology=1.,background=0.):
    offsets=(np.arange(29)-14)*1000
    gx,gy=np.meshgrid(offsets,offsets); rx=gx.ravel(); ry=gy.ravel()
    sx,sy,q,burst_count=source_vectors(hotspots,target,scenario,phenology)
    c=plume_at(sx,sy,rx,ry,q,wind_speed,wind_dir)+background
    features=[]
    for x,y,value in zip(rx,ry,c):
        lon=LON+x/(111320*math.cos(math.radians(LAT))); lat=LAT+y/111320
        dx=500/(111320*math.cos(math.radians(LAT))); dy=500/111320
        features.append({'type':'Feature','geometry':{'type':'Polygon','coordinates':[[[lon-dx,lat-dy],[lon+dx,lat-dy],[lon+dx,lat+dy],[lon-dx,lat+dy],[lon-dx,lat-dy]]]},'properties':{'concentration':round(float(value),2),'risk':risk(value)}})
    nx=np.array([(n[3]-LON)*111320*math.cos(math.radians(LAT)) for n in NEIGHBORHOODS]); ny=np.array([(n[2]-LAT)*111320 for n in NEIGHBORHOODS])
    nc=plume_at(sx,sy,nx,ny,np.array(q),wind_speed,wind_dir)+background
    neighborhoods=[{'name':n[0],'district':n[1],'population_estimate':n[4],'concentration':round(float(v),2),'risk':risk(v)} for n,v in zip(NEIGHBORHOODS,nc)]
    districts=[]
    for district in sorted({n[1] for n in NEIGHBORHOODS}):
        subset=[(n,v) for n,v in zip(NEIGHBORHOODS,nc) if n[1]==district]
        districts.append({'name':district,'population_weighted_concentration':round(sum(n[4]*v for n,v in subset)/sum(n[4] for n,v in subset),2)})
    mean=sum(n[4]*v for n,v in zip(NEIGHBORHOODS,nc))/sum(n[4] for n in NEIGHBORHOODS)
    return {'type':'FeatureCollection','features':features,'summary':{'population_weighted_concentration':round(float(mean),2),'neighborhoods':neighborhoods,'districts':districts,'burst_sources':burst_count,'wind_speed_m_s':wind_speed,'wind_from_degrees':wind_dir,'background':background,'model_notice':'Illustrative steady-state model with synthetic sources and uncalibrated emissions; not suitable for health decisions.'}}
