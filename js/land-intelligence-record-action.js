(function(){
  'use strict';

  function addActions(){
    ['#simpleDocTable', '#simpleSubmissionsTable', '#staffRecordsTable'].forEach(function(selector){
      var table = document.querySelector(selector);
      if(!table) return;

      table.querySelectorAll('tbody tr').forEach(function(row){
        if(row.dataset.landIntelligenceAction === '1') return;

        var cells = row.querySelectorAll('td');
        if(!cells.length) return;

        var idMatch = (cells[0].textContent || '').match(/#(\d+)/);
        if(!idMatch) return;

        var docId = idMatch[1];
        var actionCell = cells[cells.length - 1];
        if(!actionCell) return;

        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost';
        button.textContent = 'Land Intelligence';
        button.style.cssText = 'padding:4px 10px;font-size:11px;margin-left:6px;white-space:nowrap;';
        button.title = 'Open this saved document in Land Intelligence';
        button.addEventListener('click', function(){
          window.location.href = '/land-intelligence?document_id=' + encodeURIComponent(docId);
        });

        actionCell.appendChild(button);
        row.dataset.landIntelligenceAction = '1';
      });
    });
  }

  function start(){
    addActions();
    new MutationObserver(addActions).observe(document.documentElement, {subtree:true, childList:true});
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', start, {once:true});
  }else{
    start();
  }
})();
