<script setup lang="ts">
import {computed,ref} from 'vue'
import {addExtensions} from './extensions'
const model=defineModel<string[]>({required:true})
const props=withDefaults(defineProps<{label:string; presets:string[]; maxItems:number; disabled?:boolean; excluded?:string[]; allowed?:string[]; forbidden?:string[]}>(),{disabled:false,excluded:()=>[],forbidden:()=>[]})
const draft=ref(''), error=ref('')
const suggestions=computed(()=>props.presets.filter(v=>!model.value.some(x=>x.toLowerCase()===v)))
function add(value=draft.value, fromDraft=true){try{model.value=addExtensions(value,model.value,props);if(fromDraft)draft.value='';error.value=''}catch(e){error.value=(e as Error).message}}
function remove(value:string){model.value=model.value.filter(v=>v!==value);error.value=''}
function toggle(value:string){if(model.value.includes(value))remove(value);else add(value,false)}
function clearAll(){if(!model.value.length)return;model.value=[];error.value=''}
</script>
<template>
<section class="extension-editor" :aria-label="label">
  <div class="extension-heading"><strong>{{label}}</strong><span class="extension-count">{{model.length}} / {{maxItems}}<button v-if="model.length && !disabled" type="button" class="extension-clear" @click="clearAll">清空</button></span></div>
  <div class="extension-list"><span v-if="!model.length" class="extension-empty">尚未添加</span><span v-for="v in model" :key="v" class="extension-chip">{{v}}<button type="button" :disabled="disabled" :aria-label="`移除 ${v}`" @click="remove(v)">×</button></span></div>
  <template v-if="!disabled">
    <div class="extension-input"><el-input v-model="draft" :aria-label="`批量添加${label}`" placeholder="输入格式，例如 .xxx, yyy" @keyup.enter="add()"/><el-button type="primary" plain :disabled="!draft.trim()" @click="add()">添加</el-button></div>
    
    <p v-if="error" class="extension-error" role="alert">{{error}}</p>
    <div v-if="presets.length" class="extension-presets"><span>常用：</span><button v-for="v in presets" :key="v" type="button" :class="{on:model.includes(v)}" :disabled="disabled" :aria-pressed="model.includes(v)" @click="toggle(v)">{{model.includes(v)?'✓ ':'+ '}}{{v}}</button></div>
  </template>
</section>
</template>
<style scoped>
.extension-editor{padding:16px 0;border-bottom:1px solid var(--line);min-width:0}.extension-heading{display:flex;justify-content:space-between;gap:12px;align-items:center;font-size:13px}.extension-heading span,.extension-empty,.extension-help,.extension-presets>span{font-size:12px;color:var(--muted)}.extension-list{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.extension-chip{display:inline-flex;align-items:center;gap:5px;padding:4px 5px 4px 10px;border-radius:7px;background:var(--el-color-primary-light-9);color:var(--el-color-primary);font-size:13px;line-height:20px}.extension-chip button{border:0;background:transparent;color:inherit;font-size:18px;line-height:20px;width:24px;height:24px;cursor:pointer;border-radius:4px}.extension-chip button:hover{background:var(--el-color-primary-light-7)}.extension-chip button:disabled{opacity:.4;cursor:not-allowed}.extension-input{display:flex;gap:8px;max-width:560px}.extension-input :deep(.el-input){min-width:0}.extension-help{margin:7px 0;line-height:1.5}.extension-error{color:var(--el-color-danger);font-size:12px;margin:7px 0}.extension-presets{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:9px}.extension-presets button{border:1px solid var(--line);border-radius:6px;padding:4px 8px;background:transparent;color:var(--text);cursor:pointer;font-size:12px}.extension-presets button:hover{border-color:var(--el-color-primary);color:var(--el-color-primary)}.extension-presets button.on{border-color:var(--el-color-primary);color:var(--el-color-primary);background:var(--el-color-primary-light-9);font-weight:600}.extension-presets button:disabled{opacity:.5;cursor:not-allowed}.extension-count{display:inline-flex;align-items:center;gap:10px}.extension-clear{border:0;background:transparent;color:var(--el-color-danger);font-size:12px;cursor:pointer;padding:2px 4px;border-radius:4px}.extension-clear:hover{background:var(--el-color-danger-light-9)}@media(max-width:600px){.extension-editor{padding:14px 0}.extension-chip button{width:28px;height:28px}.extension-presets button{padding:7px 9px}}
</style>
