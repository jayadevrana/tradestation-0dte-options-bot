"""Public educational page (/visualize) — animated demo of every protection feature.

No login required: it contains NO account data and NO controls; it only demonstrates how the
exit logic works (stop, trim+break-even, trailing, take-profit, daily trailing $, daily loss).
The demo executes the same trigger rules the bot runs live, tick by tick.
"""

VISUALIZE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>How the bot protects your trades</title>
<style>
:root{--bg:#0d1117;--surface-2:#161b22;--border:#30363d;--border-strong:#8b949e;
--text-primary:#e6edf3;--text-secondary:#8b949e;--radius:8px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text-primary);
font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:22px 16px 40px}
h1{font-size:19px;margin:0 0 2px}
.sub{color:var(--text-secondary);font-size:12.5px;margin-bottom:16px}
button{background:var(--surface-2);color:var(--text-primary);border:1px solid var(--border);
border-radius:8px;padding:7px 12px;font-size:12.5px;cursor:pointer}
button:hover{border-color:var(--border-strong)}
.foot{color:var(--text-secondary);font-size:11px;margin-top:26px;text-align:center}
</style></head><body><div class="wrap">
<h1>How the bot protects your trades</h1>
<div class="sub">Pick a feature — the chart runs the exact trigger rules the bot uses live, tick by tick.</div>
<div style="font-family:inherit;color:var(--text-primary)">
  <div id="tabs" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px"></div>
  <div id="caption" style="font-size:12.5px;color:var(--text-secondary);margin-bottom:10px;min-height:32px"></div>

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:10px">
    <div style="border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;background:var(--surface-2)">
      <div style="font-size:11px;color:var(--text-secondary)">Position</div>
      <div id="chipPos" style="font-size:14px;font-weight:600">&#8212;</div>
    </div>
    <div style="border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;background:var(--surface-2)">
      <div style="font-size:11px;color:var(--text-secondary)" id="chipPnlLbl">P&amp;L</div>
      <div id="chipPnl" style="font-size:14px;font-weight:600">&#8212;</div>
    </div>
    <div style="border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;background:var(--surface-2)">
      <div style="font-size:11px;color:var(--text-secondary)">Peak</div>
      <div id="chipPeak" style="font-size:14px;font-weight:600">&#8212;</div>
    </div>
    <div style="border:1px solid var(--border);border-radius:var(--radius);padding:8px 10px;background:var(--surface-2)">
      <div style="font-size:11px;color:var(--text-secondary)">Active protection</div>
      <div id="chipProt" style="font-size:13px;font-weight:600">&#8212;</div>
    </div>
  </div>

  <div style="border:1px solid var(--border);border-radius:12px;background:var(--surface-2);padding:10px 10px 4px">
    <svg id="cv" viewBox="0 0 656 264" width="100%" role="img" aria-label="Price chart animating the selected protection scenario"></svg>
    <div id="legend" style="display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--text-secondary);padding:2px 4px 8px"></div>
  </div>

  <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
    <button id="replay">&#8635; Replay</button>
    <div style="font-size:11.5px;color:var(--text-secondary)">Same rules the bot runs live &#8212; the demo executes the actual trigger logic tick by tick.</div>
  </div>

  <div id="log" style="margin-top:10px;display:flex;flex-direction:column;gap:5px"></div>
</div>

