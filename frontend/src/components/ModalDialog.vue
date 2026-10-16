<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { X } from '@lucide/vue';
defineProps<{ title: string }>();
const emit = defineEmits<{ close: [] }>(),
  panel = ref<HTMLElement>(),
  previous = document.activeElement as HTMLElement | null;
const previousOverflow = document.body.style.overflow;
function keys(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.stopPropagation();
    emit('close');
  }
  if (e.key !== 'Tab') return;
  const nodes = panel.value?.querySelectorAll<HTMLElement>(
    'button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
  );
  if (!nodes?.length) {
    e.preventDefault();
    return;
  }
  const first = nodes[0]!,
    last = nodes[nodes.length - 1]!;
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  }
  if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}
onMounted(async () => {
  document.body.style.overflow = 'hidden';
  await nextTick();
  panel.value?.focus();
});
onBeforeUnmount(() => {
  document.body.style.overflow = previousOverflow;
  previous?.focus();
});
</script>
<template>
  <Teleport to="body"
    ><div class="modal-backdrop" @click.self="$emit('close')">
      <section
        ref="panel"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
        class="modal-panel"
        @keydown="keys"
      >
        <header>
          <div>
            <span class="eyebrow">STRIMKEEP</span>
            <h2>{{ title }}</h2>
          </div>
          <button class="icon-button" aria-label="关闭详情" @click="$emit('close')">
            <X :size="22" />
          </button>
        </header>
        <div class="modal-content"><slot /></div>
        <footer v-if="$slots.footer"><slot name="footer" /></footer>
      </section></div
  ></Teleport>
</template>
