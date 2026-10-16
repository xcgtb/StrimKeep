<script setup lang="ts">
defineProps<{ title: string; subtitle: string; eyebrow?: string }>();
</script>
<template>
  <section class="page-heading panel-heading">
    <div>
      <span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span>
      <h1>{{ title }}</h1>
      <p>{{ subtitle }}</p>
    </div>
    <slot />
  </section>
</template>
