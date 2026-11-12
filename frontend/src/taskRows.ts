export function reconcileRows(previous:any[],incoming:any[]){
 const old=new Map(previous.map(row=>[row.id,row]))
 const next=incoming.map(row=>{const saved=old.get(row.id);return saved&&JSON.stringify(saved)===JSON.stringify(row)?saved:row})
 return next.length===previous.length&&next.every((row,i)=>row===previous[i])?previous:next
}
