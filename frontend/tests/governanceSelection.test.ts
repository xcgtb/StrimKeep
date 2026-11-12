import test from 'node:test'
import assert from 'node:assert/strict'
import {actionKey, bulkConflictKeys, oneClickCandidates, selectionConflict} from '../src/governanceSelection.ts'

const row = {actionType:'single', kind:'tv', tmdb_id:42, season:1, episode:null, reason:'episode_gap', deleteSide:'cloud'}
test('opposite gap choices for the same season are mutually exclusive', () => {
  assert.match(selectionConflict({...row,deleteSide:'share'}, [row]), /同季已选择网盘库/)
  assert.equal(selectionConflict({...row,season:2,deleteSide:'share'}, [row]), '')
  assert.equal(selectionConflict(row, [row]), '')
})
test('bulk selection leaves both opposing choices for the user to review', () => {
  const other = {...row,deleteSide:'share'}
  const independent = {...row,season:2}
  const blocked = bulkConflictKeys([row, other, independent])
  assert.ok(blocked.has(actionKey(row)))
  assert.ok(blocked.has(actionKey(other)))
  assert.ok(!blocked.has(actionKey(independent)))
})
test('a full gap cleanup overlaps other single-library actions in that season', () => {
  assert.match(selectionConflict({...row,reason:'quality',episode:3}, [row]), /重叠/)
  assert.equal(selectionConflict({...row,reason:'quality',episode:3,deleteSide:'share'}, [row]), '')
  assert.notEqual(actionKey(row), actionKey({...row,deleteSide:'share'}))
})
test('one click excludes both conflicting sides and respects cleanup switches', () => {
  const other = {...row, deleteSide:'share'}
  const independent = {...row,season:2}
  const dual = {actionType:'dual',kind:'movie',tmdb_id:9,season:null,deleteSide:'share'}
  assert.deepEqual(oneClickCandidates([row,other,independent,dual],true,true), [independent,dual])
  assert.deepEqual(oneClickCandidates([independent,dual],true,false), [dual])
})
test('explicit S00 cleanup may include both libraries', () => {
  const a = {actionType:'dual',kind:'tv',tmdb_id:42,season:0,deleteSide:'cloud',action_reason:'special_delete_local'}
  const b = {...a,deleteSide:'share',action_reason:'special_delete_share'}
  assert.equal(selectionConflict(a,[b]), '')
  assert.deepEqual(oneClickCandidates([a,b],true,true), [a,b])
  assert.notEqual(selectionConflict({...a,season:1}, [{...b,season:1}]), '')
})
