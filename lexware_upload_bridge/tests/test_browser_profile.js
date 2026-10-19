'use strict';
// Offline UI regressions. The browser DOM, storage, clock and fetch are all local fakes.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
const html = fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const pendingKey = 'wer-lexware-pending-job';
const REQUEST_ID = 'a7c66ab7-874c-49d5-8f29-f369af0fc52f';

class Element {
  constructor(tag='div') {
    this.tagName=tag;this.textContent='';this.children=[];this.attributes={};this.listeners={};this.disabled=false;this.hidden=false;
    const classes=new Set();
    this.classList={toggle:(key,on)=>{if(on)classes.add(key);else classes.delete(key);},contains:key=>classes.has(key)};
  }
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
  setAttribute(key,value) {this.attributes[key]=String(value);}
  removeAttribute(key) {delete this.attributes[key];}
  addEventListener(key,handler) {this.listeners[key]=handler;}
}
function text(element) {return element.textContent+' '+element.children.map(text).join(' ');}
function all(element,tag) {return [element,...element.children.flatMap(child=>all(child,tag))].filter(child=>child.tagName===tag);}
function response(data,status=200) {return {ok:status>=200&&status<300,status,json:async()=>data};}
function deferred() {let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function job(status,id='job-one',extras={}) {return {id,status,scan_request_id:id==='job-one'?REQUEST_ID:null,created_at:'2026-10-11T12:00:00+00:00',...extras};}
function status(extras={}) {
  return {busy:false,scan_enabled:true,upload_enabled:true,scanner_configured:true,mcp_configured:true,csrf:'private-csrf',jobs:[],active_job:null,
    scan_profile:{resolution:1200,mode:'Color',format:'JPG',paper_size:'A4'},device_state:{state:'idle',label:'Bereit'},...extras};
}
async function settle() {for(let i=0;i<12;i++)await Promise.resolve();}
function harness(initial=status(),saved=null) {
  const elements=new Map();
  for(const id of ['scan','status','profile','progress','progress-label','progress-bar','step-scan','step-archive','step-upload','message','jobs']) {
    elements.set(id,new Element(id==='scan'?'button':'div'));
  }
  elements.get('scan').disabled=true;
  for(const [key,label] of [['scan','Scan'],['archive','Sichern'],['upload','Hochladen']])elements.get('step-'+key).textContent=label;
  const storage=new Map(saved===null?[]:[[pendingKey,JSON.stringify(saved)]]);
  const calls=[],timers=new Map();let timerId=0;
  const app={elements,calls,timers,storage,fresh:initial,hook:null};
  const context={document:{getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment')},
    sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    crypto:{randomUUID:()=>REQUEST_ID},AbortController,setTimeout:(callback,ms)=>{const id=++timerId;timers.set(id,{callback,ms});return id;},clearTimeout:id=>timers.delete(id),
    fetch:async(url,options={})=>{
      calls.push({url,options});
      if(app.hook){const handled=app.hook(url,options);if(handled!==undefined)return await handled;}
      if(url==='api/status')return response(app.fresh);
      if(url==='api/scan')return response({job:job('queued')},202);
      throw new Error('Unexpected URL '+url);
    }};
  vm.createContext(context);vm.runInContext(source,context);
  app.run=code=>vm.runInContext(code,context);
  app.scan=()=>app.run('scan()');app.refresh=()=>app.run('refresh()');
  app.button=elements.get('scan');app.read=()=>app.run('({connected,sending,pending:submission&&{id:submission.id,uncertain:submission.uncertain}})');
  return app;
}


test('one main button, no checkbox, local scan, browser scanner profile or history upload',()=>{
  assert.equal((html.match(/<button\b/g)||[]).length,1);
  assert.match(html,/Scannen und Hochladen/);
  assert.doesNotMatch(html,/<input\b|checkbox|id="business"|id="local"/);
  assert.doesNotMatch(source,/localStorage|readScannerProfile|confirm_business|scanner_settings|confirm\(/);
  assert.doesNotMatch(source,/\/upload['"]|setInterval/);
  assert.match(html,/role="progressbar"/);assert.match(html,/aria-live="polite"/);
});

test('both server gates and a healthy status response are required',async()=>{
  for(const flags of [{scan_enabled:false},{upload_enabled:false},{scan_enabled:false,upload_enabled:false}]) {
    const app=harness(status(flags));await settle();assert.equal(app.button.disabled,true);
    await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,0);
  }
  const app=harness();await settle();assert.equal(app.button.disabled,false);
  assert.equal(app.elements.get('status').textContent,'Bereit');
  assert.equal(app.elements.get('profile').textContent,'1200 dpi · Farbe · JPG · A4');
});

test('explicit click sends only empty JSON with CSRF and cannot double-click',async()=>{
  const app=harness();await settle();const pending=deferred();
  app.hook=url=>url==='api/scan'?pending.promise:undefined;
  const first=app.scan();await settle();await app.scan();
  const posts=app.calls.filter(call=>call.url==='api/scan');
  assert.equal(posts.length,1);assert.equal(posts[0].options.body,'{}');
  assert.equal(posts[0].options.headers['X-CSRF-Token'],'private-csrf');
  assert.equal(posts[0].options.headers['Content-Type'],'application/json');
  assert.equal(posts[0].options.headers['X-Scan-Request-ID'],REQUEST_ID);
  assert.equal(app.button.disabled,true);
  app.fresh=status({busy:true,jobs:[job('scanning')],active_job:job('scanning')});
  pending.resolve(response({job:job('queued')},202));await first;
  assert.equal(app.button.disabled,true);
});

test('lock survives all scan-save-upload phases and accepted session cleanup',async()=>{
  const app=harness();await settle();
  app.fresh=status({busy:true,jobs:[job('scanning')],active_job:job('scanning')});
  await app.scan();
  for(const [phase,stage,completed,label] of [
    ['scanning','scan',0,'Scan läuft'],['receiving','archive',1,'Wird gesichert'],
    ['preparing','archive',1,'Wird gesichert'],['ready','upload',2,'Upload wird vorbereitet'],
    ['preflight','upload',2,'Upload wird vorbereitet'],['uploading','upload',2,'Upload läuft'],
    ['uploaded','complete',3,'Lexware hat den Upload angenommen'],
  ]) {
    app.fresh=status({busy:true,jobs:[job(phase)],active_job:job(phase),progress:{stage,completed_steps:completed}});
    await app.refresh();assert.equal(app.button.disabled,true,phase);
    assert.equal(app.elements.get('status').textContent,label);
    assert.equal(app.elements.get('progress-bar').attributes['aria-valuenow'],undefined);
    await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,1);
  }
  app.fresh=status({jobs:[job('uploaded')]});await app.refresh();
  assert.equal(app.button.disabled,false);
  assert.equal(app.elements.get('progress-bar').classList.contains('complete'),true);
  assert.equal(app.elements.get('message').textContent,'Lexware hat den Upload angenommen');
  assert.equal(app.storage.has(pendingKey),false);
});

test('failed refresh disables the button and restores it only after status returns',async()=>{
  const app=harness();await settle();
  app.hook=url=>url==='api/status'?Promise.reject(new Error('offline')):undefined;
  await app.refresh();assert.equal(app.button.disabled,true);
  assert.equal(app.elements.get('status').textContent,'Verbindung unterbrochen');
  await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,0);
  app.hook=null;await app.refresh();assert.equal(app.button.disabled,false);
  assert.equal(app.elements.get('message').textContent,'');
});

test('an unknown POST result never unlocks just because a later status is idle',async()=>{
  const app=harness(status({jobs:[job('uploaded','older')]}));await settle();
  app.hook=url=>url==='api/scan'?Promise.reject(new Error('request timeout')):undefined;
  await app.scan();assert.equal(app.button.disabled,true);assert.equal(app.read().pending.uncertain,true);
  await app.refresh();assert.equal(app.button.disabled,true);
  await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,1);
  app.fresh=status({busy:true,jobs:[job('uploading'),job('uploaded','older')],active_job:job('uploading')});
  await app.refresh();assert.equal(app.read().pending.id,'job-one');assert.equal(app.button.disabled,true);
  app.fresh=status({jobs:[job('uploaded'),job('uploaded','older')]});await app.refresh();
  assert.equal(app.button.disabled,false);
});

test('unknown POST acceptance remains locked across page reload using only session job state',async()=>{
  const first=harness();await settle();
  first.hook=url=>url==='api/scan'?Promise.reject(new Error('lost response')):undefined;
  await first.scan();const saved=JSON.parse(first.storage.get(pendingKey));
  assert.deepEqual(Object.keys(saved).sort(),['id','requestId']);
  assert.equal(saved.requestId,REQUEST_ID);
  const reloaded=harness(status(),saved);await settle();assert.equal(reloaded.button.disabled,true);
  await reloaded.scan();assert.equal(reloaded.calls.filter(call=>call.url==='api/scan').length,0);
  reloaded.fresh=status({jobs:[job('needs_review', 'resolved',{scan_request_id:REQUEST_ID,error:{message:'In Lexware prüfen.'}})]});
  await reloaded.refresh();assert.equal(reloaded.button.disabled,false);
  assert.equal(reloaded.elements.get('message').textContent,'In Lexware prüfen.');
});

test('incomplete success response and server errors are uncertain, explicit rejection is not',async()=>{
  for(const [payload,code,locked] of [[{},202,true],[{error:'Serverfehler'},500,true],[{error:'Noch gesperrt'},409,false]]) {
    const app=harness();await settle();app.hook=url=>url==='api/scan'?response(payload,code):undefined;
    await app.scan();assert.equal(app.button.disabled,locked,String(code));
  }
});

test('a failed refresh during an accepted job cannot release its lock',async()=>{
  const app=harness();await settle();app.fresh=status({busy:true,jobs:[job('scanning')],active_job:job('scanning')});
  await app.scan();
  app.hook=url=>url==='api/status'?Promise.reject(new Error('disconnected')):undefined;
  await app.refresh();assert.equal(app.button.disabled,true);assert.equal(app.read().pending.id,'job-one');
  assert.equal(app.elements.get('progress-bar').classList.contains('paused'),true);
  app.hook=null;app.fresh=status({jobs:[job('uploaded')]});await app.refresh();assert.equal(app.button.disabled,false);
});

test('refreshes are serialized including response body parsing and polling',async()=>{
  const app=harness();await settle();const body=deferred();
  app.hook=url=>url==='api/status'?{ok:true,status:200,json:()=>body.promise}:undefined;
  const before=app.calls.length,one=app.refresh(),two=app.refresh();
  assert.equal(one,two);await settle();assert.equal(app.calls.length,before+1);
  const pollTimer=[...app.timers.values()].find(timer=>timer.ms===1800);
  assert.ok(pollTimer);const poll=pollTimer.callback();await settle();assert.equal(app.calls.length,before+1);
  body.resolve(status());await Promise.all([one,two,poll]);
  assert.ok([...app.timers.values()].some(timer=>timer.ms===1800));
});

test('a GET begun before the POST cannot unlock the newly accepted job',async()=>{
  const app=harness();await settle();const old=deferred();let held=true;
  app.hook=url=>url==='api/status'&&held?old.promise:undefined;
  const refreshing=app.refresh();await settle();
  const scanning=app.scan();await settle();
  held=false;old.resolve(response(status()));await Promise.all([refreshing,scanning]);
  assert.equal(app.button.disabled,true);assert.equal(app.read().pending.id,'job-one');
  app.fresh=status({busy:true,jobs:[job('scanning')],active_job:job('scanning')});
  await app.refresh();assert.equal(app.button.disabled,true);
});

test('history is compact and never exposes IDs or creates another upload button',async()=>{
  const app=harness(status({jobs:[job('ready','private-job-id',{
    file_id:'private-file-id',original:{bytes:200},upload:{bytes:190},error:{message:'Upload noch gesperrt.'}
  })]}));await settle();
  const history=app.elements.get('jobs');assert.equal(all(history,'button').length,0);
  assert.equal(all(history,'a').length,2);assert.match(text(history),/Original/);assert.match(text(history),/Upload/);
  assert.match(text(history),/Upload noch gesperrt/);assert.doesNotMatch(text(history),/private-job-id|private-file-id/);
});

test('initial load and polling never POST automatically',async()=>{
  const app=harness();await settle();
  const timer=[...app.timers.values()].find(value=>value.ms===1800);await timer.callback();await settle();
  assert.equal(app.calls.filter(call=>call.url==='api/scan').length,0);
  assert.equal(app.calls.every(call=>call.url==='api/status'),true);
});

test('actual POST timeout leaves acceptance unknown and never retries automatically',async()=>{
  const app=harness();await settle();
  app.hook=(url,options)=>url==='api/scan'?new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('timeout')))):undefined;
  const scanning=app.scan();await settle();
  const timer=[...app.timers.values()].find(value=>value.ms===15000);assert.ok(timer);timer.callback();await scanning;
  assert.equal(app.button.disabled,true);assert.equal(app.read().pending.uncertain,true);
  assert.equal(app.calls.filter(call=>call.url==='api/scan').length,1);
});

