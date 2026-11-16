/* No credential persistence. All data inserted as text, never server-supplied HTML. */
const rmText = (id,value) => {const el=document.getElementById(id);if(el) el.textContent=value;};
async function rmAPI(path, options) {
  const response=await apiFetch('/api/running'+path,options);
  const data=await response.json();
  if(!response.ok) throw Error(data.error || '请求失败');
  return data;
}
function rmActivityList(id, rows) {
  const list=document.getElementById(id); if(!list)return; list.replaceChildren();
  if(!rows.length){list.textContent='暂无活动；请导入 FIT 或连接佳明。';return;}
  rows.forEach(a=>{
    const li=document.createElement('li'), link=document.createElement('a');
    link.href='/activities.html#'+a.id;
    link.textContent=`${a.start_time} · ${a.sport} · ${a.distance_m==null?'距离未知':(a.distance_m/1000).toFixed(2)+' km'} · 来源 ${a.source}`;
    li.append(link);list.append(li);
  });
}
async function rmDetail(){
  if(!/^#[0-9]+$/.test(location.hash))return;
  const {activity:a}=await rmAPI('/activities/'+location.hash.slice(1));
  const fields=document.getElementById('activityFields'); if(!fields)return;
  fields.replaceChildren();
  for(const [label,value] of [['开始时间（UTC）',a.start_time],['运动',a.sport],['距离（m）',a.distance_m],['时长（s）',a.duration_s],['平均心率（bpm）',a.avg_hr],['来源',a.source],['文件 SHA-256',a.file_sha256]]){
    const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value??'未知';dd.style.overflowWrap='anywhere';fields.append(dt,dd);
  }
  document.getElementById('activityDetail').hidden=false;
}
async function rmDashboard(){
  const d=await rmAPI('/dashboard');
  rmText('weeklyKm',d.weekly_km==null?'未知':d.weekly_km+' km');
  rmText('weeklyEvidence',`周起点 ${d.weekly_start} / UTC · 来源活动 #${d.weekly_evidence.join(', #')||'无'} · 未核验${d.weekly_missing_distance?'；存在缺失距离':''}`);
  rmActivityList('recentActivities',d.recent_activities);rmText('dashboardNotice',d.notice);
  const chart=document.getElementById('loadTrend');chart.replaceChildren();
  d.load_trend.values.forEach((v,i)=>{const bar=document.createElement('div');bar.className='rm-bar';bar.style.height=(v*3)+'px';bar.textContent=`演示 ${i+1}：${v}`;chart.append(bar);});
}
let rmOffset=0;
async function rmLoadActivities(){const d=await rmAPI('/activities?limit=50&offset='+rmOffset);rmActivityList('activitiesList',d.activities);document.getElementById('moreActivities').disabled=d.activities.length<50;}
async function rmGarmin(){
  const key=document.getElementById('garminKey'),mode=document.getElementById('garminMode');
  const expiry=document.getElementById('garminExpiry');
  if(!expiry.value){const d=new Date();d.setDate(d.getDate()+30);expiry.value=d.toISOString().slice(0,10);}
  async function status(){
    const s=await rmAPI('/garmin');
    rmText('garminState',`${s.status} · ${s.mode||'无连接'} · 有效期 ${s.expiry||'未知'}（用户声明）${s.renewal_due?' · 请续期':''}`);
    rmText('garminSyncState',s.sync ? `最近同步 ${s.sync.last_run_at} / ${s.sync.status} ${s.sync.error||''}` : '尚无同步记录');
    document.getElementById('garminSync').disabled=['disconnected','expired'].includes(s.status);
  }
  mode.onchange=()=>{key.value='';key.disabled=mode.value==='demo';};
  document.getElementById('garminForm').onsubmit=async e=>{
    e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
    const payload={mode:mode.value,expiry:expiry.value};if(mode.value==='live')payload.key=key.value;
    try{const r=await rmAPI('/garmin/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});rmText('runningStatus','已连接 · '+r.mode+' · '+r.notice);await status();}
    catch(e){rmText('runningStatus',e.message);}finally{key.value='';delete payload.key;button.disabled=false;}
  };
  document.getElementById('garminSync').onclick=async e=>{e.target.disabled=true;try{const r=await rmAPI('/garmin/sync',{method:'POST'});rmText('runningStatus',`本次新增 ${r.added} 条。${r.notice}`);}catch(e){rmText('runningStatus',e.message);}finally{await status();}};
  document.getElementById('garminDisconnect').onclick=async()=>{try{await rmAPI('/garmin',{method:'DELETE'});rmText('runningStatus','已断开，活动保留。');await status();}catch(e){rmText('runningStatus',e.message);}};
  await status();
}
async function rmReady(){
  try {
    if(window.PAGE==='activities'){
      await rmLoadActivities();await rmDetail();
      window.addEventListener('hashchange',()=>rmDetail().catch(e=>rmText('runningStatus',e.message)));
      document.getElementById('moreActivities').onclick=()=>{rmOffset+=50;rmLoadActivities().catch(e=>rmText('runningStatus',e.message));};
      document.getElementById('fitForm').onsubmit=async e=>{
        e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
        try{const d=await rmAPI('/activities/import',{method:'POST',body:new FormData(e.target)});rmText('runningStatus',d.duplicate?'重复活动，未再次导入。':'导入完成，摘要未经独立核验。');rmOffset=0;await rmLoadActivities();location.hash=String(d.activity.id);}catch(e){rmText('runningStatus',e.message);}finally{button.disabled=false;}
      };
    }
    if(window.PAGE==='home') await rmDashboard();
    if(window.PAGE==='garmin') await rmGarmin();
  } catch(e){rmText('runningStatus',e.message);}
}
function onAuthReady(user){if(user)rmReady();}
// Data loads only after common.js has verified the current session.
