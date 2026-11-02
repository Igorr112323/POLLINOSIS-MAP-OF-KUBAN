"""Vegetation screening from OSM land parcels and Sentinel-2, not species detection.

Only public STAC / Overpass APIs and public COGs are used. Raster values are
actual observations; results are candidates requiring field verification.
"""
from datetime import date, datetime, timezone
import hashlib
import json
import math
from urllib.parse import urlparse

import httpx
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask, shapes
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from pyproj import Geod, Transformer
from shapely.geometry import Polygon, LineString, shape, mapping, box
from shapely.ops import polygonize, transform, unary_union
from sqlalchemy import select, func
from app.core.geography import NEIGHBORHOODS
from app.models.tables import Hotspot, SatelliteScene, WorkOrder

BBOX=(38.88,44.98,39.13,45.14)
STAC='https://earth-search.aws.element84.com/v1/search'
OVERPASS='https://overpass-api.de/api/interpreter'
# Public mirrors; the primary may reject shared CI runner IPs.
OVERPASS_ENDPOINTS=('https://maps.mail.ru/osm/tools/overpass/api/interpreter','https://overpass.private.coffee/api/interpreter',OVERPASS,'https://overpass.kumi.systems/api/interpreter')
COG_HOSTS={'sentinel-cogs.s3.us-west-2.amazonaws.com','sentinel-cogs.s3.amazonaws.com','earth-search-sentinel-data.s3.us-west-2.amazonaws.com'}
THUMB_HOSTS=COG_HOSTS | {'earth-search.aws.element84.com'}
GEOD=Geod(ellps='WGS84')
MAX_CANDIDATES=1000

class SatelliteError(Exception):
    pass

def _trusted_url(url, hosts):
    p=urlparse(url or '')
    if p.scheme!='https' or p.hostname not in hosts or p.username or p.password or p.port not in (None,443):
        raise SatelliteError('Каталог вернул неподдерживаемый адрес спутникового файла')
    return url

def _json_post(url, timeout=90, **kwargs):
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout,connect=10),follow_redirects=False) as client:
            with client.stream('POST',url,**kwargs) as response:
                if response.is_error:
                    response.read()
                    detail=response.text[:400].replace('\n',' ')
                    raise SatelliteError(f'{urlparse(url).hostname}: HTTP {response.status_code}: {detail}')
                chunks=[]; size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>30_000_000: raise SatelliteError('Ответ геосервиса слишком велик')
                    chunks.append(chunk)
        return json.loads(b''.join(chunks))
    except (httpx.HTTPError,ValueError) as e:
        raise SatelliteError(f'Геосервис недоступен или вернул некорректные данные: {type(e).__name__}') from e

def _season(today):
    year=today.year if (today.month,today.day)>=(7,25) else today.year-1
    start=date(year,7,25); end=min(today,date(year,8,31))
    return f'{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z'

def find_scene(today=None, window=None, max_cloud=20):
    window=window or _season(today or date.today())
    data=_json_post(STAC,json={'collections':['sentinel-2-l2a'],'bbox':list(BBOX),'datetime':window,'query':{'eo:cloud_cover':{'lt':max_cloud}},'limit':100,'sortby':[{'field':'properties.datetime','direction':'desc'}]})
    features=data.get('features',[]); candidates=[]; area=box(*BBOX)
    for f in features:
        try:
            props=f['properties']; cloud=float(props['eo:cloud_cover']); assets=f['assets']; geom=shape(f['geometry'])
            if not math.isfinite(cloud) or not 0<=cloud<max_cloud or not all(k in assets for k in ('red','nir','swir16','scl')): continue
            coverage=geom.intersection(area).area/area.area
            if coverage<=0: continue
            candidates.append((coverage,str(props['datetime']),f))
        except (KeyError,TypeError,ValueError): continue
    if not candidates: raise SatelliteError(f'Не найдена сцена Sentinel-2 L2A с нужными каналами и облачностью ниже {max_cloud}% за сезон '+window[:10])
    # Prefer complete coverage, otherwise explicitly report the partial coverage.
    candidates.sort(key=lambda x:(x[0]>=.999,x[1],x[0]),reverse=True)
    coverage,_,f=candidates[0]; props=f['properties']; assets=f['assets']
    urls={k:_trusted_url(assets[k]['href'],COG_HOSTS) for k in ('red','nir','swir16','scl')}
    calibration={}
    for band in ('red','nir','swir16'):
        try:
            metadata=assets[band]['raster:bands'][0]
            scale=float(metadata['scale']); offset=float(metadata['offset'])
            if not (math.isfinite(scale) and math.isfinite(offset) and 0<scale<=1 and -1<=offset<=1): raise ValueError()
            calibration[band]={'scale':scale,'offset':offset}
        except (KeyError,IndexError,TypeError,ValueError) as e:
            raise SatelliteError(f'У канала {band} отсутствует корректный scale/offset в STAC; расчёт отменён') from e
    thumbnail=assets.get('thumbnail',{}).get('href')
    try: thumbnail=_trusted_url(thumbnail,THUMB_HOSTS) if thumbnail else None
    except SatelliteError: thumbnail=None
    return {'scene_id':str(f['id'])[:250],'acquired_at':str(props['datetime']),'cloud_cover':float(props['eo:cloud_cover']),'thumbnail':thumbnail,'coverage_pct':round(coverage*100,1),'season':window,'calibration':calibration},urls

