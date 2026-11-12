import {onMounted,onActivated,onDeactivated,onBeforeUnmount} from 'vue'
export function usePageActivity(start:()=>void,stop:()=>void){
 let active=false,running=false
 function update(){const next=active&&!document.hidden;if(next===running)return;running=next;if(next)start();else stop()}
 function activate(){if(active)return;active=true;document.addEventListener('visibilitychange',update);update()}
 function deactivate(){active=false;document.removeEventListener('visibilitychange',update);update()}
 onMounted(activate);onActivated(activate);onDeactivated(deactivate);onBeforeUnmount(deactivate)
}
export function useVisiblePolling(callback:()=>Promise<unknown>,milliseconds:number){
 let timer:ReturnType<typeof setInterval>|undefined,busy=false,last=Date.now()
 async function run(){if(busy)return;busy=true;last=Date.now();try{await callback()}catch{}finally{busy=false}}
 usePageActivity(()=>{if(Date.now()-last>=milliseconds)void run();timer=setInterval(run,milliseconds)},()=>{if(timer)clearInterval(timer);timer=undefined})
}