test('an upload failure preserves completed scan and archive steps without claiming success',async()=>{
  const app=harness();await settle();app.fresh=status({busy:true,jobs:[job('scanning')],active_job:job('scanning')});await app.scan();
  app.fresh=status({jobs:[job('needs_review','job-one',{original:{bytes:200},upload:{bytes:190},error:{message:'In Lexware prüfen.'}})]});
  await app.refresh();assert.equal(app.elements.get('step-scan').classList.contains('done'),true);
  assert.equal(app.elements.get('step-archive').classList.contains('done'),true);
  assert.equal(app.elements.get('step-upload').classList.contains('done'),false);
  assert.equal(app.elements.get('progress-bar').classList.contains('paused'),true);
  assert.equal(app.elements.get('progress-bar').classList.contains('complete'),false);
});

test('multiple new jobs cannot resolve a POST with an unknown response',async()=>{
  const app=harness();await settle();app.hook=url=>url==='api/scan'?Promise.reject(new Error('lost response')):undefined;
  await app.scan();app.fresh=status({jobs:[job('uploaded','another-client'),job('uploaded','possible-own')]});await app.refresh();
  assert.equal(app.button.disabled,true);assert.equal(app.read().pending.id,null);
});

test('missing scanner or MCP configuration keeps the main button disabled',async()=>{
  for(const missing of [{scanner_configured:false},{mcp_configured:false}]) {
    const app=harness(status(missing));await settle();assert.equal(app.button.disabled,true);
    assert.equal(app.elements.get('status').textContent,'Einrichtung erforderlich');
    await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,0);
  }
});

