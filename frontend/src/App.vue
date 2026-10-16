<script setup lang="ts">
import { useRoute } from 'vue-router';
import { useUi } from './stores/ui';
import AppShell from './layouts/AppShell.vue';
const route = useRoute(),
  ui = useUi();
</script>
<template>
  <RouterView v-if="route.path === '/login'" />
  <AppShell v-else />
  <div v-if="ui.notice" role="status" class="toast" :class="ui.noticeKind">
    <span>{{ ui.notice }}</span
    ><button class="icon-button" aria-label="关闭提示" @click="ui.notice = ''">×</button>
  </div>
</template>
