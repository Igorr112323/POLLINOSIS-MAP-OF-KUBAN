"""Experimental phenology models; no claim of clinical validation."""
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import math
import joblib
import numpy as np
from scipy.stats import spearmanr
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
from sklearn.metrics import roc_auc_score, roc_curve, recall_score, accuracy_score
from sqlalchemy import select
from app.core.config import settings
from app.models.tables import Observation, WeeklyObservation, ImportedSample

MODEL_VERSION=2

def daylength(day, latitude=45.035):
    decl=0.409*math.sin(2*math.pi*day.timetuple().tm_yday/365-1.39)
    x=max(-1,min(1,-math.tan(math.radians(latitude))*math.tan(decl)))
    return 24/math.pi*math.acos(x)

def feature(day,tmax,tmin,dew,gdd):
    doy=day.timetuple().tm_yday
    return [gdd,daylength(day),float(doy>172),(tmax+tmin)/2-dew,doy, (tmax+tmin)/2]

def training_data(db):
    imported=list(db.scalars(select(ImportedSample).order_by(ImportedSample.date)))
    if imported:
        # Weather not provided by the user is filled from synthetic reference climatology;
        # weekly means remain one sample per week (not seven fabricated daily records).
        climate={o.date:o for o in db.scalars(select(Observation))}
        rows=[]
        for r in imported:
            ref=r.date
            if ref not in climate:
                try: ref=ref.replace(year=2024)
                except ValueError: ref=date(2024,2,28)
            c=climate.get(ref)
            if not c: raise ValueError('Synthetic reference climatology unavailable')
            rows.append(SimpleNamespace(date=r.date,pollen=r.pollen,tmax=r.tmax if r.tmax is not None else c.tmax,tmin=r.tmin if r.tmin is not None else c.tmin,dew=r.dew if r.dew is not None else c.dew,granularity=r.granularity))
    else:
        rows=list(db.scalars(select(Observation).order_by(Observation.date)))
    xs=[]; ys=[]; totals={}
    gdd_by_date={}
    if imported:
        # Complete the temperature series with explicitly synthetic reference climate;
        # do not mistake missing observation dates for days with zero thermal accumulation.
        samples={r.date:r for r in imported}
        for year in {r.date.year for r in imported}:
            day=date(year,4,1); last=max(r.date for r in imported if r.date.year==year); total=0.
            while day<=last:
                try: ref=day if day in climate else day.replace(year=2024)
                except ValueError: ref=date(2024,2,28)
                c=climate.get(ref)
                if not c: raise ValueError('Synthetic reference climatology unavailable')
                r=samples.get(day)
                if r is None and imported[0].granularity=='weekly': r=samples.get(day-timedelta(days=day.weekday()))
                tmax=r.tmax if r and r.tmax is not None else c.tmax
                tmin=r.tmin if r and r.tmin is not None else c.tmin
                total+=max(0,(tmax+tmin)/2-10)
                gdd_by_date[day]=total; day+=timedelta(days=1)
    for r in rows:
        if imported: g=gdd_by_date.get(r.date,0)
        else:
            g=totals.get(r.date.year,0)
            if r.date.month>=4: g+=max(0,(r.tmax+r.tmin)/2-10)
            totals[r.date.year]=g
        xs.append(feature(r.date,r.tmax,r.tmin,r.dew,g)); ys.append(r.pollen)
    return rows,np.array(xs),np.array(ys)

def initialize(db):
    rows,x,y=training_data(db)
    if not rows: raise ValueError('No observations')
    year=min(r.date.year for r in rows)
    train=np.array([r.date.year==year for r in rows]); labels=y>=settings.clinical_threshold
    if sum(train)<10 or len(np.unique(labels[train]))<2:
        raise ValueError('Training season must contain at least 10 samples and both safe and dangerous classes')
    classifier=GradientBoostingClassifier(n_estimators=80,max_depth=2,learning_rate=.05,random_state=42).fit(x[train],labels[train])
    regressor=GradientBoostingRegressor(n_estimators=100,max_depth=2,learning_rate=.04,loss='huber',random_state=42).fit(x[train],y[train])
    bundle={'classifier':classifier,'regressor':regressor,'threshold':.25,'version':MODEL_VERSION,'training_year':year,'data_provenance':'uploaded_dataset' if db.scalar(select(ImportedSample.date).limit(1)) else 'synthetic_demo'}
    path=Path(settings.model_path); path.parent.mkdir(parents=True,exist_ok=True)
    joblib.dump(bundle,path)
    return bundle

