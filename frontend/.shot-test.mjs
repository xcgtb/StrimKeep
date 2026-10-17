import { chromium } from '@playwright/test';
const base='http://192.168.3.55:8396';
const res=await fetch(base+'/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'test123456'})});
const raw=res.headers.getSetCookie?.()?.[0]||'';const [pair]=raw.split(';');const [name,value]=pair.split('=');
const b=await chromium.launch();const c=await b.newContext({viewport:{width:1500,height:1000}});
await c.addCookies([{name,value,domain:'192.168.3.55',path:'/'}]);
const p=await c.newPage();
for(const [r,n] of [['/dashboard','dash'],['/mapping','mapping'],['/cloud/files','cloud']]){
  await p.goto(base+'/#'+r,{waitUntil:'domcontentloaded'});await p.waitForTimeout(4000);
  await p.screenshot({path:`../.venv/v3/test-${n}.png`});
}
await b.close();
