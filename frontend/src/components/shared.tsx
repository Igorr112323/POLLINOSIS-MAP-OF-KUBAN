import type { ReactNode } from 'react'
import type { Mode, Passport } from '../types'

export type ModuleProps={token:string;role:string;passport:Passport|null;mode:Mode;target:string;refresh:number;onRefresh:()=>void;onFocusHotspot:(id:number)=>void;addressDraft?:{lat:number;lon:number}|null;planIds?:number[];onClearPlan?:()=>void}
export const num=(v:number|null|undefined,n=1)=>v==null||!Number.isFinite(v)?'—':v.toLocaleString('ru-RU',{minimumFractionDigits:n,maximumFractionDigits:n})
export const rub=(v:number|null|undefined)=>v==null?'—':`${num(v/1_000_000,2)} млн ₽`
export const dateText=(v:string|null|undefined)=>v?`${v.slice(8,10)}.${v.slice(5,7)}.${v.slice(0,4)}`:'—'
export const prov=(v:string|null|undefined)=>v==='synthetic_demo'?'СИНТЕТИЧЕСКИЙ ДЕМО-НАБОР':v==='uploaded_dataset'?'ДАННЫЕ ОПЕРАТОРА · НЕ ВЕРИФИЦИРОВАНЫ':v==='sentinel2_osm_live'?'SENTINEL-2 / OPENSTREETMAP · КАНДИДАТЫ':v==='uploaded_geotiff'?'GEOTIFF ОПЕРАТОРА · КАНДИДАТЫ':v||'ИСТОЧНИК НЕИЗВЕСТЕН'
export const medicalDisclaimer='Информационно-профилактический расчёт не заменяет консультацию и назначения врача. Модель не прошла клиническую валидацию.'
export const targetQuery=(mode:Mode,target:string)=>mode==='simulation'?`?target_date=${encodeURIComponent(target)}`:''

function errorMessage(body:unknown,status:number){
 if(body&&typeof body==='object'&&'detail' in body){const detail=(body as {detail:unknown}).detail;if(typeof detail==='string')return detail;if(Array.isArray(detail))return detail.map(e=>`${e.loc?.join('.')||''}: ${e.msg||''}`).join('; ')}
 return `HTTP ${status}`
}
export async function api<T>(path:string,opts:{token?:string;method?:string;json?:unknown;body?:BodyInit;headers?:Record<string,string>;signal?:AbortSignal}={}):Promise<T>{
 const headers:Record<string,string>={...opts.headers};if(opts.token)headers.Authorization=`Bearer ${opts.token}`;if(opts.json!==undefined)headers['Content-Type']='application/json'
 const r=await fetch(`/api/v1${path}`,{method:opts.method||'GET',headers,body:opts.json!==undefined?JSON.stringify(opts.json):opts.body,signal:opts.signal});if(!r.ok){let body:unknown;try{body=await r.json()}catch{body=null}throw new Error(errorMessage(body,r.status))}return await r.json() as T
}
export async function download(path:string,token:string,name:string){const r=await fetch('/api/v1'+path,{headers:{Authorization:`Bearer ${token}`}});if(!r.ok)throw new Error(`Ошибка выгрузки: HTTP ${r.status}`);const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)}
export const message=(e:unknown)=>e instanceof Error?e.message:'Неизвестная ошибка'
export function PageHead({title,description}:{code:string;title:string;description:string;tag?:string}){return <div className="workspace-heading module-heading"><div><h1>{title}</h1><p>{description}</p></div></div>}
export function Panel({eyebrow,title,aside,children,className=''}:{eyebrow:string;title:string;aside?:ReactNode;children:ReactNode;className?:string}){return <section className={`work-panel ${className}`}><div className="work-panel-head"><div><div className="eyebrow">{eyebrow}</div><h2>{title}</h2></div>{aside}</div><div className="work-panel-body">{children}</div></section>}
export function Feedback({text,kind='info'}:{text:string;kind?:'error'|'info'|'success'}){return text?<div className={`feedback ${kind}`} role={kind==='error'?'alert':'status'}>{text}</div>:null}
export function Permission({children}:{children:ReactNode}){return <div className="permission-note">{children} Переключите роль в верхней панели.</div>}
export function Label({children}:{children:ReactNode}){return <span className="field-label">{children}</span>}
export function Stat({label,value,unit,foot}:{label:string;value:string;unit?:string;foot?:string}){return <div className="work-stat"><span>{label}</span><strong className="mono">{value} {unit&&<small>{unit}</small>}</strong>{foot&&<em>{foot}</em>}</div>}