def _polygons(element):
    def coords(geom): return [(float(p['lon']),float(p['lat'])) for p in geom]
    if element.get('type')=='way':
        pts=coords(element.get('geometry',[]))
        if len(pts)>=4 and pts[0]==pts[-1]:
            p=Polygon(pts)
            if p.is_valid and not p.is_empty: return [p]
        return []
    if element.get('type')!='relation': return []
    outer=[];inner=[]
    for m in element.get('members',[]):
        pts=coords(m.get('geometry',[]))
        if len(pts)>=2: (inner if m.get('role')=='inner' else outer).append(LineString(pts))
    if not outer:return []
    joined=unary_union(list(polygonize(unary_union(outer))))
    if inner: joined=joined.difference(unary_union(list(polygonize(unary_union(inner)))))
    if joined.is_empty:return []
    return [joined] if joined.geom_type=='Polygon' else list(joined.geoms) if joined.geom_type=='MultiPolygon' else []

def _area_ha(poly):
    return abs(GEOD.geometry_area_perimeter(poly)[0])/10000

def osm_parcels(return_endpoint=False):
    bbox='44.98,38.88,45.14,39.13'
    land='brownfield|construction|industrial|railway|grass|meadow'
    natural='scrub|grassland'
    query=f'''[out:json][timeout:75];(way["landuse"~"^({land})$"]({bbox});relation["landuse"~"^({land})$"]({bbox});way["natural"~"^({natural})$"]({bbox});relation["natural"~"^({natural})$"]({bbox});way["landuse"="forest"]({bbox});relation["landuse"="forest"]({bbox});way["natural"="wood"]({bbox});relation["natural"="wood"]({bbox});way["leisure"="park"]({bbox});relation["leisure"="park"]({bbox}););out geom;'''
    errors=[]; data=None; endpoint=None
    for candidate in OVERPASS_ENDPOINTS:
        try:
            data=_json_post(candidate,data={'data':query});endpoint=candidate;break
        except SatelliteError as e:errors.append(str(e))
    if data is None:raise SatelliteError('Все Overpass-серверы недоступны: '+'; '.join(errors))
    lands=[]; forests=[]; bounds=box(*BBOX)
    for el in data.get('elements',[]):
        try:
            tags=el.get('tags',{}); polys=_polygons(el)
            forest=tags.get('natural')=='wood' or tags.get('landuse')=='forest' or tags.get('leisure')=='park'
            for index,p in enumerate(polys):
                if forest: forests.append(p);continue
                if not bounds.covers(p):continue  # do not invent clipped boundaries
                area=_area_ha(p)
                if .2<=area<=25: lands.append({'geometry':p,'area_ha':area,'osm_id':f"{el['type']}/{el['id']}/{index}",'land_type':str(tags.get('landuse') or tags.get('natural')),'name':str(tags.get('name') or 'OSM участок')[:200]})
        except (KeyError,TypeError,ValueError): continue
    forest=unary_union(forests) if forests else None
    # Exclude whole parcels that intersect mapped woods/parks, not just their centroids.
    filtered=[p for p in lands if forest is None or p['geometry'].disjoint(forest)]
    return (filtered,endpoint) if return_endpoint else filtered

