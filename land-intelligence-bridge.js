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

  async function hydrateLandPage(){
    const id=new URLSearchParams(location.search).get("document_id");
    if(!id) return;
    try{
      const d=await api(`/api/land/intelligence/document/${encodeURIComponent(id)}`);
      const source=d.document||{};
      const matches=d.matches||[];
      const root=document.querySelector(".hero p");
      if(root) root.innerHTML=`Investigating saved source record <strong>#${esc(source.id)}</strong> · ${esc(source.filename)}. OCR evidence remains the source; property and parcel conclusions are reviewable.`;
      const scenarios=document.querySelector("#scenarios");
      if(scenarios){
        const m=matches[0];
        scenarios.insertAdjacentHTML("afterbegin",`<div class="record-card" style="border-left:4px solid #315ba8"><div class="record-main"><strong>Source record #${esc(source.id)} · ${esc(source.filename)}</strong><span>${esc(source.doc_type||"Land Record")} · ${esc(source.owner||"—")} · Survey ${esc(source.survey||"—")}</span><small>${esc(source.village||"—")}, ${esc(source.district||"—")} · OCR ${esc(source.confidence??0)}%</small></div><div class="record-meta"><span class="status ok">LAND INTELLIGENCE INPUT</span><span class="muted">${m?`Property match ${esc(Math.round((m.confidence||0)*100))}%`:"Property match pending"}</span></div></div>`);
      }
      if(matches[0]?.property){
        const p=matches[0].property;
        const search=document.getElementById("search");
        const btn=document.getElementById("searchBtn");
        const key=p.survey_number||p.parcel_id||p.village||"";
        if(search&&btn&&key){ search.value=key; btn.click(); }
        const status=document.getElementById("mapStatus");
        if(status) status.textContent=`Source #${source.id} · property match ${Math.round((matches[0].confidence||0)*100)}%`;
      } else {
        const status=document.getElementById("mapStatus");
        if(status) status.textContent=`Source #${source.id} · property match requires review`;
        const panel=document.getElementById("propertyPanel");
        if(panel) panel.innerHTML=`<div class="empty"><strong>Property resolution</strong><p>No parcel matched the saved document strongly enough for automatic selection.</p><p class="muted">${esc((d.reasons||[]).join(" "))}</p></div>`;
      }
    }catch(e){
      const status=document.getElementById("mapStatus");
      if(status) status.textContent="Source record could not be resolved; manual property search is available.";
    }
  }

  function start(){
    if(location.pathname==="/land-intelligence") hydrateLandPage();
    else observeRecords();
  }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",start,{once:true}); else start();
})();
