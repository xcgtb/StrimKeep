<script setup lang="ts">
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { matchesNavigation, moduleName, moduleNavigation } from '../config/navigation';
const route = useRoute();
const tabs = computed(() => moduleNavigation[moduleName(route.path, route.query.section)] || []);
</script>
<template>
  <nav v-if="tabs.length" class="module-navigation tabs" aria-label="模块功能">
    <RouterLink
      v-for="item in tabs"
      :key="item.to"
      :to="item.to"
      class="tab"
      :class="{
        current: matchesNavigation(item.to, route.path, route.query),
        active: matchesNavigation(item.to, route.path, route.query),
      }"
      >{{ item.name }}</RouterLink
    >
  </nav>
</template>