def read_scene(urls, resolution=None, grid=None):
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',GDAL_HTTP_TIMEOUT='30',GDAL_HTTP_MAX_RETRY='1',CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.tif,.tiff'):
            with rasterio.open(urls['red']) as src:
                if not src.crs: raise SatelliteError('У сцены отсутствует система координат')
                crs=src.crs; left,bottom,right,top=transform_bounds('EPSG:4326',crs,*BBOX,densify_pts=21)
            if grid is not None:
                trans,crs,height,width=grid
            else:
                width=height=512 if resolution is None else None
                if resolution is not None:
                    width=max(1,math.ceil((right-left)/resolution));height=max(1,math.ceil((top-bottom)/resolution))
                    if width*height>6_000_000:raise SatelliteError('Окно спутникового снимка слишком велико')
                trans=from_bounds(left,bottom,right,top,width,height)
            arrays={}
            for key,url in urls.items():
                with rasterio.open(url) as src, WarpedVRT(src,crs=crs,transform=trans,width=width,height=height,resampling=Resampling.nearest if key=='scl' else Resampling.bilinear) as vrt:
                    arrays[key]=vrt.read(1,masked=True).astype('float32').filled(np.nan)
        return arrays,trans,crs
    except (rasterio.errors.RasterioError, OSError, ValueError) as e:
        raise SatelliteError('Не удалось прочитать публичные COG Sentinel-2: '+str(e)[:250]) from e

def _indices(red,nir):
    denom=nir+red
    with np.errstate(divide='ignore',invalid='ignore'):
        ndvi=np.divide(nir-red,denom,out=np.full(red.shape,np.nan,dtype='float32'),where=denom>0)
    return ndvi

def classify_parcels(parcels,arrays,trans,crs,calibration):
    def reflectance(band):return arrays[band]*calibration[band]['scale']+calibration[band]['offset']
    red=reflectance('red');nir=reflectance('nir');swir=reflectance('swir16');scl=arrays['scl'];ndvi=_indices(red,nir)
    valid=np.isin(scl,[4])&np.isfinite(ndvi)&(red>=0)&(red<=1)&(nir>=0)&(nir<=1)&(swir>=0)&(swir<=1)
    project=Transformer.from_crs('EPSG:4326',crs,always_xy=True).transform
    result=[]
    for parcel in parcels:
        p=transform(project,parcel['geometry']); inside=geometry_mask([mapping(p)],out_shape=red.shape,transform=trans,invert=True)
        count=int(inside.sum()); usable=inside&valid
        if not count or int(usable.sum())/count<.5:continue
        mean=float(np.mean(ndvi[usable]))
        if mean<.38:continue
        result.append({**parcel,'ndvi':mean,'b04':float(np.mean(red[usable])),'b08':float(np.mean(nir[usable])),'b11':float(np.mean(swir[usable])),'valid_pixels':int(usable.sum())})
        if len(result)>MAX_CANDIDATES:raise SatelliteError('Слишком много кандидатов; импорт отменён')
    return result

def offline_candidates(red_bytes,nir_bytes=None,red_band=1,nir_band=2,reflectance_scale=1.,reflectance_offset=0.):
    if not math.isfinite(reflectance_scale) or not 0<reflectance_scale<=1 or not math.isfinite(reflectance_offset) or not -1<=reflectance_offset<=1:raise SatelliteError('Некорректный масштаб или сдвиг отражения')
    try:
        with MemoryFile(red_bytes) as rm:
            with rm.open() as a:
                if a.driver!='GTiff' or not a.crs or a.width*a.height>64_000_000 or not 1<=red_band<=a.count:raise SatelliteError('Нужен геопривязанный GeoTIFF и корректный номер красного канала (не более 64 млн пикселей)')
                factor=min(1,1024/max(a.width,a.height));h=max(1,round(a.height*factor));w=max(1,round(a.width*factor))
                bounds=transform_bounds(a.crs,'EPSG:4326',*a.bounds,densify_pts=21)
                if not box(*BBOX).covers(box(*bounds)):raise SatelliteError('GeoTIFF должен целиком находиться в пределах области Краснодара')
                trans=a.transform*a.transform.scale(a.width/w,a.height/h);crs=a.crs
                red=a.read(red_band,out_shape=(h,w),masked=True,resampling=Resampling.nearest).astype('float32').filled(np.nan)*reflectance_scale+reflectance_offset
                if nir_bytes is None:
                    if not 1<=nir_band<=a.count:raise SatelliteError('В многоканальном GeoTIFF отсутствует указанный NIR канал')
                    nir=a.read(nir_band,out_shape=(h,w),masked=True,resampling=Resampling.nearest).astype('float32').filled(np.nan)*reflectance_scale+reflectance_offset
                else:
                    with MemoryFile(nir_bytes) as nm:
                        with nm.open() as b:
                            if b.driver!='GTiff' or b.crs!=a.crs or b.transform!=a.transform or b.width!=a.width or b.height!=a.height:raise SatelliteError('B04 и B08 должны иметь одинаковую сетку, CRS и размеры')
                            nir=b.read(1,out_shape=(h,w),masked=True,resampling=Resampling.nearest).astype('float32').filled(np.nan)*reflectance_scale+reflectance_offset
        invalid=((red<0)|(red>1)|(nir<0)|(nir>1))&np.isfinite(red)&np.isfinite(nir)
        if invalid.any(): raise SatelliteError('Каналы вне диапазона отражения 0..1: задайте правильный масштаб, например 0.0001')
        ndvi=_indices(red,nir);mask=np.isfinite(ndvi)&(ndvi>=.38)
        to_wgs=Transformer.from_crs(crs,'EPSG:4326',always_xy=True).transform;result=[];seen=0
        digest=hashlib.sha256(red_bytes+(nir_bytes or b'')+str((red_band,nir_band,reflectance_scale,reflectance_offset)).encode()).hexdigest()[:24]
        for geom,value in shapes(mask.astype('uint8'),mask=mask,transform=trans):
            if not value:continue
            seen+=1
            if seen>10000:raise SatelliteError('Слишком много фрагментов растра')
            poly=transform(to_wgs,shape(geom));area=_area_ha(poly)
            if not .2<=area<=25 or not poly.is_valid:continue
            inside=geometry_mask([geom],out_shape=red.shape,transform=trans,invert=True)&mask
            result.append({'geometry':poly,'area_ha':area,'osm_id':f'geotiff/{digest}/{seen}','land_type':'растительность по GeoTIFF','name':'Кандидат по GeoTIFF','ndvi':float(np.mean(ndvi[inside])),'b04':float(np.mean(red[inside])),'b08':float(np.mean(nir[inside])),'b11':None,'valid_pixels':int(inside.sum())})
            if len(result)>MAX_CANDIDATES:raise SatelliteError('Слишком много кандидатов; импорт отменён')
        return result
    except SatelliteError:raise
    except (rasterio.errors.RasterioError,ValueError,TypeError,OSError) as e:raise SatelliteError('Некорректный GeoTIFF: '+str(e)[:250]) from e

