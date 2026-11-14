<script setup lang="ts">
import {computed,ref,watch,type Component} from 'vue'
import {ArrowDown,Setting} from '@element-plus/icons-vue'
const props=withDefaults(defineProps<{title:string;description?:string;summary?:string;icon?:Component;query?:string;keywords?:string;defaultOpen?:boolean}>(),{query:'',description:'',summary:'',keywords:'',defaultOpen:false})
const expanded=ref(props.defaultOpen)
const initialized=ref(props.defaultOpen)
const searchCollapsed=ref(false)
const matches=computed(()=>!props.query.trim()||[props.title,props.description,props.summary,props.keywords].join(' ').toLowerCase().includes(props.query.trim().toLowerCase()))
const open=computed(()=>props.query.trim()?!searchCollapsed.value:expanded.value)
watch(open,value=>{if(value)initialized.value=true},{immediate:true})
watch(()=>props.query,()=>{searchCollapsed.value=false})
function toggle(){if(props.query.trim())searchCollapsed.value=!searchCollapsed.value;else expanded.value=!expanded.value}
</script>
<template>
<section v-show="matches" class="setting-card" :class="{expanded:open}">
  <button class="setting-card-heading" :aria-expanded="open" @click="toggle">
    <span class="setting-card-icon"><el-icon><component :is="icon||Setting"/></el-icon></span>
    <span class="setting-card-copy"><strong>{{title}}</strong><span v-if="description">{{description}}</span><small v-if="summary">{{summary}}</small></span>
    <span class="setting-expand"><span>{{open?'收起':'展开'}}</span><el-icon :class="{rotated:open}"><ArrowDown/></el-icon></span>
  </button>
  <div v-if="initialized" v-show="open" class="setting-card-body"><slot/></div>
</section>
</template>
