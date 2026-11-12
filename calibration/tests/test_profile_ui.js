// Run the real profile page script with explicit DOM/API unit-test substitutes.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const {test}=require('node:test');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../../static/profile.html'),'utf8');
const code=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).filter(s=>s.includes('function collectForm'))[0];
function setup(fetcher){
 const elements=new Map();const alerts=[];
 function get(id){if(!elements.has(id))elements.set(id,{id,value:'',checked:false,disabled:false,style:{},innerHTML:'',textContent:'',listeners:{},addEventListener(type,fn){this.listeners[type]=fn;},appendChild(){}});return elements.get(id);}
 const calls=[];const context=vm.createContext({document:{getElementById:get,createElement:()=>({style:{}})},
  apiFetch:async(url,options)=>{calls.push({url,options});return fetcher(url,options)},alert:m=>alerts.push(m),escapeHtml:s=>String(s??''),console});
 vm.runInContext(code,context);vm.runInContext('loadRanges=async()=>{}',context);
 return {get,context,calls,alerts,run:s=>vm.runInContext(s,context)};
}
const ok=p=>({ok:true,json:async()=>({status:'success',profile:p})});
test('collectForm preserves legitimate zero km and years',()=>{
 const t=setup(()=>ok({}));t.get('weeklyKm').value='0';t.get('yearsRunning').value='0';
 const p=t.run('collectForm()');assert.equal(p.weekly_km,0);assert.equal(p.years_running,0);assert.equal(p.height_cm,null);
});
test('blank and invalid numeric strings never become zero or partial numbers',()=>{
 const t=setup(()=>ok({}));for(const value of ['', ' ', 'Infinity', 'NaN', '12abc']){t.get('heightCm').value=value;assert.equal(t.run("nullableNumber('heightCm')"),null)}
});
test('loadProfile retains zero values and saved supplementary fields',async()=>{
 const t=setup(()=>ok({weekly_km:0,years_running:0,leg_length_cm:85,notes:'测试备注'}));await t.run('loadProfile()');
 assert.equal(t.get('weeklyKm').value,0);assert.equal(t.get('yearsRunning').value,0);assert.equal(t.get('legLengthCm').value,85);assert.equal(t.get('notes').value,'测试备注');
});
test('HTTP save error always restores button',async()=>{
 const t=setup(()=>({ok:false,json:async()=>({error:'测试拒绝'})}));const b=t.get('saveProfileBtn');await b.listeners.click.call(b);
 assert.equal(b.disabled,false);assert.match(b.innerHTML,/保存画像/);assert.deepEqual(t.alerts,['测试拒绝']);
});
test('network save error always restores button',async()=>{
 const t=setup(()=>{throw new Error('test offline')});const b=t.get('saveProfileBtn');await b.listeners.click.call(b);assert.equal(b.disabled,false);assert.equal(t.alerts.length,1);
});
test('successful save posts actual zero payload and leaves button reusable',async()=>{
 const t=setup(()=>ok({weekly_km:0,years_running:0}));t.get('weeklyKm').value='0';t.get('yearsRunning').value='0';const b=t.get('saveProfileBtn');await b.listeners.click.call(b);
 const payload=JSON.parse(t.calls[0].options.body);assert.equal(payload.weekly_km,0);assert.equal(payload.years_running,0);assert.equal(b.disabled,false);assert.equal(t.get('profileStatus').textContent,'已保存');
});
