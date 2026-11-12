export type QualityRule = {key:string;enabled:boolean;preferences:string[];platforms:string[];descending:boolean;order:string[]}
export const numericRules = ['resolution','fps','bit_depth','size','revision']
export function moveEntry<T>(items:T[],index:number,delta:number) {
  const target=index+delta
  if(index<0 || index>=items.length || target<0 || target>=items.length) return
  const [item]=items.splice(index,1);items.splice(target,0,item!)
}
export function setOrder(rule:QualityRule,values:string[]) {rule.order=[...values];rule.preferences=[];rule.platforms=[]}
export function orderedValues(rule:QualityRule) {return rule.order.length ? rule.order : rule.preferences}
export function usesLegacySource(rule:QualityRule) {return rule.key==='source' && !rule.order.length && rule.platforms.length>0}
export function serializePolicy(config:any) {
  // 共存维度只剩「杜比 / 非杜比」：分辨率 / 帧率 / 三档动态范围不再上传，后端也不再读它们。
  return {mode:config.mode,dynamic_groups:config.dynamic_groups==='dovi'?'dovi':'off',deduplicate_identical:true,season_layout:'standard',
    rules:config.rules.map((r:QualityRule)=>({key:r.key,enabled:r.enabled,preferences:[...r.preferences],platforms:[...r.platforms],descending:r.descending,order:[...r.order]}))}
}
