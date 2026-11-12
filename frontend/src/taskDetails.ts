type Detail = {job:any;items:any[];[key:string]:any}

// Keep an in-flight refresh tied to the exact drawer that requested it.
export function mergeTaskDetail(current:Detail|null, requested:Detail, response:Detail):Detail|null {
  if(current!==requested)return current
  const updates=new Map((response.items||[]).map(item=>[item.id,item]))
  return {...current,job:response.job,items:current.items.map(item=>updates.get(item.id)||item)}
}
