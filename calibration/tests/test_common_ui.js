// Real common.js logout callback; DOM stub, no backend/model substitution claims.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname,'../../static/common.js'),'utf8');
const callback = source.match(/bind\('navLogoutBtn', 'click', (async function \(e\) \{[\s\S]*?\n    \})\);/);
assert.ok(callback,'actual logout callback must be present');
function harness(token,network) {
  const values = new Map([['poserun_last_result','PRIVATE RESULT'],['poserun_user','{"id":7}']]);
  if (token) values.set('poserun_token',token);
  const events=[];
  const ctx = vm.createContext({window:{},document:{readyState:'loading',addEventListener(){}},
    localStorage:{getItem:k=>values.get(k)||null,removeItem:k=>values.delete(k)},
    fetch:async(url,options)=>{events.push({url,options,cleared:!values.has('poserun_token')&&!values.has('poserun_last_result')});return network?network():{status:200};},
    location:{reload:()=>events.push({reload:true})},console});
  vm.runInContext(source,ctx);
  ctx.showAuthOverlay=()=>events.push({overlay:true});
  const run=vm.runInContext('('+callback[1]+')',ctx);
  return {values,events,run:()=>run({preventDefault(){}})};
}
test('logout captures bearer token before immediately clearing local private data',async()=>{
  const h=harness('TEST-ONLY-TOKEN');await h.run();
  const request=h.events.find(e=>e.url);
  assert.equal(request.url,'/api/auth/logout');
  assert.equal(request.options.method,'POST');
  assert.equal(request.options.headers.Authorization,'Bearer TEST-ONLY-TOKEN');
  assert.equal(request.cleared,true);
  assert.equal(h.values.size,0);
  assert.equal(h.events.at(-1).reload,true);
});
test('network failure still clears cached result and reloads to authentication',async()=>{
  const h=harness('TEST-ONLY-TOKEN',()=>{throw new Error('offline');});await h.run();
  assert.equal(h.values.size,0);assert.equal(h.events.at(-1).reload,true);
});
test('expired bearer response does not preserve a local session',async()=>{
  const h=harness('EXPIRED-TEST-TOKEN',()=>({status:401}));await h.run();
  assert.equal(h.values.size,0);assert.equal(h.events.at(-1).reload,true);
});
test('missing token is not serialized as Bearer null',async()=>{
  const h=harness(null);await h.run();
  assert.equal(Object.keys(h.events.find(e=>e.url).options.headers).length,0);
  assert.equal(h.values.size,0);
});
