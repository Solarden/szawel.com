// yoman's row-detail modal and browser-local timestamps, copied from the real app. Detail goes
// in through textContent only, never as markup.
(function(){
  var modal=document.getElementById('modal'),head=document.getElementById('modal-head'),body=document.getElementById('modal-body');
  function close(){modal.hidden=true;head.textContent='';body.replaceChildren();}
  modal.querySelector('.modal-backdrop').addEventListener('click',close);
  document.addEventListener('keydown',function(e){if(e.key==='Escape')close();});
  function render(obj){
    var frag=document.createDocumentFragment(),keys=Object.keys(obj||{});
    if(!keys.length){var m=document.createElement('div');m.className='muted';m.textContent='(no detail)';frag.appendChild(m);return frag;}
    keys.forEach(function(k){
      var h=document.createElement('div');h.className='m-key';h.textContent=k;frag.appendChild(h);
      var v=obj[k];
      if(Array.isArray(v)){
        if(!v.length){var z=document.createElement('div');z.className='muted';z.textContent='[]';frag.appendChild(z);return;}
        var ul=document.createElement('ul');
        v.forEach(function(it){var li=document.createElement('li');
          li.textContent=(it&&typeof it==='object')?Object.entries(it).map(function(p){return p[0]+': '+p[1];}).join('  \u00b7  '):String(it);
          ul.appendChild(li);});
        frag.appendChild(ul);
      }else{var pre=document.createElement('pre');pre.textContent=(v&&typeof v==='object')?JSON.stringify(v,null,2):String(v);frag.appendChild(pre);}
    });
    return frag;
  }
  document.querySelectorAll('tr.logrow').forEach(function(row){
    row.addEventListener('click',function(){
      var c=row.children;
      head.textContent=c[0].textContent+'  \u00b7  '+c[1].textContent+' / '+c[2].textContent+'  \u00b7  '+c[3].textContent.trim();
      body.replaceChildren();
      var s=document.createElement('div');s.className='m-summary';s.textContent=c[5].textContent;body.appendChild(s);
      var d={};try{d=JSON.parse(row.dataset.detail||'{}');}catch(e){}
      body.appendChild(render(d));
      modal.hidden=false;
    });
  });
})();
(function(){
  var p=function(n){return String(n).padStart(2,'0');};
  Array.prototype.forEach.call(document.querySelectorAll('[data-ts]'),function(el){
    var d=new Date(el.getAttribute('data-ts'));
    if(isNaN(d.getTime())) return;
    var s=d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());
    if(!el.hasAttribute('data-ts-min')) s+=':'+p(d.getSeconds());
    el.textContent=s;
  });
})();