def save_candidates(db,candidates,origin,scene=None,commit=True):
    inserted=updated=skipped=0
    next_id=(db.scalar(select(func.max(Hotspot.id))) or 0)+1
    if scene:
        record=db.get(SatelliteScene,scene['scene_id'])
        if not record:
            record=SatelliteScene(scene_id=scene['scene_id'],acquired_at=scene['acquired_at'],cloud_cover=scene['cloud_cover'],coverage_pct=scene.get('coverage_pct'),thumbnail=scene['thumbnail'],band_calibration=scene.get('calibration'),searched_at=datetime.now(timezone.utc))
            db.add(record)
        else:
            record.searched_at=datetime.now(timezone.utc)
            if scene.get('coverage_pct') is not None:record.coverage_pct=scene['coverage_pct']
    for c in candidates:
        key=origin+':'+c['osm_id']; existing=db.scalar(select(Hotspot).where(Hotspot.source_key==key))
        if existing and (existing.status!='detected' or db.scalar(select(WorkOrder.id).where(WorkOrder.hotspot_id==existing.id).limit(1)) is not None):
            skipped+=1;continue
        p=c['geometry'];center=p.centroid; nearest=min(NEIGHBORHOODS,key=lambda n:(n[2]-center.y)**2+(n[3]-center.x)**2)
        values=dict(lat=center.y,lon=center.x,area_ha=c['area_ha'],geometry=mapping(p),district=nearest[1],landmark=c['name'],land_type=c['land_type'],b02=0.,b03=0.,b04=c['b04'],b08=c['b08'],b11=c['b11'] if c['b11'] is not None else 0.,status='detected',data_provenance=origin,provenance=('sentinel2_raster_missing_b02_missing_b03' if origin=='sentinel2_raster_live' else 'sentinel2_osm_missing_b02_missing_b03') if scene else 'uploaded_geotiff_missing_b02_missing_b03_missing_b11',scene_id=scene['scene_id'] if scene else None,source_key=key,observed_ndvi=c['ndvi'])
        if existing:
            for k,v in values.items():setattr(existing,k,v)
            updated+=1
        else:
            db.add(Hotspot(id=next_id,**values));next_id+=1;inserted+=1
    if commit:db.commit()
    else:db.flush()
    return {'detected':len(candidates),'inserted':inserted,'updated':updated,'skipped_reserved':skipped,'scene':scene,'data_provenance':origin,'note':'NDVI показывает зелёную растительность, а не вид растения или наличие амброзии. Нужна проверка на месте.'}
