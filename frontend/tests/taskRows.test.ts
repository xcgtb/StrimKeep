import test from 'node:test'
import assert from 'node:assert/strict'
import {reconcileRows} from '../src/taskRows.ts'
test('unchanged push preserves array and row identities, changed row replaces only itself',()=>{
 const previous=[{id:1,state:'running'},{id:2,state:'completed'}]
 assert.equal(reconcileRows(previous,JSON.parse(JSON.stringify(previous))),previous)
 const updated=reconcileRows(previous,[{id:1,state:'completed'},{id:2,state:'completed'}])
 assert.notEqual(updated[0],previous[0]);assert.equal(updated[1],previous[1]);assert.equal(previous[0].state,'running')
})
