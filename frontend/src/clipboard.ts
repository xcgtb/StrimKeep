/** Copy a complete string, including on HTTP pages without navigator.clipboard. */
export async function copyText(text:string, clipboard:Pick<Clipboard,'writeText'>|undefined, doc:Document):Promise<void> {
  if (!text) throw new Error('没有可复制的内容')
  if (clipboard?.writeText) {
    try { await clipboard.writeText(text); return } catch { /* Try the local fallback. */ }
  }
  const previous=doc.activeElement as HTMLElement|null
  const field=doc.createElement('textarea')
  field.value=text
  field.readOnly=true
  field.style.position='fixed'
  field.style.opacity='0'
  field.style.pointerEvents='none'
  doc.body.appendChild(field)
  try {
    field.focus()
    field.select()
    field.setSelectionRange(0,text.length)
    if (!doc.execCommand('copy')) throw new Error('复制失败，请手动选择完整地址复制')
  } finally {
    field.remove()
    previous?.focus?.()
  }
}
