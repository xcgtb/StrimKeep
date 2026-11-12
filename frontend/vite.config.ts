import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// 组件按需引入：每个 .vue 只把自己用到的 Element Plus 组件打进所在分片。
// 之前全部在 main.ts 里 app.component() 注册，等于把它们都塞进入口 chunk，
// 连一次都没用到的 el-table / el-tree 也被打包。
export default defineConfig({
  plugins: [vue(), Components({ resolvers: [ElementPlusResolver({ importStyle: 'css' })], dts: false })],
  build: { rollupOptions: { output: { manualChunks: { vue: ['vue'] } } } },
  server: { proxy: { '/api': 'http://127.0.0.1:9000' } },
})
