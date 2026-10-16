<script setup lang="ts">
import { ref } from 'vue';
import ModalDialog from './ModalDialog.vue';
defineProps<{
  title: string;
  description: string;
  busy?: boolean;
  phrase?: string;
  danger?: boolean;
  disabled?: boolean;
}>();
defineEmits<{ close: []; confirm: [] }>();
const checked = ref(false),
  input = ref('');
</script>
<template>
  <ModalDialog :title="title" @close="!busy && $emit('close')"
    ><p class="confirm-description">{{ description }}</p>
    <slot /><label class="checkbox-label confirm-checkbox"
      ><input
        v-model="checked"
        type="checkbox"
        :disabled="busy"
      />我已核对上面的操作范围和影响</label
    ><label v-if="phrase"
      >输入“{{ phrase }}”确认<input v-model="input" autocomplete="off" :disabled="busy" /></label
    ><template #footer
      ><button class="button" :disabled="busy" @click="$emit('close')">返回核对</button
      ><button
        class="button"
        :class="danger ? 'danger' : 'primary'"
        :disabled="busy || disabled || !checked || (!!phrase && input !== phrase)"
        @click="$emit('confirm')"
      >
        {{ busy ? '正在提交…' : '确认执行' }}
      </button></template
    ></ModalDialog
  >
</template>
