import {createApp,h} from 'vue'
import {ElConfigProvider,ElLoading} from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/theme-chalk/dark/css-vars.css'
// 这三个是脚本里直接调用的（ElMessage/ElMessageBox/v-loading），
// 模板里用到的组件由 unplugin-vue-components 各自注入样式。
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/loading/style/css'
import './app.css'
import App from './App.vue'
import router from './router'
const app=createApp({render:()=>h(ElConfigProvider,{locale:zhCn},()=>h(App))})
app.directive('loading',ElLoading.directive)
app.use(router)
app.mount('#app')
