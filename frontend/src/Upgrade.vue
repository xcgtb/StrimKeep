<script setup lang="ts">
import {ref,computed,onMounted,onUnmounted} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {type QualityRule,numericRules,moveEntry,setOrder,orderedValues as values,usesLegacySource,serializePolicy} from './upgradeEditor'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>;shareMode?:boolean}>()
const busy=ref(false),error=ref(''),config=ref<any>(null),saved=ref(''),search=ref(''),expanded=ref<string[]>([])
const labels=ref<Record<string,string>>({}),options=ref<Record<string,Record<string,string>>>({}),suggestions=ref<Record<string,string[]>>({})
const additions=ref<Record<string,string>>({}),dragged=ref('')
const captions:Record<string,string>={
  revision:'比较文件名中明确标识的 V1、V2 等修订版本；缺失时不猜测。',
  group:'按原始文件名末尾明确标记的发布组比较，忽略大小写。',source:'资源类型与 WEB-DL 平台统一排序，排在前面的来源优先。',
  resolution:'使用实测画面尺寸，兼顾宽银幕裁切及 1440p。',dynamic_range:'区分杜比视界 Profile、HDR10+、HDR10、HLG 和 SDR。',
  fps:'使用实测帧率，23.976 / 59.94 / 119.88 归一为 24 / 60 / 120。',bit_depth:'比较探测得到的实际色深；未知值转待处理。',
  audio:'按编码顺序从全部音轨中选择代表轨，同编码再比较声道数。',codec:'按你的设备兼容性和编码偏好设置顺序。',size:'在前面的已启用规则均相同时比较大小。',
}
const dirty=computed(()=>config.value && JSON.stringify(serializePolicy(config.value))!==saved.value)
const enabled=computed<QualityRule[]>(()=>config.value?.rules.filter((r:QualityRule)=>r.enabled)||[])
const visibleRules=computed<{rule:QualityRule;index:number}[]>(()=>(config.value?.rules||[]).map((rule:QualityRule,index:number)=>({rule,index})).filter(({rule}:{rule:QualityRule})=>
  [labels.value[rule.key],captions[rule.key],...Object.values(options.value[rule.key]||{}),...values(rule)].join(' ').toLowerCase().includes(search.value.trim().toLowerCase())))
