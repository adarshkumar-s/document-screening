(() => {
  "use strict";
  const esc = v => String(v ?? "—").replace(/[&<>\"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#39;"}[c]));
  const tokenHeaders = () => { const h={Accept:"application/json"}; try { const t=localStorage.getItem("lrtoken"); if(t) h.Authorization="Bearer "+t; } catch(_){} return h; };
  const api = async (url, options={}) => { const r=await fetch(url,{...options,headers:{...tokenHeaders(),...(options.headers||{})}}); let d={}; try{d=await r.json()}catch(_){} if(!r.ok) throw new Error(d.detail||d.error||`Request failed (${r.status})`); return d; };
  function landUrl(id){ return `/land-intelligence?document_id=${encodeURIComponent(id)}`; }

  function addRecordButtons(){
    const table=document.querySelector("#simpleDocTable");
    if(!table) return;
    table.querySelectorAll("tbody tr").forEach(row=>{
      if(row.querySelector(".land-intel-action")) return;
      const cells=row.querySelectorAll("td");
      if(cells.length<7) return;
      const id=(cells[0].textContent||"").trim();
      if(!id) return;
      const a=document.createElement("a");
      a.className="btn ghost land-intel-action";
      a.href=landUrl(id);
      a.textContent="Land Intelligence →";
      a.title="Investigate this saved document in Land Intelligence";
      a.style.cssText="display:inline-block;padding:5px 9px;font-size:11px;text-decoration:none;margin-left:5px";
      cells[6].appendChild(a);
    });
  }

  function observeRecords(){
    addRecordButtons();
    const table=document.querySelector("#simpleDocTable");
    if(table) new MutationObserver(addRecordButtons).observe(table,{subtree:true,childList:true});
  }

  function investigationPanel(d){
    const root=document.querySelector(".scenario-panel");
    if(!root) return;
    let panel=document.getElementById("savedDocumentInvestigation");
    if(!panel){ panel=document.createElement("section"); panel.id="savedDocumentInvestigation"; panel.className="panel"; root.insertAdjacentElement("afterend",panel); }
    const s=d.document||{}, r=d.resolution||{}, p=d.property, related=d.related_documents||[], match=r.matches?.[0];
    const status=match?`PROPERTY MATCH · ${Math.round((match.confidence||0)*100)}%`:`PROPERTY RESOLUTION · ${esc(r.status||"REVIEW REQUIRED")}`;
    const relHtml=related.length?related.map(x=>`<div class="record-card"><div class="record-main"><strong>#${esc(x.id)} · ${esc(x.filename)}</strong><span>${esc(x.doc_type)} · ${esc(x.owner)} · Survey ${esc(x.survey)}</span><small>${esc(x.village)}, ${esc(x.district)} · matched: ${esc((x.matched_fields||[]).join(", "))}</small></div><div class="record-meta"><span class="status neutral">${esc(x.status||"UNKNOWN")}</span><a class="btn ghost" style="padding:4px 8px;font-size:11px;text-decoration:none" href="${landUrl(x.id)}">Investigate →</a></div></div>`).join(""):`<div class="empty">No other saved record shares enough land-identity fields yet.</div>`;
    const propertyHtml=p?`<div class="intel-grid"><div><b>Parcel</b><span>${esc(p.parcel_id)}</span></div><div><b>Survey</b><span>${esc(p.survey_number)}</span></div><div><b>Village</b><span>${esc(p.village)}</span></div><div><b>District</b><span>${esc(p.district)}</span></div><div><b>Area</b><span>${esc(p.area)} ${esc(p.area_unit||"")}</span></div><div><b>Location</b><span>${p.latitude!=null?`${esc(p.latitude)}, ${esc(p.longitude)}`:"Not georeferenced"}</span></div></div>`:`<div class="empty"><strong>No automatic parcel selected</strong><p>Use the property search or verification workflow to resolve the parcel manually.</p></div>`;
    panel.innerHTML=`<div class="section-head"><div><span class="eyebrow">SAVED DOCUMENT INVESTIGATION</span><h2>${esc(status)}</h2></div><span class="muted">Source record #${esc(s.id)}</span></div><div class="card" style="margin-bottom:12px"><strong>${esc(s.filename)}</strong><div class="muted" style="margin-top:5px">${esc(s.doc_type)} · Owner: ${esc(s.owner)} · Survey: ${esc(s.survey)} · ${esc(s.village)}, ${esc(s.district)}</div><div class="muted" style="margin-top:5px">OCR confidence: ${esc(s.confidence??0)}% · Processing status: ${esc(s.status)}</div></div><div class="card" style="margin-bottom:12px"><h3>Resolved property</h3>${propertyHtml}</div><div class="card"><h3>Related saved records (${esc(d.related_count||0)})</h3><div class="scenario-list">${relHtml}</div></div>`;
  }

  async function hydrateLandPage(){
    const id=new URLSearchParams(location.search).get("document_id");
    if(!id) return;
    try{
      const d=await api(`/api/land/intelligence/document/${encodeURIComponent(id)}`);
      const source=d.document||{};
      const root=document.querySelector(".hero p");
      if(root) root.innerHTML=`Investigating saved source record <strong>#${esc(source.id)}</strong> · ${esc(source.filename)}. OCR evidence remains the source; property and parcel conclusions are reviewable.`;
      investigationPanel(d);
      if(d.matches?.[0]?.property){
        const p=d.matches[0].property, search=document.getElementById("search"), btn=document.getElementById("searchBtn"), key=p.survey_number||p.parcel_id||p.village||"";
        if(search&&btn&&key){ search.value=key; btn.click(); }
        const status=document.getElementById("mapStatus");
        if(status) status.textContent=`Source #${source.id} · property match ${Math.round((d.matches[0].confidence||0)*100)}%`;
      } else {
        const status=document.getElementById("mapStatus");
        if(status) status.textContent=`Source #${source.id} · property match requires review`;
      }
    }catch(e){
      const status=document.getElementById("mapStatus");
      if(status) status.textContent="Source record could not be resolved; manual property search is available.";
    }
  }

  function start(){ if(location.pathname==="/land-intelligence") hydrateLandPage(); else observeRecords(); }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",start,{once:true}); else start();
})();
