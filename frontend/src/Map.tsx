import { useEffect, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { Feature, Grid, HotspotProperties, Hotspots } from './types'

type Spot = Feature<HotspotProperties>
export type MapSelection = {kind:'hotspot';data:Spot}|{kind:'address';lat:number;lon:number}|null
export type PointSource = {hotspot_id:number;latitude:number;longitude:number;distance_m:number;contribution_grains_m3:number;status:string;data_provenance:string}
export type BaseMap='streets'|'satellite'|'voyager'
interface Props {
 hotspots:Hotspots|null; grid:Grid|null; selection:MapSelection; onSelect:(s:MapSelection)=>void
 showPlume:boolean;showHotspots:boolean;showCells:boolean;windEnabled:boolean;windSpeed:number;windFrom:number
 base:BaseMap; onTileError:(unavailable:boolean)=>void; focusKey:number; sources:PointSource[]; city:{latitude:number;longitude:number}|null
}
const tiles:Record<BaseMap,{url:string;attribution:string;maxZoom:number;subdomains?:string}>= {
 streets:{url:'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',maxZoom:19,subdomains:'abc'},
 satellite:{url:'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',attribution:'Tiles &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics',maxZoom:19},
 voyager:{url:'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',maxZoom:19,subdomains:'abcd'},
}
const center=(f:Spot):L.LatLngTuple=>{const r=f.geometry.coordinates[0], pts=r.length>1?r.slice(0,-1):r;return [pts.reduce((s,p)=>s+p[1],0)/pts.length,pts.reduce((s,p)=>s+p[0],0)/pts.length]}
const rounded=(n:number)=>Math.round(n*100000)/100000

/** Rasterises the actual 1km field using bilinear interpolation; it never fabricates data outside the grid. */
function usePlume(map:L.Map|null,grid:Grid|null,enabled:boolean){
 useEffect(()=>{
  if(!map||!grid||!enabled||!grid.features.length)return
  const canvas=L.DomUtil.create('canvas','plume-canvas');map.getPane('plume')!.appendChild(canvas)
  const records=grid.features.map(f=>{const r=f.geometry.coordinates[0];return {lon:(r[0][0]+r[2][0])/2,lat:(r[0][1]+r[2][1])/2,value:f.properties.concentration}})
  const xs=[...new Set(records.map(p=>rounded(p.lon)))].sort((a,b)=>a-b),ys=[...new Set(records.map(p=>rounded(p.lat)))].sort((a,b)=>b-a)
  const values=new Float32Array(xs.length*ys.length), lookup=new Map(records.map(p=>[`${rounded(p.lon)},${rounded(p.lat)}`,p.value]))
  ys.forEach((lat,j)=>xs.forEach((lon,i)=>values[j*xs.length+i]=lookup.get(`${lon},${lat}`)??0))
  const draw=()=>{
   const size=map.getSize(),scale=4,w=Math.ceil(size.x/scale),h=Math.ceil(size.y/scale);if(!w||!h)return
   const pos=map.containerPointToLayerPoint([0,0]);L.DomUtil.setPosition(canvas,pos);canvas.style.width=`${size.x}px`;canvas.style.height=`${size.y}px`;canvas.width=w;canvas.height=h
   const ctx=canvas.getContext('2d');if(!ctx||xs.length<2||ys.length<2)return
   const nw=map.latLngToContainerPoint([ys[0],xs[0]]),se=map.latLngToContainerPoint([ys[ys.length-1],xs[xs.length-1]])
   const dx=(se.x-nw.x)/(xs.length-1),dy=(se.y-nw.y)/(ys.length-1);if(dx<=0||dy<=0)return
   const image=ctx.createImageData(w,h)
   for(let y=0;y<h;y++)for(let x=0;x<w;x++){
    const gx=(x*scale-nw.x)/dx, gy=(y*scale-nw.y)/dy
    if(gx<0||gy<0||gx>xs.length-1||gy>ys.length-1)continue
    const x0=Math.min(xs.length-2,Math.floor(gx)),y0=Math.min(ys.length-2,Math.floor(gy)),fx=gx-x0,fy=gy-y0
    const a=values[y0*xs.length+x0]*(1-fx)+values[y0*xs.length+x0+1]*fx,b=values[(y0+1)*xs.length+x0]*(1-fx)+values[(y0+1)*xs.length+x0+1]*fx,c=a*(1-fy)+b*fy
    if(c<10)continue
    const t=Math.max(0,Math.min(1,(c-10)/90)), k=(y*w+x)*4
    image.data[k]=Math.round(223+17*t);image.data[k+1]=Math.round(165-112*t);image.data[k+2]=Math.round(54-12*t);image.data[k+3]=Math.round(65+67*t)
   }
   ctx.putImageData(image,0,0)
  }
  map.on('moveend zoomend resize',draw);draw();return()=>{map.off('moveend zoomend resize',draw);canvas.remove()}
 },[map,grid,enabled])
}
function useWind(map:L.Map|null,enabled:boolean,speed:number,from:number){
 useEffect(()=>{
  if(!map||!enabled)return
  const canvas=L.DomUtil.create('canvas','wind-canvas');map.getPane('wind')!.appendChild(canvas)
  let width=0,height=0,frame=0,last=0;const particles:Array<{x:number;y:number;life:number}>=[]
  const resize=()=>{const size=map.getSize(),p=map.containerPointToLayerPoint([0,0]);L.DomUtil.setPosition(canvas,p);width=size.x;height=size.y;canvas.width=width;canvas.height=height;particles.length=0;for(let i=0;i<Math.min(65,Math.ceil(width*height/20000));i++)particles.push({x:Math.random()*width,y:Math.random()*height,life:Math.random()*120})}
  const tick=(now:number)=>{const ctx=canvas.getContext('2d');if(!ctx)return;const dt=Math.min(.05,(now-(last||now))/1000);last=now;ctx.clearRect(0,0,width,height);const a=(from+180)*Math.PI/180,ux=Math.sin(a),uy=-Math.cos(a),v=Math.min(38,Math.max(7,speed*5));ctx.strokeStyle='rgba(255,255,255,0.55)';ctx.lineWidth=1.2
   for(const p of particles){p.x+=ux*v*dt;p.y+=uy*v*dt;p.life-=dt;if(p.x<0||p.x>width||p.y<0||p.y>height||p.life<=0){p.x=Math.random()*width;p.y=Math.random()*height;p.life=5+Math.random()*12}ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(p.x-ux*9,p.y-uy*9);ctx.stroke()}
   frame=requestAnimationFrame(tick)}
  map.on('moveend zoomend resize',resize);resize();frame=requestAnimationFrame(tick)
  return()=>{cancelAnimationFrame(frame);map.off('moveend zoomend resize',resize);canvas.remove()}
 },[map,enabled,speed,from])
}
export default function MapView({hotspots,grid,selection,onSelect,showPlume,showHotspots,showCells,windEnabled,windSpeed,windFrom,base,onTileError,focusKey,sources,city}:Props){
 const container=useRef<HTMLDivElement>(null),[map,setMap]=useState<L.Map|null>(null),[tileUnavailable,setTileUnavailable]=useState(false),selectionRef=useRef(onSelect)
 selectionRef.current=onSelect
 useEffect(()=>{if(!container.current)return;const m=L.map(container.current,{zoomControl:false,attributionControl:true,minZoom:5,maxZoom:19}).setView([45.065,38.985],11)
  m.createPane('plume');m.getPane('plume')!.style.zIndex='300';m.createPane('wind');m.getPane('wind')!.style.zIndex='350';m.on('click',e=>selectionRef.current({kind:'address',lat:e.latlng.lat,lon:e.latlng.lng}));L.control.zoom({position:'bottomright'}).addTo(m);setMap(m)
  return()=>{m.remove();setMap(null)}
 },[])
 useEffect(()=>{if(!map)return;setTileUnavailable(false);onTileError(false);let errors=0;const layer=L.tileLayer(tiles[base].url,{...tiles[base],crossOrigin:false});layer.on('tileerror',()=>{errors++;if(errors>=2){setTileUnavailable(true);onTileError(true)}});layer.on('tileload',()=>{errors=0;setTileUnavailable(false);onTileError(false)});layer.addTo(map);return()=>{layer.remove()}},[map,base,onTileError])
 useEffect(()=>{if(map&&city)map.flyTo([city.latitude,city.longitude],11,{duration:.7})},[map,city?.latitude,city?.longitude])
 useEffect(()=>{if(!map||!showHotspots||!hotspots)return;const group=L.featureGroup().addTo(map)
  for(const f of hotspots.features){const p=f.properties,active=selection?.kind==='hotspot'&&selection.data.properties.id===p.id,done=['mowed_verified','completed_late_bloom'].includes(p.status)
   const coords=f.geometry.coordinates.map(r=>r.map(c=>[c[1],c[0]] as L.LatLngTuple));const shape=L.polygon(coords,{color:done?'#198257':'#d8423b',weight:active?3:1.5,fillColor:done?'#24a568':'#e34d46',fillOpacity:.55,bubblingMouseEvents:false}).addTo(group)
   shape.bindTooltip(`Очаг #${p.id} · ${p.area_ha.toFixed(1)} га`,{direction:'top'});shape.on('click',e=>{L.DomEvent.stopPropagation(e.originalEvent);selectionRef.current({kind:'hotspot',data:f})})
   const marker=L.circleMarker(center(f),{radius:active?7:4,color:'#fff',weight:1.5,fillColor:done?'#168857':'#d73631',fillOpacity:1,bubblingMouseEvents:false}).addTo(group);marker.on('click',e=>{L.DomEvent.stopPropagation(e.originalEvent);selectionRef.current({kind:'hotspot',data:f})})
  }
  return()=>{group.remove()}
 },[map,hotspots,showHotspots,selection?.kind==='hotspot'?selection.data.properties.id:null])
 useEffect(()=>{if(!map||!grid||!showCells)return;const group=L.geoJSON(grid as unknown as GeoJSON.GeoJsonObject,{style:{weight:.6,color:'#4d615b',opacity:.3,fillOpacity:0},interactive:false}).addTo(map);return()=>{group.remove()}},[map,grid,showCells])
 useEffect(()=>{if(!map||selection?.kind!=='address')return;const icon=L.divIcon({className:'address-pin-icon',html:'<span></span>',iconSize:[26,32],iconAnchor:[13,30]});const marker=L.marker([selection.lat,selection.lon],{icon,interactive:false}).addTo(map);const lines=L.layerGroup().addTo(map)
  sources.forEach(s=>L.polyline([[selection.lat,selection.lon],[s.latitude,s.longitude]],{color:'#276e59',weight:1.5,opacity:.7,dashArray:'5 7',interactive:false}).addTo(lines))
  return()=>{marker.remove();lines.remove()}
 },[map,selection?.kind==='address'?selection.lat:null,selection?.kind==='address'?selection.lon:null,sources])
 useEffect(()=>{if(map&&focusKey&&selection?.kind==='hotspot')map.flyTo(center(selection.data),15,{duration:.7})},[map,focusKey])
 usePlume(map,grid,showPlume);useWind(map,windEnabled,windSpeed,windFrom)
 return <div className="map-surface"><div className={"map-offline-underlay "+(tileUnavailable?"visible":"")}><span>Подложка недоступна · нужна сеть</span></div><div ref={container} className="leaflet-map" aria-label="Интерактивная карта пыльцы Краснодара"/></div>
}
