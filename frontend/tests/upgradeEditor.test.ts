import {test} from 'node:test'
import assert from 'node:assert/strict'
import {type QualityRule,moveEntry,setOrder,orderedValues,serializePolicy,usesLegacySource} from '../src/upgradeEditor.ts'
const rule=():QualityRule=>({key:'source',enabled:true,preferences:['web-dl','bluray'],platforms:['nf','amzn'],descending:true,order:[]})
test('moving a rule changes the serialized comparison order and preserves disabled status',()=>{
  const a=rule(),b={...rule(),key:'size',enabled:false,preferences:[],platforms:[]}
  const config={mode:'upgrade',dynamic_groups:'dovi',rules:[a,b]}
  moveEntry(config.rules,1,-1)
  assert.deepEqual(serializePolicy(config).rules.map(r=>[r.key,r.enabled]),[['size',false],['source',true]])
  moveEntry(config.rules,0,-1);assert.equal(config.rules[0],b)
})
test('legacy source settings round-trip without becoming a different policy',()=>{
  const a=rule();assert.equal(usesLegacySource(a),true)
  const copy=serializePolicy({mode:'upgrade',dynamic_groups:'off',rules:[a]})
  assert.deepEqual(copy.rules[0],a)
  moveEntry(a.platforms,1,-1);assert.deepEqual(copy.rules[0].platforms,['nf','amzn'])
})
test('explicitly selecting a new order removes legacy fields and keeps every position distinct',()=>{
  const a=rule(),options=['web-dl:nf','uhd-remux','web-dl:amzn']
  setOrder(a,options);moveEntry(orderedValues(a),2,-1)
  assert.deepEqual(a.order,['web-dl:nf','web-dl:amzn','uhd-remux'])
  assert.deepEqual(options,['web-dl:nf','uhd-remux','web-dl:amzn'])
  assert.deepEqual(a.preferences,[]);assert.deepEqual(a.platforms,[])
  assert.equal(usesLegacySource(a),false)
})
test('saving never includes removed coexistence controls or temporary editor state',()=>{
  const config={mode:'pending',dynamic_groups:'dovi',resolution_groups:true,fps_groups:true,season_layout:'spec',rules:[{...rule(),open:true,text:'not-a-policy'}]}
  const result=serializePolicy(config)
  assert.equal('resolution_groups' in result,false);assert.equal('fps_groups' in result,false);assert.equal(result.season_layout,'standard')
  assert.equal(result.deduplicate_identical,true)
  assert.equal('open' in result.rules[0]!,false);assert.equal('text' in result.rules[0]!,false)
})
