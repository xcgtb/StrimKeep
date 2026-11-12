<script setup lang="ts">
import {onMounted,ref} from 'vue'
import {ElMessage} from 'element-plus'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const local=ref('/strm'),remote=ref('/strimkeep'),busy=ref(false),error=ref('')
onMounted(async()=>{try{const c=(await props.api('/console/config')).config;local.value=c.strm_container_path;remote.value=c.emby_strm_path}catch(e){error.value=(e as Error).message}})
async function save(){busy.value=true;error.value='';try{const c=(await props.api('/console/config')).config;await props.api('/console/config','PUT',{...c,strm_container_path:local.value,emby_strm_path:remote.value});ElMessage.success('Emby 路径映射已保存')}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
</script>
<template><el-alert v-if="error" :title="error" type="error"/><el-form label-position="top" class="mapping-form" @submit.prevent><div class="mapping-grid"><el-form-item label="StrimKeep 容器内 STRM 目录"><el-input v-model="local" readonly/></el-form-item><span class="mapping-arrow" aria-hidden="true">→</span><el-form-item label="Emby 容器内对应目录"><el-input v-model="remote" placeholder="/strimkeep"/></el-form-item></div></el-form><p class="hint">同一份 NAS 文件夹在两边的挂载路径。通知入库时自动换成 Emby 能读取的准确路径，目录需已挂载到 Emby。</p><el-button :loading="busy" @click="save">保存路径映射</el-button></template>
<style scoped>
.mapping-form{margin-bottom:6px}
.mapping-grid{align-items:end;gap:14px}
.mapping-grid :deep(.el-form-item){margin-bottom:0}
.mapping-arrow{padding-bottom:11px;font-size:16px;line-height:1}
.mapping-form :deep(.el-input__inner[readonly]){color:var(--muted)}
@media(max-width:760px){.mapping-arrow{display:none}}
</style>
