/* Back Office navigation. Review and scheduling forms use real server responses. */
(() => {
  const sidebar=document.getElementById('bo-sidebar');
  const trigger=document.getElementById('bo-menu');
  const closeButton=document.getElementById('bo-close');
  const backdrop=document.getElementById('bo-backdrop');
  if(!sidebar||!trigger||!closeButton||!backdrop)return;
  const canvas=sidebar.parentElement.querySelector('.main-canvas');
  const desktop=window.matchMedia('(min-width: 900px)');
  let open=false;
  const focusable=()=>Array.from(sidebar.querySelectorAll('a[href],button:not([disabled]),input:not([type="hidden"]),[tabindex="0"]')).filter(node=>node.getClientRects().length&&!node.inert);
  function close(restore=true) {
    open=false;sidebar.classList.remove('is-open');backdrop.hidden=true;
    document.body.classList.remove('bo-drawer-open');
    if(canvas)canvas.inert=false;
    trigger.setAttribute('aria-expanded','false');
    sidebar.removeAttribute('role');sidebar.removeAttribute('aria-modal');
    if(restore&&!desktop.matches)trigger.focus();
    sidebar.inert=!desktop.matches;
    if(desktop.matches)sidebar.removeAttribute('aria-hidden');else sidebar.setAttribute('aria-hidden','true');
  }
  function show() {
    if(desktop.matches)return;
    open=true;sidebar.inert=false;sidebar.classList.add('is-open');
    sidebar.removeAttribute('aria-hidden');sidebar.setAttribute('role','dialog');sidebar.setAttribute('aria-modal','true');
    trigger.setAttribute('aria-expanded','true');backdrop.hidden=false;
    document.body.classList.add('bo-drawer-open');closeButton.focus();
    if(canvas)canvas.inert=true;
  }
  trigger.addEventListener('click',()=>open?close():show());
  closeButton.addEventListener('click',()=>close());
  backdrop.addEventListener('click',()=>close());
  document.addEventListener('keydown',event=>{
    if(!open)return;
    if(event.key==='Escape'){event.preventDefault();close();return;}
    if(event.key==='Tab'){
      const nodes=focusable(),first=nodes[0],last=nodes[nodes.length-1];
      if(!first){event.preventDefault();closeButton.focus();return;}
      if(!sidebar.contains(document.activeElement)){event.preventDefault();(event.shiftKey?last:first).focus();}
      else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
    }
  });
  sidebar.querySelectorAll('a[href]').forEach(link=>link.addEventListener('click',()=>{if(open)close();}));
  function resize() {
    const active=document.activeElement;
    const disappears=trigger.contains(active)||closeButton.contains(active);
    const inSidebar=sidebar.contains(active);
    close(false);
    if(desktop.matches&&disappears)(sidebar.querySelector('[aria-current="page"]')||sidebar.querySelector('a[href]'))?.focus();
    else if(!desktop.matches&&inSidebar)trigger.focus();
  }
  desktop.addEventListener('change',resize);
  window.addEventListener('pagehide',()=>close(false));
  resize();
})();


/* Account disclosure uses normal Tab navigation; it does not trap focus. */
(() => {
  const wrapper=document.getElementById('bo-profile-wrapper');
  const trigger=document.getElementById('bo-profile-trigger');
  const menu=document.getElementById('bo-profile-menu');
  if(!wrapper||!trigger||!menu)return;
  function closeProfile(restore=false) {
    const focusInside=menu.contains(document.activeElement);
    if(restore||focusInside)trigger.focus();
    menu.hidden=true;trigger.setAttribute('aria-expanded','false');
  }
  trigger.addEventListener('click',()=>{
    if(!menu.hidden){closeProfile();return;}
    menu.hidden=false;trigger.setAttribute('aria-expanded','true');
  });
  document.addEventListener('click',event=>{
    if(!menu.hidden&&!wrapper.contains(event.target))closeProfile();
  });
  document.addEventListener('focusin',event=>{
    if(!menu.hidden&&!wrapper.contains(event.target))closeProfile();
  });
  document.addEventListener('keydown',event=>{
    if(!menu.hidden&&event.key==='Escape'){event.preventDefault();closeProfile(true);}
  });
  window.addEventListener('pagehide',()=>closeProfile());
  closeProfile();
})();
