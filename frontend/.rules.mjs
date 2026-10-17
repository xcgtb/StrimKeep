import { chromium } from '@playwright/test';
const base='http://127.0.0.1:5184';
const res=await fetch('http://192.168.3.55:8321/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'admin',password:'zzx1231230'})});
const raw=res.headers.getSetCookie?.()?.[0]||'';const [pair]=raw.split(';');const [name,value]=pair.split('=');
const b=await chromium.launch();const c=await b.newContext({viewport:{width:1500,height:1000}});
await c.addCookies([{name,value,domain:'127.0.0.1',path:'/'}]);
const p=await c.newPage();
await p.goto(base+'/#/media/explore',{waitUntil:'domcontentloaded'});await p.waitForTimeout(4000);
const rules=await p.evaluate(()=>{
  const el=document.querySelector('.redo147');
  const out=[];
  for (const sheet of document.styleSheets) {
    let list; try { list=sheet.cssRules; } catch { continue; }
    const walk=(ruleList, mediaText)=>{
      for (const r of ruleList) {
        if (r.cssRules) { walk(r.cssRules, r.conditionText||''); continue; }
        if (!r.selectorText) continue;
        try {
          if (el.matches(r.selectorText)) {
            const d=r.style.getPropertyValue('display');
            const o=r.style.getPropertyValue('opacity');
            if (d||o) out.push({sel:r.selectorText, display:d, opacity:o, media:mediaText});
          }
        } catch {}
      }
    };
    walk(list,'');
  }
  return out;
});
console.log(JSON.stringify(rules,null,1));
await b.close();
