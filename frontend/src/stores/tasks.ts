import { defineStore } from 'pinia';
import { ref } from 'vue';
export interface AcceptedTask {
  id: string;
  kind: 'cloud' | 'task' | 'delete';
  path: string;
  at: number;
}
export const useTasks = defineStore('accepted-tasks', () => {
  const items = ref<AcceptedTask[]>([]);
  function accept(value: Omit<AcceptedTask, 'at'>) {
    items.value = [
      { ...value, at: Date.now() },
      ...items.value.filter((t) => t.id !== value.id),
    ].slice(0, 40);
  }
  return { items, accept };
});
