import {test} from 'node:test'
import {strict as assert} from 'node:assert'
import {suggestedDirectory} from '../src/tmdbLookup.ts'

test('suggests a safe work directory with the TMDB ID',()=>{
  assert.equal(suggestedDirectory({title:'征途',year:'2020',id:611395}),'征途 (2020) {tmdb-611395}')
  assert.equal(suggestedDirectory({title:'A: B / C',year:'2024',id:9}),'A_ B _ C (2024) {tmdb-9}')
  assert.equal(suggestedDirectory({title:'未知',year:'',id:1}),'')
})
test('limits UTF-8 title bytes without cutting a character',()=>{
  assert.equal(suggestedDirectory({title:'影'.repeat(40),year:'2020',id:1}),'影'.repeat(33)+' (2020) {tmdb-1}')
})
