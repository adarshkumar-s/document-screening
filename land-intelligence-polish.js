(() => {
  "use strict";
  const replacements = [
    ["DEMO / SYNTHETIC DATA", "OPERATIONAL WORKSPACE"],
    ["DEMO / SYNTHETIC", "PROJECT DATASET"],
    ["DEMO / SYNTHETIC DATASET", "PROJECT DATASET"],
    ["Synthetic project data only", "Project workspace"],
    ["Synthetic/project-owned geography.", "Geography linked to the current parcel dataset."],
    ["Demo GIS dataset", "Parcel geometry dataset"],
    ["Synthetic/demo dataset", "Application record dataset"],
    ["Synthetic GIS dataset", "Parcel geometry dataset"],
    ["Project synthetic dataset", "Application parcel dataset"],
    ["project-owned schematic · not authoritative", "parcel view · verification required"],
    ["project-owned synthetic geometry", "parcel geometry · verification required"],
    ["DEMO-SYNTHETIC", "PROJECT DATASET"]
  ];
  function clean(root=document.body){
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    const nodes=[];let n;while(n=walker.nextNode())nodes.push(n);
    nodes.forEach(node=>{let t=node.nodeValue;replacements.forEach(([a,b])=>{t=t.replaceAll(a,b)});if(t!==node.nodeValue)node.nodeValue=t});
  }
  function start(){
    clean();
    const observer=new MutationObserver(()=>clean());
    observer.observe(document.body,{subtree:true,childList:true,characterData:true});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
})();