test('direct API uses upload configuration instead of the legacy MCP flag',async()=>{
  const app=harness(status({upload_backend:'api',upload_configured:true,mcp_configured:false}));
  await settle();assert.equal(app.button.disabled,false);
  const missing=harness(status({upload_backend:'api',upload_configured:false,mcp_configured:true}));
  await settle();assert.equal(missing.button.disabled,true);
  assert.equal(missing.elements.get('status').textContent,'Einrichtung erforderlich');
  await missing.scan();assert.equal(missing.calls.filter(call=>call.url==='api/scan').length,0);
});

test('HTTP-compatible UUID generation uses getRandomValues without randomUUID',async()=>{
  const app=harness();await settle();app.run('crypto={getRandomValues:bytes=>{bytes.fill(5);return bytes;}}');
  app.hook=(url,options)=>{
    if(url!=='api/scan')return undefined;
    const nonce=options.headers['X-Scan-Request-ID'];assert.match(nonce,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    const accepted=job('scanning','job-one',{scan_request_id:nonce});
    app.fresh=status({busy:true,active_job:accepted,jobs:[accepted]});
    return response({job:accepted},202);
  };
  await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,1);
  assert.equal(app.read().pending.id,'job-one');
  assert.doesNotMatch(source,/Math\.random/);
});

