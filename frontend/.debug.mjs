import { chromium } from '@playwright/test';
const base='http://127.0.0.1:5184';
const res=await fetch('http://192.168.3.55:8321/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'zzx1231230'})});
const raw=res.headers.getSetCookie?.()?.[0]||'';const [pair]=raw.split(';');const [name,value]=pair.split('=');
const b=await chromium.launch();const c=await b.newContext({viewport:{width:1500,height:1000}});
await c.addCookies([{name,value,domain:'127.0.0.1',path:'/'}]);
const p=await c.newPage();
await p.goto(base+'/#/media/explore',{waitUntil:'domcontentloaded'});await p.waitForTimeout(5000);
const info=await p.evaluate(()=>{
  const el=document.querySelector('.redo147');
  if(!el) return {found:false};
  const r=el.getBoundingClientRect();
  return {found:true, w:Math.round(r.width), h:Math.round(r.height),
    display:getComputedStyle(el).display, visibility:getComputedStyle(el).visibility,
    opacity:getComputedStyle(el).opacity, childCount:el.children.length,
    html:el.innerHTML.slice(0,300)};
});
console.log(JSON.stringify(info,null,1));
await b.close();
