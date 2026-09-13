(function(){
'use strict';

const UTILITIES={
  users:{label:'Users',tab:'users'},
  settings:{label:'Settings',tab:'account'},
  ai:{label:'AI Corrections',tab:'learn'}
};

const norm=s=>String(s||'').replace(/\s+/g,' ').trim().toLowerCase();
const $=s=>document.querySelector(s);

function hideWorkspaceUtilities(){
  const labels=['users','user management','settings','account settings','ai corrections','ai correction','learned ocr corrections','ocr corrections'];
  document.querySelectorAll('#staffTabsList .tab, #simpleNavItems button, button, a, [role="tab"]').forEach(el=>{
    if(el.closest('#administrationNav')||el.closest('#adminUtilityModal')) return;
    const t=norm(el.textContent);
    const id=norm([el.id,el.dataset?.stab,el.dataset?.spane,el.dataset?.tab,el.dataset?.pane,el.dataset?.target].join(' '));
    if(labels.some(x=>t===x||id.includes(x))) el.style.setProperty('display','none','important');
  });
}

function switchTo(tab){
  if(typeof window.switchStaffTab==='function'){
    window.switchStaffTab(tab);
    return true;
  }
  const target=document.querySelector('[data-stab="'+tab+'"]');
  if(target){target.click();return true;}
  return false;
}

function openUtility(kind){
  const cfg=UTILITIES[kind];
  if(!cfg)return;
  switchTo(cfg.tab);
  setTimeout(()=>{
    const pane=$('#staff-tab-'+cfg.tab);
    const old=$('#adminUtilityModal');
    if(old)old.remove();
    if(!pane){showMessage('Administration feature is unavailable for this account.');return;}

    const parent=pane.parentNode;
    const next=pane.nextSibling;
    const wasHidden=pane.classList.contains('hidden');
    pane.classList.remove('hidden');

    const overlay=document.createElement('div');
    overlay.id='adminUtilityModal';
    overlay.className='admin-modal-backdrop';
    const card=document.createElement('div');
    card.className='admin-modal-card';
    const head=document.createElement('div');
    head.className='admin-modal-head';
    const title=document.createElement('div');
    title.innerHTML='<span class="admin-modal-kicker">ADMINISTRATION</span><h2>'+cfg.label+'</h2>';
    const close=document.createElement('button');
    close.type='button';close.className='btn ghost';close.textContent='Close';
    head.append(title,close);
    card.appendChild(head);
    card.appendChild(pane);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    let closed=false;
    function restore(){
      if(closed)return;closed=true;
      if(next&&next.parentNode===parent)parent.insertBefore(pane,next);else parent.appendChild(pane);
      if(wasHidden)pane.classList.add('hidden');
      overlay.remove();
      hideWorkspaceUtilities();
    }
    close.onclick=restore;
    overlay.onclick=e=>{if(e.target===overlay)restore()};
    document.addEventListener('keydown',function esc(e){if(e.key==='Escape'){restore();document.removeEventListener('keydown',esc)}},{once:true});
  },80);
}

function showMessage(message){
  const old=$('#adminUtilityModal');if(old)old.remove();
  const overlay=document.createElement('div');overlay.id='adminUtilityModal';overlay.className='admin-modal-backdrop';
  overlay.innerHTML='<div class="admin-modal-card admin-message-card"><div class="admin-modal-head"><div><span class="admin-modal-kicker">ADMINISTRATION</span><h2>Unavailable</h2></div><button class="btn ghost" type="button">Close</button></div><p>'+message+'</p></div>';
  document.body.appendChild(overlay);overlay.querySelector('button').onclick=()=>overlay.remove();overlay.onclick=e=>{if(e.target===overlay)overlay.remove()};
}

function build(){
  if($('#administrationNav'))return;
  const userPanel=$('.gov-user-panel');
  if(!userPanel)return;

  const nav=document.createElement('div');
  nav.id='administrationNav';nav.className='administration-nav';
  const toggle=document.createElement('button');
  toggle.type='button';toggle.id='administrationToggle';toggle.className='btn administration-toggle';
  toggle.setAttribute('aria-haspopup','true');toggle.setAttribute('aria-expanded','false');toggle.innerHTML='Administration <span aria-hidden="true">▾</span>';
  const menu=document.createElement('div');menu.id='administrationMenu';menu.className='administration-menu';

  Object.entries(UTILITIES).forEach(([kind,cfg])=>{
    const b=document.createElement('button');b.type='button';b.className='administration-item';b.textContent=cfg.label;
    b.onclick=e=>{e.stopPropagation();menu.classList.remove('open');toggle.setAttribute('aria-expanded','false');openUtility(kind)};
    menu.appendChild(b);
  });
  const divider=document.createElement('div');divider.className='administration-divider';menu.appendChild(divider);
  const land=document.createElement('a');land.className='administration-item';land.href='/land-intelligence';land.textContent='Land Intelligence';menu.appendChild(land);
  const logout=document.createElement('button');logout.type='button';logout.className='administration-item';logout.textContent='Log out';
  logout.onclick=()=>{const real=$('#logoutBtn');if(real)real.click();else location.href='/'};menu.appendChild(logout);

  nav.append(toggle,menu);
  userPanel.insertBefore(nav,userPanel.firstChild);
  toggle.onclick=e=>{e.stopPropagation();const open=menu.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open))};
  document.addEventListener('click',e=>{if(!nav.contains(e.target)){menu.classList.remove('open');toggle.setAttribute('aria-expanded','false')}});
}

function run(){
  if(typeof window.me!=='undefined' && window.me && window.me.role && window.me.role!=='ADMIN')return;
  build();hideWorkspaceUtilities();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',run,{once:true});else run();
new MutationObserver(()=>{build();hideWorkspaceUtilities()}).observe(document.documentElement,{subtree:true,childList:true});
})();
