<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,watch,shallowRef} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {House,Cloudy,FolderOpened,Refresh,Document,Setting,Menu,Sunny,Moon,SwitchButton,List,Link,Download,Film,Promotion,MagicStick,Search,Share} from '@element-plus/icons-vue'
const route=useRoute(),router=useRouter()
const ready=ref(false),logged=ref(false),csrf=ref(''),username=ref(''),busy=ref(false),error=ref(''),mobile=ref(false),version=ref('—')
const draft=ref({username:'',password:''})
const connectionError=ref(false)
const menu=[
 {id:'overview',title:'媒体总览',icon:House,group:'media'},
 {id:'explore',title:'影视探索',icon:Film,group:'media'},
 {id:'inventory',title:'库存管理',icon:List,group:'media'},
 {id:'account',title:'115 网盘',icon:Cloudy,group:'media'},
 {id:'transfer',title:'资源入库',icon:Link,group:'media'},
 {id:'sharestrm',title:'分享 STRM',icon:Share,group:'media'},
 {id:'channels',title:'频道监控',icon:Promotion,group:'media'},
 {id:'organize',title:'影视整理',icon:FolderOpened,group:'media'},
 {id:'recognize',title:'识别测试',icon:MagicStick,group:'media'},
 {id:'sync',title:'STRM 管理',icon:Refresh,group:'media'},
 {id:'tasks',title:'任务中心',icon:List,group:'system'},
 {id:'logs',title:'实时日志',icon:Document,group:'system'},
 {id:'settings',title:'系统设置',icon:Setting,group:'system'},
] as const
const headerSearch=ref('')
function searchMedia(){const q=headerSearch.value.trim();if(!q)return;mobile.value=false;void router.push({name:'explore',query:{q}})}
const quickMenu=menu.filter(m=>['overview','explore','account','tasks'].includes(m.id))
const aliases:Record<string,string>={connections:'settings',system:'notifications',archive:'tasks'}
const page=computed(()=>String(route.name||'overview'))
const title=computed(()=>String(route.meta.title||'媒体总览'))
const pageProps=computed(()=>route.meta.tab?{tab:route.meta.tab}:{})
const appearance=ref({theme:'system',background_url:'',background_opacity:.12}),dark=ref(false)
const currentViewRef=shallowRef<any>()
const matcher=window.matchMedia('(prefers-color-scheme: dark)')
// 主题以本机为准，服务端 console_config 只负责背景图这类共享项。
// 之前刷新跳浅色，是因为这里先读了服务端配置、把本机选的深色盖掉了。
// 只有本机没有记录（首次进入）才跟随服务端；之后每次切换都会立刻回写，跨设备仍会收敛。
let storedAppearance=''
try{storedAppearance=localStorage.getItem('strimkeep-appearance')||''}catch{}
function paintTheme(theme:string){dark.value=theme==='dark'||(theme==='system'&&matcher.matches);document.documentElement.classList.toggle('dark',dark.value);document.querySelector('meta[name="theme-color"]')?.setAttribute('content',dark.value?'#192434':'#ffffff')}
function applyAppearance(value:any){appearance.value={...appearance.value,...value};paintTheme(appearance.value.theme);document.documentElement.style.setProperty('--app-wallpaper',appearance.value.background_url?`url(${JSON.stringify(appearance.value.background_url)})`:'none');document.documentElement.style.setProperty('--wallpaper-opacity',String(appearance.value.background_opacity??.12));try{localStorage.setItem('strimkeep-appearance',JSON.stringify(appearance.value))}catch{}}
try{applyAppearance(JSON.parse(storedAppearance||'{}'))}catch{}
function systemTheme(){if(appearance.value.theme==='system')applyAppearance(appearance.value)}
async function saveTheme(theme:string){try{const c=(await api('/console/config')).config;await api('/console/config','PUT',{...c,theme})}catch{}}
function toggleTheme(){const next=dark.value?'light':'dark';applyAppearance({...appearance.value,theme:next});void saveTheme(next)}
const pendingReads=new Map<string,Promise<any>>()
async function api(path:string,method='GET',body?:unknown):Promise<any>{
 if(method==='GET'&&pendingReads.has(path))return pendingReads.get(path)
 const request=(async()=>{const controller=method==='GET'&&/^\/organize\/jobs\/\d+$/.test(path)?new AbortController():null;const timer=controller?window.setTimeout(()=>controller.abort(),15000):null;try{const response=await fetch('/api/v1'+path,{method,credentials:'same-origin',signal:controller?.signal,headers:{'Content-Type':'application/json',...(csrf.value?{'X-CSRF-Token':csrf.value}:{})},body:body===undefined?undefined:JSON.stringify(body)});let data:any;try{data=await response.json()}catch{throw new Error('服务响应暂不可用，请稍后再试。')}if(!response.ok){if(response.status===401)logged.value=false;throw Object.assign(new Error(data.error?.message||'操作未完成'),{status:response.status})}return data}finally{if(timer!==null)window.clearTimeout(timer)}})()
 if(method==='GET')pendingReads.set(path,request)
 try{return await request}finally{if(pendingReads.get(path)===request)pendingReads.delete(path)}
}
watch(title,value=>{document.title=value+' · StrimKeep'},{immediate:true})
watch([dark,mobile],()=>{document.querySelector('meta[name="theme-color"]')?.setAttribute('content',dark.value?'#192434':'#ffffff');document.body.classList.toggle('menu-open',mobile.value)})
function closeMenu(event:KeyboardEvent){if(event.key==='Escape')mobile.value=false}
window.addEventListener('keydown',closeMenu)
onUnmounted(()=>{window.removeEventListener('keydown',closeMenu);document.body.classList.remove('menu-open')})
async function loadAppearance(){try{const c=(await api('/console/config')).config;const theme=storedAppearance?appearance.value.theme:(c.theme||'system');applyAppearance({...c,theme});if(storedAppearance&&c.theme!==theme)void saveTheme(theme)}catch{}}
async function login(){busy.value=true;error.value='';try{const data=await api('/auth/login','POST',draft.value);csrf.value=data.csrf;username.value=data.username;logged.value=true;draft.value.password='';await loadAppearance()}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
function go(target:string){
 const name=aliases[target]||target
 if(name===page.value){mobile.value=false;return}
 mobile.value=false
 void router.push({name}).catch(()=>{})
}
// 离开页面前征求当前页面同意（表单有未保存修改时），与 vue-router 的标准守卫一致。
router.beforeEach(async (to,from)=>{
 if(to.name===from.name)return true
 const view=currentViewRef.value
 if(view?.canLeave&&!(await view.canLeave()))return false
 return true
})
async function logout(){await api('/auth/logout','POST');logged.value=false;csrf.value='';currentViewRef.value=undefined;void router.replace({name:'overview'})}
async function restoreSession(){ready.value=false;connectionError.value=false;try{const data=await api('/auth/me');csrf.value=data.csrf;username.value=data.username;logged.value=true;await loadAppearance()}catch(e){if((e as {status?:number}).status===401)logged.value=false;else connectionError.value=true}finally{ready.value=true}}
onMounted(async()=>{matcher.addEventListener('change',systemTheme);try{version.value=(await fetch('/health/ready').then(r=>r.json())).version||'—'}catch{}
await restoreSession()})
onUnmounted(()=>{matcher.removeEventListener('change',systemTheme)})
</script>
<template>
<div v-if="!ready" class="loading">正在连接 StrimKeep…</div>
<main v-else-if="connectionError" class="auth-page"><section class="auth-card"><h1>连接暂不可用</h1><p>服务可能正在重启，请稍后重试。</p><el-button type="primary" @click="restoreSession">重试连接</el-button></section></main>
<main v-else-if="!logged" class="auth-page"><section class="auth-card"><div class="app-logo">S</div><h1>StrimKeep</h1><p class="muted">让资源归位，让观看更简单。</p><el-form label-position="top" @submit.prevent="login"><el-form-item label="管理员账号"><el-input v-model="draft.username" autocomplete="username" size="large"/></el-form-item><el-form-item label="密码"><el-input v-model="draft.password" type="password" show-password autocomplete="current-password" size="large"/></el-form-item><el-alert v-if="error" :title="error" type="error" :closable="false"/><el-button type="primary" native-type="submit" :loading="busy" size="large" class="full">登录</el-button></el-form><small class="muted">{{version}} · 响应式工作台</small></section></main>
<div v-else class="app-shell" :class="{'has-wallpaper':appearance.background_url}">
 <aside id="main-navigation" :class="['app-sidebar',{open:mobile}]"><div class="app-brand"><div class="app-logo">S</div><div><strong>StrimKeep</strong><small>私人媒体工作台</small></div><button class="sidebar-close icon-button" aria-label="关闭导航" @click="mobile=false">×</button></div>
  <nav aria-label="主导航"><template v-for="group in ['media','system']" :key="group"><p class="nav-group-heading">{{group==='media'?'媒体管理':'运行与系统'}}</p><button v-for="m in menu.filter(m=>m.group===group)" :key="m.id" :class="{active:page===m.id}" :aria-current="page===m.id?'page':undefined" @click="go(m.id)"><el-icon><component :is="m.icon"/></el-icon><span>{{m.title}}</span></button></template></nav>
  <div class="sidebar-info"><span class="status-light"/>服务运行状态以总览为准<small>版本 {{version}}</small></div>
 </aside><div v-if="mobile" class="app-scrim" @click="mobile=false"/>
 <main class="app-main"><header class="app-header"><div class="header-leading"><button class="icon-button mobile-menu" aria-label="打开导航" aria-controls="main-navigation" :aria-expanded="mobile" @click="mobile=true"><el-icon><Menu/></el-icon></button><span class="desktop-crumb">工作空间</span><span class="muted desktop-crumb">/</span><strong class="desktop-crumb">{{title}}</strong><div class="mobile-brand"><span class="mini-logo">S</span><span><b>StrimKeep</b><small>私人媒体工作台</small></span></div></div><div class="header-actions"><form class="header-search" @submit.prevent="searchMedia"><el-icon><Search/></el-icon><input v-model="headerSearch" aria-label="搜索电影或剧集" placeholder="搜索电影、剧集或关键词…"/></form><button class="icon-button" :aria-label="dark?'切换浅色':'切换深色'" @click="toggleTheme()"><el-icon><Sunny v-if="dark"/><Moon v-else/></el-icon></button><span class="user-name">{{username}}</span><el-button text :icon="SwitchButton" @click="logout">退出</el-button></div></header>
  <div class="app-content"><section class="cached-workspace" :data-page="page"><RouterView v-slot="{Component}"><KeepAlive :max="12"><component :is="Component" :key="route.name" v-bind="pageProps" ref="currentViewRef" :api="api" @navigate="go" @appearance="applyAppearance"/></KeepAlive></RouterView></section></div>
 </main>
 <nav class="mobile-bottom-nav" aria-label="常用导航"><button v-for="m in quickMenu" :key="m.id" :class="{active:page===m.id}" :aria-current="page===m.id?'page':undefined" @click="go(m.id)"><el-icon><component :is="m.icon"/></el-icon><span>{{m.title}}</span></button><button aria-label="打开完整导航" aria-controls="main-navigation" :aria-expanded="mobile" @click="mobile=true"><el-icon><Menu/></el-icon><span>导航</span></button></nav>
</div>
</template>
