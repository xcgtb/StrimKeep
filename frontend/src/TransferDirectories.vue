<script setup lang="ts">
import {computed,onMounted,ref} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import {FolderOpened,Setting} from '@element-plus/icons-vue'
import DirectoryPicker from './DirectoryPicker.vue'

const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>;disabled?:boolean;hideChannel?:boolean}>()
const emit=defineEmits<{saved:[];navigate:[page:string]}>()
const rows=ref<any[]>([]),busy=ref(false),error=ref(''),channel=ref('')
const draft=ref<any>(null),initial=ref(''),picker=ref('')
// 影视探索是独立入口：目录单独存一份，留空表示跟随 Web 的 115 分享目录。
const exploreCid=ref(''),exploreLabel=ref(''),exploreBusy=ref(false)
const channelRows=computed(()=>rows.value.filter(r=>r.source==='channel'))
const visibleRows=computed(()=>{const list=[...rows.value.filter(r=>r.source!=='channel')]
 if(props.hideChannel)return list
 return [...list,...channelRows.value.filter(r=>r.channel===channel.value)]})
const dirty=computed(()=>!!draft.value&&JSON.stringify(draft.value)!==initial.value)
const types=[{key:'share',label:'115 分享'},{key:'magnet',label:'磁力'},{key:'ed2k',label:'ED2K'}]
function title(row:any){return row.source==='web'?'Web':row.source==='bot'?'Telegram 机器人':'Telegram 频道'}
function value(row:any,key:string){if(key==='share'&&row.source==='channel'&&row.share_mode!=='transfer')return row.share_mode==='strm'?'免转存 STRM':'不处理';return row[key+'_label']||row[key+'_cid']||'未设置'}
function open(row:any){draft.value=JSON.parse(JSON.stringify(row));initial.value=JSON.stringify(draft.value);error.value=''}
function choose(folder:any){
 if(picker.value==='explore'){exploreCid.value=folder.id;exploreLabel.value=folder.name;picker.value='';void saveExplore();return}
 if(draft.value&&picker.value){draft.value[picker.value+'_cid']=folder.id;draft.value[picker.value+'_label']=folder.name}
 picker.value=''
}
async function loadExplore(){
 try{const r=await props.api('/transfer/config');exploreCid.value=r.config?.explore_cid||'';exploreLabel.value=r.config?.explore_label||''}catch(e){error.value=(e as Error).message}
}
async function saveExplore(){
 if(exploreBusy.value)return;exploreBusy.value=true;error.value=''
 try{
  const r=await props.api('/transfer/config')
  const saved=await props.api('/transfer/config','PUT',{...r.config,explore_cid:exploreCid.value,explore_label:exploreLabel.value})
  exploreCid.value=saved.config?.explore_cid||'';exploreLabel.value=saved.config?.explore_label||''
  ElMessage.success(exploreCid.value?'影视探索转存目录已更新':'影视探索已改为跟随 Web 目录')
 }catch(e){error.value=(e as Error).message}finally{exploreBusy.value=false}
}
function clearExplore(){exploreCid.value='';exploreLabel.value='';void saveExplore()}
function clear(key:string){draft.value[key+'_cid']='';draft.value[key+'_label']=''}
async function canLeave(){if(!dirty.value)return true;try{await ElMessageBox.confirm('目录改动还没有保存。','放弃修改？',{confirmButtonText:'放弃修改',cancelButtonText:'继续编辑',type:'warning'});return true}catch{return false}}
async function close(){if(await canLeave()){draft.value=null;error.value=''}}
async function save(){if(!draft.value)return;busy.value=true;error.value='';try{const r=await props.api('/transfer/destinations','PUT',draft.value);rows.value=r.rows||[];initial.value=JSON.stringify(draft.value);draft.value=null;emit('saved');ElMessage.success('目录已更新')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
onMounted(async()=>{void loadExplore();try{const r=await props.api('/transfer/destinations');rows.value=r.rows||[];channel.value=channelRows.value[0]?.channel||''}catch(e){error.value=(e as Error).message}})
defineExpose({canLeave})
</script>

<template>
<section class="panel destinations">
 <div class="destination-heading"><div class="heading-copy"><span class="heading-icon"><el-icon><FolderOpened/></el-icon></span><div><h2>保存目录</h2><p>为不同来源选择 115 目录</p></div></div></div>
 <el-alert v-if="error&&!draft" :title="error" type="error" show-icon class="destination-error"/>
 <div class="destination-content">
  <div class="intro"><p>各入口独立设置，也可以选择同一个目录。</p><el-button text @click="emit('navigate','channels')">管理频道</el-button></div>
  <p v-if="disabled" class="hint">请先保存下方的转存设置。</p>
  <div class="directory-table" role="table" aria-label="各入口保存目录">
   <div class="table-header" role="row"><span>来源</span><span>115 分享</span><span>磁力</span><span>ED2K</span><span></span></div>
   <div v-for="row in visibleRows" :key="row.source+row.channel" class="table-row" role="row">
    <div class="source"><strong>{{title(row)}}</strong><el-select v-if="row.source==='channel'" v-model="channel" size="small" aria-label="选择频道"><el-option v-for="c in channelRows" :key="c.channel" :label="c.channel" :value="c.channel"/></el-select></div>
    <div v-for="type in types" :key="type.key" class="table-value" :class="{empty:value(row,type.key)==='未设置'}"><span class="mobile-label">{{type.label}}</span><span :title="value(row,type.key)">{{value(row,type.key)}}</span></div>
    <el-button text type="primary" :disabled="busy||disabled" @click="open(row)">编辑</el-button>
   </div>
   <div v-if="!channelRows.length" class="channel-empty"><span>还没有添加频道</span><el-button text type="primary" @click="emit('navigate','channels')">前往添加</el-button></div>
  </div>
  <div class="explore-row">
   <div class="explore-copy"><strong>影视探索</strong><span :class="{empty:!exploreCid}">{{ exploreLabel||exploreCid||'跟随 Web 的 115 分享目录' }}</span></div>
   <div class="explore-actions">
    <el-button size="small" :disabled="busy||disabled||exploreBusy" @click="picker='explore'">选择目录</el-button>
    <el-button v-if="exploreCid" size="small" text :disabled="busy||disabled||exploreBusy" @click="clearExplore">改为跟随 Web</el-button>
   </div>
  </div>
  <p class="footnote">影视探索里点「转存」就落到这一行的目录。转存后自动整理需要目标在待整理目录中；直链和关键词分流可在离线下载页设置。</p>
 </div>
</section>

<el-dialog :model-value="!!draft" :title="draft?`编辑${title(draft)}`:'编辑目录'" width="min(560px, 92vw)" class="directory-dialog" :close-on-click-modal="false" :before-close="close" @update:model-value="(v:boolean)=>{if(!v)close()}">
 <template v-if="draft">
  <el-alert v-if="error" :title="error" type="error" show-icon class="destination-error"/>
  <p class="dialog-intro">选择这个入口接收不同链接时使用的目录。</p>
  <div v-if="draft.source==='channel'" class="mode-row"><span>分享链接</span><el-select v-model="draft.share_mode" aria-label="分享链接处理方式"><el-option label="转存到 115" value="transfer"/><el-option label="免转存 STRM" value="strm"/><el-option label="不处理" value="none"/></el-select></div>
  <div v-for="type in types" :key="type.key" class="edit-row"><div class="edit-label">{{type.label}}<small v-if="type.key==='share'&&draft.source==='channel'&&draft.share_mode!=='transfer'">{{draft.share_mode==='strm'?'免转存无需网盘目录':'已关闭'}}</small></div>
   <div v-if="!(type.key==='share'&&draft.source==='channel'&&draft.share_mode!=='transfer')" class="edit-controls"><button class="folder-choice" type="button" @click="picker=type.key"><el-icon><FolderOpened/></el-icon><span :class="{placeholder:!draft[type.key+'_cid']}" :title="value(draft,type.key)">{{draft[type.key+'_label']||draft[type.key+'_cid']||'选择目录'}}</span><span class="change">更换</span></button><el-button v-if="draft[type.key+'_cid']" text @click="clear(type.key)">清除</el-button></div>
  </div>
  <p class="dialog-note">不需要处理的类型可以留空。机器人发送 /save 目录ID 可临时指定分享目录。</p>
 </template>
 <template #footer><el-button @click="close">取消</el-button><el-button type="primary" :loading="busy" :disabled="!dirty" @click="save">保存</el-button></template>
</el-dialog>
<DirectoryPicker v-if="picker" :api="api" @close="picker=''" @select="choose"/>
</template>

<style scoped>
.destination-heading,.heading-copy,.intro{display:flex;align-items:center;gap:12px}.destination-heading{justify-content:space-between}.heading-copy h2{margin:0;font-size:16px}.heading-copy p,.intro p{margin:5px 0 0;color:var(--muted);font-size:12px}.heading-icon{display:grid;place-items:center;width:38px;height:38px;flex:none;border-radius:11px;background:var(--surface-soft);color:var(--accent);font-size:19px}.destination-content{margin-top:18px}.intro{justify-content:space-between;margin-bottom:12px}.intro p{margin:0}.directory-table{border:1px solid var(--line);border-radius:10px;overflow:hidden}.table-header,.table-row{display:grid;grid-template-columns:minmax(145px,1.1fr) repeat(3,minmax(0,1fr)) 58px;gap:14px;align-items:center;padding:12px 14px}.table-header{background:var(--surface-soft);color:var(--muted);font-size:12px}.table-row+.table-row,.channel-empty{border-top:1px solid var(--line)}.source{display:grid;gap:6px;font-size:13px}.source :deep(.el-select){max-width:170px}.table-value{min-width:0;font-size:13px;color:var(--text)}.table-value>span:last-child{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.table-value.empty{color:var(--muted)}.mobile-label{display:none}.table-row>.el-button{margin:0}.channel-empty{display:flex;align-items:center;justify-content:space-between;padding:7px 14px;color:var(--muted);font-size:12px}.footnote,.dialog-intro,.dialog-note{font-size:12px;line-height:1.7;color:var(--muted)}.footnote{margin:11px 0 0}.destination-error{margin:12px 0}.dialog-intro{margin:0 0 16px}.mode-row,.edit-row{display:grid;grid-template-columns:90px minmax(0,1fr);align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line)}.mode-row :deep(.el-select){width:100%}.edit-label{font-weight:600;font-size:13px}.edit-label small{display:block;margin-top:4px;color:var(--muted);font-weight:400}.edit-controls{display:flex;gap:4px;align-items:center;min-width:0}.folder-choice{display:flex;align-items:center;gap:8px;min-width:0;flex:1;border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--surface);color:var(--text);text-align:left;cursor:pointer}.folder-choice:hover{border-color:var(--accent)}.folder-choice>span:not(.change){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.folder-choice .placeholder{color:var(--muted)}.folder-choice .change{margin-left:auto;color:var(--accent);font-size:12px;flex:none}.dialog-note{margin:14px 0 0}
.table-value>span:last-child,.folder-choice>span:not(.change){white-space:normal;overflow-wrap:anywhere;line-height:1.5}.source :deep(.el-select){width:100%;max-width:220px}
@media(max-width:760px){.destination-heading{align-items:flex-start}.heading-copy p{line-height:1.5}.table-header{display:none}.table-row{grid-template-columns:1fr auto;gap:10px}.source{grid-column:1}.table-row>.el-button{grid-column:2;grid-row:1}.table-value{grid-column:1/-1;display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.table-value>span:last-child{text-align:right;white-space:normal;overflow-wrap:anywhere;min-width:0}.mobile-label{display:inline;color:var(--muted);font-size:12px;flex:none}.mode-row,.edit-row{grid-template-columns:1fr;gap:7px}.folder-choice>span:not(.change){white-space:normal;overflow-wrap:anywhere;line-height:1.5}}
.explore-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--surface-soft)}
.explore-copy{display:grid;gap:5px;font-size:13px;min-width:0}
.explore-copy span{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.explore-copy span.empty{color:var(--muted)}
.explore-actions{display:flex;gap:4px;flex:none}
@media(max-width:760px){.explore-row{align-items:flex-start;flex-direction:column}.explore-actions{width:100%}}
</style>
