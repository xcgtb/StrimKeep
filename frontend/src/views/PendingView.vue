<script setup lang="ts">
defineProps<{ title: string }>();
</script>
<template>
  <section class="tab active">
    <div class="panel">
      <h2>{{ title }}</h2>
      <p class="list-empty">
        该模块正在按 1.4.7 的原有界面迁移为 Vue 3，功能与接口保持不变。当前版本优先完成了
        媒体总览、影视探索、片库映射、双库治理、目录清理、计划存档与日志中心。
      </p>
    </div>
  </section>
</template>
