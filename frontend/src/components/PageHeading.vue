<script setup lang="ts">
import ModuleTabs from './ModuleTabs.vue';
import { useRoute } from 'vue-router';
defineProps<{ title: string; subtitle: string; eyebrow?: string }>();
const route = useRoute();
</script>
<template>
  <section class="page-heading pageheading">
    <div>
      <span class="eyebrow">{{ eyebrow || route.meta.group }}</span>
      <h1>{{ title }}</h1>
      <p>{{ subtitle }}</p>
    </div>
    <div v-if="$slots.default" class="page-heading-actions actions"><slot /></div>
  </section>
  <ModuleTabs />
</template>