const label=(key:string,value:string)=>options.value[key]?.[value]||value
function available(rule:QualityRule) {
  const selected=values(rule)
  return Object.entries(options.value[rule.key]||{}).filter(([v])=>!selected.includes(v) &&
    !(rule.key==='source' && ((v==='web-dl' && selected.some(x=>x.startsWith('web-dl:'))) || (v.startsWith('web-dl:') && selected.includes('web-dl')))))
}
function summary(rule:QualityRule) {
  const list=values(rule)
  return list.length ? list.slice(0,3).map(v=>label(rule.key,v)).join(' → ')+(list.length>3?` · 共 ${list.length} 项`:'') : numericRules.includes(rule.key)?(rule.descending?'数值较大优先':'数值较小优先'):'尚未设置顺序'
}
function toggle(key:string) {const opening=!expanded.value.includes(key);expanded.value=opening?[...expanded.value,key]:expanded.value.filter(x=>x!==key);if(opening){const rule=config.value.rules.find((r:QualityRule)=>r.key===key);if(rule&&rule.enabled&&!values(rule).length&&suggestions.value[key]?.length)setOrder(rule,suggestions.value[key]!)}}
function accept(data:any) {
  labels.value={revision:'修订版本',...data.labels};data.rules=(data.rules||[]).filter((r:any)=>r.key!=='revision');options.value=data.options;suggestions.value=data.suggested_order
  config.value={...data.config,rules:data.config.rules.map((r:any)=>({...r,order:r.order||[]}))};saved.value=JSON.stringify(serializePolicy(config.value))
  expanded.value=[]
}
async function load() {
  if(dirty.value) {try {await ElMessageBox.confirm('将放弃本页尚未保存的修改。','重新读取设置',{confirmButtonText:'重新读取',cancelButtonText:'保留修改'})}catch{return}}
  busy.value=true;error.value=''
  try{accept(await props.api('/upgrade/config'))}catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
function enable(rule:QualityRule) {
  if(rule.enabled && !values(rule).length && !numericRules.includes(rule.key) && suggestions.value[rule.key]?.length) setOrder(rule,suggestions.value[rule.key]!)
  if(rule.enabled && !expanded.value.includes(rule.key)) expanded.value.push(rule.key)
}
function preset(rule:QualityRule) {setOrder(rule,suggestions.value[rule.key]||[])}
function add(rule:QualityRule) {
  const value=(additions.value[rule.key]||'').trim()
  if(!value)return
  if(values(rule).some(v=>v.toLowerCase()===value.toLowerCase())){ElMessage.warning('该选项已在列表中');return}
  if(rule.key==='group' && !/^[a-z][a-z0-9]{1,24}$/i.test(value)){ElMessage.warning('发布组使用 2–25 位英文字母或数字，以字母开头');return}
  if(rule.key!=='group' && !options.value[rule.key]?.[value])return
  if(!rule.order.length && rule.preferences.length)setOrder(rule,[...rule.preferences])
  rule.order.push(value);additions.value[rule.key]=''
}
function dropRule(key:string) {
  const list:QualityRule[]=config.value.rules,from=list.findIndex(r=>r.key===dragged.value),to=list.findIndex(r=>r.key===key)
  if(!busy.value && from>=0 && to>=0)moveEntry(list,from,to-from)
  dragged.value=''
}
async function save() {
  busy.value=true;error.value=''
  try {
    if(config.value.mode==='upgrade' && !enabled.value.length)throw new Error('允许洗版前，请至少启用一项规则。')
    for(const r of config.value.rules as QualityRule[])if(r.enabled && !numericRules.includes(r.key) && !values(r).length)throw new Error(`请先设置“${labels.value[r.key]}”的顺序。`)
    accept(await props.api('/upgrade/config','PUT',serializePolicy(config.value)));ElMessage.success('已保存，下次新建整理任务时生效')
  }catch(e){error.value=(e as Error).message}finally{busy.value=false}
}
async function canLeave(){
  if(!dirty.value)return true
  try{await ElMessageBox.confirm('洗版规则尚未保存，关闭后将丢弃修改。','离开洗版设置',{confirmButtonText:'丢弃并离开',cancelButtonText:'继续编辑'});return true}catch{return false}
}
defineExpose({canLeave})
function beforeUnload(e:BeforeUnloadEvent){if(dirty.value){e.preventDefault();e.returnValue=''}}
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);load()})
onUnmounted(()=>window.removeEventListener('beforeunload',beforeUnload))
</script>
<template>
  <div v-if="config" class="upgrade-page">
    <div class="quality-toolbar"><el-input v-model="search" clearable placeholder="查找规则，如音频、Netflix" aria-label="查找洗版规则" /><el-button type="primary" :loading="busy" :disabled="!dirty" @click="save">保存修改</el-button><span class="save-state" :class="{dirty}">{{dirty?'有未保存的修改':'已与保存配置一致'}}</span></div>
    <el-alert v-if="error" :title="error" type="error" show-icon class="global-error" />
    <section class="panel quality-intro"><p class="eyebrow">VERSION POLICY</p><h2>按你的顺序，保留更合适的版本</h2><p class="hint">同一部电影或同一集，按启用的规则依次比较；当前项相同才比较下一项。{{shareMode?'分享资源按已识别作品和集数比较。':'同片同集按规则比较；内容完全一致时归并字幕并回收新副本。'}}只有规格相同、内容未确认一致，或关键参数未知时，转待处理。</p><div class="priority-trail"><template v-if="enabled.length"><span v-for="(r,i) in enabled" :key="r.key"><b>{{i+1}}</b>{{labels[r.key]}}</span></template><span v-else>尚未启用比较规则</span></div></section>
    <section class="panel"><div class="section-title"><h2>冲突处理</h2><el-tag effect="plain">下次新任务生效</el-tag></div><div class="policy-modes"><label v-for="mode in [{value:'pending',title:'转待处理',description:'保留旧版，新副本转待处理目录。'},{value:'skip',title:'直接跳过',description:'同片或同集保持新旧文件原位置。'},{value:'upgrade',title:'允许洗版',description:shareMode?'按规则比较，淘汰旧分享 STRM。':'按规则比较，淘汰版本进入 115 回收站。'}]" :key="mode.value" :class="['mode-choice',{selected:config.mode===mode.value}]"><input type="radio" name="quality-mode" v-model="config.mode" :value="mode.value" :disabled="busy" /><span><strong>{{mode.title}}</strong><small>{{mode.description}}</small></span></label></div><el-alert v-if="config.mode==='upgrade'" :title="shareMode?'分享洗版会替换已登记的旧分享 STRM；请先用少量测试内容验证。':'洗版替换在新输出就绪后回收旧版；完全相同副本先归并字幕，再回收新视频及多余字幕副本。请先用少量可恢复副本验证。'" type="warning" :closable="false" show-icon /></section>
    <section class="panel"><h2>多版本共存</h2><p class="hint">不同分组分别保留；同一分组内继续按洗版规则选优。只有保存并应用到新任务后生效，不修改正在执行的快照。</p>
