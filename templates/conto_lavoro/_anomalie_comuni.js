// ── Anomalie del Conto Lavoro: funzioni comuni (Mauri, 28/11/2026) ──────────
// Incluso con include in LIVE, Riepilogo ordini, Magazzino e pagina Anomalie.
// Due fonti, una sola lista:
//   - OPERAIO: segnalate dal cellulare in MasterWork (letta via IronProduction),
//     chiuse in cl_anomalia_risolta;
//   - ANGELO:  segnalate dal pulsante ⚠️ su un codice (cl_anomalia).
const CLA = {aperte: [], perCodice: {}, avviso: ''};
const claEsc = t => String(t ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const claData = iso => {
  if(!iso) return '';
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z');
  return isNaN(d) ? iso : d.toLocaleString('it-IT', {timeZone:'Europe/Rome', day:'2-digit', month:'2-digit',
                                                       year:'2-digit', hour:'2-digit', minute:'2-digit'});
};
async function claJson(url, opzioni){
  try{ const r = await fetch(url, opzioni); return await r.json(); }catch(e){ return null; }
}
const claPost = (url, corpo) => claJson(url, {method:'POST', headers:{'Content-Type':'application/json'},
                                              body: JSON.stringify(corpo || {})});

// genere: TEMPO = problema di produzione (operai e anomalie di tempo di Angelo) → compare nel LIVE;
//         PREZZO = tempi già ottimizzati, si tratta solo col cliente → NON compare nel LIVE.
const CLA_GENERI = {TEMPO: '⏱ Tempo', PREZZO: '💶 Prezzo'};
// Carica le anomalie APERTE delle due fonti. codici = codici cliente da controllare per MasterWork.
async function claCarica(codici){
  const avvisi = [];
  const [risolte, mw, angelo] = await Promise.all([
    claJson('/conto-lavoro/api/anomalie-risolte'),
    codici && codici.length ? claPost('/api/masterwork/anomalie', {codici}) : Promise.resolve({anomalie: []}),
    claJson('/conto-lavoro/api/anomalie-angelo?stato=aperte'),
  ]);
  const giaRisolte = new Set(Array.isArray(risolte) ? risolte.map(x => x.mw_id) : []);
  if(!Array.isArray(risolte) && risolte && risolte.error) avvisi.push(risolte.error);
  if(mw && mw.ok === false && mw.error) avvisi.push('MasterWork: ' + mw.error);
  if(!Array.isArray(angelo) && angelo && angelo.error) avvisi.push(angelo.error);
  const lista = [];
  ((mw && mw.anomalie) || []).filter(a => !giaRisolte.has(a.id)).forEach(a => lista.push({
    chiave: 'mw-' + a.id, tipo: 'mw', id: a.id, fonte: 'OPERAIO', genere: 'TEMPO', codici: a.codici || [],
    chi: a.operaio || '', descrizione: a.motivo || '', quando: a.creato_il || '', fase: a.fase || '',
    codice_interno: a.codice_interno || '', azione: a.azione || '', grezza: a}));
  (Array.isArray(angelo) ? angelo : []).forEach(a => lista.push({
    chiave: 'an-' + a.id, tipo: 'angelo', id: a.id, fonte: 'ANGELO', genere: a.tipo || 'TEMPO', codici: [a.codice],
    chi: a.segnalata_da || '', descrizione: a.descrizione || '', quando: a.segnalata_il || '',
    numero_ordine: a.numero_ordine || '', cliente: a.cliente || '', grezza: a}));
  lista.sort((x, y) => String(y.quando).localeCompare(String(x.quando)));
  CLA.aperte = lista;
  CLA.perCodice = {};
  lista.forEach(a => a.codici.forEach(c => (CLA.perCodice[c] = CLA.perCodice[c] || []).push(a)));
  CLA.avviso = avvisi.join(' · ');
  return lista;
}

// Etichetta rossa cliccabile "⚠️ N da risolvere" → pagina Anomalie filtrata sul codice
function claBadge(codice, grande){
  const n = (CLA.perCodice[codice] || []).length;
  if(!n) return '';
  return `<a href="/conto-lavoro/anomalie?codice=${encodeURIComponent(codice)}" target="_blank" class="cla-badge${grande ? ' grande' : ''}"
             title="Apri le anomalie di questo codice" onclick="event.stopPropagation()">⚠️ ${n} ${n === 1 ? 'anomalia' : 'anomalie'} da risolvere</a>`;
}
// Il codice: se ha anomalie aperte diventa rosso e cliccabile (porta alla pagina Anomalie)
function claCodice(codice){
  if(!(CLA.perCodice[codice] || []).length) return `<b>${claEsc(codice)}</b>`;
  return `<a href="/conto-lavoro/anomalie?codice=${encodeURIComponent(codice)}" target="_blank" onclick="event.stopPropagation()"
             style="color:#ff6b6b;font-weight:900;text-decoration:underline" title="Anomalie da risolvere: clicca">⚠️ ${claEsc(codice)}</a>`;
}
// ── finestre (stile proprio: funzionano sia nelle pagine normali sia nel LIVE)
(function(){
  const st = document.createElement('style');
  st.textContent = `
  .cla-badge{display:inline-block;margin-top:3px;padding:2px 7px;border-radius:5px;background:#5a1010;color:#fff !important;
    border:1px solid #ff4d4d;font-size:10px;font-weight:800;text-decoration:none;white-space:nowrap;animation:claLamp 1.4s infinite;cursor:pointer}
  .cla-badge.grande{font-size:12px;padding:4px 10px}
  @keyframes claLamp{50%{opacity:.6}}
  .cla-btn-segnala{background:transparent;color:#ff8a8a;border:1px solid #c0392b;border-radius:6px;padding:3px 8px;
    font-size:11px;font-weight:700;cursor:pointer;white-space:nowrap}
  .cla-btn-segnala:hover{background:#5a1010;color:#fff}
  .cla-overlay{position:fixed;inset:0;background:rgba(2,8,18,.8);display:flex;align-items:center;justify-content:center;z-index:9999;padding:16px}
  .cla-box{background:#0a1623;color:#eaf4ff;border:2px solid #1e6fa5;border-radius:12px;padding:20px;width:min(560px,96vw);
    font-family:'Segoe UI',Arial,sans-serif;box-shadow:0 10px 40px rgba(0,0,0,.6)}
  .cla-box.verde{border-color:#1a9e5c}.cla-box.rossa{border-color:#ff4d4d}
  .cla-tit{font-size:20px;font-weight:900;margin-bottom:6px}
  .cla-sub{font-size:12px;color:#9fb8d0;margin-bottom:10px}
  .cla-cit{font-size:15px;color:#ffd6d6;margin:6px 0}
  .cla-lab{display:block;font-size:12px;color:#6b8aaa;margin:10px 0 4px}
  .cla-box textarea,.cla-box input{width:100%;box-sizing:border-box;background:#0f2237;color:#fff;border:1px solid #1e6fa5;
    border-radius:8px;padding:10px;font:inherit;font-size:14px}
  .cla-box textarea{min-height:110px}
  .cla-err{color:#ff6b6b;font-size:12px;min-height:16px;margin-top:6px}
  .cla-azioni{display:flex;gap:8px;margin-top:12px}
  .cla-azioni button{border:0;border-radius:8px;padding:10px;font-weight:800;cursor:pointer;font-size:14px;color:#fff}
  .cla-tipi{display:flex;gap:8px}
  .cla-tipo{flex:1;text-align:left;background:#0f2237;color:#fff;border:2px solid #33475e;border-radius:10px;padding:10px;
    font-weight:900;font-size:15px;cursor:pointer}
  .cla-tipo small{display:block;font-size:11px;font-weight:400;color:#9fb8d0;margin-top:4px}
  .cla-tipo[data-tipo="TEMPO"].scelto{border-color:#e0a800;background:#2a2000}
  .cla-tipo[data-tipo="PREZZO"].scelto{border-color:#a970ff;background:#1e1235}
  .cla-genere{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:800}
  .cla-genere.TEMPO{background:#3d2a00;color:#ffc94d}.cla-genere.PREZZO{background:#2a1a48;color:#c9a5ff}
  .cla-annulla{background:#33475e;flex:1}.cla-ok{background:#1a9e5c;flex:2}.cla-ok.rosso{background:#c0392b}`;
  document.head.appendChild(st);
})();
function claChiudi(){ document.querySelectorAll('.cla-overlay').forEach(e => e.remove()); }
function claFinestra(html){
  claChiudi();
  const o = document.createElement('div');
  o.className = 'cla-overlay';
  o.innerHTML = html;
  o.addEventListener('click', e => { if(e.target === o) claChiudi(); });
  document.body.appendChild(o);
  const primo = o.querySelector('textarea');
  if(primo) setTimeout(() => primo.focus(), 50);
  return o;
}
document.addEventListener('keydown', e => { if(e.key === 'Escape') claChiudi(); });
const claNome = () => { try{ return localStorage.getItem('cla_nome') || 'Angelo'; }catch(e){ return 'Angelo'; } };
const claSalvaNome = n => { try{ localStorage.setItem('cla_nome', n); }catch(e){} };

// Angelo segnala un'anomalia su un codice: di TEMPO o di PREZZO
function claSegnala(info){
  claFinestra(`<div class="cla-box rossa">
    <div class="cla-tit">⚠️ Segnala anomalia</div>
    <div class="cla-sub"><b>${claEsc(info.codice)}</b> ${claEsc(info.descr)}</div>
    <label class="cla-lab">Tipo di anomalia *</label>
    <div class="cla-tipi">
      <button type="button" class="cla-tipo" data-tipo="TEMPO" onclick="claScegliTipo(this)">⏱ TEMPO
        <small>problema di produzione: tempi, fasi, pezzo — la vedono plant manager e operai nel LIVE</small></button>
      <button type="button" class="cla-tipo" data-tipo="PREZZO" onclick="claScegliTipo(this)">💶 PREZZO
        <small>tempi già ottimizzati: si parla solo col cliente — NON compare nel LIVE</small></button>
    </div>
    <label class="cla-lab">Descrizione dell'anomalia *</label>
    <textarea id="cla-descr" placeholder="es. tempi della fase di saldatura da rivedere / prezzo non più remunerativo…"></textarea>
    <label class="cla-lab">Segnalata da</label>
    <input id="cla-chi" value="${claEsc(claNome())}">
    <div class="cla-err" id="cla-err"></div>
    <div class="cla-azioni"><button class="cla-annulla" onclick="claChiudi()">Annulla</button>
      <button class="cla-ok rosso" id="cla-conferma">⚠️ Segnala</button></div></div>`);
  document.getElementById('cla-conferma').onclick = async () => {
    const scelto = document.querySelector('.cla-tipo.scelto');
    const descrizione = document.getElementById('cla-descr').value.trim();
    const chi = document.getElementById('cla-chi').value.trim() || 'Angelo';
    if(!scelto){ document.getElementById('cla-err').textContent = 'Scegli il tipo: TEMPO o PREZZO.'; return; }
    if(!descrizione){ document.getElementById('cla-err').textContent = "Descrivi l'anomalia."; return; }
    const d = await claPost('/conto-lavoro/api/anomalie-angelo', {articolo_id: info.articolo_id, ordine_id: info.ordine_id,
                            tipo: scelto.dataset.tipo, descrizione, segnalata_da: chi});
    if(!d || !d.ok){ document.getElementById('cla-err').textContent = (d && d.error) || 'Errore di rete'; return; }
    claSalvaNome(chi);
    claChiudi();
    if(typeof claDopoModifica === 'function') claDopoModifica();
  };
}
function claScegliTipo(b){
  document.querySelectorAll('.cla-tipo').forEach(x => x.classList.toggle('scelto', x === b));
}

// "È stato risolto" con nota — vale per tutte e due le fonti
function claRisolvi(chiave){
  const a = CLA.aperte.find(x => x.chiave === chiave);
  if(!a) return;
  claFinestra(`<div class="cla-box verde">
    <div class="cla-tit">✅ Anomalia risolta</div>
    <div class="cla-cit">“${claEsc(a.descrizione)}”</div>
    <div class="cla-sub"><span class="cla-genere ${a.genere}">${CLA_GENERI[a.genere] || ''}</span> ${a.fonte === 'OPERAIO' ? '👷' : '🧑‍💼'} ${claEsc(a.chi)} · ${claEsc(a.codici.join(', '))} · ${claEsc(claData(a.quando))}</div>
    <label class="cla-lab">Nota: come è stata risolta *</label>
    <textarea id="cla-nota" placeholder="es. ripresi i tempi, aggiunta la fase di sbavatura…"></textarea>
    <label class="cla-lab">Risolta da</label>
    <input id="cla-chi" value="${claEsc(claNome())}">
    <div class="cla-err" id="cla-err"></div>
    <div class="cla-azioni"><button class="cla-annulla" onclick="claChiudi()">Annulla</button>
      <button class="cla-ok" id="cla-conferma">✅ Conferma: è stato risolto</button></div></div>`);
  document.getElementById('cla-conferma').onclick = async () => {
    const nota = document.getElementById('cla-nota').value.trim();
    const chi = document.getElementById('cla-chi').value.trim() || 'Angelo';
    if(!nota){ document.getElementById('cla-err').textContent = 'Scrivi una nota su come è stata risolta.'; return; }
    let d;
    if(a.tipo === 'mw'){
      d = await claPost('/conto-lavoro/api/anomalie-risolte', {mw_id: a.id, nota, risolta_da: chi, operaio: a.chi,
            codice_interno: a.codice_interno, motivo: a.descrizione, segnalata_il: a.quando});
      if(d && d.ok) claPost('/api/masterwork/anomalie/segna-lette', {ids: [a.id]});
    } else {
      d = await claPost(`/conto-lavoro/api/anomalie-angelo/${a.id}/risolvi`, {nota, risolta_da: chi});
    }
    if(!d || !d.ok){ document.getElementById('cla-err').textContent = (d && d.error) || 'Errore di rete'; return; }
    claSalvaNome(chi);
    CLA.aperte = CLA.aperte.filter(x => x.chiave !== chiave);
    Object.keys(CLA.perCodice).forEach(c => { CLA.perCodice[c] = CLA.perCodice[c].filter(x => x.chiave !== chiave);
                                             if(!CLA.perCodice[c].length) delete CLA.perCodice[c]; });
    claChiudi();
    if(typeof claDopoModifica === 'function') claDopoModifica();
  };
}
