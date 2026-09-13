(function(){
  'use strict';
  const norm=s=>(s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const names={user:['users','user management','manage users'],settings:['settings','account settings','account'],ai:['ai corrections','ai correction','learned ocr corrections','ocr corrections']};
  function find(kind){return [...document.querySelectorAll('button,a,[role="tab"],[data-tab],[data-pane],[data-target]')].find(n=>{if(n.closest('#globalAdministration'))return false;const t=norm(n.textContent),a=norm((n.getAttribute('data-tab')||'')+' '+(n.getAttribute('data-pane')||'')+' '+(n.getAttribute('data-target')||'')+' '+(n.id||''));return names[kind].some(x=>t===x||a.includes(x))})}
  function open(kind){const n=find(kind);if(n)n.click()}
  function hide(){document.querySelectorAll('#staffPortal button,#staffPortal a,[role="tab"]').forEach(n=>{if(n.closest('#globalAdministration'))return;const t=norm(n.textContent),a=norm((n.getAttribute('data-tab')||'')+' '+(n.getAttribute('data-pane')||'')+' '+(n.getAttribute('data-target')||'')+' '+(n.id||''));if(Object.values(names).flat().some(x=>t===x||a.includes(x)))n.style.setProperty('display','none','important')})}
  function nav(){
    const broken=document.getElementById('globalUtilityNav');
    if(broken){
      const moved=broken.querySelector('#logoutBtn');
      const host=document.querySelector('.gov-user-panel');
      if(moved&&host){moved.style.display='none';host.appendChild(moved)}
      broken.remove();
    }
    let box=document.getElementById('globalAdministration');
    const host=document.querySelector('.gov-user-panel');
    if(!host)return;
    if(!box){
      box=document.createElement('div');box.id='globalAdministration';box.style.cssText='position:relative;display:inline-flex;align-items:center;margin-right:8px;z-index:100001';
      const b=document.createElement('button');b.type='button';b.className='btn ghost';b.textContent='Administration';b.style.cssText='padding:6px 12px;font-size:12px';
      const m=document.createElement('div');m.className='admin-menu';m.style.cssText='display:none;position:absolute;right:0;top:calc(100% + 6px);min-width:210px;background:#fff;border:1px solid #cbd5e1;border-radius:8px;padding:5px;box-shadow:0 12px 28px rgba(0,0,0,.18);z-index:100002';
      [['User','user'],['Settings','settings'],['AI Corrections','ai']].forEach(([label,k])=>{const x=document.createElement('button');x.type='button';x.className='btn ghost';x.textContent=label;x.style.cssText='display:block;width:100%;text-align:left;border:0;padding:9px 10px;font-size:12px';x.onclick=e=>{e.stopPropagation();m.style.display='none';open(k)};m.appendChild(x)});
      const land=document.createElement('a');land.href='/map';land.className='btn ghost';land.textContent='Land Records Map';land.style.cssText='display:block;width:100%;box-sizing:border-box;text-align:left;border:0;padding:9px 10px;font-size:12px;text-decoration:none';m.appendChild(land);
      const logout=document.getElementById('logoutBtn');
      if(logout){const item=document.createElement('button');item.type='button';item.className='btn ghost';item.textContent='Log out';item.style.cssText='display:block;width:100%;text-align:left;border:0;padding:9px 10px;font-size:12px';item.onclick=e=>{e.stopPropagation();logout.click()};m.appendChild(Object.assign(document.createElement('div'),{style:'border-top:1px solid #e2e8f0;margin:5px 0 0;padding-top:5px'}));m.lastChild.appendChild(item);logout.style.display='none'}
      b.onclick=e=>{e.stopPropagation();m.style.display=m.style.display==='none'?'block':'none'};
      box.append(b,m);host.insertBefore(box,host.firstChild);
    }
  }
  function records(){document.querySelectorAll('#simpleDocTable tbody tr,#staffRecordsTable tbody tr').forEach(r=>{if(r.querySelector('.land-map-file-link'))return;const c=r.querySelectorAll('td');if(c.length<2)return;const id=(c[0].textContent||'').trim().replace(/^#/,'').split(/\s+/)[0];if(!/^\d+$/.test(id))return;const a=document.createElement('a');a.className='btn ghost land-map-file-link';a.href='/map?document_id='+encodeURIComponent(id);a.textContent='Open map';a.style.cssText='display:inline-block;margin-left:6px;padding:5px 9px;font-size:11px;white-space:nowrap;text-decoration:none';c[c.length-1].appendChild(a)})}
  function run(){nav();hide();records()}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',run,{once:true});else run();
  new MutationObserver(run).observe(document.documentElement,{subtree:true,childList:true});
  document.addEventListener('click',e=>{if(!e.target.closest('#globalAdministration')){const m=document.querySelector('#globalAdministration .admin-menu');if(m)m.style.display='none'}},true);
})();
