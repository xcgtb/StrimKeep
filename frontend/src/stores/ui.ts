import { defineStore } from 'pinia';
import { ref } from 'vue';
export const useUi = defineStore('ui', () => {
  const theme = ref(localStorage.getItem('strimkeep-vue-theme') === 'dark' ? 'dark' : 'light');
  const notice = ref(''),
    noticeKind = ref('info');
  const appearance = ref<Record<string, string>>({});
  function setAppearance(value: Record<string, string>) {
    appearance.value = {
      background_light_url: value.background_light_url || '',
      background_dark_url: value.background_dark_url || '',
    };
    applyTheme();
  }
  let timer: ReturnType<typeof setTimeout>;
  function applyTheme() {
    document.documentElement.dataset.theme = theme.value;
    const value =
      appearance.value[theme.value === 'light' ? 'background_light_url' : 'background_dark_url'] ||
      '';
    try {
      const parsed = new URL(value);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password)
        throw new Error();
      document.documentElement.style.setProperty(
        '--user-background',
        `url(${JSON.stringify(parsed.href)})`,
      );
    } catch {
      document.documentElement.style.removeProperty('--user-background');
    }
  }
  function toggleTheme() {
    theme.value = theme.value === 'light' ? 'dark' : 'light';
    localStorage.setItem('strimkeep-vue-theme', theme.value);
    applyTheme();
  }
  function notify(message: string, kind = 'info') {
    notice.value = message;
    noticeKind.value = kind;
    clearTimeout(timer);
    timer = setTimeout(() => (notice.value = ''), 7000);
  }
  applyTheme();
  return { theme, notice, noticeKind, toggleTheme, notify, setAppearance };
});
