'use strict';
let token = '', timer = null, pending = false, generation = 0;
const $ = id => document.getElementById(id);
const labels = {current:'ข้อมูลปัจจุบัน',error:'พบข้อผิดพลาด',locked:'ล็อกความเสี่ยง',stale:'ข้อมูลเก่า',unavailable:'ไม่มีข้อมูล',unknown:'ยังยืนยันไม่ได้',synthetic:'ข้อมูลสังเคราะห์',not_connected:'ยังไม่เชื่อม',not_verified:'ยังไม่ยืนยัน',active:'ทำงานอยู่',stopped:'หยุดอยู่'};
const details = {scanner:['🐱','อ่านตลาดและคัดกรองคู่เทรด'],strategy:['🐰','วิเคราะห์สัญญาณจากกลยุทธ์'],risk:['🐻','ตรวจขนาดสถานะและ risk lock'],execution:['🐶','จำลองการเข้าออกสถานะ'],news:['🦉','ยังไม่ได้เชื่อมแหล่งข่าว'],testnet:['🐼','ยังไม่มีหลักฐานการเชื่อมบัญชี Testnet']};
const date = n => n ? new Date(n).toLocaleString('th-TH',{timeZone:'Asia/Bangkok',hour12:false}) : '—';
const fmt = n => typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString('en-US',{maximumFractionDigits:2}) : '—';
function node(tag, text, cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;}
function notice(text,good=false){$('connection').textContent=text;$('connection').className='notice '+(good?'good':'bad');}
function render(data){
 const l=data.ledger||{}, m=l.metrics||{};
 $('updated').textContent='ตรวจล่าสุด '+date(data.checked_at_ms)+' · เวลาไทย';
 notice(data.healthy?'VPS และข้อมูลบอทปัจจุบัน · โหมดจำลอง Paper':!data.monitor_fresh?'ตัวตรวจไม่ได้อัปเดต — ข้อมูลด้านล่างอาจเก่า':'ต้องตรวจสอบ · '+(labels[l.status]||'ยังยืนยันสถานะไม่ได้'),data.healthy);
 $('equity').textContent=fmt(m.equity);$('pnl').textContent=fmt(m.pnl);$('trades').textContent=fmt(m.closed_trades);$('winrate').textContent=typeof m.win_rate==='number'?fmt(m.win_rate*100)+'%':'—';
 $('agents').replaceChildren();
 for(const a of data.agents||[]){const [avatar,desc]=details[a.id]||['🤖',''];const state=data.monitor_fresh?a.status:'stale';const card=node('article',undefined,'agent '+(state==='current'?'working':''));const desk=node('div',undefined,'desk');desk.setAttribute('aria-hidden','true');desk.append(node('span',avatar,'avatar'),node('span','💻','laptop'));const info=node('div',undefined,'agent-info');const top=node('div',undefined,'agent-top');top.append(node('h3',a.name),node('span',labels[state]||state,'badge '+state));info.append(top,node('p',desc));card.append(desk,info);$('agents').append(card);}
 const rows=[['Paper service',labels[data.services?.['gptsalov-paper.service']?.status]||'—'],['Forward service',labels[data.services?.['gptsalov-forward.service']?.status]||'—'],['โหมดที่อ่านได้',l.mode==='paper'?'Paper simulator':'ยังไม่มีข้อมูล'],['อัปเดตจากบอท',date(l.observed_at_ms)],['ข้อมูลแท่งราคา',date(l.as_of_ms)],['สถานะจำลองที่ถือ',l.position_symbol||'—'],['Risk lock',l.locked===undefined?'—':l.locked?'ล็อกอยู่':'ไม่ล็อก'],['Binance Testnet','ยังไม่ยืนยัน'],['News / AI','ยังไม่เชื่อม']];
 $('health').replaceChildren();for(const [k,v]of rows){const row=node('div');row.append(node('dt',k),node('dd',v));$('health').append(row);}
 $('events').replaceChildren();for(const e of l.events||[]){const row=node('li');row.append(node('span',e.kind),node('time',date(e.timestamp_ms)));$('events').append(row);}if(!l.events?.length)$('events').append(node('li','ยังไม่มีบันทึกให้แสดง'));
}
async function refresh(){
 if(pending||!token)return;pending=true;const gen=generation;$('refresh').disabled=true;
 try{const response=await fetch('/api/status',{headers:{Authorization:'Bearer '+token},cache:'no-store',signal:AbortSignal.timeout(10000)});if(response.status===401)throw new Error('AUTH');if(!response.ok)throw new Error('NETWORK');const data=await response.json();if(data.schema!==1||!Array.isArray(data.agents))throw new Error('SCHEMA');if(gen!==generation)return;$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;$('login-error').textContent='';render(data);}
 catch(e){if(gen!==generation)return;if(e.message==='AUTH'){logout();$('login-error').textContent='รหัสไม่ถูกต้อง หรือถูกเปลี่ยนแล้ว';}else{$('login-error').textContent='เชื่อมต่อไม่ได้ กรุณาลองอีกครั้ง';notice('ขาดการเชื่อมต่อ — สถานะที่แสดงเป็นข้อมูลครั้งก่อน');document.querySelectorAll('.working').forEach(el=>el.classList.remove('working'));document.querySelectorAll('.badge.current').forEach(el=>{el.textContent='ขาดการเชื่อมต่อ';el.className='badge stale';});}}
 finally{pending=false;$('refresh').disabled=false;}
}
function logout(){generation++;token='';clearInterval(timer);timer=null;$('workspace').hidden=true;$('login').hidden=false;$('logout').hidden=true;$('token').value='';$('agents').replaceChildren();}
$('login-form').addEventListener('submit',e=>{e.preventDefault();token=$('token').value.trim();$('token').value='';clearInterval(timer);refresh();timer=setInterval(refresh,30000);});$('refresh').addEventListener('click',refresh);$('logout').addEventListener('click',logout);
