// Isolated gesture safety: tap select, horizontal multi-select, vertical scroll.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/mapping.js'),'utf8');
const start=source.indexOf('var mappingBatchSuppressClickUntil =');
const end=source.indexOf('async function mappingBatchPrepare(',start);
assert(start>=0 && end>start);
const listeners={}, selected=new Set(), allCards=new Map();
const makeCard=(id)=>({getAttribute:k=>k==='data-series-id'?id:null,
 classList:{toggle(){}},querySelector:()=>({checked:false})});
for(const id of ['one','two','three']) allCards.set(id,makeCard(id));
const list={__mappingSwipeBound:false,contains:()=>true,
 addEventListener:(type,fn)=>listeners[type]=fn};
let details=0; const win={};
const context=vm.createContext({Math,Date,Set,document:{
  elementFromPoint:(x)=>x<80?{closest:()=>allCards.get('one')}:x<160?{closest:()=>allCards.get('two')}:{closest:()=>allCards.get('three')},
}, $:id=>list,mappingBatchMode:true,mappingBatchAll:false,mappingDeleteBusy:false,
 mappingBatchAllowed:()=>true,mappingBatchSelected:selected,
 mappingBatchSetItem:(id,checked)=>checked?selected.add(id):selected.delete(id),
 showEmbyDetailById:()=>details++,showMovieDetailById:()=>details++});
vm.runInContext(source.slice(start,end),context);
context.mappingBatchBindSwipe();
context.mappingBatchCardClick('series','one',{stopPropagation(){}});
assert(selected.has('one') && details===0,'tap selects without opening details');
selected.clear();
const rootCard=allCards.get('one');
const startEvent={target:{closest:sel=>sel==='.poster-card'?rootCard:null},touches:[{clientX:20,clientY:40}]};
listeners.touchstart(startEvent);
let cancelled=0;
listeners.touchmove({touches:[{clientX:175,clientY:42}],cancelable:true,preventDefault(){cancelled++}});
listeners.touchend({});
assert(cancelled===1 && selected.size===3,'horizontal swipe marks the whole row');
const before=selected.size;
context.mappingBatchCardClick('series','two',{stopPropagation(){}});
assert(selected.size===before,'synthesized click after swipe must not unselect');
context.mappingBatchSuppressClickUntil=0;
selected.clear();
listeners.touchstart(startEvent);
listeners.touchmove({touches:[{clientX:25,clientY:140}],cancelable:true,preventDefault(){cancelled++}});
listeners.touchend({});
assert(cancelled===1 && selected.size===0,'vertical scroll must not mark or block scrolling');
context.mappingBatchMode=false;
context.mappingBatchCardClick('series','one',{});
assert(details===1,'normal mode still opens details');
console.log('PASS mobile tap/swipe select, gesture isolation, old details click');