<div class="coexist-grid">
 <div class="coexist-item"><div class="coexist-head"><strong>动态范围</strong><el-select v-model="config.dynamic_groups" :disabled="busy"><el-option label="不分组，一起比较" value="off"/><el-option label="杜比 / 非杜比" value="dovi"/></el-select></div><p>杜比视界单独保留，其余格式一起比较。</p></div>
</div>
<p class="hint">{{config.dynamic_groups==='dovi'?'动态范围未知时转待处理；杜比与非杜比各保留一份。':'关闭分组后，所有动态范围按洗版规则一起比较。'}}</p></section>
    <section class="panel rules-panel"><div class="section-title"><div><h2>洗版优先级</h2><p class="hint">9 项规则 · 越靠前越优先 · 手机用箭头，桌面也可拖动左侧手柄</p></div><el-button text :disabled="busy" @click="expanded=expanded.length?[]:config.rules.map((r:QualityRule)=>r.key)">{{expanded.length?'全部收起':'全部展开'}}</el-button></div><p v-if="search" class="hint search-result">找到 {{visibleRules.length}} 项规则；箭头按完整列表位置移动。</p><el-empty v-if="!visibleRules.length" description="没有匹配的规则" :image-size="70" />
      <article v-for="{rule,index} in visibleRules" :key="rule.key" :class="['quality-rule',{enabled:rule.enabled}]" @dragover.prevent @drop.prevent="dropRule(rule.key)">
        <div class="rule-heading"><span class="drag-handle" :draggable="!busy" @dragstart="dragged=rule.key;$event.dataTransfer?.setData('text/plain',rule.key)" @dragend="dragged=''" title="拖动调整规则顺序">↕</span><span class="rule-number">{{String(index+1).padStart(2,'0')}}</span><button class="rule-toggle" :disabled="busy" :aria-expanded="expanded.includes(rule.key)" :aria-controls="`rule-${rule.key}`" @click="toggle(rule.key)"><strong>{{labels[rule.key]}}</strong><small>{{summary(rule)}}</small></button><el-switch v-model="rule.enabled" :disabled="busy" :aria-label="`启用${labels[rule.key]}`" @change="enable(rule)" /></div>
        <div class="rule-actions"><span>{{rule.enabled?'已启用':'已关闭'}}</span><el-button :disabled="busy||index===0" :aria-label="`上移${labels[rule.key]}规则`" @click="moveEntry(config.rules,index,-1)">↑</el-button><el-button :disabled="busy||index===config.rules.length-1" :aria-label="`下移${labels[rule.key]}规则`" @click="moveEntry(config.rules,index,1)">↓</el-button><el-button :disabled="busy" @click="toggle(rule.key)">{{expanded.includes(rule.key)?'收起设置':'设置顺序'}}</el-button></div>
        <div v-if="expanded.includes(rule.key)" :id="`rule-${rule.key}`" class="rule-settings"><p class="hint rule-caption">{{captions[rule.key]}}</p>
          <div v-if="numericRules.includes(rule.key)&&!rule.order.length" class="number-order"><label><input type="radio" v-model="rule.descending" :value="true" :name="`direction-${rule.key}`" :disabled="busy" />{{rule.key==='size'?'大文件优先':'数值较大优先'}}</label><label><input type="radio" v-model="rule.descending" :value="false" :name="`direction-${rule.key}`" :disabled="busy" />{{rule.key==='size'?'小文件优先':'数值较小优先'}}</label></div>
          <template v-if="!numericRules.includes(rule.key)||rule.order.length"><p class="hint order-description">序号越小越优先。未列入列表的值转待处理。</p><div v-if="!values(rule).length" class="empty-order">{{rule.key==='group'?'先添加你偏好的发布组。':'点击下方按钮载入建议顺序。'}}</div><div v-for="(value,position) in values(rule)" :key="value" class="preference-row"><span class="preference-number">{{position+1}}</span><span class="preference-label">{{label(rule.key,value)}}</span><div class="preference-actions"><el-button :disabled="busy||position===0" :aria-label="`上移${label(rule.key,value)}`" @click="moveEntry(values(rule),position,-1)">↑</el-button><el-button :disabled="busy||position===values(rule).length-1" :aria-label="`下移${label(rule.key,value)}`" @click="moveEntry(values(rule),position,1)">↓</el-button><el-button v-if="!usesLegacySource(rule)" :disabled="busy" :aria-label="`移除${label(rule.key,value)}`" @click="values(rule).splice(position,1)">×</el-button></div></div>
            <template v-if="usesLegacySource(rule)"><p class="hint">沿用旧版：同资源类型时再按下方平台比较。点击“改用建议顺序”可改为统一列表。</p><div v-for="(value,position) in rule.platforms" :key="value" class="preference-row"><span class="preference-number">{{position+1}}</span><span class="preference-label">{{label('source',`web-dl:${value}`)}}</span><div class="preference-actions"><el-button :disabled="busy||position===0" @click="moveEntry(rule.platforms,position,-1)" :aria-label="`上移旧平台${value}`">↑</el-button><el-button :disabled="busy||position===rule.platforms.length-1" @click="moveEntry(rule.platforms,position,1)" :aria-label="`下移旧平台${value}`">↓</el-button></div></div></template>
            <div v-else-if="rule.key==='group'||rule.order.length" class="add-preference"><el-input v-if="rule.key==='group'" v-model="additions[rule.key]" placeholder="发布组，如 LangWeb" :disabled="busy" :maxlength="25" @keyup.enter="add(rule)" aria-label="新增发布组名称" /><el-select v-else v-model="additions[rule.key]" placeholder="选择要加入的选项" filterable :disabled="busy"><el-option v-for="[value,name] in available(rule)" :key="value" :value="value" :label="name" /></el-select><el-button :disabled="busy||!additions[rule.key]" @click="add(rule)">添加</el-button></div>
          </template>
          <div class="order-tools"><el-button v-if="suggestions[rule.key]?.length" :disabled="busy" text @click="preset(rule)">{{values(rule).length?'改用建议顺序':numericRules.includes(rule.key)?'自定义各规格顺序':'载入建议顺序'}}</el-button><el-button v-if="numericRules.includes(rule.key)&&rule.order.length" :disabled="busy" text @click="setOrder(rule,[])">改为按数值比较</el-button></div><p v-if="rule.key==='audio'" class="hint">{{shareMode?'无法比较的音频项跳过，继续比较其他已启用项目。':'未知 DTS profile 或不能归类的音轨转待处理；不靠文件名猜测 Atmos。'}}</p><p v-if="rule.key==='group'" class="hint">只匹配明确的发布组名称，不使用正则表达式。</p>
        </div>
      </article>
      <p class="hint">关闭的规则会跳过。保存不会改变正在执行任务的规则快照。</p><div class="quality-bottom"><el-button type="primary" :loading="busy" :disabled="!dirty" @click="save">保存修改</el-button><el-button :disabled="busy" @click="load">重新读取已保存设置</el-button></div>
    </section>
  </div><el-alert v-else-if="error" :title="error" type="error" show-icon /><div v-else class="loading">正在读取洗版设置…</div>
