// Saved backgrounds, previews, races and fallback using the shipped handlers.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(__dirname+'/../../static/js/theme-meta.js','utf8');
const nodes={},props=new Map(),images=[],calls=[],storage=new Map();
const node=id=>nodes[id]||(nodes[id]={value:'',disabled:false,textContent:''});
let saved={background_light_url:'',background_dark_url:''},fail=false;
const ctx=vm.createContext({console,URL,Promise,JSON,String,Object,Array,Number,Date,
 $:node,localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
 document:{querySelector:()=>null,documentElement:{getAttribute:()=>null,style:{setProperty:(k,v)=>props.set(k,v),removeProperty:k=>props.delete(k)}}},
 MutationObserver:class{observe(){}},setTimeout:()=>1,clearTimeout(){},
 Image:class{constructor(){images.push(this);}set src(v){this.url=v;}},
 api:async(url,opts)=>{calls.push({url,opts});if(fail)throw Error('offline');if(opts)saved=JSON.parse(opts.body);return {status:'success',appearance:{...saved}};}});
ctx.window=ctx;ctx.location={protocol:'https:'};ctx.matchMedia=()=>({matches:false,addEventListener(){}});
vm.runInContext(source,ctx);
const tick=()=>new Promise(r=>setImmediate(r));
function complete(img,ok=true){img.naturalWidth=ok?1200:0;(ok?img.onload:img.onerror)?.();}
(async()=>{
 node('background-light-url').value='https://img.example/light.webp';node('background-dark-url').value='https://img.example/dark.webp';
 let pending=ctx.previewAppearance();assert(node('backgroundSaveBtn').disabled);assert.equal(calls.length,0);
 images.slice(-2).forEach(im=>complete(im));await pending;
 assert(props.get('--glass-light-image').includes('light.webp'));assert(!node('backgroundSaveBtn').disabled);
 await ctx.saveAppearance();assert.equal(calls.at(-1).url,'/api/appearance');assert(saved.background_dark_url.endsWith('dark.webp'));assert(storage.has('strimkeep-backgrounds'));
 console.log('PASS preview never writes; saving persists both theme addresses and unlocks controls');
 fail=true;node('background-light-url').value='https://img.example/unsaved.webp';await ctx.saveAppearance();
 assert(props.get('--glass-light-image').includes('light.webp'));assert(node('backgroundResult').textContent.includes('offline'));fail=false;
 node('background-light-url').value='javascript:alert(1)';const count=calls.length;await ctx.saveAppearance();assert.equal(calls.length,count);
 node('background-light-url').value='http://img.example/insecure.jpg';await ctx.saveAppearance();assert(node('backgroundResult').textContent.includes('HTTPS'));
 console.log('PASS invalid schemes/mixed content are rejected; failed save preserves the applied background');
 node('background-light-url').value='https://img.example/broken.webp';pending=ctx.previewAppearance();complete(images.at(-1),false);await pending;
 assert(!props.has('--glass-light-image'));assert(props.get('--glass-dark-image').includes('dark.webp'));assert(node('backgroundResult').textContent.includes('无法加载'));
 console.log('PASS failed image falls back for only that theme, retaining the other background');
 let reply;ctx.api=()=>new Promise(r=>reply=r);pending=ctx.loadAppearance(true);
 ctx.appearanceRevision++;reply({status:'success',appearance:{background_light_url:'https://img.example/obsolete.jpg'}});await pending;
 assert.equal(node('background-light-url').value,'https://img.example/broken.webp');
 const old=ctx.applyAppearance({background_light_url:'https://img.example/late.jpg'}),im=images.at(-1);
 await ctx.applyAppearance({background_light_url:'',background_dark_url:''});complete(im);await old;
 assert(!props.has('--glass-light-image')&&!props.has('--glass-dark-image'));
 console.log('PASS obsolete GET/image responses cannot overwrite newer edits or restored backgrounds');
 ctx.api=async(url,opts)=>{saved=JSON.parse(opts.body);return{status:'success',appearance:saved};};await ctx.resetAppearance();
 assert.equal(saved.background_light_url,'');assert.equal(saved.background_dark_url,'');assert.equal(node('background-dark-url').value,'');
 console.log('PASS restore clears both saved URLs and returns to built-in backgrounds');
})().catch(e=>{console.error(e);process.exitCode=1;});
