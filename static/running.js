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
let rmOffset=0;
async function rmLoadActivities(){const d=await rmAPI('/activities?limit=50&offset='+rmOffset);rmActivityList('activitiesList',d.activities);document.getElementById('moreActivities').disabled=d.activities.length<50;}
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
if(typeof authUser!=='undefined' && authUser)rmReady();