def predict(bundle,features):
    x=np.asarray(features)
    return bundle['classifier'].predict_proba(x)[:,1], np.maximum(0,bundle['regressor'].predict(x))

def validation(db,bundle):
    rows,x,y=training_data(db); probability,estimate=predict(bundle,x)
    year=bundle['training_year']; test=np.array([r.date.year>year for r in rows]); labels=y>=settings.clinical_threshold
    yt=labels[test]; pr=probability[test]; pred=pr>=bundle['threshold']
    fp=int(np.sum(pred & ~yt)); negatives=int(np.sum(~yt))
    if len(yt) and len(np.unique(yt))==2:
        fpr,tpr,thresholds=roc_curve(yt,pr); auc=float(roc_auc_score(yt,pr))
        curve=[{'fpr':float(a),'tpr':float(b),'threshold':float(c) if np.isfinite(c) else None} for a,b,c in zip(fpr,tpr,thresholds)]
    else: auc=None; curve=[]
    mapping={r.date:float(estimate[i]) for i,r in enumerate(rows)}
    if bundle['data_provenance']=='uploaded_dataset':
        samples=list(db.scalars(select(ImportedSample).order_by(ImportedSample.date)))
        grouped={}
        for r in samples:
            day=r.date if r.granularity=='weekly' else r.date-timedelta(days=r.date.weekday())
            grouped.setdefault(day,[]).append(r)
        graph=[{'date':d.isoformat(),'year':d.year,'week':d.isocalendar().week,'observed':float(np.mean([r.pollen for r in rs])),'predicted':float(np.mean([mapping[r.date] for r in rs])),'evaluation':d.year>year,'samples':len(rs)} for d,rs in sorted(grouped.items())]
    else:
        weeks=list(db.scalars(select(WeeklyObservation).order_by(WeeklyObservation.start_date)))
        graph=[]
        for w in weeks:
            vals=[mapping[w.start_date+timedelta(days=j)] for j in range(7)]
            graph.append({'date':w.start_date.isoformat(),'year':w.year,'week':w.season_week,'observed':round(w.pollen,2),'predicted':round(float(np.mean(vals)),2),'evaluation':w.year>year})
    evaluation=[g for g in graph if g['evaluation']]
    rho=p=None
    if len(evaluation)>=3:
        a,b=spearmanr([g['observed'] for g in evaluation],[g['predicted'] for g in evaluation])
        if np.isfinite(a) and np.isfinite(b): rho,p=float(a),float(b)
    blind=np.array([r.date.year==2024 and r.date.year>year for r in rows])
    starts=[]
    for yyear in sorted({r.date.year for r in rows if r.date.year>year}):
        eligible=[i for i,r in enumerate(rows) if r.date.year==yyear and date(yyear,7,15)<=r.date<=date(yyear,9,1)]
        observed=next((rows[i].date for i in eligible if labels[i]),None); forecast=next((rows[i].date for i in eligible if probability[i]>=bundle['threshold']),None)
        if observed and forecast: starts.append({'year':yyear,'observed':observed.isoformat(),'predicted':forecast.isoformat(),'error_days':(forecast-observed).days})
    return {'data_provenance':bundle['data_provenance'],'weather_feature_note':'Missing weather and gaps in thermal accumulation use synthetic climatology; weekly temperature values are repeated across the week for GDD.','training_years':[year],'evaluation_years':sorted({r.date.year for r in rows if r.date.year>year}),'weekly_count':len(graph),'spearman_rho':rho,'p_value':p,'roc_auc':auc,'recall':float(recall_score(yt,pred,zero_division=0)) if np.any(yt) else None,'fpr':fp/negatives if negatives else None,'clinical_probability_threshold':bundle['threshold'],'blind_2024_accuracy':float(accuracy_score(labels[blind],probability[blind]>=bundle['threshold'])) if np.any(blind) else None,'start_window_accuracy':sum(abs(s['error_days'])<=3 for s in starts)/len(starts) if starts and all(getattr(r,'granularity','daily')=='daily' for r in rows) else None,'start_dates':starts,'weekly_series':graph,'roc_curve':curve}
