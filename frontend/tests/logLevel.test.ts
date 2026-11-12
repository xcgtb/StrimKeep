import {test} from 'node:test'
import {strict as assert} from 'node:assert'
import {levelOf} from '../src/logLevel.ts'

test('historical zero counts are not failures or warnings',()=>{
  for(const message of ['批量探测预取完成，命中缓存 2 个、新探测 0 个、失败 0 个','处理完成：失败：0项，跳过 0 条','失败0'])
    assert.equal(levelOf({kind:'system',message}),'info')
})
test('actual failures remain visible even beside zero counts',()=>{
  for(const message of ['失败 1 个','失败 0.5','失败 10 个','失败 0 个，但请求超时','失败 0 个，操作失败'])
    assert.equal(levelOf({kind:'system',message}),'error')
  assert.equal(levelOf({kind:'error',message:'失败 0 个'}),'error')
})
test('zero failures do not hide other warnings',()=>{
  assert.equal(levelOf({kind:'system',message:'失败 0 个，等待重试'}),'warn')
  assert.equal(levelOf({kind:'system',message:'跳过 1 个'}),'warn')
})
