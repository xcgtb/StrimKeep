<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import CloudEntryList from './CloudEntryList.vue'
const props = defineProps<{ api: (path: string, method?: string, body?: unknown) => Promise<any>; fileRoot?: { id: string; name: string } }>()
const emit = defineEmits<{ select: [value: { id: string; name: string }]; close: [] }>()
const path = ref([props.fileRoot ?? { id: '0', name: '115 网盘' }]), files = ref<any[]>([])
const nextOffset = ref(0)
const offset = ref(0), more = ref(false), loading = ref(false), error = ref('')
let sequence=0
async function load(id = path.value.at(-1)!.id, name?: string, index?: number, next = 0) {
  const request=++sequence
  const targetPath=index!==undefined?path.value.slice(0,index+1):name?[...path.value,{id,name}]:[...path.value]
  loading.value = true; error.value = ''
  try {
    const data = await props.api(`/cloud/files?cid=${encodeURIComponent(id)}&offset=${next}&limit=100`)
    if(request!==sequence)return
    files.value = data.items; more.value = data.has_more; offset.value = next; nextOffset.value = data.next_offset ?? next + data.items.length
    path.value=targetPath
  } catch (e) { if(request===sequence)error.value = (e as Error).message }
  finally { if(request===sequence)loading.value = false }
}
onMounted(() => load())
onUnmounted(()=>sequence++)
function open(row: { id: string; name: string; is_dir: boolean }) {
  if (row.is_dir) return load(row.id, row.name)
  emit('select', { id: row.id, name: row.name })
}
</script>
<template>
  <el-dialog :model-value="true" :title="fileRoot ? '选择要预览的源文件' : '选择 115 文件夹'" width="min(700px, 94vw)" append-to-body @close="emit('close')">
    <el-alert v-if="error" :title="error" type="error" />
    <nav class="cloud-breadcrumbs" aria-label="当前目录"><template v-for="(folder,i) in path" :key="folder.id"><span v-if="i" class="cloud-path-divider" aria-hidden="true">/</span><button :aria-current="i===path.length-1?'location':undefined" @click="load(folder.id,undefined,i)">{{folder.name}}</button></template></nav>
    <div class="cloud-list-toolbar"><el-button :disabled="loading||path.length<2" @click="load(path[path.length-2].id,undefined,path.length-2)">返回上级</el-button><span v-if="!fileRoot" class="muted">选择文件夹后确认</span></div>
    <CloudEntryList :items="fileRoot ? files : files.filter(f=>f.is_dir)" :loading="loading" :select-files="!!fileRoot" :empty-text="fileRoot ? '本页没有文件或子文件夹' : '本页没有子文件夹'" @open="open"/>
    <div v-if="offset||more" class="cloud-pagination"><el-button :disabled="loading || offset === 0" @click="load(undefined, undefined, undefined, Math.max(0, offset - 100))">上一页</el-button><span class="muted">第 {{Math.floor(offset/100)+1}} 页</span><el-button :disabled="loading || !more" @click="load(undefined, undefined, undefined, nextOffset)">下一页</el-button></div>
    <template #footer><el-button @click="emit('close')">取消</el-button><el-button v-if="!fileRoot" type="primary" :disabled="loading || path.at(-1)!.id === '0'" @click="emit('select', {id: path.at(-1)!.id, name: path.map(x => x.name).join(' / ')})">选择当前文件夹</el-button></template>
  </el-dialog>
</template>
