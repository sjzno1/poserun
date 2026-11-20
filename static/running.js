/* No credential persistence. All data inserted as text, never server-supplied HTML. */
const rmText = (id,value) => {const el=document.getElementById(id);if(el) el.textContent=value;};
async function rmAPI(path, options) {
  const token=authToken;
  const response=await apiFetch('/api/running'+path,{...options,cache:'no-store'});
  const data=await response.json();
  if (rmRetired || token !== authToken || token !== localStorage.getItem('poserun_token')) throw Error('会话已切换');
  if(!response.ok) throw Error(data.error || '请求失败');
  return data;
}
function rmActivityList(id, rows, trackEnabled) {
  const list=document.getElementById(id); if(!list)return; list.replaceChildren();
  if(!rows.length){list.textContent='暂无活动；请导入 FIT 或连接佳明。';return;}
  rows.forEach(a=>{
    const li=document.createElement('li'), link=document.createElement('a');
    link.href='/activities.html#'+a.id;
    link.textContent=`${a.start_time} · ${a.sport} · ${a.distance_m==null?'距离未知':(a.distance_m/1000).toFixed(2)+' km'} · 来源 ${rmSource(a.source)}`;
    li.append(link);
    if(trackEnabled){const t=document.createElement('a');t.className='rm-track-link';t.href='/activity-track?id='+a.id;t.textContent='轨迹';li.append(document.createTextNode(' · '),t);}
    list.append(li);
  });
}
async function rmDetail(){
  if(!/^#[0-9]+$/.test(location.hash))return;
  const {activity:a}=await rmAPI('/activities/'+location.hash.slice(1));
  const fields=document.getElementById('activityFields'); if(!fields)return;
  fields.replaceChildren();
  for(const [label,value] of [['开始时间（UTC）',a.start_time],['运动',a.sport],['距离（m）',a.distance_m],['时长（s）',a.duration_s],['平均心率（bpm）',a.avg_hr],['来源',rmSource(a.source)],['文件 SHA-256',a.file_sha256]]){
    const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value??'未知';dd.style.overflowWrap='anywhere';fields.append(dt,dd);
  }
  document.getElementById('activityDetail').hidden=false;
}
async function rmDashboard(){
  const d=await rmAPI('/dashboard?timezone='+encodeURIComponent(rmZone()));
  document.getElementById('weeklyEntry').hidden=!d.weekly_report_enabled;
  const gearEntry=document.getElementById('gearEntry'); if(gearEntry) gearEntry.hidden=!d.gear_enabled;
  const statsEntry=document.getElementById('statsEntry'); if(statsEntry) statsEntry.hidden=!d.training_stats_enabled;
  const trackEntry=document.getElementById('trackEntry'); if(trackEntry) trackEntry.hidden=!d.track_map_enabled;
  const loadEntry=document.getElementById('loadEntry'); if(loadEntry) loadEntry.hidden=!d.load_enabled;
  rmText('weeklyKm',!d.weekly_evidence.length?'暂无记录':d.weekly_km==null?'距离缺测':d.weekly_km+' km');
  rmText('weeklyEvidence',`周起点 ${d.weekly_start} / ${d.week_timezone} · 来源活动 #${d.weekly_evidence.join(', #')||'无'} · 未核验${d.weekly_missing_distance?'；存在缺失距离':''}`);
  rmActivityList('recentActivities',d.recent_activities,d.track_map_enabled);rmText('dashboardNotice',d.notice);

}
let rmOffset=0;
async function rmLoadActivities(){const d=await rmAPI('/activities?limit=50&offset='+rmOffset);rmActivityList('activitiesList',d.activities,d.track_map_enabled);document.getElementById('moreActivities').disabled=d.activities.length<50;}
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
    if(window.PAGE==='weekly') await rmWeekly();
    if(window.PAGE==='home') await rmDashboard();
    if(window.PAGE==='garmin') await rmGarmin();
    if(window.PAGE==='training-stats') await rmTrainingStats();
  } catch(e){rmText('runningStatus',e.message);}
}
const rmSource = s => ({fit:'FIT 导入 · 未核验', api:'佳明导入 · 未核验', demo:'演示数据 · 非实测', manual:'手工记录 · 未核验'}[s] || '其他来源 · 未核验');
const rmZoneKey = () => 'running.week-zone.' + authUser.id;
const rmZone = () => localStorage.getItem(rmZoneKey()) || 'Asia/Shanghai';
let rmRetired=false, rmRevision=0;
function rmForget(){
  rmRetired=true;
  document.querySelector('main')?.replaceChildren();
}
window.addEventListener('pagehide',rmForget);
window.addEventListener('pageshow',e=>{if(e.persisted){rmForget();location.reload();}});
window.addEventListener('storage',e=>{if(e.key==='poserun_token'||e.key===null){rmForget();location.reload();}});
function onAuthReady(user){
  if(rmRetired){location.reload();return;}
  if(user)rmReady();
}
async function rmWeekly(){
  const select=document.getElementById('weekZone'), root=document.getElementById('reportWeeks');
  select.value=rmZone();
  async function render(){
    const revision=++rmRevision;
    root.replaceChildren();rmText('runningStatus','加载中…');
    try {
      const report=await rmAPI('/weekly-report?timezone='+encodeURIComponent(select.value));
      if(revision!==rmRevision || rmRetired)return;
      const statsNav=document.getElementById('statsNav'); if(statsNav) statsNav.hidden=!report.training_stats_enabled;
      for(const [title,w] of [['本周',report.current],['上周',report.previous]]){
        const panel=document.createElement('section');panel.className='rm-panel';
        function line(tag,text){const el=document.createElement(tag);el.textContent=text;panel.append(el);return el;}
        line('h2',title);line('p',`${w.start.slice(0,10)} 起 · ${report.timezone}`);
        if(!w.count)line('p','暂无记录');
        else {
          line('p',w.distance_km==null?'距离缺测，暂不合计':w.distance_km+' km').className='rm-number';
          line('p',`${w.count} 次 · ${w.duration_s==null?'时长缺测':w.duration_s+' 秒'}`);
          for(const a of w.activities){const link=line('a',`活动 #${a.id} · ${rmSource(a.source)}`);link.href='/activities.html#'+a.id;panel.append(document.createElement('br'));}
        }
        root.append(panel);
      }
      rmText('runningStatus','');
    } catch(e){if(revision===rmRevision&&!rmRetired)rmText('runningStatus',e.message);}
  }
  select.onchange=()=>{localStorage.setItem(rmZoneKey(),select.value);return render();};
  await render();
}
/* 训练统计仪表盘（P2 第四切片）：月/年聚合＋PB＋12 周趋势。全部 textContent 插入，不插服务器 HTML；图表为内联 SVG，离线可用。 */
const rmFmtKm = v => v==null ? '—' : v.toFixed(2)+' km';
const rmFmtDur = s => { if(s==null) return '—'; const h=Math.floor(s/3600), m=Math.round((s%3600)/60); return h ? h+' 时 '+m+' 分' : m+' 分'; };
const rmFmtPace = p => { if(p==null) return '—'; const m=Math.floor(p/60), s=Math.round(p%60); return m+'\u2032'+String(s).padStart(2,'0')+'\u2033/km'; };
function rmBarChart(el, bars, unit) {
  const W=760,H=230,pad=30,svgNS='http://www.w3.org/2000/svg';
  const slot=(W-pad*2)/bars.length, bw=Math.min(46,slot*0.62);
  const max=Math.max(...bars.map(b=>b.value==null?0:b.value),1);
  const svg=document.createElementNS(svgNS,'svg');
  svg.setAttribute('viewBox','0 0 '+W+' '+H);
  svg.setAttribute('class','stats-svg');
  svg.setAttribute('role','img');
  svg.setAttribute('aria-label',bars.map(b=>b.label+'：'+(b.value==null?(b.note||'缺测'):(b.value===0?'暂无记录':b.value.toFixed(2)+' '+unit))).join('；'));
  const base=document.createElementNS(svgNS,'line');
  base.setAttribute('x1',pad-6);base.setAttribute('x2',W-pad+6);base.setAttribute('y1',H-pad);base.setAttribute('y2',H-pad);base.setAttribute('stroke','currentColor');base.setAttribute('opacity','0.4');
  svg.append(base);
  bars.forEach((b,i)=>{
    const x=pad+slot*i+(slot-bw)/2;
    if(b.value!=null){
      const h=Math.max(2,(H-pad-24)*b.value/max);
      const rect=document.createElementNS(svgNS,'rect');
      rect.setAttribute('x',x);rect.setAttribute('y',H-pad-h);rect.setAttribute('width',bw);rect.setAttribute('height',h);
      rect.setAttribute('fill','currentColor');rect.setAttribute('opacity','0.75');
      const t=document.createElementNS(svgNS,'title');
      t.textContent=b.label+'：'+(b.value===0?'暂无记录':b.value.toFixed(2)+' '+unit);
      rect.append(t);svg.append(rect);
    }
    const note=document.createElementNS(svgNS,'text');
    note.setAttribute('x',x+bw/2);note.setAttribute('y',H-pad-8);note.setAttribute('text-anchor','middle');note.setAttribute('font-size','10');
    note.textContent=b.value==null?(b.note||'—'):(b.value===0?'暂无记录':'');
    svg.append(note);
    const label=document.createElementNS(svgNS,'text');
    label.setAttribute('x',x+bw/2);label.setAttribute('y',H-pad+16);label.setAttribute('text-anchor','middle');label.setAttribute('font-size','11');
    label.textContent=b.label;
    svg.append(label);
  });
  el.replaceChildren(svg);
}
function rmRenderMonths(d){
  const root=document.getElementById('statsMonths'); if(!root)return; root.replaceChildren();
  const chart=document.createElement('div');
  rmBarChart(chart,d.months.map(m=>({label:m.month+'月',value:m.distance_km,note:'—'})),'km');
  const wrap=document.createElement('div');wrap.className='stats-scroll';
  const table=document.createElement('table');table.className='stats-table';
  table.innerHTML='<caption>月度合计（北京时间）；缺测显示「—」，不补零</caption><thead><tr><th>月份</th><th class="num">次数</th><th class="num">距离</th><th class="num">时长</th><th class="num">均配</th><th>缺测</th></tr></thead>';
  const tb=document.createElement('tbody');
  for(const m of d.months){
    const tr=document.createElement('tr');
    const miss=(m.excluded.distance||m.excluded.duration)?'距离缺 '+m.excluded.distance+' · 时长缺 '+m.excluded.duration:'—';
    const cells=[['月份',m.month+' 月',null],['次数',m.count||'—','num'],['距离',rmFmtKm(m.distance_km),'num'],['时长',rmFmtDur(m.duration_s),'num'],['均配',rmFmtPace(m.avg_pace_s_per_km),'num'],['缺测',miss,null]];
    for(const [label,value,cls] of cells){
      const th=document.createElement('th');th.scope='row';th.textContent=label;tr.append(th);
      const td=document.createElement('td');td.textContent=value;if(cls)td.className=cls;tr.append(td);
    }
    tb.append(tr);
  }
  table.append(tb);
  wrap.append(table);
  root.append(chart,wrap);
}
function rmPbCard(root,title,value,evidence){
  const s=document.createElement('section');s.className='rm-panel pb-card';
  const h=document.createElement('h3');h.textContent=title;s.append(h);
  const p=document.createElement('p');p.className='pb-value';p.textContent=value;s.append(p);
  const e=document.createElement('p');e.className='rm-evidence';e.textContent=evidence;s.append(e);
  root.append(s);
}
function rmRenderPb(d){
  const root=document.getElementById('statsPb'); if(!root)return; root.replaceChildren();
  const scope='已导入活动内最佳 · '+d.pb.window+' 年（非终身纪录）';
  const ref=a=>a?('活动 #'+a.id+' · '+a.date+' · '+rmFmtKm(a.distance_km)):'暂无记录';
  rmPbCard(root,'最长距离',rmFmtKm(d.pb.longest_distance.value_km),scope+' · '+ref(d.pb.longest_distance.activity));
  rmPbCard(root,'最长时长',rmFmtDur(d.pb.longest_duration.value_s),scope+' · '+ref(d.pb.longest_duration.activity));
  rmPbCard(root,'最快均配（整条活动均配）',rmFmtPace(d.pb.fastest_pace.value_s_per_km),scope+' · '+d.pb.fastest_pace.note+' · '+ref(d.pb.fastest_pace.activity));
  const bw=d.pb.best_week;
  rmPbCard(root,'最佳周（周距离最大）',bw?rmFmtKm(bw.distance_km):'—',bw?scope+' · '+bw.week_start+' 起 · '+bw.count+' 次 · 活动 '+bw.activity_ids.map(id=>'#'+id).join(', '):'暂无记录');
}
function rmRenderTrend(d){
  const root=document.getElementById('statsTrend'); if(!root)return; root.replaceChildren();
  rmBarChart(root,d.weekly_trend.map(w=>({label:w.week_start.slice(5),value:w.distance_km,note:w.count===0?'暂无记录':'—'})),'km');
  const p=document.createElement('p');p.className='rm-evidence';
  p.textContent='柱状图仅按周合计，不连线插值；空周为 0（暂无记录），缺距离周不画柱。';
  root.append(p);
}
async function rmTrainingStats(){
  const yearSel=document.getElementById('statsYear'); if(!yearSel)return;
  const y=new Date().getFullYear();
  for(const v of [y-1,y,y+1]){const o=document.createElement('option');o.value=String(v);o.textContent=v+' 年';yearSel.append(o);}
  yearSel.value=String(y);
  async function render(){
    try{
      const d=await rmAPI('/stats/overview?year='+encodeURIComponent(yearSel.value));
      if(rmRetired)return;
      rmRenderMonths(d);rmRenderPb(d);rmRenderTrend(d);
      rmText('runningStatus','');
    }catch(e){if(!rmRetired)rmText('runningStatus',e.message);}
  }
  yearSel.onchange=()=>render();
  await render();
}
// Data loads only after common.js has verified the current session.
