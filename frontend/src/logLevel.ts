// Existing events have no structured severity; retain the historical keyword
// heuristic, but do not interpret a zero count as a failure or warning.
export function levelOf(row:{kind?:string,message?:unknown}):'error'|'warn'|'info'{
  if(row.kind==='error')return 'error'
  const text=String(row.message||'').replace(/(?:失败|异常|失效|超时|拒绝|中断|限流|跳过)\s*[:：]?\s*0(?![\d.])(?:\s*(?:个|条|项|次|份|部|集|文件|任务))?/g,'')
  if(/核对超时后只刷新命中的目标库/.test(text))return 'info'
  if(/失败|无法|异常|失效|超时|拒绝|中断/.test(text))return 'error'
  if(/限流|等待|跳过|未找到|稍后|不足|已停止|待处理/.test(text))return 'warn'
  return 'info'
}
