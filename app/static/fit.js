// Read the existing translated HTML without rewriting stored results or running article scripts.
(async()=>{
  const message=document.querySelector('#message'),frame=document.querySelector('#reader');
  try{
    const id=new URLSearchParams(location.search).get('job');
    if(!/^\d+$/.test(id||''))throw new Error('올바른 작업 번호가 필요합니다.');
    const response=await fetch(`/api/jobs/${id}/translated`,{cache:'no-store'});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const doc=new DOMParser().parseFromString(await response.text(),'text/html');
    const article=doc.querySelector('article.tm-article-presenter__content')||doc.querySelector('.tm-article-body, #post-content-body')||doc.querySelector('article');
    if(!article)throw new Error('번역 본문을 찾을 수 없습니다. 기존 번역본 링크를 이용해 주세요.');
    // Retain the saved article markup, including tables, code and mathematical notation.
    article.querySelectorAll('script,style,link,meta,base,iframe,object,embed,form,button,input,textarea,select').forEach(e=>e.remove());
    for(const element of [article,...article.querySelectorAll('*')]){
      for(const attr of [...element.attributes]){
        if(attr.name.startsWith('on')||['style','srcset','sizes','background','bgcolor','color'].includes(attr.name)||(element.tagName==='IMG'&&['width','height'].includes(attr.name)))element.removeAttribute(attr.name);
      }
      for(const attr of ['href','src','xlink:href']){
        const value=element.getAttribute(attr);
        if(!value||value.startsWith('#'))continue;
        try{
          const url=new URL(value,'https://habr.com/');
          if(!['https:','http:'].includes(url.protocol)){element.removeAttribute(attr);continue}
          element.setAttribute(attr,url.href);
        }catch{element.removeAttribute(attr)}
      }
      if(element.tagName==='A'){element.setAttribute('target','_blank');element.setAttribute('rel','noopener noreferrer')}
      if(element.tagName==='IMG')element.setAttribute('referrerpolicy','no-referrer');
    }
    // Render the saved TeX verbatim; never recolor images or repair guessed notation.
    for(const image of article.querySelectorAll('img.formula')){
      const source=image.getAttribute('source')||image.getAttribute('alt');
      if(!source)continue;
      const inline=image.classList.contains('inline'),formula=doc.createElement('span');
      formula.className=`math-formula${inline?' math-inline':' math-block'}`;
      formula.tabIndex=0;
      try{
        formula.innerHTML=katex.renderToString(source,{
          output:'mathml',displayMode:!inline,throwOnError:true,strict:'ignore',
          trust:false,maxSize:20,maxExpand:1000,
        });
      }catch{
        // Unsupported or malformed input remains visible without inventing a formula.
        formula.classList.add('math-source');formula.textContent=source;
        formula.title='원문 수식: 자동 렌더링할 수 없어 저장된 문자열을 그대로 표시합니다.';
      }
      image.replaceWith(formula);
    }
    // A script-free sandbox and independent CSS replace unavailable captured Habr CSS.
    frame.srcdoc=`<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; img-src https: data:; font-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'"><link rel="stylesheet" href="${location.origin}/static/fit.css?v=14"></head><body>${article.outerHTML}</body></html>`;
    frame.hidden=false;message.hidden=true;
    try{localStorage.setItem(`habr-kor:fit:${id}`,'1')}catch{/* The reader remains fitted even without browser storage. */}
  }catch(error){message.textContent=`번역 결과를 열 수 없습니다: ${error.message}`}
})();
