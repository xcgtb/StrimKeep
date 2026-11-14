<script setup lang="ts">
import {computed,ref} from 'vue'
import {addExtensions} from './extensions'
const model=defineModel<string[]>({required:true})
const props=withDefaults(defineProps<{label:string; presets:string[]; maxItems:number; disabled?:boolean; excluded?:string[]; allowed?:string[]; forbidden?:string[]}>(),{disabled:false,excluded:()=>[],forbidden:()=>[]})
const draft=ref(''), error=ref(''), adding=ref(false)
const choices=computed(()=>[...props.presets,...model.value.filter(v=>!props.presets.some(p=>p.toLowerCase()===v.toLowerCase()))])
const selected=(value:string)=>model.value.some(v=>v.toLowerCase()===value.toLowerCase())
function add(value=draft.value, fromDraft=true){try{model.value=addExtensions(value,model.value,props);if(fromDraft)draft.value='';error.value=''}catch(e){error.value=(e as Error).message}}
function remove(value:string){model.value=model.value.filter(v=>v!==value);error.value=''}
function toggle(value:string){if(selected(value))remove(model.value.find(v=>v.toLowerCase()===value.toLowerCase())!);else add(value,false)}
function clearAll(){if(!model.value.length)return;model.value=[];error.value=''}
</script>
<template>
<section class="extension-editor" :aria-label="label">
  <div class="extension-heading"><div><strong>{{label}}</strong><span class="extension-count">已选 {{model.length}} / {{maxItems}}</span></div><button v-if="!disabled" type="button" class="extension-more" :aria-expanded="adding" @click="adding=!adding">{{adding?'收起':'添加格式'}}</button></div>
  <p class="extension-help">{{disabled?'当前设置为只读。':'点击格式可选择或取消。'}}</p>
  <div class="extension-choices"><button v-for="v in choices" :key="v" type="button" :class="{on:selected(v)}" :disabled="disabled" :aria-pressed="selected(v)" @click="toggle(v)"><span aria-hidden="true">{{selected(v)?'✓':'+'}}</span>{{v}}</button></div>
  <div v-if="adding && !disabled" class="extension-extra"><div class="extension-input"><el-input v-model="draft" :aria-label="`批量添加${label}`" placeholder="输入格式，多个用逗号分隔" @keyup.enter="add()"/><el-button type="primary" plain :disabled="!draft.trim()" @click="add()">添加</el-button></div><button v-if="model.length" type="button" class="extension-clear" @click="clearAll">清空已选格式</button></div>
  <p v-if="error" class="extension-error" role="alert">{{error}}</p>
</section>
</template>
<style scoped>
.extension-editor{padding:12px 0;border-bottom:1px solid var(--line);min-width:0}.extension-heading{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:13px}.extension-heading>div{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}.extension-count,.extension-help{font-size:11px;color:var(--muted)}.extension-help{margin:5px 0 9px;line-height:1.4}.extension-choices{display:flex;flex-wrap:wrap;gap:6px}.extension-choices button{display:inline-flex;align-items:center;gap:5px;min-height:32px;padding:5px 9px;border:1px solid var(--line);border-radius:7px;background:var(--surface);color:var(--muted);cursor:pointer;font:12px ui-monospace,SFMono-Regular,Menlo,monospace}.extension-choices button.on{border-color:var(--el-color-primary);background:var(--el-color-primary-light-9);color:var(--el-color-primary);font-weight:600}.extension-choices button:focus-visible,.extension-more:focus-visible{outline:2px solid var(--el-color-primary);outline-offset:2px}.extension-choices button:disabled{cursor:not-allowed}.extension-more,.extension-clear{border:0;background:transparent;color:var(--el-color-primary);font-size:12px;cursor:pointer;padding:4px}.extension-extra{margin-top:10px}.extension-input{display:flex;gap:8px;max-width:560px}.extension-input :deep(.el-input){min-width:0}.extension-clear{margin-top:6px;color:var(--el-color-danger)}.extension-error{color:var(--el-color-danger);font-size:12px;margin:7px 0}@media(max-width:600px){.extension-editor{padding:10px 0}.extension-choices button{min-height:36px;padding:6px 10px}}
</style>
