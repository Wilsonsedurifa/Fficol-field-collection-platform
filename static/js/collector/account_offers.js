/* Map offer selection with real, CSRF-protected server actions. */
(() => {
  const configElement=document.getElementById('offers-config');
  if(!configElement) return;
  const config=JSON.parse(configElement.textContent);
  const el=id=>document.getElementById(id);
  const csrf=el('offer-actions').querySelector('input[name="csrfmiddlewaretoken"]').value;
  let state=null, selected=null, assigned=false, busy=false, centred=false, serverOffset=0;
  const desktop=window.matchMedia('(min-width:900px)');
  let listOpen=false;
  function syncPanels() {
    const visible=desktop.matches||listOpen;
    el('offer-list-panel').classList.toggle('is-open',visible);
    el('offer-list-panel').hidden=!visible;
    el('list-toggle').setAttribute('aria-expanded',String(!desktop.matches&&listOpen));
    el('list-toggle').textContent=`${listOpen&&!desktop.matches?'Close list':'Offer list'} (${el('offer-count').textContent})`;
    el('selected-offer').hidden=!selected||(!desktop.matches&&listOpen);
  }
  function fitMap() {map?.resize();map?.fitOffers(selected?.id);}
  const feedback=(message,error=false)=>{el('offers-feedback').textContent=message;el('offers-feedback').classList.toggle('is-error',error);};
  const map=window.FiColMap?.create('offers-map',config,selectOffer,message=>feedback(message,true));
  if(!map) feedback('Map unavailable. The offer list and account actions still work.',true);
  const messages={offline:'Go online to request nearby offers.',daily_limit_reached:'You have accepted 10 accounts in the last 24 hours. Existing accounts remain assigned to you.',no_client_access:'Ask your supervisor to grant access to a client before receiving offers.',location_required:'Update your location to find nearby offers.',ready:'Select an offer marker or choose an account from the list.'};

  async function api(url,payload) {
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),20000);
    try {
      const options={credentials:'same-origin',mode:'same-origin',signal:controller.signal,headers:{Accept:'application/json'}};
      if(payload!==undefined) Object.assign(options,{method:'POST',headers:{...options.headers,'Content-Type':'application/json','X-CSRFToken':csrf},body:JSON.stringify(payload)});
      const response=await fetch(url,options);
      if(response.redirected||response.status===401) throw new Error('Please sign in again.');
      let data=null;
      if(response.headers.get('content-type')?.includes('application/json')) {
        try {data=await response.json();} catch(error) {throw new Error('The server response could not be understood. Please refresh and retry.');}
      }
      if(!response.ok) {
        const rawMessage=data?.error?.message;
        const validMessage=typeof rawMessage==='string'&&rawMessage.trim().length>0?rawMessage.trim():null;
        const err=new Error(validMessage||(response.status===403?'Reload this page and check your account access.':'The request could not be completed. Please retry.'));
        err.status=response.status;
        err.code=data?.error?.code;
        err.hasServerMessage=Boolean(validMessage);
        throw err;
      }
      if(!data) throw new Error('The server response could not be understood. Please refresh and retry.');
      return data;
    } catch(error) {
      if(error.name==='AbortError') {
        const err=new Error('The request timed out. Refresh to check its result before trying again.');
        err.isTimeout=true;
        throw err;
      }
      if(error instanceof TypeError) {
        const err=new Error('Check your connection. Refresh to confirm the result before retrying.');
        err.isConnection=true;
        throw err;
      }
      throw error;
    } finally {clearTimeout(timer);}
  }
  function syncButtons() {
    for(const id of ['availability-toggle','update-location','refresh-offers','decline-offer','confirm-acceptance']) el(id).disabled=busy;
    const expired=selected&&Date.parse(selected.expires_at)<=Date.now()+serverOffset;
    el('accept-offer').disabled=busy||!selected||expired||assigned;
    el('confirm-acceptance').disabled=busy||!selected||expired||assigned;
  }
  async function run(work) {
    if(busy) return;
    busy=true;syncButtons();
    try {await work();} catch(error) {feedback(error.message,true);} finally {busy=false;syncButtons();}
  }
  function locationLabel() {
    if(!state?.location) return;
    const captured=Date.parse(state.location.captured_at);
    const age=Number.isFinite(captured)?Math.max(0,Math.floor((Date.now()+serverOffset-captured)/1000)):null;
    const when=age===null?'Saved location':age<60?`Captured ${age}s ago`:`Captured ${Math.floor(age/60)}m ago`;
    el('location-label').textContent=when+` · ±${Math.ceil(state.location.accuracy_m)} m accuracy`;
  }
  function expiry() {
    locationLabel();
    if(!selected||assigned) return;
    const seconds=Math.max(0,Math.ceil((Date.parse(selected.expires_at)-Date.now()-serverOffset)/1000));
    el('selected-expiry').textContent=seconds?`Expires in ${Math.floor(seconds/60)}m ${seconds%60}s`:'Offer expired';
    syncButtons();
  }
  function distance(metres) {if(metres<100)return 'Within about 100 m';return metres<1000?`About ${metres} m away`:`About ${(metres/1000).toFixed(1)} km away`;}
  function selectOffer(id, restoring=false) {
    if(busy&&!restoring) return;
    const listHadFocus=el('offer-list-panel').contains(document.activeElement);
    selected=state?.offers.find(offer=>offer.id===id)||null;
    assigned=false;
    el('selected-offer').hidden=!selected;
    if(!selected) return;
    el('selected-reference').textContent=selected.reference;
    el('selected-area').textContent=selected.area;
    el('selected-distance').textContent=distance(selected.distance_m)+' · Straight-line distance';
    el('selected-terms').textContent=selected.commission_terms;
    el('selected-privacy').textContent='Approximate area only. Exact address becomes available after acceptance.';
    el('offer-response-actions').hidden=false;el('assigned-actions').hidden=true;
    el('selected-offer').classList.remove('is-collapsed');el('sheet-toggle').setAttribute('aria-expanded','true');
    listOpen=false;syncPanels();
    for(const item of el('offer-list').children) item.classList.toggle('is-selected',item.dataset.offerId===String(id));
    map?.select(id);expiry();
    fitMap();
    if(!desktop.matches&&listHadFocus) el('sheet-toggle').focus();
  }
  function allowanceLabel(data) {
    const quota=data.daily_acceptance;
    if(!quota||!Number.isInteger(quota.used)||quota.used<0||quota.limit!==10||!Number.isInteger(data.workload)||data.workload<0)throw new Error('Your account allowance could not be confirmed. Refresh and retry.');
    return `Accepted in last 24 hours: ${quota.used}/${quota.limit} · ${data.workload} active accounts`;
  }
  function render(data) {
    if(!Array.isArray(data.offers)) throw new Error('Offers could not be loaded. Please retry.');
    const quotaText=allowanceLabel(data);
    state=data;serverOffset=Date.parse(data.server_time)-Date.now();
    el('availability-toggle').textContent=data.availability==='online'?'Go offline':'Go online';
    el('availability-label').textContent=data.availability==='online'?'Online':data.availability==='busy'?'Busy':'Offline';
    if(el('sidebar-availability')) {
      el('sidebar-availability').className='availability-status-pill pill-'+data.availability;
      el('sidebar-availability-dot').className='status-indicator-dot dot-'+data.availability;
      el('sidebar-availability-label').textContent=data.availability==='online'?'Available':data.availability==='busy'?'Busy':'Offline';
      el('sidebar-availability-code').textContent=data.availability.toUpperCase();
    }
    el('workload-label').textContent=quotaText+` · ${data.radius_m} m radius`;
    el('location-label').textContent='Location not saved yet';locationLabel();
    map?.setLocation(data.location,!centred&&!!data.location);if(data.location) centred=true;
    map?.setOffers(data.offers);
    el('offer-count').textContent=data.offers.length;
    el('offer-list').replaceChildren();
    el('offers-empty').hidden=data.offers.length>0;
    el('offers-empty').textContent=data.state==='ready'?'No nearby offers yet. Refresh to check for eligible accounts.':messages[data.state]||'No offers available.';
    for(const offer of data.offers) {
      const item=document.createElement('button');item.type='button';item.className='offer-list-item';item.dataset.offerId=String(offer.id);
      const title=document.createElement('strong');title.textContent=offer.reference;
      const area=document.createElement('span');area.textContent=offer.area;
      const near=document.createElement('span');near.textContent=distance(offer.distance_m);
      item.append(title,area,near);item.addEventListener('click',()=>selectOffer(offer.id));el('offer-list').append(item);
    }
    const previous=selected?.id;
    selected=null;assigned=false;el('selected-offer').hidden=true;
    const next=data.offers.find(offer=>offer.id===previous)||data.offers[0];
    if(next) selectOffer(next.id,true);
    else {listOpen=true;syncPanels();fitMap();}
    feedback(data.offers.length?messages.ready:messages[data.state]||'No offers available.');
    syncButtons();
  }
  async function refresh() {feedback('Checking nearby offers...');render(await api(config.refreshUrl,{}));}
  el('refresh-offers').addEventListener('click',()=>run(refresh));
  el('availability-toggle').addEventListener('click',()=>run(async()=>{
    const availability=state?.availability==='online'?'offline':'online';
    feedback('Updating availability...');await api(config.availabilityUrl,{availability});await refresh();
  }));
  function detect() {
    return new Promise((resolve,reject)=>{
      if(!navigator.geolocation){reject(new Error('This browser does not support location detection.'));return;}
      navigator.geolocation.getCurrentPosition(resolve,error=>reject(new Error(error.code===1?'Allow location access in your browser settings.':'Location could not be detected. Try again in an open area.')),{enableHighAccuracy:true,timeout:20000,maximumAge:0});
    });
  }
  el('update-location').addEventListener('click',()=>run(async()=>{
    feedback('Detecting your location. Allow access when asked.');
    const position=await detect();
    const coords=position.coords;
    if(!Number.isFinite(position.timestamp)||!Number.isFinite(coords.longitude)||!Number.isFinite(coords.latitude)||!Number.isFinite(coords.accuracy)){
      throw new Error('Your location could not be accepted. Update your location and try again.');
    }
    feedback('Saving your location...');
    let result;
    try {
      result=await api(config.locationUrl,{longitude:coords.longitude,latitude:coords.latitude,accuracy_m:coords.accuracy,captured_at:new Date(position.timestamp).toISOString()});
    } catch(err) {
      if(err.status===400&&!err.hasServerMessage){
        throw new Error('Your location could not be accepted. Update your location and try again.');
      }
      if(err.isTimeout){
        throw new Error('The request timed out. Check your connection and try again.');
      }
      if(err.isConnection){
        throw new Error('Could not connect to the server. Check your connection and try again.');
      }
      throw err;
    }
    if(result.saved!==true) throw new Error('Location save could not be confirmed.');
    centred=false;await refresh();
  }));
  el('recenter-map').addEventListener('click',()=>map?.recenter());
  el('list-toggle').addEventListener('click',()=>{
    listOpen=!listOpen;syncPanels();fitMap();
  });
  el('sheet-toggle').addEventListener('click',()=>{
    const collapsed=el('selected-offer').classList.toggle('is-collapsed');el('sheet-toggle').setAttribute('aria-expanded',String(!collapsed));fitMap();
  });
  const dialog=el('offer-confirmation');
  el('accept-offer').addEventListener('click',()=>{
    if(!selected||busy) return;
    el('confirmation-copy').textContent=`${selected.reference}: ${selected.commission_terms}`;
    dialog.showModal();el('cancel-confirmation').focus();expiry();
  });
  el('cancel-confirmation').addEventListener('click',()=>dialog.close());
  el('confirm-acceptance').addEventListener('click',()=>run(async()=>{
    if(!selected) return;
    dialog.close();feedback('Confirming account acceptance...');
    const result=await api(selected.accept_url,{});
    if(result.accepted!==true||!result.assignment) throw new Error('Acceptance could not be confirmed. Refresh your offers.');
    const item=result.assignment;assigned=true;
    state.workload=result.workload;state.daily_acceptance=result.daily_acceptance;el('workload-label').textContent=allowanceLabel(result)+` · ${state.radius_m} m radius`;map?.setOffers([]);map?.showAssigned(item);
    el('selected-reference').textContent=item.reference;el('selected-expiry').textContent='Assigned to you';
    el('selected-area').textContent=item.address;el('selected-distance').textContent='Verified collection location';el('selected-terms').textContent=item.commission_terms;
    el('selected-privacy').textContent='This account is now assigned to you. Directions open in Google Maps.';
    el('offer-response-actions').hidden=true;el('assigned-actions').hidden=false;
    el('assigned-detail').href=item.detail_url;el('assigned-directions').href=item.directions_url;
    el('offer-list').replaceChildren();el('offers-empty').hidden=false;el('offers-empty').textContent='Account accepted. Open My Accounts or refresh for more offers.';el('offer-count').textContent='0';
    listOpen=false;syncPanels();fitMap();
    feedback('Account assigned to you. You can now open its details or get directions.');el('assigned-detail').focus();
  }));
  el('decline-offer').addEventListener('click',()=>run(async()=>{
    if(!selected) return;
    const result=await api(selected.decline_url,{});if(result.declined!==true) throw new Error('Decline could not be confirmed.');
    render(await api(config.listUrl));el('refresh-offers').focus();
  }));
  const timer=setInterval(expiry,1000);
  desktop.addEventListener('change',()=>{
    const focused=document.activeElement;
    if(desktop.matches&&(focused===el('list-toggle')||focused===el('sheet-toggle'))) el('recenter-map').focus();
    if(!desktop.matches&&!listOpen&&el('offer-list-panel').contains(focused)) el('list-toggle').focus();
    syncPanels();fitMap();
  });
  let resizeFrame;
  const observer=new ResizeObserver(()=>{
    cancelAnimationFrame(resizeFrame);
    resizeFrame=requestAnimationFrame(fitMap);
  });
  observer.observe(el('offers-map'));
  observer.observe(el('selected-offer'));
  syncPanels();
  window.addEventListener('pagehide',()=>{clearInterval(timer);observer.disconnect();cancelAnimationFrame(resizeFrame);});
  run(async()=>render(await api(config.listUrl)));
})();