</template>
<style scoped>
.upgrade-page{--line:#e2e9e1;--ink:#28483a}.quality-toolbar{position:sticky;top:0;z-index:12;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;padding:13px 16px;margin:-10px 0 22px;background:#f5f7f5f5;border:1px solid var(--line);border-radius:12px;backdrop-filter:blur(12px)}.quality-toolbar .el-button{height:36px}.save-state{font-size:11px;color:#829289;grid-column:1/-1}.save-state.dirty{color:#986727}.quality-intro{background:#f0f6ef;border-color:#d6e5d4}.quality-intro h2{font-size:21px;line-height:1.5;margin-bottom:9px}.quality-intro .hint{margin-top:0}.priority-trail{display:flex;gap:7px;flex-wrap:wrap;margin-top:19px}.priority-trail>span{display:flex;align-items:center;gap:6px;padding:6px 9px;border-radius:6px;background:#ffffffb0;color:#39634c;font-size:11px}.priority-trail b{font-size:10px;color:#7c9c83}.policy-modes{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.mode-choice{border:1px solid var(--line);border-radius:10px;display:flex;gap:9px;padding:15px 12px;align-items:flex-start;cursor:pointer}.mode-choice.selected{border-color:#6ca08b;background:#eff6f0}.mode-choice input{accent-color:#24735f;margin:4px 0 0}.mode-choice strong{font-size:13px;display:block}.mode-choice small{font-size:12px;line-height:1.7;display:block;color:#7a8b81;margin-top:7px}.recycle-check{display:flex;align-items:center;gap:14px;margin:20px 0}.recycle-check .hint{margin:0}.coexist-heading{display:flex;gap:20px;align-items:center;justify-content:space-between}.coexist-heading h2{margin-bottom:6px}.coexist-heading .hint{margin:0}.coexist-heading .el-switch{flex-shrink:0}.coexist-preview{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:20px;padding:19px;background:#f5f8f4;border-radius:10px;font-size:12px;color:#819085}.coexist-preview span{padding:7px 12px;background:white;border:1px solid var(--line);border-radius:6px;color:#347255}.coexist-preview b{font-weight:400}.rules-panel>.section-title .hint{margin-top:8px;font-size:12px}.quality-rule{border:1px solid var(--line);border-radius:10px;margin-top:12px;overflow:hidden;background:#fff}.quality-rule.enabled{border-color:#b7d2bf}.rule-heading{display:flex;align-items:center;gap:10px;padding:16px 16px 10px}.drag-handle{cursor:grab;color:#a1afa6;padding:4px}.rule-number{font-size:12px;font-weight:650;width:30px;height:30px;border-radius:9px;background:#eff3ee;color:#6b8b75;display:grid;place-items:center;flex-shrink:0}.enabled .rule-number{background:#e8f3e8;color:#34714d}.rule-toggle{border:0;background:none;text-align:left;padding:0;flex:1;min-width:0;color:var(--ink)}.rule-toggle strong{display:block;font-size:14px}.rule-toggle small{display:block;font-size:11px;color:#849087;line-height:1.6;margin-top:5px;overflow-wrap:anywhere}.rule-actions{display:flex;gap:7px;align-items:center;padding:0 16px 14px}.rule-actions>span{margin-right:auto;color:#8e9c93;font-size:11px}.enabled .rule-actions>span{color:#418360}.rule-actions .el-button+.el-button,.preference-actions .el-button+.el-button{margin:0}.rule-actions .el-button{height:32px;padding:7px 11px}.rule-settings{border-top:1px solid var(--line);padding:17px;background:#fbfcfa}.rule-caption{margin:0 0 13px}.order-description{font-size:12px;margin:5px 0 13px}.preference-row{border:1px solid var(--line);background:white;border-radius:8px;display:flex;align-items:center;gap:11px;padding:10px 12px;margin-top:8px}.preference-number{flex-shrink:0;width:23px;height:23px;display:grid;place-items:center;font-size:10px;background:#eff4ed;border-radius:50%;color:#6b8c71}.preference-label{flex:1;min-width:0;font-size:12px;line-height:1.6;overflow-wrap:anywhere}.preference-actions{display:flex;gap:6px;flex-shrink:0}.preference-actions .el-button{width:32px;height:32px;padding:6px}.empty-order{padding:19px;border:1px dashed #cfdbcd;border-radius:8px;color:#8a998d;font-size:12px}.add-preference{display:flex;gap:9px;margin-top:13px}.add-preference>.el-input,.add-preference>.el-select{flex:1;min-width:0}.order-tools{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}.order-tools .el-button{padding:3px 0;margin:0}.number-order{display:flex;gap:12px;flex-wrap:wrap}.number-order label{font-size:12px;color:#536e5e;display:flex;gap:6px;align-items:center;padding:11px;border:1px solid var(--line);border-radius:8px;background:white}.number-order input{accent-color:#24735f}.quality-bottom{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}.quality-bottom .el-button{margin:0}.search-result{margin-top:0}
@media(max-width:760px){.quality-toolbar{padding:11px;margin:-7px 0 17px;gap:7px}.quality-intro h2{font-size:18px}.policy-modes{grid-template-columns:1fr}.mode-choice{padding:12px}.mode-choice small{margin-top:3px}.coexist-heading{flex-wrap:wrap;gap:12px}.coexist-preview{gap:10px;padding:15px 8px}.coexist-preview span{padding:6px 9px}.recycle-check{flex-direction:column;align-items:flex-start;gap:9px}.rule-heading{padding:14px 11px 10px;gap:8px}.drag-handle{display:none}.rule-number{width:27px;height:27px}.rule-actions{padding:0 11px 12px}.rule-settings{padding:13px 11px}.preference-row{padding:10px;gap:8px;flex-wrap:wrap}.preference-actions{margin-left:auto}.preference-label{min-width:110px}.rules-panel>.section-title{align-items:flex-start}.quality-rule .el-switch{height:25px}.quality-rule .el-switch :deep(.el-switch__core){min-width:33px}.quality-bottom>.el-button{flex:1}.number-order{gap:8px}.number-order label{flex:1;justify-content:center;white-space:nowrap}}
/* Match the workspace palette in both appearance modes. */
.upgrade-page{--line:var(--el-border-color);--ink:var(--text)}
.quality-toolbar{top:60px;background:var(--surface);border-color:var(--line)}
.quality-intro,.mode-choice.selected,.rule-settings,.coexist-preview{background:var(--surface-soft);border-color:var(--line)}
.priority-trail>span,.coexist-preview span,.preference-row,.number-order label,.quality-rule{background:var(--surface);border-color:var(--line);color:var(--text)}
.priority-trail b,.rule-number,.preference-number{color:var(--accent);background:var(--el-color-primary-light-9)}
.mode-choice input,.number-order input{accent-color:var(--accent)}.mode-choice small,.rule-toggle small,.rule-actions>span,.save-state,.empty-order{color:var(--muted)}
.quality-rule.enabled,.mode-choice.selected{border-color:var(--accent)}.enabled .rule-actions>span{color:var(--accent)}
@media(max-width:760px){.quality-toolbar{top:60px}.coexist-heading>strong{min-width:0;overflow-wrap:anywhere}}
.coexist-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;margin:14px 0}.coexist-item{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--surface-soft)}.coexist-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.coexist-head strong{font-size:13px;font-weight:600}.coexist-item p{margin:7px 0 0;font-size:11px;line-height:1.6;color:var(--muted)}.coexist-item .el-select{width:132px}</style>
