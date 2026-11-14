<script setup lang="ts">
import {ref} from 'vue'
const props=defineProps<{api:(path:string,method?:string,body?:unknown)=>Promise<any>}>()
const emit=defineEmits<{select:[value:{id:string;name:string}];close:[]}>()
const selected=ref<{id:string;name:string}|null>(null)

function pathOf(node:any){
  const names:string[]=[]
  let cur=node
  while(cur&&cur.data){ if(cur.data.name) names.unshift(cur.data.name); cur=cur.parent }
  return names.join(' / ')
}
async function loadNode(node:any,resolve:(rows:any[])=>void){
  const cid=node.level===0?'0':node.data.id
  try{
    const data=await props.api(`/cloud/files?cid=${encodeURIComponent(cid)}&offset=0&limit=200`)
    resolve((data.items||[]).filter((x:any)=>x.is_dir).map((x:any)=>({id:x.id,name:x.name,leaf:false})))
  }catch{resolve([])}
}
function pick(data:any,node:any){
  // el-tree 的 node-click 签名是 (data, node, component, event)：
  // 只接第一个参数会拿到 data，node.data 永远是 undefined，按钮一直是禁用的。
  const id=data?.id||node?.data?.id
  if(!id)return
  selected.value={id,name:pathOf(node)||data?.name||''}
}
</script>
<template>
<el-dialog :model-value="true" title="选择 115 文件夹" width="620px" append-to-body @close="emit('close')">
  <p class="hint" style="margin-top:0">展开目录树，点中一个文件夹再确认；可以选任意目录，包括不在待整理里的。</p>
  <el-tree lazy :load="loadNode" node-key="id" :props="{label:'name',isLeaf:'leaf'}" highlight-current
           @node-click="pick" class="tree-box"/>
  <p class="hint" v-if="selected">已选：{{selected.name}}</p>
  <template #footer>
    <el-button @click="emit('close')">取消</el-button>
    <el-button type="primary" :disabled="!selected" @click="selected&&emit('select',selected)">选择这个文件夹</el-button>
  </template>
</el-dialog>
</template>
<style scoped>
.tree-box{max-height:52vh;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--surface)}
.tree-box :deep(.el-tree-node__content){height:auto;min-height:36px;align-items:flex-start;padding-block:5px}.tree-box :deep(.el-tree-node__label){white-space:normal;overflow-wrap:anywhere;line-height:1.55}.tree-box :deep(.el-tree-node__expand-icon){margin-top:2px}.hint{overflow-wrap:anywhere}
</style>
