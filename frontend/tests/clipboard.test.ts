import {test} from 'node:test'
import assert from 'node:assert/strict'
import {copyText} from '../src/clipboard.ts'

test('copies the complete address with Clipboard API',async()=>{
 const value='http://nas:9000/api/v1/emby/webhook?token=long-token_123'
 let copied=''
 await copyText(value,{writeText:async text=>{copied=text}},{} as Document)
 assert.equal(copied,value)
})

test('falls back to selecting the whole value when Clipboard API is unavailable',async()=>{
 const value='http://nas:9000/api/v1/emby/webhook?token=long-token_123'
 let selected:[number,number]|undefined,removed=false
 const field={value:'',readOnly:false,style:{},focus(){},select(){},setSelectionRange(a:number,b:number){selected=[a,b]},remove(){removed=true}}
 const doc={activeElement:null,createElement:()=>field,body:{appendChild(){}},execCommand:(command:string)=>command==='copy'} as unknown as Document
 await copyText(value,undefined,doc)
 assert.equal(field.value,value);assert.deepEqual(selected,[0,value.length]);assert.equal(removed,true)
})

test('reports failure and cleans up when fallback copy fails',async()=>{
 let removed=false
 const field={value:'',readOnly:false,style:{},focus(){},select(){},setSelectionRange(){},remove(){removed=true}}
 const doc={activeElement:null,createElement:()=>field,body:{appendChild(){}},execCommand:()=>false} as unknown as Document
 await assert.rejects(copyText('address',undefined,doc),/复制失败/)
 assert.equal(removed,true)
})
