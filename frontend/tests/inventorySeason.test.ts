import test from 'node:test'
import assert from 'node:assert/strict'
import {librarySeasonStatus, ordinaryEpisodes} from '../src/inventorySeason.ts'

test('season status counts aired episode identities, including gaps and extras', () => {
  const season = {official_known:true, expected:[1,2,3], unknown_air_date:[],
                  cloud:{present:[1,3,4], missing:[2], extra:[4]}}
  assert.deepEqual(librarySeasonStatus(season, 'cloud', true),
    {label:'缺 1 集 · 超 1 集', tone:'missing', percent:67, airedPresent:2})
  assert.deepEqual(ordinaryEpisodes(season.cloud), [1,3])
})

test('missing inventory or official dates cannot claim complete', () => {
  const season = {official_known:true, expected:[1], unknown_air_date:[], cloud:{present:[1],missing:[],extra:[]}}
  assert.equal(librarySeasonStatus(season, 'cloud', true).label, '完整')
  assert.equal(librarySeasonStatus(season, 'cloud', false).label, '待核实')
  assert.equal(librarySeasonStatus({...season, unknown_air_date:[2]}, 'cloud', true).label, '待核实')
  assert.equal(librarySeasonStatus({...season, official_known:false}, 'cloud', true).percent, null)
  assert.equal(librarySeasonStatus({...season, expected:[]}, 'cloud', true).label, '待核实')
})
