from threading import Lock
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.session import get_db
from app.models.tables import SatelliteScene, User
from app.services.auth_service import require
from app.services.satellite_service import SatelliteError, find_scene, osm_parcels, read_scene, classify_parcels, offline_candidates, save_candidates

router=APIRouter(prefix='/satellite',tags=['satellite'])
admin=Depends(require('admin'))
_scan_lock=Lock()  # Single-worker service; prevents concurrent scans in this process.
MAX_BYTES=25_000_000

def _acquire():
    if not _scan_lock.acquire(blocking=False): raise HTTPException(409,'Сканирование уже выполняется')

@router.post('/scan-live')
def scan_live(db:Session=Depends(get_db),user:User=admin):
    if settings.offline:raise HTTPException(503,'OFFLINE=true: живой спутниковый скан отключён. Используйте локальный GeoTIFF или включите сеть.')
    _acquire()
    try:
        scene,urls=find_scene(); parcels=osm_parcels(); arrays,trans,crs=read_scene(urls)
        candidates=classify_parcels(parcels,arrays,trans,crs,scene['calibration'])
        result=save_candidates(db,candidates,'sentinel2_osm_live',scene)
        result.update({'osm_parcels':len(parcels),'water_cloud_mask':'SCL 4 (растительность); леса и парки по OSM исключены','source_format':'sentinel2_l2a_osm'})
        return result
    except SatelliteError as e:
        db.rollback();raise HTTPException(502,str(e)) from e
    except SQLAlchemyError as e:
        db.rollback();raise HTTPException(409,'Не удалось сохранить результаты скана; данные не изменены.') from e
    finally:_scan_lock.release()

@router.post('/detect-geotiff')
def detect_geotiff(b04_tif:Annotated[UploadFile,File()],b08_tif:Annotated[UploadFile|None,File()]=None,red_band:Annotated[int,Form(ge=1,le=100)]=1,nir_band:Annotated[int,Form(ge=1,le=100)]=2,reflectance_scale:Annotated[float,Form(gt=0,le=1)]=1.,reflectance_offset:Annotated[float,Form(ge=-1,le=1)]=0.,db:Session=Depends(get_db),user:User=admin):
    _acquire()
    try:
        red=b04_tif.file.read(MAX_BYTES+1); nir=b08_tif.file.read(MAX_BYTES+1) if b08_tif else None
        if len(red)>MAX_BYTES or (nir is not None and len(nir)>MAX_BYTES):raise HTTPException(413,'Каждый файл должен быть не более 25 МБ')
        if not red or (nir is not None and not nir):raise HTTPException(422,'Пустой GeoTIFF')
        candidates=offline_candidates(red,nir,red_band,nir_band,reflectance_scale,reflectance_offset)
        result=save_candidates(db,candidates,'uploaded_geotiff')
        result.update({'source_format':'uploaded_geotiff','mask_note':'SCL и OSM не предоставлены: вода, облака, застройка и вид растений не определены.'})
        return result
    except SatelliteError as e:
        db.rollback();raise HTTPException(422,str(e)) from e
    except SQLAlchemyError as e:
        db.rollback();raise HTTPException(409,'Не удалось сохранить результаты; данные не изменены.') from e
    finally:_scan_lock.release()

@router.get('/latest-scene')
def latest_scene(db:Session=Depends(get_db)):
    scene=db.scalar(select(SatelliteScene).order_by(SatelliteScene.searched_at.desc()).limit(1))
    return {'scene':{'scene_id':scene.scene_id,'acquired_at':scene.acquired_at,'cloud_cover':scene.cloud_cover,'coverage_pct':scene.coverage_pct,'thumbnail':scene.thumbnail} if scene else None}
