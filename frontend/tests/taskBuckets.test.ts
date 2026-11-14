import {test} from 'node:test'
import assert from 'node:assert/strict'
import {taskBucket} from '../src/taskBuckets.ts'

test('task result tabs preserve incomplete and cancelled states',()=>{
  assert.equal(taskBucket({state:'running'}),'progress')
  assert.equal(taskBucket({state:'partial',counts:{done:2}}),'failed')
  assert.equal(taskBucket({state:'cancelled'}),'cancelled')
  assert.equal(taskBucket({state:'completed',unidentified:1}),'unidentified')
  assert.equal(taskBucket({state:'completed',filtered:2}),'filtered')
  assert.equal(taskBucket({state:'completed'}),'done')
})
