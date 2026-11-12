<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref} from 'vue'
import {ElMessage,ElMessageBox} from 'element-plus'
import SettingCard from './SettingCard.vue'
import ExtensionEditor from './ExtensionEditor.vue'
import {VIDEO_PRESETS} from './extensions'
import Upgrade from './Upgrade.vue'
import EmbyMapping from './EmbyMapping.vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const profile=ref<any>(null),base=ref<any>(null),baseline=ref(''),custom=ref(false),busy=ref(false),error=ref(''),ruleEditor=ref<any>(null),qualityKey=ref(0)
const dirty=computed(()=>!!profile.value&&JSON.stringify(profile.value)!==baseline.value)
function draft(e:any){return {organize:JSON.parse(JSON.stringify(e.organize)),upgrade_policy:JSON.parse(JSON.stringify(e.upgrade_policy)),notify_emby:e.notify_emby}}
function accept(data:any){custom.value=data.custom;base.value=data.base;profile.value=draft(data.effective);baseline.value=JSON.stringify(profile.value)}
async function load(){try{accept(await props.api('/share/profile'))}catch(e){error.value=(e as Error).message}}
async function followBase(){
  if(ruleEditor.value&&!await ruleEditor.value.canLeave())return
  if(custom.value){try{await ElMessageBox.confirm('将清除分享 STRM 的独立规则，改为永远跟随影视整理。','跟随影视整理',{confirmButtonText:'跟随',cancelButtonText:'取消'})}catch{return}}
  busy.value=true;error.value=''
  try{accept(await props.api('/share/profile/follow','POST',{follow:true}));qualityKey.value++;ElMessage.success('已改为跟随影视整理')}
  catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function save(){if(ruleEditor.value&&!await ruleEditor.value.canLeave())return;busy.value=true;error.value='';try{accept(await props.api('/share/profile','PUT',profile.value));qualityKey.value++;ElMessage.success('分享整理规则已保存，仅新任务生效')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
const qualityApi=async(path:string,method?:string,body?:unknown)=>{if(path==='/upgrade/config'){if(method==='PUT'&&dirty.value)throw new Error('请先保存上方分享整理设置，再保存洗版规则。');const result=await props.api('/share/quality',method,body);if(method==='PUT'){profile.value.upgrade_policy=JSON.parse(JSON.stringify(result.config));baseline.value=JSON.stringify(profile.value);custom.value=true}return result}return props.api(path,method,body)}
async function canLeave(){if(ruleEditor.value&&!await ruleEditor.value.canLeave())return false;if(!dirty.value)return true;try{await ElMessageBox.confirm('分享整理设置尚未保存。','离开页面',{confirmButtonText:'丢弃并离开',cancelButtonText:'继续编辑'});return true}catch{return false}}
defineExpose({canLeave})
function beforeUnload(e:BeforeUnloadEvent){if(dirty.value){e.preventDefault();e.returnValue=''}}
onMounted(()=>{window.addEventListener('beforeunload',beforeUnload);load()})
onUnmounted(()=>window.removeEventListener('beforeunload',beforeUnload))
</script>
<template>
<div v-if="profile" class="share-profile">
 <div class="profile-actions"><div><h2>整理规则</h2><p>{{custom?'已使用独立规则':'当前沿用影视整理规则'}}</p></div><el-button :disabled="busy||!custom" @click="followBase">{{custom?'跟随影视整理':'已跟随影视整理'}}</el-button><el-button type="primary" :loading="busy" :disabled="!dirty&&custom" @click="save">保存规则</el-button></div>
 <el-alert v-if="error" :title="error" type="error" show-icon/>
 <p class="profile-note">套用会复制影视整理的当前规则；保存后可在这里单独调整。新设置用于之后创建的任务。</p>
 <SettingCard title="文件筛选" description="决定分享中的哪些视频参加整理。" :default-open="false"><p class="hint">生成 STRM 时还会应用“STRM 同步”中的生成格式和大小规则；两边均允许的视频才会处理。</p><ExtensionEditor v-model="profile.organize.video_extensions" label="视频扩展名" :presets="VIDEO_PRESETS" :max-items="40" :excluded="profile.organize.subtitle_extensions" :disabled="busy"/><div class="organize-option"><label>最小视频大小（MB）</label><el-input-number v-model="profile.organize.min_video_size_mb" :min="0" :max="1048576"/></div><div class="organize-option"><label>忽略关键词</label><el-select v-model="profile.organize.ignore_keywords" multiple filterable allow-create default-first-option placeholder="输入关键词并回车"/></div></SettingCard>
 <SettingCard title="重命名与媒体信息" description="设置作品和文件名称。" :default-open="false"><div class="organize-option"><label>重命名 STRM</label><el-switch v-model="profile.organize.rename_enabled"/></div><div class="organize-option"><label>新作品文件夹名称</label><el-select v-model="profile.organize.folder_language"><el-option label="优先中文" value="zh"/><el-option label="优先英文" value="en"/></el-select></div><div class="organize-option"><label>视频文件中的作品名称</label><el-select v-model="profile.organize.filename_language" :disabled="!profile.organize.rename_enabled"><el-option label="优先中文" value="zh"/><el-option label="优先英文" value="en"/></el-select></div><div class="organize-option"><label>已有作品沿用原名称</label><el-switch v-model="profile.organize.keep_existing_names" :disabled="!profile.organize.rename_enabled"/></div><div class="organize-option"><label>探测媒体信息</label><el-switch v-model="profile.organize.ffprobe_enabled"/></div><div class="organize-option"><label>批量识别预取</label><el-switch v-model="profile.organize.identify_prefetch"/></div><div class="organize-option" v-if="profile.organize.identify_prefetch"><label>识别预取并发数</label><el-input-number v-model="profile.organize.probe_concurrency" :min="1" :max="4"/></div></SettingCard>
 <SettingCard title="分类" description="关闭自动分类仍保留作品目录和季目录。" :default-open="false"><div v-for="x in [{key:'classify_enabled',name:'自动分类'},{key:'children_enabled',name:'儿童节目独立分类'},{key:'concert_enabled',name:'演唱会独立分类'},{key:'region_enabled',name:'按地区分类'},{key:'year_enabled',name:'按年份分类'}]" :key="x.key" class="organize-option"><label>{{x.name}}</label><el-switch v-model="profile.organize[x.key]" :disabled="x.key!=='classify_enabled'&&x.key!=='year_enabled'&&!profile.organize.classify_enabled"/></div></SettingCard>
 <SettingCard title="洗版与多版本" description="分享库使用自己的洗版设置。" :default-open="false"><div class="organize-option"><label>允许使用洗版规则</label><el-switch v-model="profile.organize.wash_enabled"/></div><p class="hint">先保存上方设置，再编辑详细比较顺序。洗版只处理分享 STRM 登记的版本。</p><div class="inline-rules" v-if="profile.organize.wash_enabled"><Upgrade :key="qualityKey" ref="ruleEditor" :api="qualityApi" share-mode/></div></SettingCard>
 <SettingCard title="STRM 与 Emby 通知" description="映射用于把输出路径换成 Emby 能访问的路径。" :default-open="false"><div class="organize-option"><label>整理后通知 Emby</label><el-switch v-model="profile.notify_emby"/></div><p class="hint">下方路径映射与影视整理共用；修改会影响两边。还需要把分享影视库对应的目录加入 Emby 媒体库。</p><EmbyMapping :api="api"/></SettingCard>
</div><el-alert v-else-if="error" :title="error" type="error" show-icon/><p v-else class="hint">正在读取分享设置…</p>
</template>
<style scoped>.profile-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.profile-actions>div{flex:1;min-width:200px}.profile-actions h2{margin:0;font-size:16px}.profile-actions p{margin:5px 0 0;color:var(--muted);font-size:12px}.profile-note{margin:14px 0 18px;font-size:12px;line-height:1.7;color:var(--muted)}.organize-option{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--line)}.organize-option:last-child{border-bottom:0}.organize-option label{font-size:13px}.organize-option :deep(.el-select){width:250px;max-width:60%}@media(max-width:600px){.profile-actions>div{flex-basis:100%}.organize-option{align-items:stretch;flex-direction:column;gap:8px}.organize-option :deep(.el-select){width:100%;max-width:none}.organize-option :deep(.el-switch){align-self:flex-start}}.inline-rules :deep(.quality-toolbar){position:static !important;margin:0 0 12px !important;top:auto !important}</style>
