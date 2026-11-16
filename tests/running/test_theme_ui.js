// Real theme script, DOM boundary double; no browser CSS claims.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../../static/themes/theme-system.js'),'utf8');
function harness(saved, token, remote='clarion', brokenStorage=false){
  const values=new Map(),nodes=[],calls=[];if(saved)values.set('running-management.theme',saved);
  class Element{
    constructor(){this.dataset={};this.options=[];this.events={};nodes.push(this);}
    add(option){this.options.push(option);} append(...children){this.children=children;}
    setAttribute(){} addEventListener(type,fn){this.events[type]=fn;}
  }
  const settings=new Element();settings.dataset.themeSelect='';
  const topbar=new Element(),document={documentElement:{dataset:{}},readyState:'complete',
    createElement:()=>new Element(),querySelector:()=>topbar,
    querySelectorAll:()=>nodes.filter(n=>'themeSelect' in n.dataset),dispatchEvent(){}};
  const context=vm.createContext({document,authToken:token,Option:function(text,value){this.text=text;this.value=value;},
    localStorage:{getItem:k=>{if(brokenStorage)throw Error('blocked');return values.get(k);},setItem:(k,v)=>{if(brokenStorage)throw Error('blocked');values.set(k,v);}},
    apiFetch:async(url,opts)=>{calls.push({url,opts});return {ok:true,json:async()=>({theme:remote})};},CustomEvent:function(){}});
  vm.runInContext(source,context);
  return {document,settings,values,calls,nodes};
}
const tick=()=>new Promise(r=>setImmediate(r));
test('default, persistent local preference and settings/topbar switch stay synchronized',async()=>{
  const h=harness(null,null);await tick();
  assert.equal(h.document.documentElement.dataset.theme,'clarion');
  assert.equal(h.settings.options.length,3);
  h.settings.value='nightlab';await h.settings.events.change();
  assert.equal(h.document.documentElement.dataset.theme,'nightlab');
  assert.equal(h.values.get('running-management.theme'),'nightlab');
  assert.ok(h.nodes.filter(n=>'themeSelect' in n.dataset).every(n=>n.value==='nightlab'));
  const restored=harness('nightlab',null);assert.equal(restored.document.documentElement.dataset.theme,'nightlab');
});
test('authenticated server preference wins over another users cached choice and PUT saves',async()=>{
  const h=harness('nightlab','SYNTHETIC-TOKEN','fieldnote');await tick();
  assert.equal(h.document.documentElement.dataset.theme,'fieldnote');
  h.settings.value='clarion';await h.settings.events.change();
  assert.equal(h.calls[1].opts.method,'PUT');
  assert.equal(JSON.parse(h.calls[1].opts.body).theme,'clarion');
});
test('invalid stored value and unavailable localStorage fail to clarion without crashing',async()=>{
  assert.equal(harness('invalid',null).document.documentElement.dataset.theme,'clarion');
  const h=harness(null,null,'clarion',true);await tick();
  h.settings.value='fieldnote';await h.settings.events.change();
  assert.equal(h.document.documentElement.dataset.theme,'fieldnote');
});
