import { chromium } from '@playwright/test';
const base='http://127.0.0.1:5184';
const res=await fetch('http://192.168.3.55:8321/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'zzx1231230'})});
const raw=res.headers.getSetCookie?.()?.[0]||'';const [pair]=raw.split(';');const [name,value]=pair.split('=');
const b=await chromium.launch();const c=await b.newContext({viewport:{width:1500,height:1000}});
await c.addCookies([{name,value,domain:'127.0.0.1',path:'/'}]);
const p=await c.newPage();const errs=[];
p.on('pageerror',e=>errs.push('ERR:'+e.message.slice(0,110)));
p.on('console',m=>{if(m.type()==='error')errs.push('C:'+m.text().slice(0,110))});
for(const [r,n] of [['/media/explore','explore'],['/media/library','library'],['/governance','governance']]){
  errs.length=0;
  await p.goto(base+'/#'+r,{waitUntil:'domcontentloaded'});await p.waitForTimeout(5000);
  await p.screenshot({path:`../.venv/v3/redo3-${n}.png`});
  const t=(await p.locator('main').innerText().catch(()=>'')).replace(/\s+/g,' ').slice(0,70);
  console.log(`[${n}] ${t} ${errs.length?'| '+errs.slice(0,2).join('|'):'| 无错误'}`);
}
await b.close();
