export function suggestedDirectory(item:{title:string;year:string;id:number}):string {
  if(!item.title||!/^\d{4}$/.test(item.year||''))return ''
  const title=item.title.normalize('NFC').replace(/[\x00-\x1f\x7f/\\:*?"<>|]/g,'_').replace(/^[ .]+|[ .]+$/g,'')
  let trimmed='';let bytes=0
  const encoder=new TextEncoder()
  for(const c of title){const size=encoder.encode(c).length;if(bytes+size>100)break;trimmed+=c;bytes+=size}
  trimmed=trimmed.replace(/[ .]+$/g,'')
  return trimmed?`${trimmed} (${item.year}) {tmdb-${item.id}}`:''
}
