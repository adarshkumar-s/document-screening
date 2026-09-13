(() => {
"use strict";
const state={map:null,geo:null,metrics:null,selected:null,parcelLayers:new Map(),selectedLayer:null,locationMarkers:new Map(),locationRecords:new Map(),scenarios:[],activeScenario:null,loading:false,user:null,pinMode:false};
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
  renderParcels(fs);renderVillageSheet(fs);await loadMapLocations({district,taluka,village});
}
async function focusMapProperty(id){
  if(!state.map)return;const marker=state.locationMarkers.get(id);const layer=state.parcelLayers.get(id);if(marker){state.map.setView(marker.getLatLng(),17);marker.openPopup();return}if(layer){state.map.fitBounds(layer.getBounds(),{padding:[35,35],maxZoom:17});return}
  const p=state.selected;if(p?.latitude!=null&&p?.longitude!=null)state.map.setView([p.latitude,p.longitude],17);
}
function loadingPanel(id,title="Loading…"){const root=$(id);if(root)root.innerHTML='<div class="empty"><strong>'+esc(title)+'</strong><p>Please wait while evidence is retrieved.</p></div>'}

function initMap(){
  if(!window.L){notice("Interactive map library unavailable. Parcel intelligence remains available.");setStatus("Map library unavailable");return}
  try{
    state.map=L.map("map",{zoomControl:true,preferCanvas:true,attributionControl:true}).setView([28.622,77.106],14);
    const tile=L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{attribution:"© OpenStreetMap contributors",minZoom:3,maxZoom:19});
    tile.on("load",()=>{clearNotice();setStatus("Map ready")});
    tile.on("tileerror",()=>notice("Base map unavailable. Parcel geometry and evidence remain usable."));
    tile.addTo(state.map);
    window.addEventListener("resize",()=>state.map?.invalidateSize());
    state.map.on("click",e=>{if(!state.pinMode||!state.selected)return;state.pinMode=false;setExactPin(state.selected.property_id,e.latlng.lat,e.latlng.lng)});
  }catch(e){notice("Map could not be initialized. Property intelligence remains available.");setStatus("Map unavailable")}
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
  $("propertyPanel").innerHTML='<div class="property-head"><div><span class="eyebrow">PROPERTY INTELLIGENCE</span><h3>'+esc(p.property_id)+'</h3></div>'+badge("DEMO / SYNTHETIC")+'</div>'+
    '<div class="intel-grid">'+fields.map(([a,b])=>'<div class="kv"><small>'+esc(a)+'</small><b>'+esc(valueOrDash(b))+'</b></div>').join("")+'</div>'+
    '<div class="subhead">Ownership history</div>'+
    '<div class="timeline">'+(historyRows||'<span class="muted">No ownership events detected.</span>')+'</div>'+
    (findingRows?'<div class="subhead">Ownership assessment</div>'+findingRows:'')+
    '<div class="subhead">Linked documents</div>'+
    (docs.length?docs.map(d=>'<button type="button" class="result" data-doc="'+esc(d.id)+'"><strong>'+esc(d.filename)+'</strong><span class="muted">'+esc(d.doc_type)+' · OCR '+Math.round((d.ocr_confidence||0)*100)+'% · '+esc(d.verification_status)+'</span></button>').join(""):'<span class="muted">No linked document is available.</span>')+
    '<div class="subhead">Neighbouring parcels</div>'+
    (neighbours.length?neighbours.map(n=>'<button type="button" class="result" data-neighbor="'+esc(n.property_id)+'"><strong>'+esc(n.parcel_id)+'</strong><span class="muted">Survey '+esc(n.survey_number)+' · '+esc(n.area)+' '+esc(n.area_unit)+'</span></button>').join(""):'<span class="muted">No adjacent parcels detected.</span>');
  $("propertyPanel").querySelectorAll("[data-doc]").forEach(b=>b.addEventListener("click",()=>compareDoc(b.dataset.doc,p.property_id)));
  $("propertyPanel").querySelectorAll("[data-neighbor]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.neighbor)));
  $("propertyPanel").insertAdjacentHTML("beforeend",renderLocationPanel(p)+'<div class="location-actions"><button type="button" class="btn secondary" id="showMapBtn">Show on map</button></div>');
  $("showMapBtn").onclick=()=>focusMapProperty(p.property_id);
  if(canEditLocation()){const sb=$("setPinBtn");if(sb)sb.onclick=()=>{state.pinMode=true;notice("Exact-pin mode: click the map to place the selected property. The server validates and audits the change.");setStatus("Click the map to set exact pin")};const cb=$("clearPinBtn");if(cb)cb.onclick=()=>clearExactPin(p.property_id)}

}
function setExactPin(propertyId,lat,lon){
  if(!canEditLocation()){notice("Only Verification Officers or Administrators may set an exact pin.");return}
  const current=state.locationRecords.get(propertyId);
  fetch(LAND_API+"/properties/"+encodeURIComponent(propertyId)+"/location",{method:"PUT",headers:authHeaders({"Content-Type":"application/json","Accept":"application/json"}),body:JSON.stringify({latitude:lat,longitude:lon,reason:"Placed from Land Intelligence map.",expected_location_updated_at:current?.location?.updated_at})})
    .then(async r=>{let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw new Error(d.detail||"Location update failed");return d})
    .then(async()=>{await loadMapLocations();await selectParcel(propertyId);setStatus("Exact location saved and audited")})
    .catch(e=>{notice("Exact pin was not saved: "+e.message);setStatus("Pin update failed")});
}
function clearExactPin(propertyId){
  if(!canEditLocation()||!window.confirm("Clear the exact pin for this property?"))return;
  const current=state.locationRecords.get(propertyId);
  fetch(LAND_API+"/properties/"+encodeURIComponent(propertyId)+"/location",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({reason:"Cleared from Land Intelligence.",expected_location_updated_at:current?.location?.updated_at})})
    .then(async r=>{let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw new Error(d.detail||"Location update failed");return d})
    .then(async()=>{await loadMapLocations();await selectParcel(propertyId);setStatus("Exact location cleared")})
    .catch(e=>notice("Exact pin was not cleared: "+e.message));
}
function renderEvidence(p){
  const doc=(p.documents||[])[0];
  $("evidencePanel").innerHTML='<span class="eyebrow">EVIDENCE & VERIFICATION</span><h3 style="margin:3px 0;font-size:16px">'+esc(doc?doc.filename:"Evidence trail")+'</h3>'+
    (doc?'<div class="evidence-section"><div class="finding"><strong>Verification state:</strong> '+esc(doc.verification_status)+'<br><span class="muted">Human review remains authoritative.</span></div><button type="button" class="btn secondary" id="comparePrimary" style="margin-top:8px">Run document ↔ parcel comparison</button></div>':'<p class="muted">No linked document is available for this parcel.</p>')+
    '<div class="evidence-section"><div class="subhead">Provenance</div>'+((p.provenance||[]).slice(0,8).map(x=>'<div class="kv" style="margin-top:6px"><small>'+esc(x.field_name)+' · '+esc(x.source)+'</small><b>'+esc(x.value)+' · '+Math.round((x.confidence||0)*100)+'% confidence</b></div>').join("")||'<span class="muted">No provenance entries.</span>')+'</div>'+
    '<div class="evidence-section"><div class="subhead">Timeline</div><div class="timeline">'+((p.timeline||[]).map(x=>'<div class="timeline-item"><i class="timeline-dot"></i><div><strong>'+esc(x.event_type)+'</strong><div class="muted">'+esc(x.description)+'</div></div></div>').join("")||'<span class="muted">No recorded events.</span>')+'</div></div>'+
    '<div class="evidence-section"><div class="subhead">Ownership reasoning</div>'+(((p.ownership_history||{}).findings||[]).map(f=>'<div class="finding"><strong>'+esc(f.title)+'</strong><br>'+esc(f.reason)+'<br><b>Recommendation:</b> '+esc(f.human_action)+'</div>').join("")||'<span class="muted">No ownership-change finding.</span>')+'</div>'+
    '<div class="evidence-section"><div class="finding"><strong>Human verification:</strong> Evidence is decision support only. Do not treat confidence as legal truth.</div></div>';
  if(doc)$("comparePrimary").onclick=()=>compareDoc(doc.id,p.property_id);
}
async function compareDoc(docId,propertyId){
  loadingPanel("evidencePanel","Comparing document and parcel…");
  try{
    const r=await api(API+"/compare/"+encodeURIComponent(docId)+"/"+encodeURIComponent(propertyId));
    const rows=(r.checks||[]).map(c=>'<tr><td>'+esc(c.label)+'</td><td>'+esc(valueOrDash(c.document))+'</td><td>'+esc(valueOrDash(c.parcel))+'</td><td>'+badge(c.status)+(c.difference!==undefined?'<div class="muted">Δ '+esc(c.difference)+' · tolerance '+esc(c.tolerance)+'</div>':"")+'</td><td>'+esc(c.source||"Evidence comparison")+'</td><td>'+esc(Math.round(((c.confidence?.document)||0)*100))+'% / '+esc(Math.round(((c.confidence?.parcel)||0)*100))+'%</td></tr>').join("");
    $("evidencePanel").innerHTML='<span class="eyebrow">DOCUMENT ↔ PARCEL</span><div class="property-head"><h3>Comparison result</h3>'+badge(r.overall_status)+'</div>'+
      '<div style="overflow:auto"><table class="comparison"><thead><tr><th>Field</th><th>Document</th><th>Property / Parcel</th><th>Result</th><th>Source</th><th>Confidence</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
      '<div class="evidence-section"><div class="finding"><strong>What matters:</strong> '+esc(r.explanation)+'</div></div>'+
      '<div class="evidence-section"><div class="subhead">Human verification task</div><p class="muted">Review conflicting or uncertain fields against the source document and authorized land records. No demo action changes authoritative records.</p></div>';
  }catch(e){$("evidencePanel").innerHTML='<div class="empty"><strong>Comparison unavailable</strong><p>'+esc(e.message)+'</p></div>'}
}
async function selectParcel(id){
  setStatus("Loading property intelligence…");loadingPanel("propertyPanel","Loading property intelligence…");loadingPanel("evidencePanel","Loading evidence…");
  try{const p=await api(API+"/properties/"+encodeURIComponent(id));if(p.error)throw new Error(p.error);if(state.user){try{const live=await api(LAND_API+"/properties/"+encodeURIComponent(id));p.location={status:live.location_status||"UNRESOLVED",source:live.location_source,confidence:live.location_confidence,verified_by:live.location_verified_by,verified_at:live.location_verified_at,updated_at:live.location_updated_at,latitude:live.latitude,longitude:live.longitude};}catch(_){}}state.selected=p;selectMapLayer(p.property_id);renderProperty(p);renderEvidence(p);renderVillageSheet(state.geo?.features||[]);setStatus("Selected "+(p.parcel_id||p.property_id))}
  catch(e){$("propertyPanel").innerHTML='<div class="empty"><strong>Unable to load property</strong><p>'+esc(e.message)+'</p></div>';$("evidencePanel").innerHTML='<div class="empty"><strong>Evidence unavailable</strong><p>'+esc(e.message)+'</p></div>';setStatus("Property unavailable")}
}
async function search(){
  const q=$("search").value.trim();if(!q){$("results").innerHTML="";return}
  $("results").innerHTML='<div class="muted">Searching parcels…</div>';
  try{const r=await api(API+"/properties?q="+encodeURIComponent(q));const rows=r.properties||[];$("results").innerHTML=rows.map(p=>'<button type="button" class="result" data-result="'+esc(p.property_id)+'"><strong>'+esc(p.parcel_id)+'</strong><span class="muted">'+esc(p.survey_number)+' · '+esc(p.village)+'</span></button>').join("")||'<span class="muted">No candidate parcels.</span>';$("results").querySelectorAll("[data-result]").forEach(b=>b.addEventListener("click",()=>selectParcel(b.dataset.result)))}catch(e){$("results").textContent=e.message}
}
async function runScenario(id){
  state.activeScenario=id;renderScenarios();loadingPanel("propertyPanel","Loading scenario…");loadingPanel("evidencePanel","Loading scenario evidence…");
  try{const d=await api(API+"/scenario/"+encodeURIComponent(id));if(d.error)throw new Error(d.error);if(d.property){await selectParcel(d.property.property_id);if(d.comparison&&d.document)await compareDoc(d.document.id,d.property.property_id)}else{$("propertyPanel").innerHTML='<div class="property-head"><div><span class="eyebrow">PROPERTY RESOLUTION</span><h3>No parcel match</h3></div>'+badge("NO MATCH")+'</div><div class="finding"><strong>'+esc(d.document.filename)+'</strong><br>Survey '+esc(d.document.fields?.survey_number)+' did not resolve to a demo parcel. No coordinates were fabricated.</div>';$("evidencePanel").innerHTML='<span class="eyebrow">DOCUMENT EVIDENCE</span><h3 style="font-size:16px">'+esc(d.document.doc_type)+'</h3><div class="intel-grid">'+Object.entries(d.document.fields||{}).map(([k,v])=>'<div class="kv"><small>'+esc(k)+'</small><b>'+esc(v)+'</b></div>').join("")+'</div><div class="evidence-section"><div class="subhead">Provenance</div><p class="muted">'+esc(d.document.provenance)+'</p></div>'}}
  catch(e){$("propertyPanel").innerHTML='<div class="empty"><strong>Scenario failed</strong><p>'+esc(e.message)+'</p></div>';$("evidencePanel").innerHTML='<div class="empty"><strong>Evidence unavailable</strong><p>'+esc(e.message)+'</p></div>'}
}
function reset(){
  state.activeScenario=null;renderScenarios();$("search").value="";$("results").innerHTML="";clearNotice();
  if(state.geo?.features?.length)selectParcel(state.geo.features[0].properties.property_id);else{loadingPanel("propertyPanel","No property selected");loadingPanel("evidencePanel","No evidence selected")}
  setStatus("Workspace reset");
}
async function load(){
  if(state.loading)return;state.loading=true;setStatus("Loading parcel dataset…");
  try{
    initMap();
    await loadCurrentUser();
    const [geo,metrics,scenarios]=await Promise.all([api(API+"/geojson"),api(API+"/dashboard"),api(API+"/scenarios")]);
    state.geo=geo;state.metrics=metrics;state.scenarios=scenarios.scenarios||[];
    renderMetrics(metrics);renderScenarios();renderParcels(geo.features||[]);renderVillageSheet(geo.features||[]);if(state.user)await loadMapLocations();if(state.user)await populateGeography();
    if(state.map&&state.parcelLayers.size)fitAll();
    if(geo.features?.length)await selectParcel(geo.features[0].properties.property_id);else{notice("No parcel data is available.");loadingPanel("propertyPanel","No parcel data");loadingPanel("evidencePanel","No evidence data")}
  }catch(e){notice("Land Intelligence data is unavailable: "+e.message);setStatus("Data unavailable");$("metrics").innerHTML="";$("scenarios").innerHTML="";loadingPanel("propertyPanel","Land Intelligence unavailable");loadingPanel("evidencePanel","Evidence unavailable")}
  finally{state.loading=false}
}
$("searchBtn").addEventListener("click",search);
$("search").addEventListener("keydown",e=>{if(e.key==="Enter")search()});
$("demoBtn").addEventListener("click",()=>runScenario("area-review"));
$("resetBtn").addEventListener("click",reset);
$("fitBtn").addEventListener("click",fitAll);$("villageSheetBtn")?.addEventListener("click",()=>{$("villageSheet")?.classList.toggle("hidden")});
document.querySelectorAll("[data-font]").forEach(b=>b.addEventListener("click",()=>{const v=Number(b.dataset.font);const current=parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--font-scale"))||14;document.documentElement.style.setProperty("--font-scale",(v===0?14:Math.max(12,Math.min(18,current+v)))+"px")}));
$("contrastBtn").addEventListener("click",()=>document.body.classList.toggle("high-contrast"));
window.addEventListener("load",load);
})();