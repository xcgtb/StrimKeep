import {test} from 'node:test'
import assert from 'node:assert/strict'
import {mergeSchedule,scheduleDraft,scheduleIdentity,syncBaselineWithWebhookToken} from '../src/workflowSettings.ts'
const current={scan_enabled:true,scan_interval_minutes:15,sync_enabled:true,sync_interval_minutes:30,sync_source_cids:['201'],directory_labels:{'201':'同步目录','301':'其他目录'},archive_refresh_hours:72}
test('saving organize timer preserves independent sync roots, labels, interval and archive cycle',()=>{
 const draft=scheduleDraft(current,'scan');draft.enabled=false;draft.minutes=20
 const result=mergeSchedule(current,'scan',draft)
 assert.equal(result.scan_enabled,false);assert.equal(result.scan_interval_minutes,20)
 assert.equal(result.sync_enabled,true);assert.equal(result.sync_interval_minutes,30)
 assert.deepEqual(result.sync_source_cids,['201']);assert.deepEqual(result.directory_labels,current.directory_labels);assert.equal(result.archive_refresh_hours,72)
 assert.equal(current.scan_enabled,true)
})
test('saving sync timer preserves organizer and unrelated directory labels from the freshest settings',()=>{
 const draft=scheduleDraft(current,'sync');draft.minutes=45;draft.source_cids=['202'];draft.directory_labels['202']='新定时目录'
 const latest={...current,scan_interval_minutes:90,directory_labels:{...current.directory_labels,'301':'已改名'}}
 const result=mergeSchedule(latest,'sync',draft)
 assert.equal(result.scan_interval_minutes,90);assert.equal(result.scan_enabled,true);assert.equal(result.sync_interval_minutes,45)
 assert.deepEqual(result.sync_source_cids,['202']);assert.equal(result.directory_labels['301'],'已改名');assert.equal(result.directory_labels['202'],'新定时目录')
})
test('timer drafts do not share mutable source arrays and scan dirty state ignores unrelated sync labels',()=>{
 const draft=scheduleDraft(current,'sync');draft.source_cids.push('202');draft.directory_labels['201']='本页修改'
 assert.deepEqual(current.sync_source_cids,['201']);assert.equal(current.directory_labels['201'],'同步目录')
 const scan=scheduleDraft(current,'scan'),saved=scheduleIdentity(scan,'scan');scan.directory_labels['999']='其他'
 assert.equal(scheduleIdentity(scan,'scan'),saved);scan.enabled=false;assert.notEqual(scheduleIdentity(scan,'scan'),saved)
})
test('saving a webhook token keeps unrelated draft edits unsaved',()=>{
 const saved=JSON.stringify({config:{webhook_token:'old',source_cids:['saved']},base:'http://saved'})
 const baseline=syncBaselineWithWebhookToken(saved,'new')
 assert.deepEqual(JSON.parse(baseline),{config:{webhook_token:'new',source_cids:['saved']},base:'http://saved'})
 assert.notEqual(baseline,JSON.stringify({config:{webhook_token:'new',source_cids:['edited']},base:'http://edited'}))
})
