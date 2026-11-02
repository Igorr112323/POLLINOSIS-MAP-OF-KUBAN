import { useEffect, useRef, useState, type PointerEvent, type WheelEvent } from 'react'
import { Plus, Minus, LocateFixed, Navigation } from 'lucide-react'
import type { Feature, Grid, HotspotProperties, Hotspots, Neighborhood, Passport } from './types'

const W=1200,H=900, MINLON=38.78, MAXLON=39.19, MINLAT=44.94, MAXLAT=45.20
const x=(v:number)=>(v-MINLON)/(MAXLON-MINLON)*W
const y=(v:number)=>(MAXLAT-v)/(MAXLAT-MINLAT)*H
const points=(coords:number[][])=>coords.map(c=>`${x(c[0])},${y(c[1])}`).join(' ')
const riskColor=(n:number)=>n<10?'#3A876A':n<50?'#C7881A':n<100?'#C65127':'#9B2626'
const threat=(n:number)=>n>.1?'#8f3c28':n>.04?'#c48643':'#627d56'
type Selected = {kind:'hotspot';data:Feature<HotspotProperties>}|{kind:'neighborhood';data:Neighborhood}|null
interface Props { passport:Passport|null; hotspots:Hotspots|null;grid:Grid|null;showGrid:boolean;showHotspots:boolean;showLabels:boolean;selected:Selected;onSelect:(s:Selected)=>void;wind:number|null;focusKey:number }
export default function MapView({passport,hotspots,grid,showGrid,showHotspots,showLabels,selected,onSelect,wind,focusKey}:Props){
 const [view,setView]=useState({x:0,y:0,w:W,h:H}); const [cursor,setCursor]=useState<string>('—'); const ref=useRef<SVGSVGElement>(null); const drag=useRef<{x:number;y:number;view:typeof view;moved:boolean}|null>(null)
 useEffect(()=>{if(!focusKey||selected?.kind!=='hotspot')return;const ring=selected.data.geometry.coordinates[0];if(!ring.length)return;const lon=ring.reduce((a,c)=>a+c[0],0)/ring.length,lat=ring.reduce((a,c)=>a+c[1],0)/ring.length;const w=W/4,h=H/4;setView({x:Math.max(0,Math.min(W-w,x(lon)-w/2)),y:Math.max(0,Math.min(H-h,y(lat)-h/2)),w,h})},[focusKey])
 useEffect(()=>{const el=ref.current;const prevent=(e:globalThis.WheelEvent)=>e.preventDefault();el?.addEventListener('wheel',prevent,{passive:false});return()=>el?.removeEventListener('wheel',prevent)},[])
 const zoom=(factor:number, px=.5,py=.5)=>setView(v=>{const nw=Math.min(W,Math.max(W/5,v.w*factor)); const nh=nw*H/W;return {x:Math.max(0,Math.min(W-nw,v.x+(v.w-nw)*px)),y:Math.max(0,Math.min(H-nh,v.y+(v.h-nh)*py)),w:nw,h:nh}})
 const down=(e:PointerEvent<SVGSVGElement>)=>{if(e.button!==0)return; drag.current={x:e.clientX,y:e.clientY,view,moved:false}}
 const move=(e:PointerEvent<SVGSVGElement>)=>{const rect=e.currentTarget.getBoundingClientRect();const px=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width));const py=Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height));const xx=view.x+px*view.w, yy=view.y+py*view.h;setCursor(`${(MAXLAT-yy/H*(MAXLAT-MINLAT)).toFixed(4)}° N  ${(MINLON+xx/W*(MAXLON-MINLON)).toFixed(4)}° E`);if(drag.current){const d=drag.current; const dx=e.clientX-d.x,dy=e.clientY-d.y;if(Math.abs(dx)+Math.abs(dy)>4)d.moved=true;setView({x:Math.max(0,Math.min(W-d.view.w,d.view.x-dx/rect.width*d.view.w)),y:Math.max(0,Math.min(H-d.view.h,d.view.y-dy/rect.height*d.view.h)),w:d.view.w,h:d.view.h})}}
 const wheel=(e:WheelEvent<SVGSVGElement>)=>{e.preventDefault();const r=e.currentTarget.getBoundingClientRect();zoom(e.deltaY<0?.8:1.25,(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height)}
 const choose=(s:Selected)=>{if(!drag.current?.moved)onSelect(s)}
 return <div className="map-wrap">
  <svg ref={ref} className="map-svg" viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`} preserveAspectRatio="none" onPointerDown={down} onPointerMove={move} onPointerUp={()=>{setTimeout(()=>{drag.current=null},0)}} onPointerCancel={()=>{drag.current=null}} onPointerLeave={()=>{setCursor('—');drag.current=null}} onWheel={wheel} aria-label="Интерактивная схематическая карта Краснодара">
   <defs><pattern id="minor-grid" width="82" height="82" patternUnits="userSpaceOnUse"><path d="M 82 0 L 0 0 0 82" fill="none" stroke="#c8d2c7" strokeWidth=".65" strokeDasharray="3 6"/></pattern><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="#314f43" strokeWidth="2"/></pattern></defs>
   <rect width={W} height={H} fill="#e8ede5"/>
   {/* Deliberately schematic land-use shapes, not official geographic boundaries. */}
   <path d="M0 0H1200V900H0Z" fill="url(#minor-grid)"/>
   <path d="M45 85 L270 55 395 130 365 260 90 240Z M400 40 L650 35 725 130 580 250 365 260Z M765 80 L1080 50 1160 180 1045 300 760 245Z M45 335 L290 290 342 410 250 495 35 465Z M755 510 L1020 455 1170 535 1130 650 900 690Z" fill="#dce6d7" opacity=".72"/>
   <path d="M0 708 C115 670 156 721 246 699 C325 680 364 740 442 731 C521 721 560 771 650 745 C755 710 832 735 902 706 C1006 663 1090 687 1200 646 L1200 900 H0Z" fill="#d5e5e7"/>
   <path d="M0 708 C115 670 156 721 246 699 C325 680 364 740 442 731 C521 721 560 771 650 745 C755 710 832 735 902 706 C1006 663 1090 687 1200 646" fill="none" stroke="#9abfc1" strokeWidth="5"/>
   <path d="M930 665 Q1030 610 1200 600 L1200 900 L1090 900 Q1050 755 930 665Z" fill="#c7dfe1"/>
   <g fill="none" stroke="#f8faf6" strokeWidth="5" opacity=".95"><path d="M30 500 L1130 300"/><path d="M155 155 L1000 635"/><path d="M360 30 L500 710"/><path d="M615 25 L640 710"/><path d="M825 30 L770 700"/><path d="M50 310 L1110 480"/><path d="M75 585 L1050 220"/><path d="M115 420 L1000 570"/></g>
   <g fill="none" stroke="#bdc9bc" strokeWidth="1.6" strokeDasharray="9 6"><path d="M130 130 L350 300 L440 510 L420 695"/><path d="M440 510 L650 405 L900 460"/><path d="M650 405 L700 125"/><path d="M900 460 L1050 710"/></g>
   <g className="district-label"><text x="155" y="380">ЗАПАДНЫЙ</text><text x="420" y="215">ПРИКУБАНСКИЙ</text><text x="420" y="610">ЦЕНТРАЛЬНЫЙ</text><text x="910" y="560">КАРАСУНСКИЙ</text></g>
   {showGrid&&grid?.features.map((f,i)=><polygon key={i} points={points(f.geometry.coordinates[0])} fill={riskColor(f.properties.concentration)} fillOpacity={f.properties.concentration<10?.10:.39} stroke="#fff" strokeOpacity=".25" strokeWidth=".65"><title>{f.properties.concentration} пз/м³</title></polygon>)}
   {showHotspots&&hotspots?.features.map(f=>{const p=f.properties, ring=f.geometry.coordinates[0], center=ring.slice(0,-1).reduce((a,c)=>[a[0]+c[0]/(ring.length-1),a[1]+c[1]/(ring.length-1)],[0,0]);const active=selected?.kind==='hotspot'&&selected.data.properties.id===p.id;return <g key={p.id} data-hotspot-id={p.id} data-status={p.status} onClick={e=>{e.stopPropagation();choose({kind:'hotspot',data:f})}} className="map-target"><polygon points={points(ring)} fill={['mowed_verified','completed_late_bloom'].includes(p.status)?'url(#hatch)':threat(p.cost_effectiveness)} fillOpacity=".92" stroke={active?'#101c17':'#fff'} strokeWidth={active?3:1}/><circle cx={x(center[0])} cy={y(center[1])} r={active?7:4.3} fill={active?'#142c21':['mowed_verified','completed_late_bloom'].includes(p.status)?'url(#hatch)':threat(p.cost_effectiveness)} stroke="#fff" strokeWidth="1.4"/><circle cx={x(center[0])} cy={y(center[1])} r="12" fill="transparent"/><title>Очаг #{p.id} · {p.area_ha.toFixed(1)} га</title></g>})}
   {showLabels&&passport?.neighborhoods.map(n=><g key={n.name} className="map-target neighborhood" onClick={e=>{e.stopPropagation();choose({kind:'neighborhood',data:n})}}><circle cx={x(n.longitude)} cy={y(n.latitude)} r="5" fill="#fff" stroke="#355548" strokeWidth="2"/><circle cx={x(n.longitude)} cy={y(n.latitude)} r="13" fill="transparent"/><text x={x(n.longitude)+9} y={y(n.latitude)-8}>{n.name}</text></g>)}
   {passport&&<g transform={`translate(${x(passport.reference_point.longitude)} ${y(passport.reference_point.latitude)})`}><circle r="11" fill="#fff" stroke="#1b5e4a" strokeWidth="2"/><path d="M-5 0H5 M0-5V5" stroke="#1b5e4a" strokeWidth="2"/><text x="16" y="4" className="trap-label">КубГМУ</text></g>}
   <text x="245" y="775" className="water-label">р. КУБАНЬ</text><text x="1000" y="760" className="water-label">КРАСНОДАРСКОЕ</text><text x="1025" y="779" className="water-label">ВОДОХРАНИЛИЩЕ</text>
  </svg>
  <div className="map-top-label"><span className="map-dot"/>КРАСНОДАР <span className="map-label-sep">/</span> СХЕМАТИЧНАЯ ПОДЛОЖКА</div>
  <div className="map-north"><Navigation size={17} fill="#364d42"/><span>С</span></div>
  <div className="zoom"><button onClick={()=>zoom(.7)} title="Увеличить"><Plus size={16}/></button><button onClick={()=>zoom(1.43)} title="Уменьшить"><Minus size={16}/></button><button onClick={()=>setView({x:0,y:0,w:W,h:H})} title="Весь город"><LocateFixed size={15}/></button></div>
  <div className="map-bottom"><span className="mono">{cursor}</span><span>WGS 84</span><span className="map-scale">━━━ &nbsp; ~5 км</span><span>Контуры и водоёмы схематичны</span></div>
  {wind!==null&&<div className="wind-map"><span className="wind-icon" style={{transform:`rotate(${wind+180}deg)`}}>↑</span><div><b>ВЕТЕР</b><br/><span className="mono">{Math.round(wind)}°</span></div></div>}
 </div>
}
