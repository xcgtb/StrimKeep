export const VIDEO_PRESETS=['.mkv','.mp4','.m4v','.mov','.avi','.ts','.m2ts','.webm','.wmv','.rmvb','.flv','.iso']
export const SUBTITLE_PRESETS=['.srt','.ass','.ssa','.vtt','.sub','.idx','.sup']
export const METADATA_PRESETS=['.nfo','.jpg','.jpeg','.png','.webp','.svg','.ass','.srt','.sup','.ssa','.sub','.idx','.vtt','.ttml','.smi','.sbv','.ksc','.lrc','.bif','.mp3','.flac','.wav','.aac','.ape','.wma','.dff','.mka']

type Options={maxItems:number; excluded?:string[]; allowed?:string[]; forbidden?:string[]}
export function addExtensions(raw:string, existing:string[], options:Options):string[] {
  const parts=raw.split(/[\s,，;；]+/).filter(Boolean)
  const result=[...existing], seen=new Set(existing.map(v=>v.toLowerCase()))
  for(const part of parts){
    const name=part.toLowerCase().replace(/^\.+/,'')
    if(!/^[a-z0-9]{1,10}$/.test(name)) throw new Error(`“${part}”格式无效：请输入 1–10 位字母或数字`)
    const value='.'+name
    if(options.allowed&&!options.allowed.includes(value)) throw new Error(`不支持字幕格式 ${value}`)
    if(options.forbidden?.includes(value)) throw new Error(`不能添加 ${value}`)
    if(options.excluded?.some(v=>v.toLowerCase()===value)) throw new Error(`${value} 已用于另一类文件，请先从另一类移除`)
    if(!seen.has(value)){result.push(value);seen.add(value)}
  }
  if(result.length>options.maxItems) throw new Error(`最多添加 ${options.maxItems} 个扩展名`)
  return result
}
