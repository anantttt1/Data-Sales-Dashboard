const $ = (id) => document.getElementById(id);
let analyticsData = null;
let activeFile = null;
const fmt = (n) => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0}).format(n || 0);
const compact = (n) => n >= 1000000 ? `$${(n/1000000).toFixed(1)}m` : n >= 1000 ? `$${(n/1000).toFixed(n>=10000?0:1)}k` : fmt(n);

function notice(message, error=false){const el=$('notice');el.textContent=message;el.classList.toggle('error',error);if(!message)el.classList.remove('error')}
function populateFilter(id,items){const select=$(id),current=select.value;select.replaceChildren(new Option('All',''));items.forEach(value=>select.add(new Option(value,value)));select.value=items.includes(current)?current:''}
function render(data){
  analyticsData=data;
  populateFilter('category-filter',data.filter_options?.categories||[]);populateFilter('region-filter',data.filter_options?.regions||[]);
  $('revenue').textContent=fmt(data.kpis.revenue);$('orders').textContent=data.kpis.orders.toLocaleString();$('aov').textContent=fmt(data.kpis.average_order);$('repeat').innerHTML=`${data.kpis.repeat_rate}<small>%</small>`;
  const growth=$('growth');growth.textContent=`${data.kpis.growth>=0?'↗':'↘'} ${Math.abs(data.kpis.growth)}%`;growth.classList.toggle('down',data.kpis.growth<0);
  $('data-range').textContent=`${data.range.from} — ${data.range.to}`;$('row-count').textContent=`${data.rows.toLocaleString()} VALID ROWS`;
  const maxProduct=Math.max(1,...data.products.map(x=>x.value));
  $('products-list').innerHTML=data.products.map((x,i)=>`<div class="rank-row"><span class="rank-num">0${i+1}</span><span class="rank-name" title="${esc(x.name)}">${esc(x.name)}</span><span class="rank-val">${compact(x.value)}</span><div class="rank-track"><i style="width:${Math.max(4,x.value/maxProduct*100)}%"></i></div></div>`).join('')||'<div class="panel-sub">No product data in this date range.</div>';
  const maxRegion=Math.max(1,...data.regions.map(x=>x.value));
  $('regions-list').innerHTML=data.regions.map(x=>`<div class="region-item"><div class="region-name">${esc(x.name)}</div><div class="region-value">${compact(x.value)}</div><div class="region-share">${(data.kpis.revenue?x.value/data.kpis.revenue*100:0).toFixed(1)}% of sales</div></div>`).join('')||'<div class="panel-sub">No region data found.</div>';
  const best=data.products[0],bestRegion=data.regions[0];
  if(best && bestRegion){$('insight-title').textContent=`${best.name} leads the pack.`;$('insight-copy').textContent=`${best.name} is your top product at ${fmt(best.value)}. ${bestRegion.name} is the strongest region, contributing ${fmt(bestRegion.value)} in net sales.`;}
  else{$('insight-title').textContent='Your snapshot is ready.';$('insight-copy').textContent='Add product and region columns to your CSV to unlock deeper sales insights.'}
  drawChart(data.months);
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function drawChart(items){
  const canvas=$('revenue-chart'),rect=canvas.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,2);if(!rect.width||!rect.height)return;
  canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const w=rect.width,h=rect.height,p={l:43,r:8,t:9,b:27},cw=w-p.l-p.r,ch=h-p.t-p.b;
  ctx.clearRect(0,0,w,h);const values=items.map(x=>Number(x.revenue)||0);if(!values.length){ctx.fillStyle='#8390a0';ctx.font='11px Manrope';ctx.fillText('No revenue data for this date range',p.l,h/2);return}
  const low=Math.min(0,...values),high=Math.max(1,...values),span=high-low||1;const y=v=>p.t+ch-(v-low)/span*ch;
  ctx.font='9px DM Mono';ctx.textAlign='right';ctx.textBaseline='middle';
  for(let i=0;i<4;i++){const v=low+(high-low)*i/3,yy=y(v);ctx.strokeStyle='#28333e';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(p.l,yy);ctx.lineTo(w-p.r,yy);ctx.stroke();ctx.fillStyle='#748191';ctx.fillText(compact(v),p.l-8,yy)}
  const x=i=>p.l+(items.length===1?cw/2:i/(items.length-1)*cw);const points=values.map((v,i)=>({x:x(i),y:y(v)}));
  const grad=ctx.createLinearGradient(0,p.t,0,p.t+ch);grad.addColorStop(0,'rgba(99,226,174,.24)');grad.addColorStop(1,'rgba(99,226,174,0)');ctx.beginPath();ctx.moveTo(points[0].x,p.t+ch);points.forEach(pt=>ctx.lineTo(pt.x,pt.y));ctx.lineTo(points.at(-1).x,p.t+ch);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();points.forEach((pt,i)=>i?ctx.lineTo(pt.x,pt.y):ctx.moveTo(pt.x,pt.y));ctx.strokeStyle='#74edbf';ctx.lineWidth=2;ctx.lineJoin='round';ctx.stroke();
  points.forEach(pt=>{ctx.beginPath();ctx.arc(pt.x,pt.y,2.5,0,Math.PI*2);ctx.fillStyle='#0f171e';ctx.fill();ctx.strokeStyle='#8df1c5';ctx.lineWidth=1.5;ctx.stroke()});
  ctx.textAlign='center';ctx.textBaseline='top';const step=Math.max(1,Math.ceil(items.length/6));items.forEach((it,i)=>{if(i%step===0||i===items.length-1){ctx.fillStyle='#7e8b9a';const label=it.month.slice(0,7);ctx.fillText(label,x(i),h-17)}});
}
async function load(params='') {try{notice('');let response;if(activeFile){const body=new FormData();body.append('file',activeFile);for(const [key,value] of new URLSearchParams(params))body.append(key,value);response=await fetch('/api/analytics',{method:'POST',body})}else{response=await fetch(`/api/analytics${params}`)}const data=await response.json();if(!response.ok)throw Error(data.error||'Could not load analytics.');render(data)}catch(e){notice(e.message,true)}}
$('apply-filter').addEventListener('click',()=>{const from=$('from-date').value,to=$('to-date').value;if(from&&to&&from>to){notice('Start date must be before end date.',true);return}const q=new URLSearchParams();if(from)q.set('from',from);if(to)q.set('to',to);if($('category-filter').value)q.set('category',$('category-filter').value);if($('region-filter').value)q.set('region',$('region-filter').value);load(q.toString()?`?${q}`:'')});
$('upload-btn').addEventListener('click',()=>$('file-input').click());
$('file-input').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;const btn=$('upload-btn');btn.disabled=true;btn.innerHTML='<span>…</span> Processing';notice('Validating your data…');try{const fd=new FormData();fd.append('file',file);const res=await fetch('/api/upload',{method:'POST',body:fd});const data=await res.json();if(!res.ok)throw Error(data.error);activeFile=file;render(data);$('dataset-label').textContent=file.name;notice(data.message)}catch(err){notice(err.message,true)}finally{btn.disabled=false;btn.innerHTML='<span>＋</span> Import CSV';e.target.value=''}});
document.querySelectorAll('.kpi-card').forEach(card=>{card.addEventListener('pointermove',event=>{if(event.pointerType==='touch'||matchMedia('(prefers-reduced-motion: reduce)').matches)return;const box=card.getBoundingClientRect(),x=(event.clientX-box.left)/box.width,y=(event.clientY-box.top)/box.height;card.style.setProperty('--tilt-x',`${(x-.5)*5}deg`);card.style.setProperty('--tilt-y',`${(.5-y)*5}deg`);card.classList.add('tilting')});card.addEventListener('pointerleave',()=>{card.style.removeProperty('--tilt-x');card.style.removeProperty('--tilt-y');card.classList.remove('tilting')})});
let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>analyticsData&&drawChart(analyticsData.months),100)});
load();
