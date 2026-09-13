(function () {
  'use strict';

  const $ = (s, root = document) => root.querySelector(s);

  function installStyles() {
    if ($('#administration-nav-styles')) return;
    const style = document.createElement('style');
    style.id = 'administration-nav-styles';
    style.textContent = `
      #administrationNav { position:relative!important;display:inline-flex!important;align-items:center!important;margin-right:10px!important;z-index:10000!important; }
      #administrationToggle { display:inline-flex!important;align-items:center!important;gap:7px!important;min-height:34px!important;padding:7px 13px!important;border:1px solid rgba(255,255,255,.55)!important;border-radius:6px!important;background:#fff!important;color:#0b2d4d!important;font-weight:800!important;font-size:12px!important;line-height:1!important;cursor:pointer!important;box-shadow:0 2px 8px rgba(0,0,0,.16)!important;pointer-events:auto!important; }
      #administrationMenu { position:absolute!important;top:calc(100% + 8px)!important;right:0!important;width:230px!important;padding:7px!important;border:1px solid #cbd5e1!important;border-radius:9px!important;background:#fff!important;box-shadow:0 18px 45px rgba(15,23,42,.25)!important;display:none!important;z-index:10001!important;pointer-events:auto!important; }
      #administrationMenu.open { display:block!important; }
      #administrationMenu .administration-item { display:block!important;width:100%!important;box-sizing:border-box!important;margin:0!important;padding:10px 11px!important;border:0!important;border-radius:6px!important;background:#fff!important;color:#16324f!important;text-align:left!important;font:600 13px/1.2 Arial,sans-serif!important;text-decoration:none!important;cursor:pointer!important;pointer-events:auto!important; }
      #administrationMenu .administration-item:hover { background:#eef5fb!important; }
      #administrationMenu .administration-divider { height:1px;background:#e2e8f0;margin:6px 2px; }
      #adminUtilityModal.admin-modal-backdrop { position:fixed!important;inset:0!important;z-index:20000!important;display:flex!important;align-items:flex-start!important;justify-content:center!important;padding:70px 18px 18px!important;background:rgba(2,20,36,.58)!important;box-sizing:border-box!important; }
      #adminUtilityModal .admin-modal-card { width:min(1050px,96vw)!important;max-height:86vh!important;overflow:auto!important;background:#fff!important;border:1px solid #cbd5e1!important;border-radius:12px!important;box-shadow:0 24px 70px rgba(0,0,0,.32)!important;padding:18px!important; }
      #adminUtilityModal .admin-modal-head { display:flex!important;align-items:center!important;justify-content:space-between!important;gap:16px!important;margin-bottom:14px!important; }
      #adminUtilityModal .admin-modal-kicker { display:block!important;color:#d97706!important;font-size:10px!important;font-weight:800!important;letter-spacing:1.2px!important; }
      #adminUtilityModal h2 { margin:2px 0 0!important;color:#0b2d4d!important; }
      body.admin-modal-open { overflow:hidden!important; }
    `;
    document.head.appendChild(style);
  }

  function showModal(title, contentNode) {
    const old = $('#adminUtilityModal');
    if (old) old.remove();
    const overlay = document.createElement('div');
    overlay.id = 'adminUtilityModal'; overlay.className = 'admin-modal-backdrop';
    const card = document.createElement('div'); card.className = 'admin-modal-card';
    const head = document.createElement('div'); head.className = 'admin-modal-head';
    const heading = document.createElement('div'); heading.innerHTML = '<span class="admin-modal-kicker">ADMINISTRATION</span><h2>' + title + '</h2>';
    const close = document.createElement('button'); close.type='button'; close.className='btn ghost'; close.textContent='Close';
    head.append(heading, close); card.append(head, contentNode); overlay.appendChild(card); document.body.appendChild(overlay); document.body.classList.add('admin-modal-open');
    const destroy=()=>{overlay.remove();document.body.classList.remove('admin-modal-open');};
    close.onclick=destroy; overlay.onclick=e=>{if(e.target===overlay)destroy();};
  }

  function openExistingPanel(tab) {
    if (typeof window.switchStaffTab === 'function') {
      try { window.switchStaffTab(tab); } catch (_) {}
    }
    setTimeout(() => {
      const pane = $('#staff-tab-' + tab) || $('[data-staff-tab="' + tab + '"]') || $('[data-tab="' + tab + '"]');
      if (!pane) {
        const p=document.createElement('p'); p.textContent='This Administration workspace is not available in the current session.';
        showModal(tab==='learn'?'AI Corrections':tab==='account'?'Settings':'Users',p); return;
      }
      const parent=pane.parentNode, next=pane.nextSibling, wasHidden=pane.classList.contains('hidden'), oldStyle=pane.getAttribute('style');
      pane.classList.remove('hidden');
      const overlay=document.createElement('div'); overlay.id='adminUtilityModal'; overlay.className='admin-modal-backdrop';
      const card=document.createElement('div'); card.className='admin-modal-card';
      const head=document.createElement('div'); head.className='admin-modal-head';
      const heading=document.createElement('div'); const label=tab==='learn'?'AI Corrections':tab==='account'?'Settings':'Users'; heading.innerHTML='<span class="admin-modal-kicker">ADMINISTRATION</span><h2>'+label+'</h2>';
      const close=document.createElement('button'); close.type='button'; close.className='btn ghost'; close.textContent='Close'; head.append(heading,close); card.append(head,pane); overlay.appendChild(card); document.body.appendChild(overlay); document.body.classList.add('admin-modal-open');
      let done=false;
      const restore=()=>{if(done)return;done=true;if(next&&next.parentNode===parent)parent.insertBefore(pane,next);else parent.appendChild(pane);if(wasHidden)pane.classList.add('hidden');if(oldStyle===null)pane.removeAttribute('style');else pane.setAttribute('style',oldStyle);overlay.remove();document.body.classList.remove('admin-modal-open');};
      close.onclick=restore; overlay.onclick=e=>{if(e.target===overlay)restore();};
    },60);
  }

  function bind(nav) {
    const toggle=$('#administrationToggle',nav), menu=$('#administrationMenu',nav);
    if(!toggle||!menu||toggle.dataset.bound==='1') return;
    toggle.dataset.bound='1';
    toggle.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();const open=menu.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));});
    nav.addEventListener('click',e=>e.stopPropagation());
    document.addEventListener('click',()=>{menu.classList.remove('open');toggle.setAttribute('aria-expanded','false');});

    const users=nav.querySelector('[data-admin-action="users"]');
    const settings=nav.querySelector('[data-admin-action="settings"]');
    const ai=nav.querySelector('[data-admin-action="ai"]');
    const logout=nav.querySelector('[data-admin-action="logout"]');
    if(users) users.onclick=()=>{menu.classList.remove('open');openExistingPanel('users');};
    if(settings) settings.onclick=()=>{menu.classList.remove('open');openExistingPanel('account');};
    if(ai) ai.onclick=()=>{menu.classList.remove('open');openExistingPanel('learn');};
    if(logout) logout.onclick=()=>{const real=$('#logoutBtn');if(real)real.click();else location.href='/';};
  }

  function build() {
    installStyles();
    const panel=$('.gov-user-panel') || $('.gov-header .gov-user-panel') || $('[class*="gov-user-panel"]');
    if(!panel)return false;
    let nav=$('#administrationNav');
    if(!nav){
      nav=document.createElement('div'); nav.id='administrationNav'; nav.className='administration-nav';
      const toggle=document.createElement('button'); toggle.id='administrationToggle'; toggle.type='button'; toggle.setAttribute('aria-haspopup','true'); toggle.setAttribute('aria-expanded','false'); toggle.innerHTML='Administration <span aria-hidden="true">&#9660;</span>';
      const menu=document.createElement('div'); menu.id='administrationMenu';
      const add=(label,action)=>{const b=document.createElement('button');b.type='button';b.className='administration-item';b.textContent=label;b.dataset.adminAction=action;menu.appendChild(b);};
      add('Users','users'); add('Settings','settings'); add('AI Corrections','ai');
      const divider=document.createElement('div');divider.className='administration-divider';menu.appendChild(divider);
      const land=document.createElement('a');land.className='administration-item';land.href='/land-intelligence';land.textContent='Land Intelligence';menu.appendChild(land);
      add('Log out','logout');
      nav.append(toggle,menu); panel.insertBefore(nav,panel.firstChild);
    }
    bind(nav);
    nav.style.setProperty('display','inline-flex','important');nav.style.setProperty('visibility','visible','important');nav.style.setProperty('opacity','1','important');
    return true;
  }

  function run(){installStyles();build();}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',run,{once:true});else run();
  const observer=new MutationObserver(()=>{if(!$('#administrationNav'))build();else bind($('#administrationNav'));});
  observer.observe(document.documentElement,{subtree:true,childList:true});
  setTimeout(run,250);setTimeout(run,1000);setTimeout(run,2500);
})();
