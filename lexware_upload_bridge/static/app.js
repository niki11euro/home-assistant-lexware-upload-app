'use strict';
let state = null;
let csrf = '';
let connected = false;
let sending = false;
let refreshPromise = null;
let revision = 0;
let submission = null;
let lastFinished = null;
let cryptoFailed = false;
const $ = id => document.getElementById(id);
const pendingKey = 'wer-lexware-pending-job';
const requestIdPattern=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const terminal = new Set(['uploaded','duplicate','needs_review','interrupted','error','ready']);
const names = {
  queued:'Auftrag startet', scanning:'Scan läuft', receiving:'Wird gesichert',
  archived:'Original gesichert', preparing:'Wird gesichert', ready:'Lokal gesichert · Upload nicht erfolgt',
  preflight:'Upload wird vorbereitet', uploading:'Upload läuft', uploaded:'Lexware hat den Upload angenommen',
  duplicate:'Bereits an Lexware übergeben', needs_review:'Uploadstatus in Lexware prüfen',
  interrupted:'Auftrag unterbrochen', error:'Auftrag fehlgeschlagen'
};
function persistSubmission() {
  try {
    if (submission) sessionStorage.setItem(pendingKey, JSON.stringify({id:submission.id, requestId:submission.requestId}));
    else sessionStorage.removeItem(pendingKey);
  } catch (_) { /* The in-memory lock remains active if browser storage is unavailable. */ }
}
try {
  const pending = JSON.parse(sessionStorage.getItem(pendingKey) || 'null');
  if (pending && (pending.id === null || typeof pending.id === 'string') &&
      typeof pending.requestId==='string' && requestIdPattern.test(pending.requestId)) {
    submission = {...pending, uncertain:true, preview:null};
  }
} catch (_) { /* No saved scanner settings are read or written. */ }
function message(text, error=false) {
  $('message').textContent = text;
  $('message').classList.toggle('error', error);
}
function cryptoAvailable() {
  return !cryptoFailed && typeof crypto!=='undefined' &&
    (typeof crypto.randomUUID==='function' || typeof crypto.getRandomValues==='function');
}
function uploadConfigured() {
  return state?.upload_configured ?? state?.mcp_configured;
}
function controls() {
  $('scan').disabled = !connected || !state || !state.scan_enabled || !state.upload_enabled ||
    !state.scanner_configured || !uploadConfigured() || !cryptoAvailable() || state.busy || sending || Boolean(submission);
  $('scan').setAttribute('aria-busy', String(Boolean(sending || submission || state?.busy)));
}
function reconcile() {
  if (!submission || sending) return;
  const jobs = state.jobs;
  if (!submission.id) {
    const candidates=new Map(jobs.filter(job=>job.scan_request_id===submission.requestId).map(job=>[job.id,job]));
    if (state.active_job?.scan_request_id===submission.requestId) candidates.set(state.active_job.id,state.active_job);
    if (candidates.size===1) {
      const found=candidates.values().next().value;
      submission.id=found.id;submission.preview=found;persistSubmission();
    }
  }
  const job = jobs.find(job => job.id===submission.id && job.scan_request_id===submission.requestId) ||
    (state.active_job?.id===submission.id && state.active_job?.scan_request_id===submission.requestId ? state.active_job : null);
  if (job && terminal.has(job.status) && !state.busy) {
    lastFinished = job;
    submission = null;
    persistSubmission();
    message(job.error?.message || names[job.status] || 'Auftrag abgeschlossen.', !['uploaded','duplicate'].includes(job.status));
  }
}
function displayedJob() {
  if (submission) {
    return (state?.active_job?.scan_request_id===submission.requestId ? state.active_job : null) ||
      state?.jobs.find(job=>job.id===submission.id && job.scan_request_id===submission.requestId) || submission.preview;
  }
  return state?.active_job || lastFinished;
}
function phaseLabel(status) {
  return status==='ready' && state?.busy ? 'Upload wird vorbereitet' : names[status] || 'Auftrag läuft';
}
function renderProgress(job) {
  const visible = Boolean(submission || state?.busy || lastFinished);
  $('progress').hidden = !visible;
  if (!visible) return;
  const status = job?.status || 'queued';
  let stage = 'scan', completed = 0;
  if (['receiving','archived','preparing'].includes(status)) {stage='archive';completed=1;}
  if (['ready','preflight','uploading'].includes(status)) {stage='upload';completed=2;}
  if (['uploaded','duplicate'].includes(status)) {stage='complete';completed=3;}
  if (['needs_review','interrupted','error'].includes(status)) {
    if (job?.upload) {stage='upload';completed=2;}
    else if (job?.original || job?.source_filename) {stage='archive';completed=1;}
  }
  if (submission?.uncertain && !job) stage='unknown';
  if (job && state?.busy && state.progress && ['scan','archive','upload','complete'].includes(state.progress.stage)) {
    stage=state.progress.stage;
    completed=Math.max(0,Math.min(3,Number(state.progress.completed_steps)||0));
  }
  const stopped = !connected || (terminal.has(status) && !state?.busy && !['uploaded','duplicate'].includes(status));
  const label = !connected ? 'Verbindung unterbrochen · Status wird erneut geprüft' :
    submission?.uncertain && !job ? 'Auftragsantwort fehlt · Status wird geprüft' : phaseLabel(status);
  $('progress-label').textContent = label;
  $('progress-bar').setAttribute('aria-valuetext', label);
  $('progress-bar').classList.toggle('complete',stage==='complete' && !state?.busy);
  $('progress-bar').classList.toggle('paused',stopped);
  // No percentages are inferred while the scanner supplies no numeric progress.
  $('progress-bar').removeAttribute('aria-valuenow');
  $('progress-bar').removeAttribute('aria-valuemin');
  $('progress-bar').removeAttribute('aria-valuemax');
  for (const [index,key] of ['scan','archive','upload'].entries()) {
    const step=$('step-'+key), done=index<completed, current=stage===key && !stopped;
    step.classList.toggle('done',done);step.classList.toggle('current',current);
    step.setAttribute('aria-label',step.textContent+': '+(done?'abgeschlossen':current?'läuft':'ausstehend'));
    if (current) step.setAttribute('aria-current','step'); else step.removeAttribute('aria-current');
  }
}
function renderStatus() {
  let label='Bereit',tone='ready';
  const job=displayedJob();
  if (!connected) {label='Verbindung unterbrochen';tone='offline';}
  else if (sending || submission || state.busy) {
    label=submission?.uncertain && !job ? 'Auftrag wird geprüft' : job ? phaseLabel(job.status) : 'Auftrag startet';
    tone='working';
  } else if (!state.scan_enabled || !state.upload_enabled) {label='Nicht freigegeben';tone='disabled';}
  else if (!state.scanner_configured || !uploadConfigured() || state.device_state?.state==='not_configured') {label='Einrichtung erforderlich';tone='attention';}
  else if (!cryptoAvailable()) {label='Browser nicht unterstützt';tone='attention';message('Dieser Browser unterstützt keinen sicheren Auftragsstart. Bitte einen aktuellen Browser verwenden.',true);}
  else if (state.device_state?.state==='attention') {label=state.device_state.label || 'Auftrag prüfen';tone='attention';}
  $('status').textContent=label;$('status').setAttribute('data-tone',tone);
  const profile=state?.scan_profile;
  if (profile) {
    const modes={Color:'Farbe',Gray:'Graustufen',Lineart:'Schwarzweiß'};
    const paper=String(profile.paper_size || '').toLowerCase()==='full'?'Flachbett':profile.paper_size;
    $('profile').textContent=[profile.resolution ? profile.resolution+' dpi' : '',modes[profile.mode]||profile.mode,
      profile.format?.toUpperCase(),paper].filter(Boolean).join(' · ');
  }
  controls();renderProgress(job);
}
function renderJobs(jobs) {
  const fragment=document.createDocumentFragment();
  for (const job of jobs) {
    const article=document.createElement('article');article.className='job';
    const heading=document.createElement('div');heading.className='job-head';
    const date=document.createElement('time');date.className='job-date';date.dateTime=job.created_at;
    date.textContent=new Date(job.created_at).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
    const status=document.createElement('span');status.className='job-status';status.textContent=names[job.status]||job.status;
    heading.append(date,status);article.append(heading);
    const links=document.createElement('div');links.className='job-files';
    for (const kind of ['original','upload']) {
      if (!job[kind]) continue;
      const link=document.createElement('a');link.href='api/jobs/'+encodeURIComponent(job.id)+'/files/'+kind;
      link.textContent=kind==='original'?'Original':'Upload';link.download='';links.append(link);
    }
    article.append(links);
    if (job.error) {const error=document.createElement('p');error.className='job-error';error.textContent=job.error.message;article.append(error);}
    fragment.append(article);
  }
  if (!jobs.length) {const empty=document.createElement('p');empty.className='empty';empty.textContent='Noch keine Belege.';fragment.append(empty);}
  $('jobs').replaceChildren(fragment);
}
async function fetchWithTimeout(path, options, timeout) {
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),timeout);
  try {
    const response=await fetch(path,{...options,signal:controller.signal});
    return {response,data:await response.json()};
  }
  finally {clearTimeout(timer);}
}
function refresh() {
  if (refreshPromise) return refreshPromise;
  const requestedRevision=revision;
  refreshPromise=(async()=>{
    try {
      const {response,data:fresh}=await fetchWithTimeout('api/status',{cache:'no-store'},8000);
      if (!response.ok) throw new Error('Status nicht erreichbar.');
      if (!fresh || !Array.isArray(fresh.jobs) || typeof fresh.busy!=='boolean' || typeof fresh.csrf!=='string') {
        throw new Error('Statusantwort unvollständig.');
      }
      if (requestedRevision!==revision) return; // A GET begun before a scan cannot unlock its new job.
      const wasConnected=connected;
      state=fresh;csrf=fresh.csrf;connected=true;
      if (!wasConnected) message(submission ? (submission.uncertain?'Auftragsantwort fehlt. Status wird geprüft.':'Auftrag läuft.') :
        lastFinished ? lastFinished.error?.message || names[lastFinished.status] : '',Boolean(lastFinished?.error));
      reconcile();renderJobs(state.jobs);renderStatus();
    } catch (_) {
      if (requestedRevision!==revision) return;
      connected=false;renderStatus();
      message(submission?'Verbindung unterbrochen. Der Auftrag bleibt gesperrt, bis sein Status feststeht.':'Verbindung unterbrochen. Status wird erneut geprüft.',true);
    }
  })().finally(()=>{refreshPromise=null;});
  return refreshPromise;
}
async function scan() {
  if ($('scan').disabled) return;
  let requestId;
  try {
    if (typeof crypto.randomUUID==='function') requestId=crypto.randomUUID();
    else {
      const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
      bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
      const hex=Array.from(bytes,byte=>byte.toString(16).padStart(2,'0')).join('');
      requestId=hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);
    }
    if (!requestIdPattern.test(requestId)) throw new Error('Request-ID fehlt.');
  } catch (_) {cryptoFailed=true;renderStatus();message('Auftrag konnte nicht sicher gestartet werden. Bitte Browser neu laden.',true);return;}
  submission={id:null,requestId,uncertain:false,preview:null};
  persistSubmission();revision+=1;sending=true;lastFinished=null;
  message('Auftrag startet.');renderStatus();
  try {
    const {response,data:result}=await fetchWithTimeout('api/scan',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,'X-Scan-Request-ID':requestId},body:'{}'},15000);
    if (!response.ok) {
      if ([400,401,403,404,409,413,415,422].includes(response.status)) {
        submission=null;persistSubmission();message(result.error||'Auftrag wurde abgewiesen.',true);
      } else throw new Error('Auftragsantwort unklar.');
    } else {
      if (!result.job || typeof result.job.id!=='string' || result.job.scan_request_id!==requestId) throw new Error('Auftragsantwort unvollständig.');
      submission.id=result.job.id;submission.preview=result.job;persistSubmission();
      message('Auftrag läuft.');
    }
  } catch (_) {
    submission.uncertain=true;persistSubmission();
    message('Auftragsantwort fehlt. Status wird geprüft; bitte keinen weiteren Scan starten.',true);
  } finally {
    sending=false;renderStatus();await refresh();
  }
}
$('scan').addEventListener('click',scan);
async function poll() {
  await refresh();
  // Only local bridge status is polled; every scan remains an explicit click.
  setTimeout(poll,1800);
}
if (submission) message('Ein vorheriger Auftrag wird geprüft. Bitte keinen weiteren Scan starten.');
poll();
