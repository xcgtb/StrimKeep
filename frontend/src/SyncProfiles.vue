<script setup lang="ts">
import {onMounted,ref} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import DirectoryPicker from './DirectoryPicker.vue'
type Profile={id?:string;name:string;source_cid:string;source_label:string;source_cids:string[];source_labels:Record<string,string>;mode:'incremental'|'full';metadata_mode:'inherit'|'source'|'technical';exclude_keywords:string[];after_transfer?:boolean;watch_changes?:boolean;emby_target:string;enabled:boolean;frequency:'daily'|'weekly'|'interval'|'cron';weekday:number;time:string;interval_minutes:number;cron:string;timezone:string;next_due?:number}
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{navigate:[page:string]}>()
const profiles=ref<Profile[]>([]),draft=ref<Profile|null>(null),picker=ref(false),busy=ref(false),error=ref('')
const activityStatus=ref('')
type PathPreview = { cloud_path: string; local_path?: string; emby_path?: string | null; emby_target_name?: string; emby_error?: string; skip_reason?: string; sidecar?: boolean }
type EmbyTarget = { id:string; name:string; upstream:string; api_key_configured:boolean }
const embyTargets = ref<EmbyTarget[]>([])
const embyTargetsError = ref(false)
const previewRoot = ref<{ id: string; name: string }>()
const directoryRun = ref<Profile>()
async function executeDirectory(folder:{id:string;name:string}){
  const profile = directoryRun.value
  directoryRun.value = undefined
  if(!profile)return
  await runAction(async()=>{
    const job = await props.api('/sync/profiles/'+profile.id+'/run-directory','POST',{source_cid:folder.id})
    ElMessage.success(folder.name+' 已加入目录同步（任务 #'+job.id+'）')
  })
}
const previewOpen = ref(false)
const previewBusy = ref(false)
const preview = ref<PathPreview>()
const previewError = ref('')
const weekdays=['周日','周一','周二','周三','周四','周五','周六']
const metadataLabels = {
  inherit: '沿用全局设置',
  source: 'STRM + 网盘已有附属文件',
  technical: 'STRM + 技术媒体信息',
} satisfies Record<Profile['metadata_mode'], string>
const blank=():Profile=>({name:'',source_cid:'',source_label:'',source_cids:[],source_labels:{},mode:'incremental',metadata_mode:'inherit',exclude_keywords:[],emby_target:'main',enabled:false,frequency:'daily',weekday:1,time:'03:00',interval_minutes:60,cron:'0 3 * * *',timezone:'Asia/Shanghai'})
function normalized(p:Profile):Profile{const ids=p.source_cids?.length?[...p.source_cids]:p.source_cid?[p.source_cid]:[];return {...blank(),...p,emby_target:p.emby_target||'main',exclude_keywords:[...(p.exclude_keywords||[])],source_cids:ids,source_labels:{...(p.source_labels||{}),...(p.source_cid&&p.source_label?{[p.source_cid]:p.source_label}:{})}}}
async function load(){
  const result=await props.api('/sync/profiles')
  profiles.value=result.items.map(normalized)
  activityStatus.value=result.activity_status||''
}
async function loadEmbyTargets() {
  try { embyTargets.value=(await props.api('/emby-proxy')).servers || [] }
  catch { embyTargetsError.value=true }
}
function targetLabel(id: string) {
  if (id==='main') return '主 Emby'
  if (id==='off') return '入库通知关闭'
  return embyTargets.value.find(target=>target.id===id)?.name || '原 Emby 实例'
}
function edit(p:Profile){draft.value=normalized(p)}
async function canLeave(){if(!draft.value)return true;try{await ElMessageBox.confirm('同步任务正在编辑，切换页面后内容可能丢失。','离开同步任务？',{confirmButtonText:'离开',cancelButtonText:'继续编辑'});return true}catch{return false}}
function hasDraft(){return !!draft.value}
defineExpose({canLeave,hasDraft})
async function runAction(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
onMounted(()=>{void runAction(load); void loadEmbyTargets()})
function select(folder:{id:string,name:string}){if(draft.value){if(!draft.value.source_cids.includes(folder.id)){if(draft.value.source_cids.length>=20){ElMessage.warning('每个任务最多添加 20 个目录');picker.value=false;return}draft.value.source_cids.push(folder.id)}draft.value.source_labels[folder.id]=folder.name}picker.value=false}
function removeDirectory(id:string){if(draft.value){draft.value.source_cids=draft.value.source_cids.filter(value=>value!==id);delete draft.value.source_labels[id]}}
async function previewFile(file: { id: string }) {
  const root = previewRoot.value
  if (!root || previewBusy.value) return
  previewRoot.value = undefined
  preview.value = undefined
  previewError.value = ''
  previewOpen.value = true
  previewBusy.value = true
  try {
    preview.value = await props.api('/sync/profiles/preview-path', 'POST', {
      source_cid: root.id, file_id: file.id, emby_target: draft.value?.emby_target || 'main',
      exclude_keywords: draft.value?.exclude_keywords || [], metadata_mode: draft.value?.metadata_mode || 'inherit',
    })
  } catch (e) {
    previewError.value = (e as Error).message
  } finally {
    previewBusy.value = false
  }
}
async function save() {
  const p = draft.value
  if (!p) return
  if (!p.name.trim() || !p.source_cids.length) {
    ElMessage.warning('请填写任务名称并选择一个目录')
    return
  }
  if (p.exclude_keywords.some(word => !word.trim() || word.trim().length > 100)) {
    ElMessage.warning('每个排除词需填写 1 至 100 个字符')
    return
  }
  const {id, mode, metadata_mode, emby_target, enabled, frequency, weekday, time} = p
  await runAction(async () => {
    await props.api(id ? `/sync/profiles/${id}` : '/sync/profiles', id ? 'PUT' : 'POST', {
      name: p.name.trim(), source_cids: p.source_cids, source_labels: p.source_labels,
      mode, metadata_mode, emby_target, enabled, frequency, weekday, time,
      exclude_keywords: p.exclude_keywords.map(word => word.trim()),
      after_transfer: mode === 'incremental' && !!p.after_transfer,
      watch_changes: !!p.watch_changes,
      interval_minutes: p.interval_minutes ?? 60, cron: p.cron || '0 3 * * *', timezone: p.timezone || 'Asia/Shanghai',
    })
    draft.value = null
    await load()
    ElMessage.success('同步任务已保存')
  })
}
async function execute(p:Profile){await runAction(async()=>{const job=await props.api(`/sync/profiles/${p.id}/run`,'POST',{});ElMessage.success(`“${p.name}”已加入队列（任务 #${job.id}）`)})}
async function remove(p:Profile){try{await ElMessageBox.confirm(`删除同步任务“${p.name}”？已入队的任务不会取消，云盘与本地文件不会被删除。`,'删除同步任务',{type:'warning'})}catch{return}await runAction(async()=>{await props.api(`/sync/profiles/${p.id}`,'DELETE');await load();ElMessage.success('同步任务已删除')})}
function timing(p:Profile){if(!p.enabled)return '仅手动';if(p.frequency==='interval')return `每 ${p.interval_minutes} 分钟`;if(p.frequency==='cron')return `Cron ${p.cron}`;return `${p.frequency==='daily'?'每天':weekdays[p.weekday]} ${p.time}`}
</script>
<template>
  <el-alert v-if="error" :title="error" type="error" show-icon/>
  <div v-if="!draft" class="sync-intro"><el-button type="primary" :disabled="busy" @click="draft=blank()">新建任务</el-button></div>
  <section v-if="draft" class="sync-editor">
    <div class="sync-editor-title"><h2>{{draft.id?'编辑任务':'新建同步任务'}}</h2></div>
    <div class="sync-editor-grid">
      <label class="sync-field"><span>任务名称</span><el-input v-model="draft.name" maxlength="80" placeholder="例如：电影库每天增量"/></label>
      <div class="sync-field wide">
        <span>同步目录（可添加多个）</span>
        <div v-for="id in draft.source_cids" :key="id" class="sync-folder">
          <span :title="draft.source_labels[id]||id">{{draft.source_labels[id]||id}}</span>
          <div class="sync-folder-actions">
            <el-button link type="primary" :disabled="previewBusy" @click="previewRoot={id,name:draft.source_labels[id]||id}">预览路径</el-button>
            <el-button link type="danger" @click="removeDirectory(id)">移除</el-button>
          </div>
        </div>
        <div><el-button @click="picker=true">＋ 添加目录</el-button><span class="sync-directory-tip"> 已添加 {{draft.source_cids.length}} / 20</span></div>
      </div>
      <div class="sync-field">
        <span>同步方式</span>
        <el-radio-group v-model="draft.mode" aria-label="同步方式"><el-radio-button value="incremental">增量同步</el-radio-button><el-radio-button value="full">全量同步</el-radio-button></el-radio-group>
        <span class="sync-directory-tip">{{draft.mode==='incremental'?'扫描目录，复用未变化的输出。':'逐项核对本地和网盘，内容相同的 STRM 不重写。'}}</span>
      </div>
      <div class="sync-field"><span>定时执行</span><div class="sync-switch"><el-switch v-model="draft.enabled" aria-label="定时执行"/><span>{{draft.enabled?'已开启':'已关闭'}}</span></div></div>
      <template v-if="draft.enabled"><div class="sync-field wide"><span>执行频率</span><el-radio-group v-model="draft.frequency"><el-radio-button value="daily">每天</el-radio-button><el-radio-button value="weekly">每周</el-radio-button><el-radio-button value="interval">间隔</el-radio-button><el-radio-button value="cron">Cron</el-radio-button></el-radio-group></div>
        <div v-if="draft.frequency==='weekly'" class="sync-field"><span>星期</span><el-select v-model="draft.weekday"><el-option v-for="(day,i) in weekdays" :key="i" :label="day" :value="i"/></el-select></div>
        <label v-if="draft.frequency==='daily'||draft.frequency==='weekly'" class="sync-field"><span>执行时间</span><el-input v-model="draft.time" type="time"/></label>
        <div v-if="draft.frequency==='interval'" class="sync-field"><span>间隔（分钟）</span><el-input-number v-model="draft.interval_minutes" :min="5" :max="43200"/></div>
        <label v-if="draft.frequency==='cron'" class="sync-field"><span>Cron 表达式</span><el-input v-model="draft.cron" placeholder="0 3 * * *"/></label>
        <div class="sync-field"><span>时区</span><el-select v-model="draft.timezone"><el-option label="北京时间" value="Asia/Shanghai"/><el-option label="UTC" value="UTC"/></el-select></div>
      </template>
    </div>
    <details class="sync-advanced">
      <summary>附属文件与高级选项</summary>
      <div class="sync-editor-grid">
        <div class="sync-field wide">
          <div class="sync-switch"><el-switch v-model="draft.watch_changes" aria-label="跟踪 115 目录变更"/><span>跟踪 115 目录变更</span></div>
          <span class="sync-directory-tip">约每 90 秒补扫有变更的目录。开启定时同步可核对漏报和失效文件。</span>
        </div>
        <div v-if="draft.mode==='incremental'" class="sync-field wide">
          <div class="sync-switch"><el-switch v-model="draft.after_transfer" aria-label="纯转存后同步"/><span>纯转存后同步</span></div>
          <span class="sync-directory-tip">转存核实后同步目标目录。已安排立即整理的转存会跳过这一步。</span>
        </div>
        <div class="sync-field wide">
          <span>路径排除词</span>
          <el-input-tag v-model="draft.exclude_keywords" :max="20" :maxlength="100"
            aria-label="路径排除词" placeholder="输入后按回车，例如 /预告片/"/>
          <span class="sync-directory-tip">按完整 115 路径匹配，忽略大小写。命中的文件会跳过，已有本地文件保留。</span>
        </div>
        <div class="sync-field wide">
          <span>元数据处理</span>
          <el-select v-model="draft.metadata_mode" aria-label="元数据处理">
            <el-option v-for="(label, value) in metadataLabels" :key="value" :label="label" :value="value"/>
          </el-select>
          <span v-if="draft.metadata_mode==='technical'" class="sync-directory-tip">提取分辨率、编码和音轨，保留本地 NFO 和图片。</span>
          <span v-else-if="draft.metadata_mode==='source'" class="sync-directory-tip">复制网盘已有的 NFO、图片和选定的附属文件。</span>
          <span v-else class="sync-directory-tip">按 STRM 全局设置处理字幕、NFO 和海报。</span>
        </div>
        <div class="sync-field">
          <span>Emby 入库通知</span>
          <el-select v-model="draft.emby_target" aria-label="Emby 入库通知">
            <el-option value="main" label="沿用默认"/>
            <el-option value="off" label="关闭入库通知"/>
            <el-option v-for="target in embyTargets" :key="target.id" :value="target.id" :label="target.name"
              :disabled="!target.upstream || !target.api_key_configured"/>
            <el-option v-if="!['main','off'].includes(draft.emby_target) && !embyTargets.some(target=>target.id===draft?.emby_target)"
              :value="draft.emby_target" label="原 Emby 实例（当前不可用）" disabled/>
          </el-select>
          <span v-if="embyTargetsError" class="sync-directory-tip">暂时无法读取 Emby 实例，请刷新页面后重试。</span>
          <span v-else-if="draft.emby_target==='main'" class="sync-directory-tip">{{draft.metadata_mode==='technical'?'技术媒体信息模式默认关闭通知。':'使用主 Emby 和全局通知设置。'}}</span>
          <span v-else-if="draft.emby_target!=='off'" class="sync-directory-tip">按所选 Emby 的路径映射提交变更，并核实入库。</span>
        </div>
      </div>
    </details>
    <div class="sync-editor-actions"><el-button :disabled="busy" @click="draft=null">取消</el-button><el-button type="primary" :disabled="busy" @click="save">保存任务</el-button></div>
  </section>
  <div v-if="profiles.length" class="sync-list">
    <article v-for="p in profiles" :key="p.id" class="sync-card">
      <div class="sync-card-top"><strong>{{p.name}}</strong><span class="sync-badge" :class="{'sync-badge-full':p.mode==='full'}">{{p.mode==='incremental'?'增量同步':'全量同步'}}</span></div>
      <div class="sync-card-directory"><div v-for="id in p.source_cids" :key="id">{{p.source_labels[id]||id}}</div></div>
      <div class="sync-card-meta">
        <span v-if="p.enabled||(!p.after_transfer&&!p.watch_changes)">{{timing(p)}}</span>
        <span v-if="p.after_transfer">纯转存后同步</span>
        <span v-if="p.watch_changes">115 变更同步{{activityStatus&&activityStatus!=='active'?' · 暂缓':''}}</span>
        <span v-if="p.metadata_mode!=='inherit'">{{metadataLabels[p.metadata_mode]}}</span>
        <span v-if="p.emby_target!=='main'">{{targetLabel(p.emby_target)}}</span>
        <span v-if="p.enabled&&p.next_due">下次 {{new Date(p.next_due*1000).toLocaleString('zh-CN',{timeZone:p.timezone||'Asia/Shanghai'})}}</span>
      </div>
      <div class="sync-card-actions"><el-button type="primary" :disabled="busy" @click="execute(p)">立即执行</el-button><el-button :disabled="busy" @click="directoryRun=p">同步单个目录</el-button><el-button :disabled="busy" @click="edit(p)">编辑</el-button><el-button text type="danger" :disabled="busy" @click="remove(p)">删除</el-button></div>
    </article>
  </div>
  <div v-else-if="!draft" class="sync-empty"><p>暂无同步任务</p></div>
  <el-button text @click="emit('navigate','tasks')">查看任务进度 →</el-button>
  <DirectoryPicker v-if="picker" :api="api" @select="select" @close="picker=false"/>
  <DirectoryPicker v-if="directoryRun" :api="api" @select="executeDirectory" @close="directoryRun=undefined"/>
  <DirectoryPicker v-if="previewRoot" :api="api" :file-root="previewRoot" @select="previewFile" @close="previewRoot=undefined"/>
  <el-dialog v-model="previewOpen" title="STRM 路径预览" width="min(600px, 94vw)" append-to-body>
    <p v-if="previewBusy" role="status">正在核对网盘路径…</p>
    <el-alert v-else-if="previewError" :title="previewError" type="error" :closable="false" />
    <template v-else-if="preview">
      <dl class="sync-preview-paths">
        <dt>115 网盘源文件</dt><dd>{{ preview.cloud_path }}</dd>
        <template v-if="preview.local_path">
          <dt>StrimKeep 输出路径</dt><dd>{{ preview.local_path }}</dd>
          <template v-if="!preview.emby_error">
            <dt>{{ preview.emby_target_name ? preview.emby_target_name + ' 映射路径' : 'Emby 入库通知' }}</dt>
            <dd>{{ preview.emby_path || (preview.emby_target_name ? '请检查路径映射' : '已关闭') }}</dd>
          </template>
        </template>
      </dl>
      <el-alert v-if="preview.skip_reason" :title="preview.skip_reason" type="warning" :closable="false" />
      <el-alert v-else-if="preview.emby_error" :title="preview.emby_error" type="warning" :closable="false" />
      <p v-else-if="preview.sidecar" class="sync-directory-tip">该文件按附属文件规则同步。选择视频可预览 STRM 路径。</p>
    </template>
  </el-dialog>
</template>
<style scoped>
.sync-advanced{margin-bottom:16px}
.sync-advanced summary{cursor:pointer;color:var(--text);font-size:13px;font-weight:600}
.sync-advanced .sync-editor-grid{padding-bottom:4px}
.sync-folder-actions{display:flex;flex:none;align-items:center;gap:12px;padding-right:10px}
.sync-folder-actions :deep(.el-button + .el-button){margin-left:0}
.sync-preview-paths{display:grid;gap:5px;margin:0 0 10px}
.sync-preview-paths dt{font-size:12px;color:var(--muted)}
.sync-preview-paths dd{margin:0 0 9px;overflow-wrap:anywhere;color:var(--text)}
.sync-intro,.sync-editor-title,.sync-card-top,.sync-editor-actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.sync-intro{padding:8px 0 18px}.sync-intro{justify-content:flex-end}.sync-editor-title h2{margin:0;font-size:17px;line-height:1.4}.sync-empty p{color:var(--muted);font-size:13px;margin:0}.sync-editor{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px}.sync-editor-title{padding-bottom:18px;border-bottom:1px solid var(--line)}.sync-editor-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 22px;padding:20px 0}.sync-field{display:flex;flex-direction:column;gap:8px;min-width:0;color:var(--text);font-size:13px}.sync-field.wide{grid-column:1/-1}.sync-folder{display:flex;align-items:center;justify-content:space-between;gap:8px;border:1px solid var(--line);border-radius:5px;min-height:32px;padding-left:10px}.sync-folder span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}.sync-directory-tip{font-size:12px;color:var(--muted);margin-left:8px}.sync-switch{display:flex;align-items:center;gap:10px;min-height:32px}.sync-editor-actions{border-top:1px solid var(--line);padding-top:16px;justify-content:flex-end}.sync-editor-actions>span{margin-right:auto;color:var(--muted);font-size:12px}.sync-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,310px),1fr));gap:14px;margin-bottom:14px}.sync-card{border:1px solid var(--line);background:var(--surface);border-radius:14px;padding:18px;min-width:0}.sync-card-top strong{min-width:0;flex:1;font-size:15px;color:var(--text);overflow-wrap:anywhere}.sync-badge{width:auto;flex:none;background:#eaf2ff;color:#2563eb;border-radius:999px;padding:4px 9px;font-size:12px;white-space:nowrap}.sync-badge.sync-badge-full{background:#fff2df;color:#a45b00}.sync-card-directory{color:var(--muted);font-size:13px;line-height:1.6;overflow-wrap:anywhere;margin:12px 0}.sync-card-meta{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--muted);font-size:12px;min-height:24px}.sync-card-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px;border-top:1px solid var(--line);padding-top:14px;margin-top:10px}.sync-card-actions :deep(.el-button + .el-button){margin-left:0}.sync-empty{text-align:center;border:1px dashed var(--line);border-radius:14px;padding:28px 12px;margin-bottom:12px}.sync-empty .el-button{margin-top:16px}@media(max-width:700px){.sync-editor-grid{grid-template-columns:1fr}.sync-field.wide{grid-column:auto}.sync-editor-actions{flex-wrap:wrap}.sync-editor-actions>span{width:100%}.sync-intro{align-items:flex-start}}
</style>
