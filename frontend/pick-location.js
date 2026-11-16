(() => {
  'use strict';
  const $=id=>document.getElementById(id),nonce=decodeURIComponent(location.hash.slice(1));
  const map=L.map('independent-map').setView([32.86,-117.22],12);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map);
  let marker=null,selected=null;
  function choose(lat,lng){
    if(!Number.isFinite(lat) || !Number.isFinite(lng) || lat<32.5 || lat>33.2 || lng< -117.6 || lng> -116.8){$('picker-status').textContent='Choose a point within the supported San Diego area.';$('picker-confirm').disabled=true;return;}
    selected={latitude:lat,longitude:lng};$('picker-lat').value=lat.toFixed(6);$('picker-lng').value=lng.toFixed(6);
    if(marker)marker.setLatLng([lat,lng]);else{marker=L.marker([lat,lng],{draggable:true}).addTo(map);marker.on('dragend',()=>{const p=marker.getLatLng();choose(p.lat,p.lng);});}
    $('picker-confirm').disabled=false;$('picker-status').textContent='Provisional user-selected point. Confirm when it represents the place you intend.';
  }
  map.on('click',event=>choose(event.latlng.lat,event.latlng.lng));
  for(const id of ['picker-lat','picker-lng'])$(id).addEventListener('change',()=>{
    if(!$('picker-lat').value || !$('picker-lng').value)return;choose(Number($('picker-lat').value),Number($('picker-lng').value));
  });
  $('picker-confirm').addEventListener('click',()=>{
    if(!selected || !nonce || !window.opener){$('picker-status').textContent='Open this picker from the active investigation to return the selected point.';return;}
    window.opener.postMessage({type:'citylens:manual-point',nonce,...selected},location.origin);
    $('picker-status').textContent='Point returned. Confirm the selected point(s) in your investigation to run analysis.';
  });
})();