test('missing crypto prevents any POST and clearly disables the button',async()=>{
  const app=harness();await settle();app.run('crypto={};renderStatus()');
  assert.equal(app.button.disabled,true);assert.equal(app.elements.get('status').textContent,'Browser nicht unterstützt');
  await app.scan();assert.equal(app.calls.filter(call=>call.url==='api/scan').length,0);
});

test('a foreign unique job or mismatched success response cannot resolve this request',async()=>{
  const app=harness();await settle();
  const foreign=job('uploaded','foreign',{scan_request_id:'a7c66ab7-874c-49d5-8f29-f369af0fc530'});
  app.hook=url=>url==='api/scan'?response({job:foreign},202):undefined;app.fresh=status({jobs:[foreign]});
  await app.scan();assert.equal(app.button.disabled,true);assert.equal(app.read().pending.id,null);
  assert.equal(app.elements.get('status').textContent,'Auftrag wird geprüft');
  app.fresh=status({jobs:[job('uploaded'),foreign]});await app.refresh();assert.equal(app.button.disabled,false);
});

test('download failure after a returned scan filename still marks the scan complete',async()=>{
  const app=harness();await settle();app.fresh=status({busy:true,jobs:[job('scanning')],active_job:job('scanning')});await app.scan();
  app.fresh=status({jobs:[job('error','job-one',{source_filename:'specific_scan.jpg',error:{message:'Download fehlgeschlagen.'}})]});
  await app.refresh();assert.equal(app.elements.get('step-scan').classList.contains('done'),true);
  assert.equal(app.elements.get('step-archive').classList.contains('done'),false);
  assert.equal(app.elements.get('progress-bar').classList.contains('paused'),true);
});
