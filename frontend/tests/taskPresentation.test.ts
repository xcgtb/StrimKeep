import test from 'node:test'
import assert from 'node:assert/strict'
import {taskPresentation} from '../src/taskPresentation.ts'
test('skipped duplicates are described as kept instead of missing from the library',()=>{
  const job={kind:'organize',state:'completed',counts:{done:2,skipped:18},total:20,filtered_items:18,scan_filter:{excluded:0}}
  const notes=taskPresentation(job).notes.join('|')
  assert.match(notes,/按洗版规则保留已有/)
  assert.doesNotMatch(notes,/未入库/)
})
test('a partial transfer is not described as completed',()=>{
  const job={kind:'transfer',state:'partial',counts:{done:1},total:2}
  assert.equal(taskPresentation(job).title,'转存有项目需处理')
})
