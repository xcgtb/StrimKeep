import { createApp } from 'vue';
import App from './App.vue';
import { router } from './router';
// 1.4.7 视觉层：所有模块共用这套样式，网盘页面也用它的 .cw-* 类
import './styles/app.css';
import './styles/overview.css';
import './styles/glass.css';
import './styles/upstream.css';
import './styles/cloud-workbench.css';
import './styles/cloud-v2-layout.css';
import './styles/cloud-studio.css';

createApp(App).use(router).mount('#app');
