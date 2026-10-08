import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
const source = (path) => readFileSync(new URL(`../extension/${path}`, import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
test('diagnostic save is unavailable until user previews and deletion clears preview', async () => {
  const dom = new JSDOM(source('options/options.html'), {runScripts:'outside-only'});
  const calls = [];
  dom.window.browser = {runtime:{onMessage:{addListener(){}}, sendMessage:async message => {
    calls.push(message);
    if (message.kind === 'ui.state') return {settings:{},jobs:[]};
    if (message.kind === 'ui.diagnosticsPreview') return {previewId:'reviewed',text:'{"code":"operation_failed"}\n'};
    return {captureActive:false,legacyLogsPresent:true};
  }}};
  dom.window.eval(source('options/options.js'));
  await tick();
  const doc = dom.window.document;
  assert.equal(doc.getElementById('diagnostics-save').disabled,true);
  doc.getElementById('diagnostics-preview').click(); await tick();
  assert.match(doc.getElementById('diagnostics-content').textContent,/operation_failed/);
  doc.getElementById('diagnostics-save').click(); await tick();
  assert.equal(calls.find(call => call.kind === 'ui.diagnosticsSave').previewId,'reviewed');
  doc.getElementById('diagnostics-delete').click(); await tick();
  assert.equal(doc.getElementById('diagnostics-content').textContent,'');
  assert.equal(doc.getElementById('diagnostics-save').disabled,true);
  dom.window.close();
});