<script>
(function(){
const COL={line:'#7F77DD',stop:'#D9544D',good:'#2E9E63',trail:'#D08700',be:'#159E8C',dim:'rgba(127,119,221,0.28)'};
const $=function(id){return document.getElementById(id);};

function wig(i,a){return a*Math.sin(i*1.7)+a*0.6*Math.sin(i*0.53+2);}
function mkPath(wp,amp){
  const out=[];
  for(let i=0;i<=120;i++){
    let j=0; while(j<wp.length-2 && wp[j+1][0]<i) j++;
    const t0=wp[j][0],v0=wp[j][1],t1=wp[j+1][0],v1=wp[j+1][1];
    const f=Math.min(1,Math.max(0,(i-t0)/Math.max(1,(t1-t0))));
    const s=f*f*(3-2*f);
    out.push(v0+(v1-v0)*s+wig(i,amp));
  }
  return out;
}

const S={
 stop:{
  name:'Stop-loss', unit:'px', dom:[0.62,1.15], entry:1.0, qty:4,
  cap:'Fixed protection: a broker-held stop at -20% (entry $1.00, stop $0.80). If the option falls there, it sells everything instantly - worst case is capped, even if the bot/server is offline.',
  wp:[[0,1.00],[15,1.06],[35,0.97],[55,1.02],[75,0.90],[95,0.82],[104,0.79],[120,0.70]], amp:0.006,
  levels:function(s){return [{v:0.80,c:COL.stop,l:'stop -20%'}];},
  legend:[['#D9544D','broker stop']],
  logic:function(st,p,i,ev){ if(!st.exited&&p<=0.80){ev('bad','Broker stop fired - sold 4 contracts @ $0.80 (-20%)');st.exited=i;st.qty=0;} st.prot='stop $0.80';}
 },
 trim:{
  name:'Trim + Break-even', unit:'px', dom:[0.85,1.48], entry:1.0, qty:4,
  cap:'Rise +30%: sell HALF (banks profit). Once up +20%, the stop moves to your entry price ("break-even") - the rest of the trade can no longer lose. Here the price fades back: the remainder exits at entry, total trade still positive.',
  wp:[[0,1.00],[20,1.12],[40,1.34],[55,1.30],[70,1.18],[90,1.06],[105,0.99],[120,0.95]], amp:0.007,
  levels:function(s){const L=[{v:1.30,c:COL.good,l:'trim +30%'}];L.push(s.beArmed?{v:1.00,c:COL.be,l:'stop at entry (BE)'}:{v:0.80,c:COL.stop,l:'stop -20%'});return L;},
  legend:[['#2E9E63','trim trigger'],['#159E8C','break-even stop'],['#D9544D','initial stop']],
  logic:function(st,p,i,ev){
    if(!st.beArmed&&st.peak>=1.20){st.beArmed=true;ev('info','Up +20%: break-even armed - stop moved from $0.80 to entry $1.00');}
    if(!st.trimmed&&p>=1.30){st.trimmed=true;st.qty=2;ev('good','Trim: +30% hit - sold 2 of 4 @ $1.30 (profit banked)');}
    if(!st.exited&&st.beArmed&&p<=1.00&&st.trimmed){st.exited=i;st.qty=0;ev('info','Price back at entry: break-even exit - sold last 2 @ $1.00. Trade net +$60, zero given back');}
    st.prot=st.beArmed?'stop @ entry $1.00':'stop $0.80';}
 },
 trail:{
  name:'Trailing stop', unit:'px', dom:[0.7,1.95], entry:1.0, qty:4,
  cap:'Rides winners: once up +30% the trail activates, an exit line follows 20% below the highest price seen and only ratchets UP. Price peaks +82%, pulls back 20%: sells at about +46%. Captures big moves a fixed +20% target would cap.',
  wp:[[0,1.00],[20,1.18],[40,1.42],[60,1.66],[75,1.82],[85,1.74],[95,1.56],[110,1.40],[120,1.30]], amp:0.01,
  levels:function(s){const L=[{v:0.80,c:COL.stop,l:'stop -20%'}];if(s.trailOn)L.push({v:s.trailLine,c:COL.trail,l:'trail (peak -20%)'});return L;},
  legend:[['#D08700','trailing line (ratchets up)'],['#D9544D','broker stop']],
  logic:function(st,p,i,ev){
    if(!st.trailOn&&p>=1.30){st.trailOn=true;ev('info','+30% reached: trailing stop ACTIVATED');}
    if(st.trailOn){st.trailLine=Math.max(st.trailLine||0,st.peak*0.8);}
    if(st.trailOn&&!st.peakNoted&&i>76){st.peakNoted=true;ev('info','Peak $'+st.peak.toFixed(2)+' (+'+Math.round((st.peak-1)*100)+'%) - trail line now $'+(st.peak*0.8).toFixed(2));}
    if(!st.exited&&st.trailOn&&p<=st.trailLine){st.exited=i;st.qty=0;ev('good','Trailing exit: sold 4 @ $'+p.toFixed(2)+' (+'+Math.round((p-1)*100)+'%) - rode the move, gave back only 20% of peak');}
    st.prot=st.trailOn?('trail $'+(st.trailLine||0).toFixed(2)):'stop $0.80';}
 },
 tp:{
  name:'Take-profit', unit:'px', dom:[0.85,1.9], entry:1.0, qty:4,
  cap:'A fixed target: a resting sell limit at +20% sits at the broker from entry. It fills the INSTANT price touches $1.20 - it cannot ride higher. The later spike happens after you are already out. Simple and consistent; opposite style to trailing.',
  wp:[[0,1.00],[15,1.05],[30,1.12],[45,1.22],[60,1.55],[70,1.78],[85,1.5],[100,1.3],[120,1.22]], amp:0.007,
  levels:function(s){return [{v:1.20,c:COL.good,l:'take-profit +20%'},{v:0.80,c:COL.stop,l:'stop -20%'}];},
  legend:[['#2E9E63','resting TP limit'],['#D9544D','broker stop']],
  logic:function(st,p,i,ev){
    if(!st.exited&&p>=1.20){st.exited=i;st.qty=0;ev('good','Take-profit limit FILLED - sold 4 @ $1.20 (+20%). Target banked');}
    if(st.exited&&!st.spikeNoted&&i>70){st.spikeNoted=true;ev('info','Later spike to +78% happens after exit - a fixed target never chases it (use trailing to ride moves)');}
    st.prot='TP $1.20 / stop $0.80';}
 },
 dtrail:{
  name:'Daily trailing $', unit:'$', dom:[-120,420], entry:0, qty:'day',
  cap:'Account-level profit lock: the bot tracks the DAY peak P&L and if it falls $150 from that peak, it closes everything and stops trading for the day. Peak +$300: halts at +$150 - half the day best is locked in, guaranteed.',
  wp:[[0,0],[15,80],[30,190],[45,300],[60,255],[75,215],[90,165],[100,146],[120,60]], amp:3,
  levels:function(s){const L=[];if(s.trailLine!==undefined)L.push({v:s.trailLine,c:COL.trail,l:'halt line (peak -$150)'});return L;},
  legend:[['#D08700','halt line (ratchets up with peak)']],
  logic:function(st,p,i,ev){
    st.trailLine=Math.max(st.trailLine===undefined?-150:st.trailLine,st.peak-150);
    if(!st.peakNoted&&i>46){st.peakNoted=true;ev('info','Day peak P&L $'+Math.round(st.peak)+' - halt line ratchets to $'+Math.round(st.peak-150));}
    if(!st.exited&&p<=st.trailLine){st.exited=i;ev('good','Fell $150 from peak: FLATTEN ALL + no more trades today. Day locked in at +$'+Math.round(p));}
    st.prot='halt @ $'+Math.round(st.trailLine);}
 },
 dloss:{
  name:'Daily loss limit', unit:'$', dom:[-320,120], entry:0, qty:'day',
  cap:'The hard floor: if the day P&L hits -$200 (you set the number), the kill-switch cancels every order, sells every position at market and halts until tomorrow. This fired for real on both live accounts - losses stopped within about $2 of the setting.',
  wp:[[0,0],[15,-30],[30,20],[45,-60],[60,-140],[75,-95],[90,-165],[105,-215],[120,-270]], amp:4,
  levels:function(s){return [{v:-200,c:COL.stop,l:'daily loss -$200'}];},
  legend:[['#D9544D','daily loss limit']],
  logic:function(st,p,i,ev){
    if(!st.exited&&p<=-200){st.exited=i;ev('bad','Day P&L hit -$200: KILL-SWITCH - flatten everything + halt for the day');}
    st.prot='kill-switch @ -$200';}
 }
};

const order=['stop','trim','trail','tp','dtrail','dloss'];
let cur='trail', anim=null;

order.forEach(function(k){
  const b=document.createElement('button');
  b.textContent=S[k].name; b.id='tab_'+k;
  b.onclick=function(){cur=k;run();};
  $('tabs').appendChild(b);
});

const W=656,H=264,PL=44,PR=104,PT=14,PB=24;
function sx(i){return PL+(W-PL-PR)*i/120;}
function makeSy(dom){return function(v){return PT+(H-PT-PB)*(1-(v-dom[0])/(dom[1]-dom[0]));};}
function fmt(v,unit){return unit==='$'?('$'+Math.round(v)):('$'+v.toFixed(2));}

function run(){
  if(anim)cancelAnimationFrame(anim);
  const sc=S[cur];
  order.forEach(function(k){const b=$('tab_'+k);b.style.outline=k===cur?'2px solid '+COL.line:'none';});
  $('caption').textContent=sc.cap;
  $('legend').innerHTML=[['#7F77DD','price / P&L']].concat(sc.legend).map(function(x){return '<span><span style="display:inline-block;width:14px;height:3px;background:'+x[0]+';border-radius:2px;vertical-align:middle;margin-right:5px"></span>'+x[1]+'</span>';}).join('');
  $('log').innerHTML='';
  $('chipPnlLbl').textContent=sc.unit==='$'?'Day P&L':'Trade P&L';

  const path=mkPath(sc.wp,sc.amp);
  const sy=makeSy(sc.dom);
  const st={peak:sc.entry===0?0:sc.entry,qty:sc.qty,exited:0,prot:''};
  const svg=$('cv');
  let grid='';
  const ticks=4;
  for(let g=0;g<=ticks;g++){
    const v=sc.dom[0]+(sc.dom[1]-sc.dom[0])*g/ticks, y=sy(v);
    grid+='<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="var(--border)" stroke-width="0.5"/>'
        +'<text x="'+(PL-6)+'" y="'+(y+3.5)+'" text-anchor="end" font-size="10" fill="var(--text-secondary)">'+fmt(v,sc.unit)+'</text>';
  }
  const xl=sc.unit==='$'?['9:30','12:45','16:00']:['entry','+45 min','+90 min'];
  grid+=xl.map(function(t,j){return '<text x="'+sx(j*60)+'" y="'+(H-8)+'" text-anchor="middle" font-size="10" fill="var(--text-secondary)">'+t+'</text>';}).join('');
  if(sc.unit!=='$') grid+='<line x1="'+PL+'" y1="'+sy(1)+'" x2="'+(W-PR)+'" y2="'+sy(1)+'" stroke="var(--border-strong)" stroke-width="0.5" stroke-dasharray="2 3"/><text x="'+(W-PR+6)+'" y="'+(sy(1)+3.5)+'" font-size="10" fill="var(--text-secondary)">entry $1.00</text>';
  else grid+='<line x1="'+PL+'" y1="'+sy(0)+'" x2="'+(W-PR)+'" y2="'+sy(0)+'" stroke="var(--border-strong)" stroke-width="0.5" stroke-dasharray="2 3"/><text x="'+(W-PR+6)+'" y="'+(sy(0)+3.5)+'" font-size="10" fill="var(--text-secondary)">$0</text>';

  svg.innerHTML=grid+'<g id="lv"></g><path id="p1" fill="none" stroke="'+COL.line+'" stroke-width="2"/><path id="p2" fill="none" stroke="'+COL.dim+'" stroke-width="2"/><g id="mk"></g><circle id="dot" r="3.5" fill="'+COL.line+'"/>';

  const ev=function(kind,text){
    const c=kind==='bad'?COL.stop:kind==='good'?COL.good:COL.be;
    const row=document.createElement('div');
    row.style.cssText='display:flex;gap:8px;align-items:flex-start;font-size:12.5px;border:1px solid var(--border);border-radius:var(--radius);padding:7px 10px;background:var(--surface-2)';
    row.innerHTML='<span style="width:8px;height:8px;border-radius:50%;background:'+c+';margin-top:4px;flex:none"></span><span>'+text+'</span>';
    $('log').prepend(row);
    st._markAt=true; st._markColor=c;
  };

  let i=0,last=0;
  function frame(ts){
    if(ts-last<45){anim=requestAnimationFrame(frame);return;}
    last=ts;
    const p=path[i];
    st.peak=Math.max(st.peak,p);
    st._markAt=false;
    sc.logic(st,p,i,ev);
    const cut=st.exited||i;
    const seg=function(a,b){return path.slice(a,b+1).map(function(v,j){return (j?'L':'M')+sx(a+j).toFixed(1)+' '+sy(v).toFixed(1);}).join('');};
    document.getElementById('p1').setAttribute('d',seg(0,Math.min(i,cut)));
    document.getElementById('p2').setAttribute('d',st.exited&&i>st.exited?seg(st.exited,i):'');
    document.getElementById('lv').innerHTML=sc.levels(st).map(function(L){
      const y=sy(L.v);
      return '<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="'+L.c+'" stroke-width="1.2" stroke-dasharray="5 4"/><text x="'+(W-PR+6)+'" y="'+(y+3.5)+'" font-size="10" fill="'+L.c+'">'+L.l+'</text>';
    }).join('');
    if(st._markAt){
      document.getElementById('mk').innerHTML+='<circle cx="'+sx(i)+'" cy="'+sy(p)+'" r="5" fill="none" stroke="'+st._markColor+'" stroke-width="2"/>';
    }
    const dot=document.getElementById('dot');
    dot.setAttribute('cx',sx(i)); dot.setAttribute('cy',sy(p));
    dot.setAttribute('fill',st.exited&&i>=st.exited?COL.dim:COL.line);
    $('chipPos').textContent=sc.qty==='day'?(st.exited?'HALTED for the day':'trading'):(st.exited?'flat (closed)':st.qty+' contracts');
    $('chipPnl').textContent=sc.unit==='$'?('$'+Math.round(st.exited?path[st.exited]:p)):((p>=1?'+':'')+Math.round((p-1)*100)+'%');
    $('chipPeak').textContent=sc.unit==='$'?('$'+Math.round(st.peak)):('+'+Math.round((st.peak-1)*100)+'%');
    $('chipProt').textContent=st.prot;
    i++;
    if(i<=120)anim=requestAnimationFrame(frame);
  }
  anim=requestAnimationFrame(frame);
}
$('replay').onclick=run;
run();
})();
</script>
<div class="foot">Educational demo — simulated prices, no live account data.</div>
</div></body></html>"""
