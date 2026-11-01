/* Offline HTML + local htmx; all client-rendered data is escaped. */
'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const text=RU.text;
let current='pool',page=1,filters={},settingsData=null,polling=false,settingsDirty=false,noticeTimer,eventsBefore=null,eventsHistory=false;
let operationBusy=false;
const selected={pool:new Set(),all:new Set()};
function notify(message,error=false){const el=$('#notice');el.textContent=message;el.classList.toggle('error',error);el.hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>el.hidden=true,error?12000:6000);}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});const value=await r.json();if(!r.ok){if(r.status===401){$('#app').hidden=true;$('#login-screen').hidden=false;}throw Error(value.message||value.error||text.error);}return value;}
function date(v){return v?new Date(v*1000).toLocaleString('ru-RU'):'—';}
function remaining(row){const seconds=Math.max(0,Math.ceil(((row.lease_expires_at||row.cooldown_until)||0)-Date.now()/1000));return row.lease_expires_at||row.cooldown_until?`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`:'—';}
function flag(v){return /^[A-Z]{2}$/.test(v||'')?[...v].map(c=>String.fromCodePoint(c.charCodeAt()+127397)).join(''):'';}
async function copy(value){try{await navigator.clipboard.writeText(value);notify(text.copied);}catch{notify('Браузер не разрешил доступ к буферу обмена',true);}}
function optionsActions(){return Object.entries(RU.actions).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');}
async function refreshSubscriptions(){
 const data=await api('/subscriptions');
 $('#subscriptions-list').innerHTML=data.items.map(s=>`<article class="panel subscription" data-id="${s.id}"><h2>${esc(s.source||'Подписка #'+s.id)}</h2><p class="mono config-raw">${esc(s.url)}</p><div class="subscription-times"><div>Последняя попытка<strong>${date(s.last_attempt_at)}</strong></div><div>Успешное обновление<strong>${date(s.last_success_at)}</strong></div><div>Следующее обновление<strong>${s.updating?'Обновляется…':s.next_update_at?date(s.next_update_at):'Только вручную'}</strong><small>${s.next_update_at&&!s.updating?'Через '+Math.max(0,Math.ceil((s.next_update_at-Date.now()/1000)/60))+' мин':''}</small></div><div>Длительность<strong>${s.duration_ms===null?'—':(s.duration_ms/1000).toFixed(2)+' с'}</strong></div></div>${s.last_error?`<p class="warning">Последняя ошибка: ${esc(({subscription_download_failed:'Не удалось скачать подписку',subscription_invalid_content:'Ответ пуст или не содержит корректных конфигов',subscription_html_content:'Вместо конфигов получена HTML-страница. Укажите Raw-ссылку на файл подписки',subscription_file_required:'Укажите ссылку на файл подписки (Raw), а не на репозиторий или папку GitHub',subscription_update_failed:'Не удалось обновить подписку'})[s.last_error]||s.last_error)}</p>`:''}${s.last_result?`<p class="muted">Последний успешный импорт: получено ${s.last_result.received}, принято ${s.last_result.accepted}, дублей ${s.last_result.duplicates}, ошибок разбора ${s.last_result.parse_errors}.</p>`:''}<form class="subscription-controls"><label>Интервал, минут<input name="interval" type="number" min="0" max="525600" required value="${s.interval_minutes}"></label><button type="submit">Сохранить интервал</button><button type="button" class="subscription-refresh" ${s.updating?'disabled':''}>Обновить сейчас</button><button type="button" class="subscription-delete" ${s.updating?'disabled':''}>Удалить подписку</button></form><small class="muted">Удаление подписки останавливает обновления; импортированные конфиги сохраняются.</small></article>`).join('')||'<div class="panel empty"><h3>Подписок пока нет</h3><p>Добавьте URL во вкладке «Добавить конфиги».</p></div>';
 $$('.subscription').forEach(card=>{
  const path='/subscriptions/'+card.dataset.id;
  const run=async(button,operation)=>{button.disabled=true;try{await operation();notify('Подписка обновлена');await refreshSubscriptions();await refreshStatus();}catch(e){notify(e.message,true);}finally{button.disabled=false;}};
  card.querySelector('form').onsubmit=e=>{e.preventDefault();run(e.submitter,()=>api(path,{method:'PATCH',body:JSON.stringify({interval_minutes:Number(card.querySelector('input').value)})}));};
  card.querySelector('.subscription-refresh').onclick=e=>run(e.target,()=>api(path+'/refresh',{method:'POST'}));
  card.querySelector('.subscription-delete').onclick=e=>run(e.target,()=>api(path,{method:'DELETE'}));
 });
}
function renderTable(data,container){const kind=container==='pool-table'?'pool':'all',set=selected[kind];
 const bulk=`<div class="bulk-bar"><span>${text.selected}: <b class="selection-count">${set.size}</b></span><select class="bulk-action" aria-label="Массовое действие">${optionsActions()}</select><button class="bulk-run">${text.apply}</button></div>`;
 let html=bulk;
 if(!data.items.length){html+=`<div class="empty"><div class="empty-icon">◇</div><h3>${text.empty}</h3><p>${text.emptyHelp}</p></div>`;}
 else{html+=`<div class="table-wrap"><table><thead><tr><th><input class="select-all" type="checkbox" aria-label="Выбрать страницу"></th><th>${kind==='pool'?text.address:text.link}</th><th>${text.country}</th><th>${text.ping}</th><th>${text.speed}</th><th>UDP</th><th>${text.state}</th><th>${text.expires}</th><th>${text.lastCheck}</th><th>${text.source}</th><th>${text.actions}</th></tr></thead><tbody>`;
 for(const r of data.items){html+=`<tr data-id="${r.id}"><td><input class="row-check" type="checkbox" ${set.has(r.id)?'checked':''} aria-label="Выбрать ${r.id}"></td><td class="config-cell"><span class="mono config-raw" title="${esc(r.remark)}">${esc(kind==='pool'?(r.addr||'—'):r.link)}</span><span class="sub">#${r.id} · ${esc(r.protocol||'—')} ${r.checking?'· проверяется':''}</span></td><td>${flag(r.geo)} ${esc(r.geo||'—')}<span class="sub mono">${esc(r.exit_ip||'')}</span></td><td>${r.ping_ms===null?'—':r.ping_ms.toFixed(0)}</td><td>${r.speed_mbps===null?'—':r.speed_mbps.toFixed(2)}</td><td>${r.udp===null?'—':r.udp?'✓':'✕'}</td><td><span class="badge ${esc(r.state)}">${esc(RU.states[r.state])}</span><span class="sub" title="${esc(r.reason)}">${esc(r.reason||'')}</span></td><td class="mono">${remaining(r)}</td><td>${date(r.last_check_at)}</td><td title="${esc(r.source)}">${esc(r.source||'—')}</td><td><div class="row-actions"><button class="copy-row" data-addr="${esc(kind==='pool'?(r.addr||''):'')}">${text.copy}</button><select class="row-action" aria-label="Действие для ${r.id}"><option value="">Действие…</option>${optionsActions()}</select></div></td></tr>`;}
 html+='</tbody></table></div>';}
 html+=`<div class="table-footer"><span>${text.total}: ${data.total.toLocaleString('ru-RU')}</span><div><button class="prev" ${data.page<=1?'disabled':''}>←</button> ${text.page} ${data.page} ${text.of} ${Math.max(1,Math.ceil(data.total/data.page_size))} <button class="next" ${data.page*data.page_size>=data.total?'disabled':''}>→</button></div></div>`;
 const root=document.getElementById(container);root.innerHTML=html;
 root.querySelectorAll('.row-check').forEach(el=>el.addEventListener('change',()=>{const id=Number(el.closest('tr').dataset.id);el.checked?set.add(id):set.delete(id);root.querySelector('.selection-count').textContent=set.size;}));
 root.querySelector('.select-all')?.addEventListener('change',e=>{root.querySelectorAll('.row-check').forEach(el=>{el.checked=e.target.checked;el.dispatchEvent(new Event('change'));});});
 root.querySelector('.bulk-run').onclick=()=>action(root.querySelector('.bulk-action').value,[...set]);
 root.querySelectorAll('.row-action').forEach(el=>el.onchange=()=>{if(el.value)action(el.value,[Number(el.closest('tr').dataset.id)]);});
 root.querySelectorAll('.copy-row').forEach(el=>el.onclick=async()=>{try{const id=el.closest('tr').dataset.id;await copy(el.dataset.addr||(await api(`/proxies/${id}/link`)).link);}catch(e){notify(e.message,true);}});
 root.querySelector('.prev').onclick=()=>{page--;refreshTable();};root.querySelector('.next').onclick=()=>{page++;refreshTable();};
}
async function action(name,ids){if(!ids.length){notify('Выберите хотя бы одну строку',true);return;}try{const result=await api('/actions',{method:'POST',body:JSON.stringify({action:name,ids})});const errors=result.results.filter(r=>r.error);selected.pool.clear();selected.all.clear();notify(errors.length?errors.map(r=>`#${r.id}: ${r.error}`).join('\n'):'Действие выполнено',!!errors.length);await refreshStatus();await refreshTable();}catch(e){notify(e.message,true);}}
async function refreshStatus(){const s=await api('/status');$('#m-raised').textContent=s.raised;$('#m-free').textContent=s.free;$('#m-leased').textContent=s.leased;$('#m-queue').textContent=s.queue.toLocaleString('ru-RU');$('#m-capacity').textContent=`${s.ports.pool_used} / ${s.ports.pool_capacity} портов · цель ${s.pool.pool_size}`;$('#m-rate').textContent=`${s.checks_per_minute} проверок / мин · сейчас ${s.checking}`;$('#core-badge').textContent=!s.operation.enabled?'○ Отключено':s.xray.ready?'● Xray работает':'○ Xray запускается';$('#core-badge').className='badge '+(!s.operation.enabled?'':s.xray.ready?'active':'invalid');$('#sync-time').textContent=new Date().toLocaleTimeString('ru-RU');$('#system-info').textContent=`ProxyHub ${s.version} · Прямой IP: ${s.direct_ip||'не определён'} · ${s.xray.version}`;
 $('#state-strip').innerHTML=Object.entries(s.states).map(([k,v])=>`<button data-state="${k}">${RU.states[k]} <b>${v.toLocaleString('ru-RU')}</b></button>`).join('');$('#state-strip').querySelectorAll('button').forEach(b=>b.onclick=()=>{filters={state:b.dataset.state};$('#filters').reset();$('#filters [name=state]').value=b.dataset.state;showTab('all');});
 renderOperation(s.operation);
 const warnings=[];if(s.operation.enabled&&!s.xray.ready)warnings.push('Ядро запускается или недоступно. Подробности — в журнале событий.');if(s.operation.enabled&&!s.geo_ready)warnings.push('Геобаза не загружена. Для проверок с определением страны нужен доступ к DB-IP или локальный MMDB.');if(s.restart_required.length)warnings.push('Есть настройки, ожидающие перезапуска: '+s.restart_required.join(', '));$('#resource-warning').hidden=!warnings.length;$('#resource-warning').textContent=warnings.join(' ');
}
function renderOperation(operation){
 const busy=operationBusy||operation.transitioning;
 $('#service-enable').disabled=busy||operation.enabled;
 $('#service-disable').disabled=busy||!operation.enabled;
 $('#operation-status').textContent=busy?'Изменение режима…':operation.enabled?'Прокси включены':'Прокси отключены';
 $('#operation-description').textContent=operation.enabled?'Отключение закроет текущие подключения и приостановит проверки и автообновление подписок.':'Подключения закрыты, проверки и автообновление подписок приостановлены. Нажмите «Включить», чтобы продолжить.';
}
async function setOperation(enabled){
 operationBusy=true;$('#service-enable').disabled=true;$('#service-disable').disabled=true;
 $('#operation-status').textContent=enabled?'Включение…':'Отключение…';
 try{await api('/service/'+(enabled?'enable':'disable'),{method:'POST'});notify(enabled?'Работа прокси включена':'Работа прокси отключена');}
 catch(e){notify(e.message,true);}
 finally{operationBusy=false;try{await refreshStatus();await refreshTable();}catch(e){notify(e.message,true);}}
}
$('#service-enable').onclick=()=>setOperation(true);
$('#service-disable').onclick=()=>setOperation(false);
async function refreshTable(){if(!['pool','all'].includes(current))return;const params=new URLSearchParams({...(current==='all'?filters:{}),page,page_size:50});if(current==='pool'){params.delete('state');params.set('raised','true');}const data=await api('/proxies?'+params);renderTable(data,current==='pool'?'pool-table':'all-table');}
async function refreshEvents(older=false){eventsHistory=older;const data=await api('/events'+(older&&eventsBefore?'?before='+eventsBefore:''));if(data.length)eventsBefore=data.at(-1).id;const html=data.map(e=>`<div class="event"><time>${date(e.time)}</time><span class="${esc(e.level)}">${esc(e.level)}</span><span>${e.proxy_id?'#'+e.proxy_id:'—'}</span><span>${esc(e.message)}</span></div>`).join('');if(older)$('#events-list').insertAdjacentHTML('beforeend',html);else $('#events-list').innerHTML=html||'<p class="muted">Событий пока нет</p>';}
async function refresh(){if(polling||$('#app').hidden)return;polling=true;try{await refreshStatus();if(current==='subscriptions'&&!document.activeElement?.matches('#subscriptions-list input'))await refreshSubscriptions();if(current!=='events'&&!document.activeElement?.matches('.proxy-panel select,.proxy-panel input:not([type=checkbox])'))await refreshTable();}catch(e){notify(e.message,true);}finally{polling=false;}}
async function showTab(tab){current=tab;page=1;eventsHistory=false;$$('[id^=tab-]').forEach(el=>el.hidden=el.id!=='tab-'+tab);$$('nav button').forEach(b=>b.classList.toggle('selected',b.dataset.tab===tab));$('#page-title').textContent=RU.tabs[tab];try{if(tab==='settings'&&!settingsData)await loadSettings();else if(tab==='api')await loadApiDocs();else if(tab==='subscriptions')await refreshSubscriptions();else if(tab==='events')await refreshEvents();else await refreshTable();}catch(e){notify(e.message,true);}}
function settingInput(key,value,schema,restart){const id='setting-'+key;let input;const attr=`id="${key=== 'api_token'?'api-token':id}" data-setting="${key}"`;
 if(typeof value==='boolean')input=`<input ${attr} type="checkbox" ${value?'checked':''}>`;
 else if(schema.enum)input=`<select ${attr}>${schema.enum.map(x=>`<option ${x===value?'selected':''}>${esc(x)}</option>`).join('')}</select>`;
 else if(Array.isArray(value)&&key.endsWith('_urls'))input=`<textarea ${attr}>${esc(value.join('\n'))}</textarea>`;
 else if(Array.isArray(value))input=`<input ${attr} value="${esc(value.join(key.endsWith('_range')?'-':', '))}">`;
 else input=`<input ${attr} type="${typeof value==='number'?'number':['socks_password','api_token'].includes(key)?'password':'text'}" ${typeof value==='number'?'step="any"':''} value="${esc(value)}">`;
 const label=esc(RU.fields[key]||key)+(restart?' ↻':'');return `<label class="${typeof value==='boolean'?'check':''} ${key.endsWith('_urls')?'wide':''}">${typeof value==='boolean'?input+label:label+input}<small>${esc(key)}</small></label>`;
}
async function loadSettings(){settingsData=await api('/settings');let html='';for(const [group,keys] of RU.groups){html+=`<section class="panel"><h2>${group}</h2><div class="settings-grid">`;for(const k of keys)html+=settingInput(k,settingsData.values[k],settingsData.schema[k],settingsData.restart_fields.includes(k));html+='</div></section>';}
 html+=`<section class="panel"><h2>Пароль веб-интерфейса</h2><p class="muted">${settingsData.values.ui_password_set?text.passwordSet:text.passwordNotSet}</p><div class="settings-grid"><label>${text.password}<input id="new-password" type="password" autocomplete="new-password" minlength="8"></label><label class="check"><input id="clear-password" type="checkbox">${text.clearPassword}</label><div><button id="copy-token">Копировать API-токен</button></div></div></section>`;$('#settings-form').innerHTML=html;$('#copy-token').onclick=()=>copy($('#api-token').value);$('#settings-form').oninput=()=>settingsDirty=true;settingsDirty=false;
}
$('#save-settings').onclick=async()=>{try{const changes={};$$('[data-setting]').forEach(el=>{const k=el.dataset.setting,old=settingsData.values[k];let v=typeof old==='boolean'?el.checked:typeof old==='number'?Number(el.value):Array.isArray(old)?(k.endsWith('_range')?el.value.split(/\s*[-–,]\s*/).map(Number):el.value.split(k.endsWith('_urls')?/\r?\n/:/,/).map(x=>x.trim()).filter(Boolean)):el.value;if(JSON.stringify(v)!==JSON.stringify(old))changes[k]=v;});if($('#clear-password').checked)changes.ui_password='';else if($('#new-password').value)changes.ui_password=$('#new-password').value;const result=await api('/settings',{method:'PATCH',body:JSON.stringify(changes)});settingsDirty=false;settingsData=null;notify(text.save+(result.restart_required.length?' · Требуется перезапуск':''));await loadSettings();}catch(e){notify(e.message,true);}};
$('#test-urls').onclick=async e=>{e.target.disabled=true;try{const data=await api('/settings/test-urls',{method:'POST'});notify(data.results.map(r=>(r.ok?'✓ ':'✕ ')+r.url).join('\n'));}catch(err){notify(err.message,true);}finally{e.target.disabled=false;}};
$('#import-file').onchange=async e=>{const file=e.target.files[0];if(file){$('#import-form [name=data]').value=await file.text();$('#import-form [name=url]').value='';}};
$('#subscription-file').onchange=async e=>{const file=e.target.files[0];if(file){try{$('#import-form [name=url]').value=await file.text();$('#import-form [name=data]').value='';}catch{notify('Не удалось прочитать файл подписок',true);}}};
$('#import-form').onsubmit=async e=>{
 e.preventDefault();const form=e.target,button=form.querySelector('button[type=submit]'),result=$('#import-result'),label=button.textContent;
 button.disabled=true;result.hidden=true;
 try{
  const d=new FormData(form),body={source:d.get('source')},urls=d.get('url').split(/\r?\n/).map(url=>url.trim()).filter(Boolean);
  if(urls.length){body.urls=urls;body.interval_minutes=Number(d.get('interval_minutes'));button.textContent='Загрузка подписок…';}else{body.data=d.get('data');button.textContent='Импорт…';}
  const r=await api('/proxies',{method:'POST',body:JSON.stringify(body)});
  const errors={invalid_subscription_url:'Некорректный URL: нужен http:// или https://',subscription_download_failed:'Не удалось скачать подписку',subscription_invalid_content:'Ответ пуст или не содержит корректных конфигов',subscription_html_content:'Вместо конфигов получена HTML-страница. Укажите Raw-ссылку на файл подписки',subscription_file_required:'Укажите ссылку на файл подписки (Raw), а не на репозиторий или папку GitHub',subscription_update_failed:'Не удалось обновить подписку',subscription_updating:'Подписка уже обновляется'};
  result.hidden=false;
  result.innerHTML='<h2>'+text.imported+'</h2>'+(r.results?`<p>Подписок: <b>${r.subscriptions_total}</b> · Успешно: <b>${r.subscriptions_succeeded}</b> · Ошибок: <b>${r.subscriptions_failed}</b> · Повторов ссылок пропущено: <b>${r.duplicate_urls}</b></p>`:'')+['received','accepted','duplicates','unsupported','parse_errors'].map(k=>`<p>${text[k]}: <b>${r[k]}</b></p>`).join('')+(r.errors.length?'<pre>'+esc(r.errors.map(e=>`${e.subscription_id?'Подписка #'+e.subscription_id+', ':''}${text.line} ${e.line}: ${e.reason}`).join('\n'))+'</pre>':'')+(r.results?'<details open><summary>Результат по каждой подписке</summary><pre>'+esc(r.results.map(item=>`${item.ok?'✓':'✕'} ${item.url}\n${item.ok?'Принято: '+item.accepted+', дублей: '+item.duplicates+', ошибок разбора: '+item.parse_errors:(errors[item.error]||item.error)}`).join('\n\n'))+'</pre></details>':'');
  await refreshStatus();
 }catch(err){notify(err.message,true);}finally{button.disabled=false;button.textContent=label;}
};
$('#filters').onsubmit=e=>{e.preventDefault();filters=Object.fromEntries([...new FormData(e.target)].filter(([,v])=>v!==''));page=1;refreshTable().catch(e=>notify(e.message,true));};$('#filters').onreset=()=>{filters={};page=1;setTimeout(()=>refreshTable().catch(e=>notify(e.message,true)),0);};
$('#filters [name=state]').insertAdjacentHTML('beforeend',Object.entries(RU.states).map(([k,v])=>`<option value="${k}">${v}</option>`).join(''));
$$('[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));$$('[data-go]').forEach(b=>b.onclick=()=>showTab(b.dataset.go));$('#refresh').onclick=async()=>{await refresh();await refreshTable();};$('#older-events').onclick=()=>refreshEvents(true).catch(e=>notify(e.message,true));
$('#delete-db').onclick=()=>{$('#confirm-text').textContent=text.confirmDelete+' Область: '+$('#delete-scope').selectedOptions[0].textContent;$('#confirm-input').value='';$('#confirm-dialog').showModal();};
$('#confirm-dialog').addEventListener('close',async()=>{if($('#confirm-dialog').returnValue!=='delete')return;if($('#confirm-input').value!=='УДАЛИТЬ'){notify('Подтверждение не совпало',true);return;}try{const params=new URLSearchParams({confirm:'yes',scope:$('#delete-scope').value,source:$('#delete-source').value});const r=await api('/proxies?'+params,{method:'DELETE'});notify(`Удалено: ${r.deleted}`);await refresh();}catch(e){notify(e.message,true);}});
$('#logout').onclick=async()=>{await api('/auth/logout',{method:'POST'});$('#app').hidden=true;$('#login-screen').hidden=false;settingsData=null;};
$('#login-form').onsubmit=async e=>{e.preventDefault();try{await api('/auth/login',{method:'POST',body:JSON.stringify({password:$('#login-password').value})});$('#login-password').value='';$('#login-screen').hidden=true;$('#app').hidden=false;await refresh();}catch(err){$('#login-error').textContent=err.message;}};
window.addEventListener('beforeunload',e=>{if(settingsDirty){e.preventDefault();e.returnValue='';}});
api('/status').then(()=>{$('#login-screen').hidden=true;$('#app').hidden=false;refresh();}).catch(()=>{});
setInterval(()=>{if(!document.hidden)refresh();},3000);

document.body.addEventListener('htmx:beforeRequest',e=>{if(e.detail.elt.id==='events-list'&&(current!=='events'||document.hidden||eventsHistory||$('#app').hidden))e.preventDefault();});
