<script setup lang="ts">
defineProps<{items:any[];loading?:boolean;emptyText?:string}>()
const emit=defineEmits<{open:[item:any]}>()
function size(bytes:number){if(!Number.isFinite(bytes))return '';if(bytes<1024)return bytes+' B';const units=['KB','MB','GB','TB'];let value=bytes/1024,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++}return value.toFixed(value<10?1:0)+' '+units[index]}
</script>
<template>
  <div class="cloud-entry-list" :aria-busy="!!loading">
    <p v-if="loading" class="cloud-list-status" :class="{overlay: items.length}" role="status">正在读取目录…</p>
    <p v-else-if="!items.length" class="cloud-list-status">{{emptyText||'此目录为空'}}</p>
    <template v-for="item in items" :key="item.id">
      <button v-if="item.is_dir" class="cloud-entry" type="button" :disabled="loading" @click="emit('open',item)">
        <svg class="cloud-entry-icon folder" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10H3Z"/></svg>
        <span class="cloud-entry-name">{{item.name}}</span>
        <svg class="cloud-entry-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>
      </button>
      <div v-else class="cloud-entry cloud-file-entry">
        <svg class="cloud-entry-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M14 3H5v18h14V8Z"/><path d="M14 3v5h5"/></svg>
        <span class="cloud-entry-name">{{item.name}}</span><span class="cloud-entry-size">{{size(item.size)}}</span>
      </div>
    </template>
  </div>
<p v-if="loading&&items.length" hidden></p>
</template>
<style scoped>
.cloud-entry-list{position:relative}
.cloud-list-status.overlay{position:absolute;top:-6px;right:0;z-index:2;margin:0;font-size:11px;color:var(--muted,#728096);background:var(--surface-soft,#f7f9fd);border:1px solid var(--line,#e8edf4);border-radius:6px;padding:2px 10px}
</style>
