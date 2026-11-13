import test from 'node:test'
import assert from 'node:assert/strict'
import {inventoryCardStatus} from '../src/inventoryStatus.ts'

const tv = {kind:'tv',manual_complete:false}
const complete = {tmdb_complete:true,inventory_complete:true,combined_missing:0,seasons:[{expected:[1],unknown_air_date:[]}],series_status:'Ended'}

test('poster status distinguishes missing, ongoing, complete and manual completion', () => {
  assert.equal(inventoryCardStatus(tv, {data:complete}).text, '✓ 完整')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,series_status:'Returning Series'}}).text, '✓ 完整')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,combined_missing:3}}).text, '缺 3 集')
  assert.equal(inventoryCardStatus({...tv,manual_complete:true}, null).text, '手动完结')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,combined_extra:2}}).category, 'extra')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,series_status:'Returning Series',combined_missing:1}}).category, 'ongoing')
})

test('incomplete evidence never claims complete', () => {
  assert.equal(inventoryCardStatus(tv, {data:{...complete,tmdb_complete:false}}).text, '待对照')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,seasons:[]}}).text, '待对照')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,inventory_complete:false}}).text, '✓ 完整 · 库存待核实')
  // 未定档的集不算待核实（连载中），但一条已播都没有就只能是待对照。
  assert.equal(inventoryCardStatus(tv, {data:{...complete,seasons:[{unknown_air_date:[1]}]}}).text, '待对照')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,inventory_complete:false,combined_missing:1}}).text, '缺 1 集 · 库存待核实')
  assert.equal(inventoryCardStatus(tv, {data:{...complete,inventory_complete:false,combined_missing:1}}).category, 'unknown')
})

test('identity conflicts take priority over manual completion or movie status', () => {
  assert.equal(inventoryCardStatus({...tv,manual_complete:true,identity_conflict:true}, null).text, '身份冲突')
  assert.equal(inventoryCardStatus({kind:'movie',identity_conflict:true}, null).category, 'unknown')
})
