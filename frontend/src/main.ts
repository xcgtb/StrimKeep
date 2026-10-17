import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import { router } from './router';
import { onUnauthorized, onAccepted } from './api/client';
import { useSession } from './stores/session';
import { useUi } from './stores/ui';
import './styles/main.css';
import './styles/template.css';
import './styles/classic-app.css';
import './styles/classic-overview.css';
import './styles/classic-glass.css';
import './styles/classic-upstream.css';
import './styles/aurora.css';
import { useTasks } from './stores/tasks';
const app = createApp(App);
app.use(createPinia());
onAccepted((value) => useTasks().accept(value));
onUnauthorized(() => {
  useSession().expire();
  if (router.currentRoute.value.path !== '/login')
    void router.replace({
      path: '/login',
      query: { redirect: router.currentRoute.value.fullPath },
    });
});
app.config.errorHandler = () => useUi().notify('页面出现异常，请刷新页面重试。', 'error');
app.use(router).mount('#app');
