import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
const source = (path) => readFileSync(new URL(`../extension/${path}`, import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function page(t, policy) {
  const dom = new JSDOM(source('options/options.html'), {runScripts:'outside-only'});
  t.after(() => dom.window.close());
  const calls=[];
  let listener;
  dom.window.browser={runtime:{onMessage:{addListener(callback){listener=callback;}},sendMessage:async message=>{
    calls.push(message);
    if(message.kind==='ui.state') return {settings:{},jobs:[{jobId:'interrupted',status:'interrupted',url:'https://example.com',updatedAt:1}]};
    if(message.kind==='ui.historyPolicy') return policy;
    if(message.kind==='ui.configureHistory') return {...policy,retainFinishedHistory:message.enabled,historyMigrationPending:false};
    return {};
  }}};
  dom.window.eval(source('options/options.js')); await tick();
  return {dom,doc:dom.window.document,calls,emit: message => listener(message)};
}
test('history retention changes only after explicit opt-in',async t=>{
  const {doc,calls}=await page(t,{retainFinishedHistory:false,retainedHistoryCount:0,historyMigrationPending:false});
  assert.equal(calls.some(c=>c.kind==='ui.configureHistory'),false);
  const checkbox=doc.getElementById('retain-history');
  assert.equal(checkbox.checked,false);
  checkbox.click(); await tick();
  assert.deepEqual(calls.filter(c=>c.kind==='ui.configureHistory').map(c=>c.enabled),[true]);
});
test('disabling offers keep or remove before changing authoritative policy',async t=>{
  const {doc,calls}=await page(t,{retainFinishedHistory:true,retainedHistoryCount:2,historyMigrationPending:false});
  doc.getElementById('retain-history').click();await tick();
  assert.equal(calls.some(c=>c.kind==='ui.configureHistory'),false);
  assert.equal(doc.getElementById('existing-history-choice').hidden,false);
  doc.getElementById('history-remove-existing').click();await tick();
  const change=calls.find(c=>c.kind==='ui.configureHistory');
  assert.equal(change.enabled,false); assert.equal(change.existing,'remove');
});
test('migration keep choice preserves off default and interrupted jobs have dismiss action',async t=>{
  const {doc,calls}=await page(t,{retainFinishedHistory:false,retainedHistoryCount:1,historyMigrationPending:true});
  assert.equal(doc.getElementById('existing-history-choice').hidden,false);
  doc.getElementById('history-keep-existing').click(); await tick();
  const change=calls.find(c=>c.kind==='ui.configureHistory');
  assert.equal(change.enabled,false); assert.equal(change.existing,'keep');
  const dismiss=[...doc.querySelectorAll('#history button')].find(b=>b.textContent==='Dismiss');
  assert.ok(dismiss);dismiss.click();await tick();
  assert.equal(calls.find(c=>c.kind==='ui.dismiss').jobId,'interrupted');
});
test('clear recovery records explicitly includes interrupted jobs',async t=>{
  const {doc,calls}=await page(t,{retainFinishedHistory:false,retainedHistoryCount:0,historyMigrationPending:false});
  doc.getElementById('dismiss-interrupted').click();await tick();
  assert.equal(calls.find(c=>c.kind==='ui.clearFinished').includeInterrupted,true);
});

test('new retained completion refreshes count before disabling',async t=>{
  const policy={retainFinishedHistory:true,retainedHistoryCount:0,historyMigrationPending:false};
  const {doc,calls,emit}=await page(t,policy);
  policy.retainedHistoryCount=1;
  emit({kind:'host',message:{type:'job',job:{jobId:'done',status:'complete',url:'https://example.com',updatedAt:1}}});
  await tick();
  doc.getElementById('retain-history').click(); await tick();
  assert.equal(calls.filter(c=>c.kind==='ui.historyPolicy').length,2);
  assert.equal(calls.some(c=>c.kind==='ui.configureHistory'),false);
  assert.equal(doc.getElementById('existing-history-choice').hidden,false);
});
