<script setup lang="ts">
import { onMounted } from 'vue';
import { useCloud } from '../stores/cloud';
const cloud = useCloud();
onMounted(() => cloud.load());
</script>
<template>
  <div class="account-picker">
    <span class="account-badge" aria-hidden="true">115</span>
    <label
      >当前 115 账号<select v-model="cloud.id" :disabled="cloud.loading">
        <option v-for="a in cloud.accounts" :key="String(a.id)" :value="a.id">
          {{ a.name || a.uid || a.id }}
        </option>
      </select></label
    ><button class="button small" :disabled="cloud.loading" @click="cloud.load()">刷新账号</button
    ><RouterLink to="/settings/accounts" class="text-link">管理账号</RouterLink>
    <p v-if="cloud.error" class="inline-error" role="alert">{{ cloud.error }}</p>
    <p v-else-if="!cloud.loading && !cloud.accounts.length" class="muted-text">
      请先连接一个 115 账号。
    </p>
  </div>
</template>
