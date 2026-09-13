(() => {
"use strict";
const state={map:null,tileLayer:null,tileSource:"osm",mapInitAttempts:0,mapRetryTimer:null,geo:null,metrics:null,selected:null,parcelLayers:new Map(),selectedLayer:null,locationMarkers:new Map(),locationRecords:new Map(),documentRecords:[],documentMarkers:new Map(),selectedDocumentId:null,documentPinMode:false,documentVillageCache:{},scenarios:[],activeScenario:null,loading:false,user:null,pinMode:false,visibleFeatures:[]};
const API="/api/demo-land";
const LAND_API="/api/land";
const $=id=>document.getElementById(id);
const esc=v=>String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const badge=status=>{const s=String(status||"UNKNOWN");const cls=["MATCH","CONSISTENT","VERIFIED"].includes(s)?"ok":(["CONFLICT","NO MATCH"].includes(s)?"bad":(["REVIEW REQUIRED","POSSIBLE MATCH","OPEN","ACKNOWLEDGED"].includes(s)?"warn":"neutral"));return '<span class="status '+cls+'">'+esc(s)+'</span>';};

function authHeaders(extra={}){
  const h={...extra};try{const token=window.localStorage.getItem("lrtoken");if(token)h.Authorization="Bearer "+token}catch(_){}
  return h;
}
async function api(url){
  const r=await fetch(url,{headers:authHeaders({"Accept":"application/json"})});
  let data={}; try{data=await r.json()}catch(_){}
  if(!r.ok) throw new Error(data.detail||data.error||("Request failed ("+r.status+")"));
  return data;
}
function setStatus(text){if($("mapStatus"))$("mapStatus").textContent=text}
function notice(text){const n=$("mapNotice");if(!n)return;n.textContent=text;n.classList.remove("hidden")}
function clearNotice(){$("mapNotice")?.classList.add("hidden")}
const MAP_TILE_SOURCES={
  osm:{url:"https://tile.openstreetmap.org/{z}/{x}/{y}.png",options:{maxZoom:19},attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'},
  carto:{url:"https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",options:{maxZoom:19,subdomains:"abcd"},attribution:'© OpenStreetMap contributors © <a href="https://carto.com/" target="_blank" rel="noopener">CARTO</a>'},
  esri:{url:"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",options:{maxZoom:18},attribution:"Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics"},
  topo:{url:"https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",options:{maxZoom:17,subdomains:"abc"},attribution:'© OpenStreetMap contributors · <a href="https://opentopomap.org" target="_blank" rel="noopener">OpenTopoMap</a>'}
};
function mapSavedTileSource(){try{return localStorage.getItem("landIntelligenceTileSource")||"osm"}catch(_){return "osm"}}
function mapSaveTileSource(key){try{localStorage.setItem("landIntelligenceTileSource",key)}catch(_){}
}
function mapSetTileSource(key){
  state.tileSource=key||"osm";const select=$("mapTileSource");if(select)select.value=state.tileSource;
  const note=$("mapSchematicNote"),fallback=$("mapFallback"),hint=$("mapLoadHint");
  if(state.tileLayer&&state.map){try{state.map.removeLayer(state.tileLayer)}catch(_){}state.tileLayer=null}
  if(state.tileSource==="schematic"){
    note?.classList.remove("hidden");fallback?.classList.add("hidden");hint?.classList.add("hidden");mapSaveTileSource("schematic");return;
  }
  note?.classList.add("hidden");hint?.classList.remove("hidden");
  if(!state.map||!window.L)return;
  const src=MAP_TILE_SOURCES[state.tileSource]||MAP_TILE_SOURCES.osm;
  state.tileLayer=L.tileLayer(src.url,{...src.options,attribution:src.attribution});
  state.tileLayer.on("tileerror",()=>fallback?.classList.remove("hidden"));
  state.tileLayer.once("tileload",()=>{fallback?.classList.add("hidden");hint?.classList.add("hidden");setStatus("Map ready")});
  state.tileLayer.addTo(state.map);mapSaveTileSource(state.tileSource);
}
function locationLabel(status){return {EXACT_PIN:"EXACT LOCATION",VILLAGE_LEVEL:"VILLAGE-LEVEL APPROXIMATE",PARCEL_GEOMETRY:"PARCEL GEOMETRY",UNRESOLVED:"LOCATION UNRESOLVED"}[status]||"LOCATION UNRESOLVED"}
function locationClass(status){return status==="EXACT_PIN"?"exact":status==="VILLAGE_LEVEL"?"approx":status==="UNRESOLVED"?"unresolved":"verify"}
function canEditLocation(){return ["VERIFICATION_OFFICER","ADMIN"].includes(state.user?.role)}
async function loadCurrentUser(){try{const r=await api("/api/auth/me");state.user=r.user||null}catch(_){state.user=null}}
function clearLocationMarkers(){state.locationMarkers.forEach(m=>m.remove());state.locationMarkers.clear()}
function renderLocationMarkers(records){
  clearLocationMarkers();if(!state.map)return;
  records.forEach(p=>{const loc=p.location||{};if(loc.latitude==null||loc.longitude==null)return;const status=loc.status||"UNRESOLVED";
    const marker=L.circleMarker([loc.latitude,loc.longitude],{radius:status==="EXACT_PIN"?7:5,color:status==="EXACT_PIN"?"#16803c":status==="VILLAGE_LEVEL"?"#315ba8":status==="UNRESOLVED"?"#64748b":"#b07b29",fillColor:status==="EXACT_PIN"?"#22a447":status==="VILLAGE_LEVEL"?"#4f7fc4":status==="UNRESOLVED"?"#94a3b8":"#d39a38",fillOpacity:.85,weight:2})
      .bindPopup("<strong>"+esc(p.parcel_id||p.property_id)+"</strong><br>"+esc(locationLabel(status))+(status==="VILLAGE_LEVEL"?"<br>Village-level location is approximate and does not represent the exact parcel.":""));
    marker.on("click",()=>selectParcel(p.property_id));marker.addTo(state.map);state.locationMarkers.set(p.property_id,marker);
  });
}
async function loadMapLocations(filters={}){
  if(!state.user)return;try{const qs=new URLSearchParams();Object.entries(filters).forEach(([k,v])=>{if(v)qs.set(k,v)});const r=await api(LAND_API+"/map/records"+(qs.toString()?"?"+qs.toString():""));state.locationRecords.clear();(r.records||[]).forEach(p=>state.locationRecords.set(p.property_id,p));renderLocationMarkers(r.records||[])}catch(_){}
}
function documentVillageKey(r){return [r.village,r.district,r.state].filter(Boolean).join("|").toLowerCase()}
function loadDocumentVillageCache(){try{const cached=JSON.parse(localStorage.getItem("landIntelligenceVillageCache")||"{}");if(cached&&typeof cached==="object")state.documentVillageCache=cached}catch(_){state.documentVillageCache={}}}
function saveDocumentVillageCache(){try{localStorage.setItem("landIntelligenceVillageCache",JSON.stringify(state.documentVillageCache))}catch(_){}
}
function documentFilteredRecords(){
  const q=($("documentMapSearch")?.value||"").toLowerCase().trim();if(!q)return state.documentRecords;
  return state.documentRecords.filter(r=>[r.id,r.filename,r.owner,r.survey,r.khasra,r.khata,r.village,r.district,r.state].join(" ").toLowerCase().includes(q));
}
function clearDocumentMarkers(){state.documentMarkers.forEach(m=>m.remove());state.documentMarkers.clear()}
function renderDocumentMarkers(){
  clearDocumentMarkers();if(!state.map||!window.L)return;
  documentFilteredRecords().forEach(r=>{
    let lat=r.lat,lon=r.lon,exact=lat!=null&&lon!=null;
    if(!exact){const cached=state.documentVillageCache[documentVillageKey(r)];if(cached){lat=cached[0];lon=cached[1]}}
    if(lat==null||lon==null)return;
    const marker=L.circleMarker([lat,lon],{radius:exact?7:5,color:exact?"#15803d":"#2563eb",fillColor:exact?"#22c55e":"#60a5fa",fillOpacity:.9,weight:2})
      .bindPopup("<strong>"+esc(r.owner||r.filename||"Document record")+"</strong><br>"+esc(exact?"Exact reviewer pin":"Village-level location (approximate)")+"<br>Survey: "+esc(r.survey||"—")+"<br>Village: "+esc(r.village||"—"));
    marker.on("click",()=>selectDocumentRecord(r.id));marker.addTo(state.map);state.documentMarkers.set(r.id,marker);
  });
}
function renderDocumentList(){
  const root=$("documentMapList"),count=$("documentMapCount");if(!root)return;
  const rows=documentFilteredRecords();if(count)count.textContent=state.documentRecords.length?rows.length+" / "+state.documentRecords.length:"Sign in to load";
  const pin=$("recordPinBtn");if(pin){pin.disabled=!state.selectedDocumentId||!canEditLocation();pin.textContent=state.documentPinMode?"Cancel pin":"Pin";pin.title=canEditLocation()?"Select a record, then click the map":"Verification Officer / Admin only"}
  if(!state.user){root.innerHTML="<div class=\"empty\">Sign in to view uploaded document locations.</div>";return}
  if(!rows.length){root.innerHTML="<div class=\"empty\">No document records match.</div>";return}
  root.innerHTML=rows.map(r=>{
    const exact=r.lat!=null&&r.lon!=null,selected=r.id===state.selectedDocumentId;
    const loc=exact?"Exact pin · "+Number(r.lat).toFixed(4)+", "+Number(r.lon).toFixed(4):(r.village?"Village · "+r.village:"No location data");
    return `<div class="document-map-row${selected ? " selected" : ""}" data-document="${esc(r.id)}"><div class="document-map-main"><strong>${esc(r.owner||r.filename||"Unnamed record")}</strong><span>Survey ${esc(r.survey||"—")} · ${esc(loc)}</span></div><button type="button" class="btn secondary document-focus" data-document-focus="${esc(r.id)}">View</button>${exact&&canEditLocation()?`<button type="button" class="btn secondary document-focus" data-document-clear="${esc(r.id)}">Clear</button>`:""}</div>`;
  }).join("");
  root.querySelectorAll("[data-document]").forEach(row=>row.addEventListener("click",e=>{if(e.target.closest("button"))return;selectDocumentRecord(row.dataset.document)}));
  root.querySelectorAll("[data-document-focus]").forEach(btn=>btn.addEventListener("click",()=>selectDocumentRecord(btn.dataset.documentFocus)));
  root.querySelectorAll("[data-document-clear]").forEach(btn=>btn.addEventListener("click",e=>{e.stopPropagation();clearDocumentPin(btn.dataset.documentClear)}));
}
async function selectDocumentRecord(id){
  const record=state.documentRecords.find(r=>r.id===id);if(!record)return;state.selectedDocumentId=id;state.documentPinMode=false;renderDocumentList();
  const marker=state.documentMarkers.get(id);if(marker){state.map?.setView(marker.getLatLng(),17);marker.openPopup()}
  else{const cached=state.documentVillageCache[documentVillageKey(record)];if(cached)state.map?.setView(cached,14)}
  await loadDocumentHistory(id);
}
async function loadDocumentHistory(id){
  try{const d=await api("/api/documents/"+encodeURIComponent(id)+"/history");const items=d.items||[];const rows=items.map((it,index)=>'<div class="timeline-item"><i class="timeline-dot"></i><div><strong>'+esc(it.year||"Year unavailable")+(it.id===id?' · THIS RECORD':'')+'</strong><div class="muted">Owner: '+esc(it.owner||"—")+' · Survey: '+esc(it.survey||"—")+' · Area: '+esc(it.area||"—")+'</div></div></div>').join("");$("evidencePanel").innerHTML='<div class="section-head"><div><span class="eyebrow">DOCUMENT PASSBOOK</span><h2>'+esc(d.survey||"Record history")+'</h2></div></div><p class="muted">'+esc(d.village?"Village: "+d.village:"Complete survey history")+' · '+items.length+' record(s)</p><div class="timeline">'+(rows||'<div class="empty">No history found.</div>')+'</div>';
  }catch(e){notice(e.message||"Record history could not be loaded.")}
}
async function setDocumentPin(id,lat,lon){
  try{const r=await fetch("/api/map/records/"+encodeURIComponent(id)+"/location",{method:"PUT",headers:{...authHeaders({"Content-Type":"application/json","Accept":"application/json"})},body:JSON.stringify({lat,lon})});let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw new Error(d.detail||"Document pin update failed");const record=state.documentRecords.find(x=>x.id===id);if(record){record.lat=d.lat;record.lon=d.lon}state.documentPinMode=false;renderDocumentList();renderDocumentMarkers();notice("Document pin saved and audited.")}catch(e){notice(e.message||"Document pin could not be saved.")}
}
async function clearDocumentPin(id){
  try{const r=await fetch("/api/map/records/"+encodeURIComponent(id)+"/location",{method:"PUT",headers:{...authHeaders({"Content-Type":"application/json","Accept":"application/json"})},body:JSON.stringify({lat:null,lon:null})});if(!r.ok){let d={};try{d=await r.json()}catch(_){}throw new Error(d.detail||"Document pin removal failed")}const record=state.documentRecords.find(x=>x.id===id);if(record){record.lat=null;record.lon=null}renderDocumentList();renderDocumentMarkers();notice("Document pin cleared; village-level location remains available.")}catch(e){notice(e.message||"Document pin could not be cleared.")}
}
async function geocodeDocumentVillages(){
  const pending=new Map();state.documentRecords.forEach(r=>{if(r.lat!=null&&r.lon!=null)return;const key=documentVillageKey(r);if(!key||state.documentVillageCache[key]||pending.has(key))return;const q=[r.village,r.district,r.state,"India"].filter(Boolean).join(", ");if(q)pending.set(key,q)});
  let failures=0;for(const [key,q] of [...pending.entries()].slice(0,25)){try{const result=await api("/api/map/geocode",{method:"POST",headers:authHeaders({"Content-Type":"application/json","Accept":"application/json"}),body:JSON.stringify({query:q})});if(result?.lat!=null){state.documentVillageCache[key]=[result.lat,result.lon];saveDocumentVillageCache();renderDocumentMarkers();if(!state.selected&&state.documentMarkers.size===1){const marker=[...state.documentMarkers.values()][0];state.map?.fitBounds(L.featureGroup([...state.documentMarkers.values()]).getBounds(),{padding:[28,28],maxZoom:14})}}else failures++}catch(_){failures++}if(failures>=3)break}
}
async function loadDocumentRecords(){
  loadDocumentVillageCache();if(!state.user){renderDocumentList();return}
  try{const result=await api("/api/map/records");state.documentRecords=result.records||[];renderDocumentList();renderDocumentMarkers();geocodeDocumentVillages()}catch(_){state.documentRecords=[];renderDocumentList()}
}
function renderLocationPanel(p){
  const loc=p.location||{};const status=loc.status||p.location_status||"UNRESOLVED";const cls=locationClass(status);
  const lat=loc.latitude??p.latitude,lon=loc.longitude??p.longitude;const coords=lat!=null&&lon!=null?lat+", "+lon:"—";
  const note=status==="VILLAGE_LEVEL"?"Village-level location is approximate and does not represent the exact parcel.":status==="PARCEL_GEOMETRY"?"Location comes from project-owned parcel geometry; it is not an authoritative cadastral pin.":status==="EXACT_PIN"?"Exact coordinate was set by an authorized human reviewer and is recorded in the audit trail.":"No usable location is currently resolved.";
  const controls=canEditLocation()?"<div class=\"location-actions\"><button type=\"button\" class=\"btn secondary\" id=\"setPinBtn\">Set exact pin</button>"+(status==="EXACT_PIN"?"<button type=\"button\" class=\"btn secondary\" id=\"clearPinBtn\">Clear exact pin</button>":"")+"</div>":"";
  return "<div class=\"subhead\">Location</div><div class=\"location-state "+cls+"\"><strong>"+esc(locationLabel(status))+"</strong><br><span class=\"muted\">"+esc(note)+"</span><br><b>Coordinates:</b> "+esc(coords)+controls+"</div>";
}
function renderVillageSheet(features){
  const root=$("villageSheet");if(!root)return;if(!features.length){root.innerHTML="<div class=\"empty\">No parcel geometry is available for this village.</div>";return}
  const polys=features.filter(f=>f.geometry?.type==="Polygon");if(!polys.length){root.innerHTML="<div class=\"empty\">No polygon parcel geometry is available.</div>";return}
  const all=polys.flatMap(f=>f.geometry.coordinates?.[0]||[]),xs=all.map(x=>x[0]),ys=all.map(x=>x[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),dx=Math.max(maxX-minX,1e-9),dy=Math.max(maxY-minY,1e-9);
  root.innerHTML="<div class=\"village-sheet-title\">Village Sheet · project-owned schematic · not authoritative</div>";
  polys.forEach(f=>{const p=f.properties||{},ring=f.geometry.coordinates[0],px=ring.map(x=>((x[0]-minX)/dx)*94+3),py=ring.map(x=>((maxY-x[1])/dy)*84+9),left=Math.max(2,Math.min(...px)),top=Math.max(8,Math.min(...py)),right=Math.min(98,Math.max(...px)),bottom=Math.min(96,Math.max(...py)),b=document.createElement("button");
    b.type="button";b.className="sheet-parcel"+(state.selected?.property_id===p.property_id?" selected":"");b.style.left=left+"%";b.style.top=top+"%";b.style.width=Math.max(8,right-left)+"%";b.style.height=Math.max(12,bottom-top)+"%";b.textContent=p.parcel_id||p.property_id;b.title="Survey "+(p.survey_number||"—")+" · "+(p.area??"—");b.onclick=()=>selectParcel(p.property_id);root.appendChild(b)});
}
async function populateGeography(){
  const d=$("districtSelect"),t=$("talukaSelect"),v=$("villageSelect");if(!d||!t||!v)return;let tree={};
  try{tree=(await api(LAND_API+"/geography")).geography||{}}catch(_){(state.geo?.features||[]).forEach(f=>{const p=f.properties||{},a=p.district||"",b=p.taluka||"",z=p.village||"";tree[a]??={};tree[a][b]??={};tree[a][b][z]={parcel_count:1}})}
  const fill=(el,values,placeholder)=>{el.innerHTML="<option value=\"\">"+placeholder+"</option>"+values.map(x=>"<option value=\""+esc(x)+"\">"+esc(x)+"</option>").join("");el.disabled=values.length===0};
  const rebuild=()=>{const td=tree[d.value]||{};fill(t,Object.keys(td).filter(Boolean),"All talukas");const vd=td[t.value]||{};fill(v,Object.keys(vd).filter(Boolean),"All villages");loadFilteredMap(d.value,t.value,v.value)};
  d.onchange=rebuild;t.onchange=()=>{const td=tree[d.value]||{},vd=td[t.value]||{};fill(v,Object.keys(vd).filter(Boolean),"All villages");loadFilteredMap(d.value,t.value,v.value)};v.onchange=()=>loadFilteredMap(d.value,t.value,v.value);rebuild();
}
async function loadFilteredMap(district,taluka,village){
  const fs=(state.geo?.features||[]).filter(f=>{const p=f.properties||{};return(!district||p.district===district)&&(!taluka||p.taluka===taluka)&&(!village||p.village===village)});
  state.visibleFeatures=fs;renderParcels(fs);renderVillageSheet(fs);await loadMapLocations({district,taluka,village});
}
async function focusMapProperty(id){
  if(!state.map)return;const marker=state.locationMarkers.get(id);const layer=state.parcelLayers.get(id);if(marker){state.map.setView(marker.getLatLng(),17);marker.openPopup();return}if(layer){state.map.fitBounds(layer.getBounds(),{padding:[35,35],maxZoom:17});return}
  const p=state.selected;if(p?.latitude!=null&&p?.longitude!=null)state.map.setView([p.latitude,p.longitude],17);
}
function loadingPanel(id,title="Loading…"){const root=$(id);if(root)root.innerHTML='<div class="empty"><strong>'+esc(title)+'</strong><p>Please wait while evidence is retrieved.</p></div>'}

function initMap(){
  if(state.map)return;
  // The local Leaflet script normally runs before this file. If a deployed
  // asset is slow or the CDN fallback is still loading, retry instead of
  // permanently giving up during the first paint.
  if(!window.L){
    state.mapInitAttempts++;
    setStatus("Loading map library…");
    if(state.mapInitAttempts <= 20){
      clearTimeout(state.mapRetryTimer);
      state.mapRetryTimer=setTimeout(initMap,250);
    }else{
      notice("Interactive map library could not be loaded. Use the parcel list and evidence panels, or reload when online.");
      setStatus("Map library unavailable");
    }
    return;
  }
  try{
    state.mapInitAttempts=0;
    state.map=L.map("map",{zoomControl:true,preferCanvas:true,attributionControl:true}).setView([28.622,77.106],14);
    mapSetTileSource(mapSavedTileSource());
    window.addEventListener("resize",()=>state.map?.invalidateSize());
    state.map.on("click",e=>{
      if(state.documentPinMode&&state.selectedDocumentId){state.documentPinMode=false;renderDocumentList();setDocumentPin(state.selectedDocumentId,e.latlng.lat,e.latlng.lng);return}
      if(state.pinMode&&state.selected){state.pinMode=false;setExactPin(state.selected.property_id,e.latlng.lat,e.latlng.lng)}
    });
    // If Leaflet became available after the data requests finished, draw the
    // already-loaded geometry and markers now rather than waiting for refresh.
    if(state.visibleFeatures.length)renderParcels(state.visibleFeatures);
    if(state.locationRecords.size)renderLocationMarkers([...state.locationRecords.values()]);
    if(state.documentRecords.length)renderDocumentMarkers();
  }catch(e){
    state.map=null;
    notice("Map could not be initialized. Property intelligence remains available.");
    setStatus("Map unavailable");
  }
}
function fitAll(){
  if(!state.map||!state.parcelLayers.size){setStatus("No parcel geometry available");return}
  const group=L.featureGroup([...state.parcelLayers.values()]);
  if(group.getLayers().length)state.map.fitBounds(group.getBounds(),{padding:[28,28],maxZoom:17});
}
function renderMetrics(m){
  const labels=[["total_properties","Total properties"],["documents_processed","Documents"],["pending_verification","Pending review"],["review_required","Review required"],["conflicts","Conflicts"],["no_parcel_match","No parcel match"],["low_confidence","Low confidence"],["completed_cases","Verification cases"]];
  $("metrics").innerHTML=labels.map(([k,l])=>'<div class="metric"><b>'+esc(m[k]??0)+'</b><span>'+l+'</span></div>').join("");
}
function renderScenarios(){
  const root=$("scenarios");
  root.innerHTML=(state.scenarios||[]).map(s=>'<button type="button" class="scenario '+(state.activeScenario===s.id?"active":"")+'" data-scenario="'+esc(s.id)+'"><strong>'+esc(s.label)+'</strong><span>'+esc(s.finding)+'</span></button>').join("");
  root.querySelectorAll("[data-scenario]").forEach(b=>b.addEventListener("click",()=>runScenario(b.dataset.scenario)));
}
function renderParcels(features){
  const root=$("parcelList");
  if(!features.length){root.innerHTML='<div class="empty">No parcel records are available.</div>';return}
  root.innerHTML=features.map(f=>{const p=f.properties||{};return '<button type="button" class="parcel-item" data-property="'+esc(p.property_id)+'"><strong>'+esc(p.parcel_id)+'</strong><span class="muted">Survey '+esc(p.survey_number)+' · '+esc(p.area)+' '+esc(p.area_unit)+'</span></button>'}).join("");
  root.querySelectorAll("[data-property]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.property)));
  if(!state.map)return;
  state.parcelLayers.forEach(l=>l.remove());state.parcelLayers.clear();
  features.forEach(f=>{
    const p=f.properties||{};
    const layer=L.geoJSON(f,{style:{color:"#68788f",weight:2,fillOpacity:.2}}).bindTooltip((p.parcel_id||"Parcel")+" · "+(p.survey_number||""));
    layer.on("click",()=>selectParcel(p.property_id));layer.addTo(state.map);state.parcelLayers.set(p.property_id,layer);
  });
}
function selectMapLayer(id){
  state.parcelLayers.forEach(l=>l.setStyle({color:"#68788f",weight:2,fillOpacity:.2}));
  const layer=state.parcelLayers.get(id);
  if(layer){layer.setStyle({color:"#315ba8",weight:4,fillOpacity:.5});state.selectedLayer=layer;state.map?.fitBounds(layer.getBounds(),{padding:[35,35],maxZoom:17})}
}
function valueOrDash(v){return v===undefined||v===null||v===""?"—":v}
function renderProperty(p){
  const docs=p.documents||[], neighbours=p.neighbors||[];
  const fields=[
    ["Property ID",p.property_id],["Parcel ID",p.parcel_id],["District",p.district],["Taluka",p.taluka],
    ["Village",p.village],["Survey Number",p.survey_number],["Gat Number",p.gat_number],["Khasra Number",p.khasra_number],
    ["Sub-division",p.sub_division],["Area",(p.area??"—")+" "+(p.area_unit||"")],["Geometry status",p.georeferenced?"Georeferenced":"Not georeferenced"],
    ["Source",p.data_source],["Geometry source",p.geometry_source],["Confidence",Math.round((p.source_confidence||p.geometry_confidence||0)*100)+"%"],
    ["Resolution status",p.resolution_status||"DEMO / SYNTHETIC"]
  ];
  const history=p.ownership_history||{events:[],findings:[],relationships:[]};
  const historyRows=(history.events||[]).map(e=>'<div class="timeline-item"><i class="timeline-dot"></i><div><strong>'+esc(e.year||"Year unavailable")+' · '+esc(e.document_type)+'</strong><div class="muted">Owner: '+esc(valueOrDash(e.owner))+' · Survey: '+esc(valueOrDash(e.survey_number))+' · Khasra: '+esc(valueOrDash(e.khasra_number))+' · Area: '+esc(valueOrDash(e.area))+'</div></div></div>').join("");
  const findingRows=(history.findings||[]).map(f=>'<div class="finding"><strong>'+esc(f.title)+'</strong><br><span class="muted">'+esc(f.reason)+'</span><br><b>Human action:</b> '+esc(f.human_action)+'</div>').join("");
  const relRows=(history.relationships||[]).map(r=>'<div class="relationship"><strong>'+esc(r.relation_type)+'</strong><span>'+esc(r.from_property_id)+' → '+esc(r.to_property_id)+'</span></div>').join("");
  const docRows=docs.map(d=>'<div class="doc-row"><div><strong>'+esc(d.document_type||d.filename)+'</strong><div class="muted">'+esc(d.filename||d.document_id||"")+'</div></div><div>'+badge(d.status||d.verification_status||"UNKNOWN")+'</div></div>').join("");
  const neighbourRows=neighbours.map(n=>'<div class="doc-row"><div><strong>'+esc(n.parcel_id||n.property_id)+'</strong><div class="muted">'+esc(n.village||"")+' · '+esc(n.survey_number||"")+'</div></div><button class="btn secondary" type="button" data-neighbour="'+esc(n.property_id)+'">Inspect</button></div>').join("");
  $("propertyPanel").innerHTML='<div class="section-head"><div><span class="eyebrow">PROPERTY INTELLIGENCE</span><h2>'+esc(p.parcel_id||p.property_id)+'</h2></div>'+badge(p.resolution_status||"DEMO / SYNTHETIC")+'</div><div class="intel-grid">'+fields.map(x=>'<div><span>'+esc(x[0])+'</span><strong>'+esc(valueOrDash(x[1]))+'</strong></div>').join("")+'</div>'+renderLocationPanel(p)+'<div class="subhead">Documents</div>'+docRows+'<div class="subhead">Ownership timeline</div><div class="timeline">'+(historyRows||'<div class="muted">No timeline events.</div>')+'</div><div class="subhead">Derived findings</div>'+(findingRows||'<div class="muted">No derived findings.</div>')+'<div class="subhead">Property relationships</div>'+(relRows||'<div class="muted">No linked property relationships.</div>')+'<div class="subhead">Nearby project parcels</div>'+(neighbourRows||'<div class="muted">No nearby parcels.</div>');
  $("propertyPanel").querySelectorAll("[data-neighbour]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.neighbour)));
  $("propertyPanel").querySelector("#setPinBtn")?.addEventListener("click",()=>{state.pinMode=true;notice("Click the map to set an exact pin. This requires authorized verification and is audited.")});
  $("propertyPanel").querySelector("#clearPinBtn")?.addEventListener("click",()=>clearExactPin(p.property_id));
}
function renderEvidence(data){
  const docs=data.documents||[],findings=data.findings||[],tasks=data.tasks||[],compare=data.comparison||null;
  const compareHtml=compare?'<div class="comparison"><div><span>Identity match</span><strong>'+badge(compare.identity_match)+'</strong></div><div><span>Area difference</span><strong>'+esc(compare.area_difference??"—")+'</strong></div><div><span>Transfer risk</span><strong>'+badge(compare.transfer_risk||"UNKNOWN")+'</strong></div></div>':'';
  const findingHtml=findings.map(f=>'<div class="finding"><strong>'+esc(f.title)+'</strong><div>'+badge(f.severity||f.status||"OPEN")+'</div><p>'+esc(f.reason||f.description||"")+'</p><small>Action: '+esc(f.human_action||"Human review")+'</small></div>').join("");
  const taskHtml=tasks.map(t=>'<div class="doc-row"><div><strong>'+esc(t.title)+'</strong><div class="muted">'+esc(t.assignee||"Unassigned")+'</div></div>'+badge(t.status||"OPEN")+'</div>').join("");
  const docHtml=docs.map(d=>'<div class="doc-row"><div><strong>'+esc(d.document_type||d.filename)+'</strong><div class="muted">'+esc(d.filename||d.document_id||"")+'</div></div>'+badge(d.status||d.verification_status||"UNKNOWN")+'</div>').join("");
  $("evidencePanel").innerHTML='<div class="section-head"><div><span class="eyebrow">EVIDENCE & VERIFICATION</span><h2>Defensible review</h2></div></div>'+compareHtml+'<div class="subhead">Findings</div>'+(findingHtml||'<div class="muted">No findings.</div>')+'<div class="subhead">Verification tasks</div>'+(taskHtml||'<div class="muted">No tasks.</div>')+'<div class="subhead">Evidence</div>'+(docHtml||'<div class="muted">No linked evidence.</div>');
}
async function selectParcel(id){
  try{loadingPanel("propertyPanel","Loading property intelligence…");const p=await api(API+"/properties/"+encodeURIComponent(id));state.selected=p;renderProperty(p);selectMapLayer(id);await focusMapProperty(id);const cmp=await api(API+"/compare/"+encodeURIComponent((p.documents||[])[0]?.document_id||"NONE")+"/"+encodeURIComponent(id)).catch(()=>({}));renderEvidence({documents:p.documents||[],findings:p.findings||[],tasks:p.tasks||[],comparison:cmp});}catch(e){notice(e.message||"Property could not be loaded.")}
}
async function loadGeo(){
  try{state.geo=await api(LAND_API+"/geojson")}catch(_){state.geo=await api(API+"/geojson")}
  state.visibleFeatures=state.geo.features||[];renderParcels(state.visibleFeatures);renderVillageSheet(state.visibleFeatures)
}
async function loadAll(){
  if(state.loading)return;state.loading=true;try{
    const [m,s]=await Promise.all([api(API+"/dashboard"),api(API+"/scenarios")]);state.metrics=m;state.scenarios=s.scenarios||[];renderMetrics(m);renderScenarios();await loadCurrentUser();await loadGeo();await populateGeography();await loadMapLocations();await loadDocumentRecords();setStatus("Map ready");
  }catch(e){notice(e.message||"Land Intelligence could not be loaded.")}finally{state.loading=false}
}
async function runScenario(id){
  state.activeScenario=id;renderScenarios();try{const d=await api(API+"/scenario/"+encodeURIComponent(id));if(d.property_id)await selectParcel(d.property_id);renderEvidence(d);}catch(e){notice(e.message||"Scenario could not be loaded.")}
}
async function search(){const q=$("search")?.value.trim();if(!q)return;try{const d=await api(API+"/properties?q="+encodeURIComponent(q));const root=$("results");root.innerHTML=(d.properties||[]).map(p=>'<button type="button" class="result-item" data-property="'+esc(p.property_id)+'"><strong>'+esc(p.parcel_id||p.property_id)+'</strong><span>'+esc(p.village||"")+' · '+esc(p.survey_number||"")+'</span></button>').join("")||'<div class="empty">No properties found.</div>';root.querySelectorAll("[data-property]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.property)));}catch(e){notice(e.message||"Search failed.")}}
async function setExactPin(id,lat,lon){
  try{const r=await fetch(LAND_API+"/properties/"+encodeURIComponent(id)+"/location",{method:"POST",headers:{...authHeaders({"Content-Type":"application/json","Accept":"application/json"})},body:JSON.stringify({latitude:lat,longitude:lon})});let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw new Error(d.detail||"Pin update failed");state.selected=d.property||state.selected;renderProperty(state.selected);await loadMapLocations();notice("Exact pin saved and audited.")}catch(e){notice(e.message||"Exact pin could not be saved.")}
}
async function clearExactPin(id){
  try{const r=await fetch(LAND_API+"/properties/"+encodeURIComponent(id)+"/location",{method:"DELETE",headers:authHeaders({"Accept":"application/json"})});let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw new Error(d.detail||"Pin removal failed");state.selected=d.property||state.selected;renderProperty(state.selected);await loadMapLocations();notice("Exact pin cleared and audited.")}catch(e){notice(e.message||"Exact pin could not be cleared.")}
}
function wire(){
  $("searchBtn")?.addEventListener("click",search);$("search")?.addEventListener("keydown",e=>{if(e.key==="Enter")search()});$("fitBtn")?.addEventListener("click",fitAll);$("villageSheetBtn")?.addEventListener("click",()=>$("villageSheet")?.classList.toggle("hidden"));$("mapTileSource")?.addEventListener("change",e=>mapSetTileSource(e.target.value));$("schematicBtn")?.addEventListener("click",()=>mapSetTileSource("schematic"));$("documentMapSearch")?.addEventListener("input",()=>{renderDocumentList();renderDocumentMarkers()});$("recordPinBtn")?.addEventListener("click",()=>{if(!canEditLocation()){notice("Only a Verification Officer or Administrator can set document pins.");return}if(!state.selectedDocumentId){notice("Select a document record first.");return}state.documentPinMode=!state.documentPinMode;state.pinMode=false;renderDocumentList();notice(state.documentPinMode?"Click the map to set the selected document's exact pin.":"Document pin mode cancelled.")});$("demoBtn")?.addEventListener("click",()=>runScenario(state.scenarios[0]?.id||""));$("resetBtn")?.addEventListener("click",()=>location.reload());
  document.querySelectorAll("[data-font]").forEach(b=>b.addEventListener("click",()=>{const d=Number(b.dataset.font);document.documentElement.style.fontSize=(16+d)+"px"}));$("contrastBtn")?.addEventListener("click",()=>document.body.classList.toggle("high-contrast"));
}
document.addEventListener("DOMContentLoaded",()=>{wire();initMap();loadAll();});
})();
