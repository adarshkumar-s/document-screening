(function () {
  'use strict';

  // Administration is deliberately rendered by this file instead of depending
  // on the staff workspace navigation. This makes the entry point reliable even
  // when the workspace is rebuilt after login.
  const $ = (s, root = document) => root.querySelector(s);
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();

  function installStyles() {
    if ($('#administration-nav-styles')) return;
    const style = document.createElement('style');
    style.id = 'administration-nav-styles';
    style.textContent = `
      #administrationNav {
        position: relative !important;
        display: inline-flex !important;
        align-items: center !important;
        margin-right: 10px !important;
        z-index: 10000 !important;
      }
      #administrationToggle {
        display: inline-flex !important;
        align-items: center !important;
        gap: 7px !important;
        min-height: 34px !important;
        padding: 7px 13px !important;
        border: 1px solid rgba(255,255,255,.55) !important;
        border-radius: 6px !important;
        background: #ffffff !important;
        color: #0b2d4d !important;
        font-weight: 800 !important;
        font-size: 12px !important;
        line-height: 1 !important;
        cursor: pointer !important;
        box-shadow: 0 2px 8px rgba(0,0,0,.16) !important;
        visibility: visible !important;
        opacity: 1 !important;
      }
      #administrationToggle:hover { background:#f8fafc !important; transform:translateY(-1px); }
      #administrationToggle .admin-chevron { font-size:11px; }
      #administrationMenu {
        position: absolute !important;
        top: calc(100% + 8px) !important;
        right: 0 !important;
        width: 230px !important;
        padding: 7px !important;
        border: 1px solid #cbd5e1 !important;
        border-radius: 9px !important;
        background: #ffffff !important;
        box-shadow: 0 18px 45px rgba(15,23,42,.25) !important;
        display: none !important;
        z-index: 10001 !important;
      }
      #administrationMenu.open { display:block !important; }
      #administrationMenu .administration-item {
        display:block !important;
        width:100% !important;
        box-sizing:border-box !important;
        margin:0 !important;
        padding:10px 11px !important;
        border:0 !important;
        border-radius:6px !important;
        background:#fff !important;
        color:#16324f !important;
        text-align:left !important;
        font:600 13px/1.2 Arial,sans-serif !important;
        text-decoration:none !important;
        cursor:pointer !important;
      }
      #administrationMenu .administration-item:hover { background:#eef5fb !important; }
      #administrationMenu .administration-divider { height:1px; background:#e2e8f0; margin:6px 2px; }
      #adminUtilityModal.admin-modal-backdrop {
        position:fixed !important; inset:0 !important; z-index:20000 !important;
        display:flex !important; align-items:flex-start !important; justify-content:center !important;
        padding:70px 18px 18px !important; background:rgba(2,20,36,.58) !important;
        box-sizing:border-box !important;
      }
      #adminUtilityModal .admin-modal-card {
        width:min(1050px,96vw) !important; max-height:86vh !important; overflow:auto !important;
        background:#fff !important; border:1px solid #cbd5e1 !important; border-radius:12px !important;
        box-shadow:0 24px 70px rgba(0,0,0,.32) !important; padding:18px !important;
      }
      #adminUtilityModal .admin-modal-head { display:flex !important; align-items:center !important; justify-content:space-between !important; gap:16px !important; margin-bottom:14px !important; }
      #adminUtilityModal .admin-modal-kicker { display:block !important; color:#d97706 !important; font-size:10px !important; font-weight:800 !important; letter-spacing:1.2px !important; }
      #adminUtilityModal h2 { margin:2px 0 0 !important; color:#0b2d4d !important; }
      body.admin-modal-open { overflow:hidden !important; }
      @media (max-width:700px) {
        #administrationNav { margin-right:5px !important; }
        #administrationToggle { padding:7px 9px !important; }
        #administrationMenu { right:auto !important; left:0 !important; }
      }
    `;
    document.head.appendChild(style);
  }

  function getUserPanel() {
    return $('.gov-user-panel') || $('.gov-header .gov-user-panel') || $('[class*="gov-user-panel"]');
  }

  function showModal(title, contentNode) {
    const old = $('#adminUtilityModal');
    if (old) old.remove();

    const overlay = document.createElement('div');
    overlay.id = 'adminUtilityModal';
    overlay.className = 'admin-modal-backdrop';

    const card = document.createElement('div');
    card.className = 'admin-modal-card';

    const head = document.createElement('div');
    head.className = 'admin-modal-head';
    const heading = document.createElement('div');
    heading.innerHTML = '<span class="admin-modal-kicker">ADMINISTRATION</span><h2>' + title + '</h2>';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn ghost';
    close.textContent = 'Close';
    head.append(heading, close);
    card.appendChild(head);
    card.appendChild(contentNode);
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    document.body.classList.add('admin-modal-open');

    function destroy() {
      overlay.remove();
      document.body.classList.remove('admin-modal-open');
    }
    close.onclick = destroy;
    overlay.addEventListener('click', e => { if (e.target === overlay) destroy(); });
    const esc = e => { if (e.key === 'Escape') { destroy(); document.removeEventListener('keydown', esc); } };
    document.addEventListener('keydown', esc);
  }

  function openExistingPanel(tab) {
    // Prefer the application's existing staff-tab switcher.
    if (typeof window.switchStaffTab === 'function') {
      try { window.switchStaffTab(tab); } catch (_) {}
    }

    setTimeout(() => {
      const selectors = [
        '#staff-tab-' + tab,
        '[data-staff-tab="' + tab + '"]',
        '[data-tab="' + tab + '"]'
      ];
      let pane = null;
      for (const selector of selectors) {
        pane = $(selector);
        if (pane) break;
      }
      if (!pane) {
        const message = document.createElement('p');
        message.textContent = 'This Administration workspace is not available in the current session.';
        showModal(tab === 'learn' ? 'AI Corrections' : tab === 'account' ? 'Settings' : 'Users', message);
        return;
      }

      const parent = pane.parentNode;
      const next = pane.nextSibling;
      const hidden = pane.classList.contains('hidden');
      pane.classList.remove('hidden');

      const oldStyle = pane.getAttribute('style');
      const overlay = document.createElement('div');
      overlay.id = 'adminUtilityModal';
      overlay.className = 'admin-modal-backdrop';
      const card = document.createElement('div');
      card.className = 'admin-modal-card';
      const head = document.createElement('div');
      head.className = 'admin-modal-head';
      const heading = document.createElement('div');
      const label = tab === 'learn' ? 'AI Corrections' : tab === 'account' ? 'Settings' : 'Users';
      heading.innerHTML = '<span class="admin-modal-kicker">ADMINISTRATION</span><h2>' + label + '</h2>';
      const close = document.createElement('button'); close.type='button'; close.className='btn ghost'; close.textContent='Close';
      head.append(heading, close);
      card.append(head, pane); overlay.appendChild(card); document.body.appendChild(overlay); document.body.classList.add('admin-modal-open');

      let done=false;
      const restore=()=>{
        if(done)return; done=true;
        if(next && next.parentNode===parent) parent.insertBefore(pane,next); else parent.appendChild(pane);
        if(hidden) pane.classList.add('hidden');
        if(oldStyle===null) pane.removeAttribute('style'); else pane.setAttribute('style',oldStyle);
        overlay.remove(); document.body.classList.remove('admin-modal-open');
      };
      close.onclick=restore;
      overlay.onclick=e=>{if(e.target===overlay)restore();};
    }, 60);
  }

  function build() {
    installStyles();
    const panel = getUserPanel();
    if (!panel) return false;

    let nav = $('#administrationNav');
    if (!nav) {
      nav = document.createElement('div');
      nav.id = 'administrationNav';
      nav.className = 'administration-nav';

      const toggle = document.createElement('button');
      toggle.id = 'administrationToggle';
      toggle.type = 'button';
      toggle.setAttribute('aria-haspopup','true');
      toggle.setAttribute('aria-expanded','false');
      toggle.innerHTML = 'Administration <span class="admin-chevron" aria-hidden="true">▼</span>';

      const menu = document.createElement('div');
      menu.id = 'administrationMenu';

      const addButton = (label, fn) => {
        const b = document.createElement('button');
        b.type='button'; b.className='administration-item'; b.textContent=label;
        b.addEventListener('click', e=>{e.stopPropagation(); menu.classList.remove('open'); toggle.setAttribute('aria-expanded','false'); fn();});
        menu.appendChild(b);
      };
      addButton('Users', ()=>openExistingPanel('users'));
      addButton('Settings', ()=>openExistingPanel('account'));
      addButton('AI Corrections', ()=>openExistingPanel('learn'));
      const divider=document.createElement('div'); divider.className='administration-divider'; menu.appendChild(divider);

      const land=document.createElement('a'); land.className='administration-item'; land.href='/land-intelligence'; land.textContent='Land Intelligence'; menu.appendChild(land);
      const logout=document.createElement('button'); logout.type='button'; logout.className='administration-item'; logout.textContent='Log out';
      logout.onclick=()=>{const real=$('#logoutBtn'); if(real) real.click(); else location.href='/';}; menu.appendChild(logout);

      nav.append(toggle,menu);
      panel.insertBefore(nav,panel.firstChild);
      toggle.onclick=e=>{e.stopPropagation(); const open=menu.classList.toggle('open'); toggle.setAttribute('aria-expanded',String(open));};
      document.addEventListener('click',e=>{if(!nav.contains(e.target)){menu.classList.remove('open');toggle.setAttribute('aria-expanded','false');}});
    }

    // Never let legacy CSS accidentally hide the Administration entry point.
    nav.style.setProperty('display','inline-flex','important');
    nav.style.setProperty('visibility','visible','important');
    nav.style.setProperty('opacity','1','important');
    return true;
  }

  function run() {
    installStyles();
    build();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, {once:true});
  else run();

  // Login/workspace code can rebuild .gov-user-panel. Re-add the button whenever that happens.
  const observer = new MutationObserver(() => { if (!$('#administrationNav')) build(); });
  observer.observe(document.documentElement, {subtree:true, childList:true});
  setTimeout(run, 250);
  setTimeout(run, 1000);
  setTimeout(run, 2500);
})();
