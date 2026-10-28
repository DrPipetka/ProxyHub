/* Offline API explorer, driven by the authenticated OpenAPI schema. */
'use strict';
let apiDocument, apiOperations=[];
function apiResolve(schema){return schema?.$ref?apiDocument.components.schemas[schema.$ref.split('/').at(-1)]:schema||{};}
function apiType(schema){schema=apiResolve(schema);return schema.enum?schema.enum.join(' | '):schema.anyOf?schema.anyOf.map(apiType).join(' | '):schema.type||'object';}
function apiOperation(){return apiOperations[Number($('#api-endpoint').value)];}
async function loadApiDocs(){
 if(apiDocument)return;
 apiDocument=await api('/openapi.json');
 for(const [path,methods] of Object.entries(apiDocument.paths))for(const [method,op] of Object.entries(methods))apiOperations.push({path,method,op});
 $('#api-endpoint').innerHTML=apiOperations.map((entry,i)=>`<option value="${i}">${esc(entry.method.toUpperCase()+' '+entry.path+' · '+entry.op.summary)}</option>`).join('');
 $('#api-endpoint').value=String(apiOperations.findIndex(e=>e.path==='/status'));
 renderApiOperation();
}
function renderApiOperation(){
 const {path,method,op}=apiOperation();
 $('#api-title').textContent=op.summary;
 $('#api-description').textContent=op.description;
 $('#api-parameters').innerHTML=(op.parameters||[]).map((p,i)=>{
  const schema=apiResolve(p.schema),value=p.in==='path'?(schema.enum?.[0]||'1'):path==='/config'&&p.name==='lease'?'0':'';
  const constraints=[p.in,p.required?'обязательно':'необязательно',apiType(schema),schema.minimum!==undefined?'мин. '+schema.minimum:'',schema.maximum!==undefined?'макс. '+schema.maximum:'',schema.default!==undefined?'по умолчанию: '+schema.default:''].filter(Boolean).join(' · ');
  return `<label>${esc(p.name)}<input data-api-param="${i}" value="${esc(value)}" ${p.required?'required':''} placeholder="${esc(schema.default??'')}"><small class="muted">${esc(constraints)}</small></label>`;
 }).join('');
 const body=op.requestBody?.content?.['application/json'];
 $('#api-body-label').hidden=!body;$('#api-body-schema').hidden=!body;
 $('#api-body').value=body?JSON.stringify(body.example||{},null,2):'';
 $('#api-schema-text').textContent=body?JSON.stringify(apiResolve(body.schema),null,2):'';
 $('#api-response').textContent='';$('#api-response-status').textContent='Запрос ещё не выполнялся';$('#api-response-headers').textContent='';
 updateApiPreview();
}
function apiRequest(){
 const {path,method,op}=apiOperation();let url=path;const query=new URLSearchParams();
 $$('[data-api-param]').forEach(el=>{const p=op.parameters[Number(el.dataset.apiParam)];if(p.in==='path')url=url.replace('{'+p.name+'}',encodeURIComponent(el.value));else if(el.value!=='')query.set(p.name,el.value);});
 if(query.size)url+='?'+query;
 return {url,method:method.toUpperCase(),body:op.requestBody?$('#api-body').value:undefined};
}
function shellQuote(value){return "'"+value.replace(/'/g,"'\\''")+"'";}
function updateApiPreview(){
 if(!apiDocument)return;
 const req=apiRequest(),headers=["  -H 'Authorization: Bearer <TOKEN>'"];
 if(req.body!==undefined)headers.push("  -H 'Content-Type: application/json'");
 $('#api-curl').textContent=['curl -X '+req.method+' '+shellQuote(location.origin+req.url),...headers,...(req.body!==undefined?['  --data-raw '+shellQuote(req.body)]:[])].join(' \\\n');
 $('#api-effect').hidden=req.method==='GET'&&!req.url.startsWith('/proxy?')&&req.url!=='/proxy'&&!(req.url.startsWith('/config')&&new URL(req.url,location.origin).searchParams.get('lease')!=='0');
}
$('#api-endpoint').onchange=renderApiOperation;
$('#api-form').oninput=updateApiPreview;
$('#api-auth').onchange=()=>{$('#api-token-label').hidden=$('#api-auth').value!=='token';};
$('#api-form').onsubmit=async e=>{
 e.preventDefault();const button=$('#api-run');button.disabled=true;
 const started=performance.now();
 try{
  const req=apiRequest(),auth=$('#api-auth').value,headers={};
  if(req.body!==undefined){JSON.parse(req.body);headers['Content-Type']='application/json';}
  if(auth==='token'){const token=$('#api-test-token').value.trim();if(!token)throw Error('Введите Bearer-токен');headers.Authorization='Bearer '+token;}
  $('#api-response-status').textContent='Выполняется…';$('#api-response').textContent='';$('#api-response-headers').textContent='';
  const response=await fetch(req.url,{method:req.method,body:req.body,headers,credentials:auth==='session'?'same-origin':'omit',signal:AbortSignal.timeout(180000)});
  const body=await response.text();
  $('#api-response-status').textContent=`HTTP ${response.status} ${response.statusText} · ${Math.round(performance.now()-started)} мс`;
  try{$('#api-response').textContent=JSON.stringify(JSON.parse(body),null,2);}catch{$('#api-response').textContent=body;}
  $('#api-response-headers').textContent=[...response.headers].map(([k,v])=>k+': '+v).join('\n');
 }catch(err){$('#api-response-status').textContent='Ошибка: '+err.message;}
 finally{button.disabled=false;}
};
