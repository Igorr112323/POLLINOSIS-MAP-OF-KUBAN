"""Screen calibrated Sentinel-2 pixels; not plant/species identification."""
import hashlib
import math
from affine import Affine
import numpy as np
from rasterio.features import shapes, geometry_mask
from shapely.geometry import shape, box, mapping
from shapely.ops import transform as geom_transform
from pyproj import Transformer
from app.services.satellite_service import BBOX, _area_ha, _json_post, OVERPASS_ENDPOINTS, SatelliteError

# Thresholds are screening heuristics, not medically or botanically validated.
NDVI_MIN=.48
NDMI_MIN=-.1
NDMI_MAX=.6

def _reflectance(arrays,calibration,key):
    return arrays[key]*calibration[key]['scale']+calibration[key]['offset']

def _indices(arrays,calibration):
    red=_reflectance(arrays,calibration,'red');nir=_reflectance(arrays,calibration,'nir');swir=_reflectance(arrays,calibration,'swir16')
    with np.errstate(divide='ignore',invalid='ignore'):
        ndvi=(nir-red)/(nir+red); ndmi=(nir-swir)/(nir+swir)
    return red,nir,swir,ndvi,ndmi

def raster_candidates(arrays,trans,crs,scene,spring=None,limit=200):
    red,nir,swir,ndvi,ndmi=_indices(arrays,scene['calibration']);scl=arrays['scl']
    # SCL 4 = vegetation; 6 water; 3,8,9,10 shadow/cloud/cirrus; 5/7/11
    # and unknown pixels are not accepted as vegetation.
    valid=(scl==4)&np.isfinite(ndvi)&np.isfinite(ndmi)&(red>=0)&(red<=1)&(nir>=0)&(nir<=1)&(swir>=0)&(swir<=1)&(ndvi>=NDVI_MIN)&(ndmi>=NDMI_MIN)&(ndmi<=NDMI_MAX)
    if spring is not None:
        spring_arrays,spring_scene=spring
        *_,spring_ndvi,_=_indices(spring_arrays,spring_scene['calibration'])
        # Persistent dense vegetation is a coarse park/forest proxy; unknown spring pixels remain unknown.
        valid &= ~((spring_arrays['scl']==4)&np.isfinite(spring_ndvi)&(spring_ndvi>=.65))
    to_wgs=Transformer.from_crs(crs,'EPSG:4326',always_xy=True).transform
    bounds=box(*BBOX);candidates=[]
    for geom,value in shapes(valid.astype('uint8'),mask=valid,connectivity=4,transform=trans):
        if not value:continue
        geo=geom_transform(to_wgs,shape(geom)).intersection(bounds)
        if geo.is_empty:continue
        parts=[geo] if geo.geom_type=='Polygon' else list(geo.geoms) if geo.geom_type=='MultiPolygon' else []
        for poly in parts:
            if not poly.is_valid:continue
            area=_area_ha(poly)
            if not .3<=area<=25:continue
            # Geometry in source CRS, no interpolation of channel values.
            p=geom_transform(Transformer.from_crs('EPSG:4326',crs,always_xy=True).transform,poly)
            inv=~trans; minx,miny,maxx,maxy=p.bounds
            c0,r0=inv*(minx,maxy);c1,r1=inv*(maxx,miny)
            x0=max(0,math.floor(min(c0,c1)));x1=min(red.shape[1],math.ceil(max(c0,c1)))
            y0=max(0,math.floor(min(r0,r1)));y1=min(red.shape[0],math.ceil(max(r0,r1)))
            if x0>=x1 or y0>=y1:continue
            sub=(slice(y0,y1),slice(x0,x1));local=trans*Affine.translation(x0,y0)
            cells=geometry_mask([mapping(p)],out_shape=red[sub].shape,transform=local,invert=True)&valid[sub]
            count=int(cells.sum())
            if count<1:continue
            digest=hashlib.sha256((scene['scene_id']+poly.wkb_hex).encode()).hexdigest()[:24]
            candidates.append({'geometry':poly,'area_ha':area,'osm_id':'raster/'+digest,'land_type':'не определён по спутнику','name':'Спутниковый кандидат',
                'ndvi':float(np.mean(ndvi[sub][cells])),'ndmi':float(np.mean(ndmi[sub][cells])),'b04':float(np.mean(red[sub][cells])),'b08':float(np.mean(nir[sub][cells])),'b11':float(np.mean(swir[sub][cells])),'valid_pixels':count})
    candidates.sort(key=lambda c:(-c['ndvi'],-c['valid_pixels'],c['osm_id']))
    return candidates[:limit]

def water_polygons(arrays,trans,crs):
    mask=arrays['scl']==6;to_wgs=Transformer.from_crs(crs,'EPSG:4326',always_xy=True).transform;bounds=box(*BBOX);result=[]
    for geom,value in shapes(mask.astype('uint8'),mask=mask,connectivity=4,transform=trans):
        if not value:continue
        g=geom_transform(to_wgs,shape(geom)).intersection(bounds)
        if g.is_empty:continue
        for p in ([g] if g.geom_type=='Polygon' else list(g.geoms) if g.geom_type=='MultiPolygon' else []):
            if p.is_valid and not p.is_empty:result.append(p)
    return result

def optional_landuse():
    # Short query, public OSM mirrors. It is enrichment only and may fail.
    query='[out:json][timeout:25];(way["landuse"~"^(industrial|construction|brownfield|railway)$"](44.98,38.88,45.14,39.13);relation["landuse"~"^(industrial|construction|brownfield|railway)$"](44.98,38.88,45.14,39.13););out geom;'
    headers={'Accept':'*/*','Content-Type':'application/x-www-form-urlencoded','User-Agent':'KubanPollinosisMap/1.0 (public vegetation screening; github.com/Igorr112323/POLLINOSIS-MAP-OF-KUBAN)'}
    errors=[]
    from app.services.satellite_service import _polygons
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            data=_json_post(endpoint,timeout=32,data={'data':query},headers=headers);records=[]
            labels={'industrial':'промзона','construction':'стройка','brownfield':'пустырь','railway':'полоса отвода ЖД'}
            for el in data.get('elements',[]):
                land=labels.get(el.get('tags',{}).get('landuse'))
                if land:
                    for p in _polygons(el):records.append((p,land,f"{el.get('type')}/{el.get('id')}"))
            return records,endpoint,errors
        except Exception as e:errors.append(f'{endpoint}: {type(e).__name__}: {str(e)[:180]}')
    return [],None,errors

def enrich(candidates,records):
    if not records:return
    from shapely.strtree import STRtree
    tree=STRtree([r[0] for r in records])
    for c in candidates:
        best=None;overlap=0
        for i in tree.query(c['geometry']):
            area=c['geometry'].intersection(records[i][0]).area
            if area>overlap:overlap=area;best=records[i]
        if best is not None and overlap>=c['geometry'].area*.5:
            c['land_type']=best[1];c['osm_reference']=best[2]
