<script setup lang="ts">
import {Search,Check,DocumentChecked} from '@element-plus/icons-vue'
const query=defineModel<string>('query',{default:''})
defineProps<{dirty:boolean;busy:boolean;ready:boolean;title:string}>()
const emit=defineEmits<{save:[]}>()
</script>
<template><div class="workflow-toolbar"><div class="workflow-toolbar-top"><h1>{{title}}</h1><span class="workflow-save-state" :class="{dirty}" role="status"><el-icon><Check v-if="!dirty&&ready"/></el-icon>{{!ready?'正在读取设置':dirty?'有未保存的修改':'已保存'}}</span></div><div class="workflow-toolbar-actions"><el-input v-model="query" clearable placeholder="搜索本页设置…" aria-label="搜索本页设置" :prefix-icon="Search"/><el-button type="primary" :icon="DocumentChecked" :loading="busy" :disabled="!ready||!dirty" @click="emit('save')">保存修改</el-button></div></div></template>
