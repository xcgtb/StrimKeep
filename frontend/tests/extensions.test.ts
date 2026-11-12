import {test} from 'node:test'
import {strict as assert} from 'node:assert'
import {addExtensions} from '../src/extensions.ts'

test('bulk input normalizes formats and removes duplicates',()=>{
  const current=['.mkv']
  assert.deepEqual(addExtensions('MP4，.avi; mkv  mp4',current,{maxItems:4}),['.mkv','.mp4','.avi'])
  assert.deepEqual(current,['.mkv'])
})
test('invalid or conflicting batches do not modify the existing list',()=>{
  const current=['.mkv']
  assert.throws(()=>addExtensions('mp4, bad-name',current,{maxItems:4}),/格式无效/)
  assert.throws(()=>addExtensions('mp4, srt',current,{maxItems:4,excluded:['.srt']}),/另一类/)
  assert.deepEqual(current,['.mkv'])
})
test('enforces supported subtitles, forbidden formats and limits',()=>{
  assert.throws(()=>addExtensions('srt, foo',[],{maxItems:20,allowed:['.srt']}),/不支持/)
  assert.throws(()=>addExtensions('strm',[],{maxItems:60,forbidden:['.strm']}),/不能添加/)
  assert.throws(()=>addExtensions('mp4 avi',['.mkv'],{maxItems:2}),/最多/)
})
