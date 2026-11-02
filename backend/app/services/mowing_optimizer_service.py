from datetime import date
import math
from app.core.config import settings
from app.core.geography import NEIGHBORHOODS

# Meteorological directions FROM; assumed, not measured.
WIND_ROSE = [(90,.38),(45,.32),(315,.20),(0,.10)]

def vegetation(h):
    ndvi=(h.b08-h.b04)/(h.b08+h.b04)
    ndmi=(h.b08-h.b11)/(h.b08+h.b11) if h.b11 is not None and h.b08+h.b11 and not h.provenance.endswith('ndvi_only') else None
    return ndvi,ndmi,max(.2,min(1.,(ndvi-.2)/.6))

def rank(hotspots):
    results=[]
    for h in hotspots:
        ndvi,ndmi,biomass=vegetation(h); weights=[]; closest=None
        for name,district,lat,lon,pop in NEIGHBORHOODS:
            east=(lon-h.lon)*111320*math.cos(math.radians(h.lat)); north=(lat-h.lat)*111320
            dist=math.hypot(east,north); bearing=math.degrees(math.atan2(east,north))%360
            if closest is None or dist<closest[0]: closest=(dist,bearing,name)
            directional=sum(p*math.exp(-.5*(math.radians((bearing-(wind_from+180)+180)%360-180)/.45)**2) for wind_from,p in WIND_ROSE)
            weights.append(pop*directional*math.exp(-dist/7000))
        probability=min(1.,sum(weights)/sum(n[4] for n in NEIGHBORHOODS))
        exposure=h.area_ha*biomass*probability
        results.append({'id':h.id,'area_ha':h.area_ha,'ndvi':ndvi,'ndre':None,'ndmi':ndmi,'biomass_density':biomass,'transport_probability':probability,'risk_index':exposure,'exposure_risk':exposure,'cost_effectiveness':exposure/h.area_ha,'nearest_neighborhood':closest[2],'distance_m':closest[0],'azimuth_deg':closest[1]})
    results.sort(key=lambda x:(-x['cost_effectiveness'],x['id']))
    for i,r in enumerate(results): r['rank']=i+1
    return results

def optimize(hotspots,budget_share_pct=100,mowing_deadline_date=None,brigades_count=185,target_risk_capture=.99,strategy='target_risk_capture',target_risk_capture_pct=None,max_area_ha=None,max_budget_rub=None,daily_norm_ha=.5,work_start_date=None):
    ranked=rank(hotspots); total_area=sum(r['area_ha'] for r in ranked); total_risk=sum(r['risk_index'] for r in ranked)
    deadline=mowing_deadline_date or date(date.today().year,8,5)
    start=work_start_date or date(deadline.year,7,1)
    working_days=max(0,(deadline-start).days+1); capacity=working_days*brigades_count*daily_norm_ha
    # Retain existing budget_share and capacity limits as safety constraints.
    limit=min(total_area*budget_share_pct/100,capacity)
    if max_area_ha is not None: limit=min(limit,max_area_ha)
    if max_budget_rub is not None: limit=min(limit,total_area*max_budget_rub/settings.budget_rub)
    target=target_risk_capture_pct/100 if target_risk_capture_pct is not None else target_risk_capture
    selected=[]; area=0.; risk=0.
    for r in ranked:
        if strategy=='target_risk_capture' and risk>=total_risk*target: break
        if area+r['area_ha']<=limit+1e-9:
            selected.append(r['id']); area+=r['area_ha']; risk+=r['risk_index']
    cost=settings.budget_rub*area/total_area if total_area else 0
    # Points at whole-site boundaries, not fractional artificial site selections.
    curve=[{'area_ha':0.,'area_pct':0.,'cost_rub':0.,'risk_capture_pct':0.}]
    cumulative_area=0.; cumulative_risk=0.; next_pct=5
    for r in ranked:
        cumulative_area+=r['area_ha']; cumulative_risk+=r['risk_index']
        while total_area and next_pct<=100 and cumulative_area/total_area*100+1e-9>=next_pct:
            curve.append({'area_ha':round(cumulative_area,4),'area_pct':round(cumulative_area/total_area*100,4),'cost_rub':round(settings.budget_rub*cumulative_area/total_area,2),'risk_capture_pct':round(cumulative_risk/total_risk*100,4) if total_risk else 0.})
            next_pct+=5
    if total_area and curve[-1]['area_pct']<99.9999:
        curve.append({'area_ha':round(total_area,4),'area_pct':100.,'cost_rub':settings.budget_rub,'risk_capture_pct':100. if total_risk else 0.})
    return {'strategy':strategy,'selected_ids':selected,'selected_count':len(selected),'total_area_ha':round(total_area,2),'selected_area_ha':round(area,2),'area_reduction_pct':round(100*(1-area/total_area),2) if total_area else 0,'risk_capture_pct':round(100*risk/total_risk,2) if total_risk else 0,'budget_baseline_rub':settings.budget_rub,'estimated_mowing_cost_rub':round(cost,2),'estimated_gross_savings_rub':round(settings.budget_rub-cost,2),'implementation_cost_rub':settings.implementation_cost_rub,'estimated_net_savings_rub':round(settings.budget_rub-cost-settings.implementation_cost_rub,2),'capacity_ha':round(capacity,2),'working_days':working_days,'deadline':deadline.isoformat(),'before_flowering':deadline<=date(deadline.year,8,5),'assumptions':['Risk is a screening proxy, based on assumed wind rose and estimated populations.','Proportional mowing cost; all calendar days assumed workable; whole sites only.','Prevented medical costs cannot be estimated without clinical and economic data.'],'target_risk_capture_pct':target*100,'target_met':risk>=total_risk*target if total_risk else None,'pareto_curve':curve}
