<script setup lang="ts">
import {Search,DocumentChecked} from '@element-plus/icons-vue'
import PageHeader from './PageHeader.vue'
const query=defineModel<string>('query',{default:''})
defineProps<{dirty:boolean;busy:boolean;ready:boolean;title:string;description?:string;saveLabel?:string}>()
const emit=defineEmits<{save:[]}>()
</script>
<template>
  <PageHeader :title="title" :description="description">
    <template #actions>
      <span class="save-state" role="status">{{ !ready ? '正在读取设置' : dirty ? '有未保存的修改' : '' }}</span>
      <el-button type="primary" :icon="DocumentChecked" :loading="busy" :disabled="!ready || !dirty"
        @click="emit('save')">{{ saveLabel || '保存设置' }}</el-button>
    </template>
  </PageHeader>
  <div class="workflow-search">
    <el-input v-model="query" clearable placeholder="搜索本页设置" aria-label="搜索本页设置" :prefix-icon="Search" />
  </div>
</template>

<style scoped>
.save-state { color: var(--muted); font-size: 12px; }
.save-state:empty { display: none; }
.workflow-search { width: min(100%, 420px); margin-bottom: 14px; }
.workflow-search :deep(.el-input__wrapper) { min-height: 40px; }
</style>
