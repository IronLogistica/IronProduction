import base64
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request, Response
from models import (db, log, CentroCostoWood, CicloLavoroWood, OrdineProduzione,
                    EventoConsuntivoPP, SequenzaMonitorMacchina, get_macchine_monitor,
                    SessioneLavoroMacchina, DocumentoTecnicoArticolo, FotoLavorazioneMacchina, FotoArticolo)
from blueprints.magazzino.routes import (_giacenza_residua_dopo_impegni, _netta_e_esplodi_wood,
                    _righe_bom_attive_wood, _esplodi_componenti_op, _residuo_giacenza_progressivo,
                    _carica_mappa_distinta_base_wood, STATI_CHE_IMPEGNANO)
from blueprints.produzione_pp.routes import _registra_evento_consuntivo, _audit, _is_carpenteria

monitor_bp = Blueprint('monitor', __name__)

SEZIONI = {
    'in_attesa':   ('🛬 IN ATTESA — Materiale / Fase Precedente', 'in_attesa-hdr'),
    'da_iniziare': ('✅ DA INIZIARE',                              'da_iniziare-hdr'),
    'lavorazione': ('🚧 IN LAVORAZIONE',                           'lavorazione-hdr'),
    'terminati':   ('✅ APPENA TERMINATI (questa fase)',           'terminati-hdr'),
}


def _pezzi_fase(op_code, nome_centro, componente=None):
    """Pezzi buoni già consuntivati per questo OP in QUESTA fase (per nome centro di costo, case-insensitive).
    'componente'=None cerca i consuntivi del PRODOTTO FINITO dell'OP (componente IS NULL nel DB);
    valorizzato cerca i consuntivi di QUEL componente intermedio specifico — non li mescola mai."""
    q = db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni)).filter(
        EventoConsuntivoPP.op_code == op_code,
        db.func.lower(EventoConsuntivoPP.fase) == nome_centro.lower())
    q = q.filter(EventoConsuntivoPP.componente.is_(None)) if not componente else q.filter(EventoConsuntivoPP.componente == componente)
    return q.scalar() or 0


def _materiale_disponibile(o, giacenza_residua=None, mappa_distinta=None):
    """
    Stessa logica usata in 'Situazione Ordini di Produzione': True se non
    manca nessun componente. 'giacenza_residua' opzionale: se il chiamante
    ha già il residuo di QUESTO OP pronto (vedi _residuo_giacenza_progressivo,
    calcolato UNA volta per tutti gli OP invece che uno alla volta), lo passa
    qui — altrimenti lo calcola da solo (più lento, va bene per un solo OP).
    'mappa_distinta' opzionale: vedi _carica_mappa_distinta_base_wood, evita
    una query per nodo dell'albero.
    """
    saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
    if saldo <= 0:
        return True
    righe = {}
    if giacenza_residua is None:
        giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=o.id, mappa=mappa_distinta)
    else:
        giacenza_residua = dict(giacenza_residua)  # copia: la netting consuma in-place
    _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, righe, mappa=mappa_distinta)
    return all(r['mancante'] <= 0 for r in righe.values())


def _righe_macchina(centro):
    """
    Estrae, per la macchina (centro di costo) data, tutte le LAVORAZIONI
    ancora aperte che passano da questa macchina — non solo quelle
    dell'articolo finito dell'OP, ma anche quelle di OGNI COMPONENTE della
    sua distinta base che ha un proprio Ciclo di Lavoro (es. 3 pezzi diversi
    che vanno tutti alla segatrice prima di diventare il prodotto finito):
    una riga per ogni coppia (OP, componente) il cui Ciclo di Lavoro include
    questa macchina — smistate nelle 4 sezioni in base all'avanzamento REALE
    (consuntivi, tracciati per componente — vedi EventoConsuntivoPP.componente).

    Ottimizzata per evitare QUATTRO trappole N+1 che rendevano questa funzione
    sempre più lenta al crescere del numero di OP aperti E della dimensione
    delle distinte base:
    1) l'intera distinta_base_wood viene caricata UNA VOLTA SOLA in memoria
       (invece di una query per ogni nodo di ogni albero di ogni OP — con
       distinte larghe/profonde reali questo da solo poteva voler dire
       centinaia di query sequenziali e 20+ secondi di risposta);
    2) il Ciclo di Lavoro di ogni componente veniva riletto con una query
       separata per OGNI (OP, componente) — ora è UNA query sola per tutti;
    3) _materiale_disponibile(o) (che a sua volta simula TUTTI gli OP aperti)
       veniva richiamata da zero per ogni componente in prima fase dello
       stesso OP — ora si calcola una volta sola per OP e si tiene in cache;
    4) _pezzi_fase() interrogava il database una volta per ogni (OP,
       componente) — fino a due volte per riga (fase corrente + fase
       precedente) — ora tutte le somme pezzi_buoni per gli OP coinvolti
       si leggono con UNA query aggregata sola, tenuta in un dizionario.
    """
    ordini = (OrdineProduzione.query
              .filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())

    sequenze_manuali = {
        (s.ordine_produzione_id, s.centro_costo_id): s.posizione
        for s in SequenzaMonitorMacchina.query.filter_by(centro_costo_id=centro.id).all()
    }

    # Tutta la distinta base in memoria una volta sola — vedi punto 1) sopra.
    mappa_distinta = _carica_mappa_distinta_base_wood()

    # Esplode la distinta di OGNI OP (in memoria, zero query aggiuntive) e
    # raccoglie tutti i codici coinvolti, per poter leggere il Ciclo di
    # Lavoro di TUTTI in una sola query invece di una per (OP, componente).
    componenti_per_op = {o.id: _esplodi_componenti_op(o, mappa_distinta=mappa_distinta) for o in ordini}
    tutti_i_codici = {c['codice'] for lista in componenti_per_op.values() for c in lista}
    fasi_per_codice = {}
    if tutti_i_codici:
        for f in (CicloLavoroWood.query.filter(CicloLavoroWood.codice.in_(tutti_i_codici))
                  .order_by(CicloLavoroWood.codice, CicloLavoroWood.sequenza).all()):
            fasi_per_codice.setdefault(f.codice, []).append(f)

    cache_materiale_disponibile = {}
    residuo_per_op, residuo_finale = _residuo_giacenza_progressivo(ordini, mappa=mappa_distinta)

    def _materiale_disponibile_cached(o):
        if o.id not in cache_materiale_disponibile:
            cache_materiale_disponibile[o.id] = _materiale_disponibile(o, residuo_per_op.get(o.id, residuo_finale), mappa_distinta=mappa_distinta)
        return cache_materiale_disponibile[o.id]

    # Vedi punto 4) sopra: una sola query aggregata per TUTTI i pezzi_buoni
    # consuntivati sugli OP coinvolti, invece di una query per (OP, fase,
    # componente) ripetuta nel ciclo sotto.
    codici_op = [o.codice for o in ordini]
    somma_pezzi = {}
    if codici_op:
        for op_code, fase, componente, tot in (db.session.query(
                EventoConsuntivoPP.op_code, db.func.lower(EventoConsuntivoPP.fase),
                EventoConsuntivoPP.componente, db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                .filter(EventoConsuntivoPP.op_code.in_(codici_op))
                .group_by(EventoConsuntivoPP.op_code, db.func.lower(EventoConsuntivoPP.fase),
                          EventoConsuntivoPP.componente).all()):
            somma_pezzi[(op_code, fase, componente)] = tot or 0

    def _pezzi_fase_cached(op_code, nome_centro, componente=None):
        return somma_pezzi.get((op_code, (nome_centro or '').lower(), componente), 0)

    righe = {k: [] for k in SEZIONI}
    for o in ordini:
        for comp in componenti_per_op[o.id]:
            codice_comp = comp['codice']
            componente_finale = (codice_comp == o.codice_articolo)
            componente_param = None if componente_finale else codice_comp

            fasi_ciclo = fasi_per_codice.get(codice_comp, [])
            idx = next((i for i, f in enumerate(fasi_ciclo) if f.centro_costo_id == centro.id), None)
            if idx is None:
                continue  # questo componente non passa da questa macchina

            qta_necessaria = round((o.qta_pianificata or 0) * comp['moltiplicatore'], 4)
            pezzi_fase = _pezzi_fase_cached(o.codice, centro.nome, componente=componente_param)
            saldo_fase = max(qta_necessaria - pezzi_fase, 0)
            pct_fase = round(pezzi_fase / qta_necessaria * 100) if qta_necessaria else 0

            if saldo_fase <= 0:
                sezione = 'terminati'
            elif pezzi_fase > 0:
                sezione = 'lavorazione'
            else:
                if idx == 0:
                    # Prima fase del SUO ciclo: pronto se il materiale grezzo
                    # dell'intero OP è disponibile (controllo approssimato a
                    # livello OP finché non affiniamo la disponibilità per
                    # singolo componente) — calcolato una volta sola per OP.
                    pronto = _materiale_disponibile_cached(o)
                else:
                    fase_prec = fasi_ciclo[idx - 1]
                    pronto = _pezzi_fase_cached(o.codice, fase_prec.centro_costo.nome, componente=componente_param) >= qta_necessaria
                sezione = 'da_iniziare' if pronto else 'in_attesa'

            fase_ciclo = fasi_ciclo[idx]
            tempo_standard_min_pz = (60 / fase_ciclo.produttivita_oraria) if fase_ciclo.produttivita_oraria else None

            # Standard fisico di consumo materiale per 1 pezzo di QUESTO componente
            # (non dell'intero OP) — solo l'alternativa attiva per gruppo.
            consumi_standard = [{'codice': rb.codice_figlio, 'quantita': rb.quantita}
                                for rb in _righe_bom_attive_wood(codice_comp, mappa=mappa_distinta)]

            posizione_manuale = sequenze_manuali.get((o.id, centro.id))
            chiave_ordine = (0, posizione_manuale) if posizione_manuale is not None else (
                1, o.priorita, o.data_prevista or datetime.max.date(), o.id, codice_comp)

            righe[sezione].append({
                'op_id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
                'codice_articolo': o.codice_articolo,
                'componente': componente_param, 'componente_finale': componente_finale,
                'codice_lavorato': codice_comp,
                'descrizione': o.descrizione or '',
                'priorita': o.priorita, 'stato_op': o.stato,
                'totale': qta_necessaria, 'saldo': saldo_fase, 'pct': pct_fase,
                'posizione_manuale': posizione_manuale,
                'data_prevista': o.data_prevista.isoformat() if o.data_prevista else None,
                'tempo_standard_min_pz': round(tempo_standard_min_pz, 2) if tempo_standard_min_pz else None,
                'scarto_max_pct': fase_ciclo.scarto_max_pct,
                'scarto_max_pezzi': (round(qta_necessaria * fase_ciclo.scarto_max_pct / 100))
                                     if fase_ciclo.scarto_max_pct else None,
                'consumi_standard': consumi_standard,
                '_chiave_ordine': chiave_ordine,
            })

    for sezione in righe:
        righe[sezione].sort(key=lambda r: r['_chiave_ordine'])
        for r in righe[sezione]:
            r.pop('_chiave_ordine')
    return righe


# ── ROUTE: pagina principale — se non specificata una macchina, va sulla prima disponibile ──
@monitor_bp.route('/monitor')
def index():
    macchine = get_macchine_monitor()
    if not macchine:
        return render_template('monitor/nessuna_macchina.html', active='monitor')
    from flask import redirect, url_for
    sega = next((m for m in macchine if 'sega' in m['nome'].lower()), None)
    return redirect(url_for('monitor.macchina', cid=(sega or macchine[0])['id']))


# Redirect di cortesia per il vecchio indirizzo /monitor/trapani
@monitor_bp.route('/monitor/trapani')
def index_trapani_legacy():
    from flask import redirect, url_for
    macchine = get_macchine_monitor()
    trapano = next((m for m in macchine if 'trapano' in m['nome'].lower()), None)
    if trapano:
        return redirect(url_for('monitor.macchina', cid=trapano['id']))
    return redirect(url_for('monitor.index'))


def _raggruppa_per_op(righe):
    """
    Raggruppa le righe (già in ordine di priorità da _righe_macchina) per OP —
    alimenta la vista 'a capo' del Monitor: una riga sola per OP con i totali
    aggregati, espandibile ai singoli componenti quando sono più di uno.
    L'ordine dei gruppi rispetta quello già calcolato riga per riga.
    """
    gruppi, indice_per_op = [], {}
    for r in righe:
        if r['op_id'] not in indice_per_op:
            indice_per_op[r['op_id']] = len(gruppi)
            gruppi.append({
                'op_id': r['op_id'], 'op_codice': r['op_codice'], 'commessa': r['commessa'],
                'codice_articolo': r['codice_articolo'], 'priorita': r['priorita'],
                'posizione_manuale': r['posizione_manuale'], 'descrizione': r['descrizione'],
                'componenti': [], 'saldo_totale': 0, 'totale_totale': 0,
            })
        g = gruppi[indice_per_op[r['op_id']]]
        g['componenti'].append(r)
        g['saldo_totale'] += r['saldo']
        g['totale_totale'] += r['totale']
    for g in gruppi:
        g['pct_aggregato'] = round(100 * (g['totale_totale'] - g['saldo_totale']) / g['totale_totale']) if g['totale_totale'] else 0
    return gruppi


@monitor_bp.route('/monitor/macchina/<int:cid>')
def macchina(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    if centro.esterno or centro.escluso_da_monitor_produzione:
        return render_template('monitor/nessuna_macchina.html', active='monitor',
            messaggio=f'"{centro.nome}" è marcato come {"esterno" if centro.esterno else "escluso dal Monitor Produzione"} — '
                      f'non genera una coda qui. Se pensi sia un errore, vai su Centri di Costo e correggi la configurazione.')
    righe_per_sezione = _righe_macchina(centro)
    gruppi_per_sezione = {sezione: _raggruppa_per_op(righe) for sezione, righe in righe_per_sezione.items()}
    return render_template('monitor/macchina.html',
        active='monitor', active_page='monitor',
        centro=centro, macchine=get_macchine_monitor(),
        sezioni_map=SEZIONI, gruppi_per_sezione=gruppi_per_sezione,
        now=datetime.now().strftime('%d/%m/%Y'))


# ── TOTEM — view live a bordo macchina, sola lettura, auto-refresh ────────────
# Pensata per un monitor/tablet fissato fisicamente vicino alla macchina: NON
# estende base.html (niente sidebar, niente editing), solo ciò che l'operaio
# deve vedere per sapere cosa lavorare adesso e cosa viene dopo in coda.
@monitor_bp.route('/totem/macchina/<int:cid>')
def totem_macchina(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    if centro.esterno or centro.escluso_da_monitor_produzione:
        return render_template('monitor/nessuna_macchina.html', active='monitor',
            messaggio=f'"{centro.nome}" è marcato come {"esterno" if centro.esterno else "escluso dal Monitor Produzione"} — nessun totem qui.')
    righe = _righe_macchina(centro)

    sessione = SessioneLavoroMacchina.query.filter_by(centro_costo_id=cid, terminata_il=None).first()
    sessione_info = None
    riga_attiva = None
    if sessione:
        minuti = round((datetime.utcnow() - sessione.iniziata_il).total_seconds() / 60)
        sessione_info = {'sessione_id': sessione.id, 'op_id': sessione.ordine_produzione_id,
                          'componente': sessione.componente,
                          'iniziata_il': sessione.iniziata_il.isoformat(), 'minuti_trascorsi': minuti}
        for lista in (righe['lavorazione'], righe['da_iniziare'], righe['in_attesa'], righe['terminati']):
            riga_attiva = next((r for r in lista if r['op_id'] == sessione.ordine_produzione_id
                                and r['componente'] == sessione.componente), None)
            if riga_attiva:
                break
        if riga_attiva is None:
            # L'OP (o il suo componente) con sessione aperta non compare in
            # nessun bucket (es. completato nel frattempo): ricostruisco
            # comunque i dati minimi per mostrarlo.
            o = sessione.ordine_produzione
            codice_lavorato = sessione.componente or o.codice_articolo
            riga_attiva = {'op_id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
                           'codice_articolo': o.codice_articolo,
                           'componente': sessione.componente, 'componente_finale': not sessione.componente,
                           'codice_lavorato': codice_lavorato, 'descrizione': o.descrizione or '',
                           'totale': o.qta_pianificata, 'saldo': max((o.qta_pianificata or 0) - (o.qta_buona or 0), 0),
                           'pct': round((o.qta_buona or 0) / o.qta_pianificata * 100) if o.qta_pianificata else 0,
                           'tempo_standard_min_pz': None, 'scarto_max_pct': None, 'scarto_max_pezzi': None,
                           'consumi_standard': [{'codice': rb.codice_figlio, 'quantita': rb.quantita}
                                                for rb in _righe_bom_attive_wood(codice_lavorato)]}
    elif righe['lavorazione']:
        riga_attiva = righe['lavorazione'][0]
    elif righe['da_iniziare']:
        riga_attiva = righe['da_iniziare'][0]

    return render_template('monitor/totem.html',
        centro=centro, righe=righe, macchine=get_macchine_monitor(),
        riga_attiva=riga_attiva, sessione=sessione_info,
        now=datetime.now().strftime('%d/%m/%Y'))


# ══════════════════════════════════════════════════════════════════════════════
#  API — dati macchina (per refresh AJAX) e riordino manuale della coda
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/api/monitor_macchina/<int:cid>')
def api_righe_macchina(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    return jsonify(_righe_macchina(centro))


@monitor_bp.route('/api/monitor_macchina/<int:cid>/ordina', methods=['POST'])
def api_ordina_macchina(cid):
    """
    Il capo fissa a mano la posizione di un OP nella coda di questa macchina
    (es. per metterlo davanti a tutti perché urgente). Se l'OP non ha ancora
    una riga qui, viene creata; altrimenti la posizione viene aggiornata.
    """
    CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    try:
        op_id = int(d['ordine_produzione_id'])
        posizione = int(d['posizione'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'ordine_produzione_id e posizione sono obbligatori (numerici)'}), 400
    OrdineProduzione.query.get_or_404(op_id)

    riga = SequenzaMonitorMacchina.query.filter_by(ordine_produzione_id=op_id, centro_costo_id=cid).first()
    if riga:
        riga.posizione = posizione
    else:
        db.session.add(SequenzaMonitorMacchina(ordine_produzione_id=op_id, centro_costo_id=cid, posizione=posizione))
    log(f'Monitor macchina: OP #{op_id} posizionato a {posizione} su centro di costo {cid}')
    db.session.commit()
    return jsonify({'ok': True})


@monitor_bp.route('/api/monitor_macchina/<int:cid>/ordina/<int:op_id>', methods=['DELETE'])
def api_reset_ordine_macchina(cid, op_id):
    """Rimuove la posizione manuale: l'OP torna a ordinarsi da solo (priorità/data consegna)."""
    riga = SequenzaMonitorMacchina.query.filter_by(ordine_produzione_id=op_id, centro_costo_id=cid).first()
    if riga:
        db.session.delete(riga)
        db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  TOTEM — INIZIO / FINE LAVORO
#  L'operatore preme un bottone sul totem: nessuna tastiera, nessun form,
#  tranne i due numeri (pezzi buoni/scarto) richiesti alla chiusura. Il
#  consuntivo generato alla chiusura passa dallo STESSO motore usato
#  dall'integrazione MasterWork (_registra_evento_consuntivo): scarico
#  giacenza, carico prodotto finito, varianza di lavorazione — tutto incluso.
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/api/totem/<int:cid>/sessione_aperta')
def api_totem_sessione_aperta(cid):
    s = (SessioneLavoroMacchina.query
         .filter_by(centro_costo_id=cid, terminata_il=None).first())
    if not s:
        return jsonify({'aperta': False})
    minuti = round((datetime.utcnow() - s.iniziata_il).total_seconds() / 60)
    return jsonify({
        'aperta': True, 'sessione_id': s.id, 'ordine_produzione_id': s.ordine_produzione_id,
        'componente': s.componente,
        'op_codice': s.ordine_produzione.codice, 'iniziata_il': s.iniziata_il.isoformat(),
        'minuti_trascorsi': minuti,
    })


@monitor_bp.route('/api/totem/<int:cid>/inizia', methods=['POST'])
def api_totem_inizia(cid):
    """
    'componente' (facoltativo): quale componente della distinta base dell'OP
    si sta lavorando in QUESTA macchina — es. uno dei pezzi che passano dalla
    segatrice prima di diventare il prodotto finito. Vuoto/assente = si sta
    lavorando il prodotto finito/assieme finale dell'OP (comportamento storico).
    """
    centro = CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    try:
        op_id = int(d['ordine_produzione_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'ordine_produzione_id obbligatorio'}), 400
    componente = (d.get('componente') or '').strip() or None
    o = OrdineProduzione.query.get_or_404(op_id)

    esistente = SessioneLavoroMacchina.query.filter_by(centro_costo_id=cid, terminata_il=None).first()
    if esistente:
        if esistente.ordine_produzione_id == op_id and esistente.componente == componente:
            return jsonify({'ok': True, 'sessione_id': esistente.id, 'gia_aperta': True})
        etichetta = esistente.componente or esistente.ordine_produzione.codice_articolo
        return jsonify({'errore': True,
                        'messaggio': f'Macchina già impegnata su {esistente.ordine_produzione.codice} ({etichetta}) — chiudi prima quel lavoro'}), 409

    if not _is_carpenteria(o.asa) or o.stato not in ('Rilasciato', 'In esecuzione'):
        return jsonify({'errore': True, 'messaggio': 'OP non attivo o non disponibile per Carpenteria Propria'}), 409

    s = SessioneLavoroMacchina(ordine_produzione_id=op_id, centro_costo_id=cid, componente=componente,
                                iniziata_il=datetime.utcnow())
    db.session.add(s)
    log(f'Totem {centro.nome}: iniziato lavoro su OP {o.codice}' + (f' — componente {componente}' if componente else ''))
    db.session.commit()
    return jsonify({'ok': True, 'sessione_id': s.id})


@monitor_bp.route('/api/totem/<int:cid>/termina', methods=['POST'])
def api_totem_termina(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    try:
        op_id = int(d['ordine_produzione_id'])
        good = int(d.get('pezzi_buoni', 0) or 0)
        scrap = int(d.get('pezzi_scarto', 0) or 0)
    except (KeyError, TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'ordine_produzione_id, pezzi_buoni e pezzi_scarto sono obbligatori (numerici)'}), 400
    if good < 0 or scrap < 0:
        return jsonify({'errore': True, 'messaggio': 'I pezzi non possono essere negativi'}), 400
    componente = (d.get('componente') or '').strip() or None

    s = (SessioneLavoroMacchina.query
         .filter_by(ordine_produzione_id=op_id, centro_costo_id=cid, componente=componente, terminata_il=None).first())
    if not s:
        return jsonify({'errore': True, 'messaggio': 'Nessuna sessione di lavoro aperta per questo OP/componente su questa macchina'}), 404

    now = datetime.utcnow()
    tempo_minuti = max(round((now - s.iniziata_il).total_seconds() / 60), 0)
    o = OrdineProduzione.query.filter_by(id=op_id).with_for_update().first()
    if not o:
        return jsonify({'errore': True, 'messaggio': 'OP non trovato'}), 404
    if o.stato not in ('Rilasciato', 'In esecuzione'):
        return jsonify({'errore': True, 'messaggio': f'OP non modificabile nello stato {o.stato}'}), 409

    event_id = f'totem-sessione-{s.id}'
    try:
        _registra_evento_consuntivo(o, centro.nome, now, good, scrap, tempo_minuti, event_id, componente=componente)
        s.terminata_il = now
        s.pezzi_buoni = good
        s.pezzi_scarto = scrap
        s.event_id_generato = event_id
        log(f'Totem {centro.nome}: terminato lavoro su OP {o.codice}' + (f' ({componente})' if componente else '') +
            f' — {good} buoni, {scrap} scarto, {tempo_minuti} min')
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': f'Errore nella registrazione del consuntivo: {e}'}), 500
    return jsonify({'ok': True, 'tempo_minuti': tempo_minuti})


# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENTAZIONE TECNICA PER ARTICOLO — disegni/istruzioni mostrati sul totem
#  quando quell'articolo è in lavorazione. Salvata come base64 nel DB.
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/api/totem/documenti/<codice_articolo>')
def api_lista_documenti_tecnici(codice_articolo):
    righe = (DocumentoTecnicoArticolo.query.filter_by(codice_articolo=codice_articolo)
             .order_by(DocumentoTecnicoArticolo.caricato_il.desc()).all())
    return jsonify([{
        'id': r.id, 'nome_file': r.nome_file, 'tipo_mime': r.tipo_mime, 'note': r.note or '',
        'caricato_il': r.caricato_il.isoformat(),
    } for r in righe])


@monitor_bp.route('/api/totem/documenti', methods=['POST'])
def api_carica_documento_tecnico():
    """Body JSON: {codice_articolo, nome_file, tipo_mime, contenuto_base64, note?} — contenuto_base64 SENZA il prefisso data:...;base64,"""
    d = request.get_json(force=True)
    codice = (d.get('codice_articolo') or '').strip()
    nome_file = (d.get('nome_file') or '').strip()
    contenuto = d.get('contenuto_base64') or ''
    if not codice or not nome_file or not contenuto:
        return jsonify({'errore': True, 'messaggio': 'codice_articolo, nome_file e contenuto_base64 sono obbligatori'}), 400
    r = DocumentoTecnicoArticolo(codice_articolo=codice, nome_file=nome_file,
                                  tipo_mime=(d.get('tipo_mime') or 'application/octet-stream'),
                                  contenuto_base64=contenuto, note=(d.get('note') or '').strip())
    db.session.add(r)
    db.session.commit()
    return jsonify({'ok': True, 'id': r.id})


@monitor_bp.route('/api/totem/documenti/file/<int:did>')
def api_file_documento_tecnico(did):
    r = DocumentoTecnicoArticolo.query.get_or_404(did)
    return Response(base64.b64decode(r.contenuto_base64), mimetype=r.tipo_mime,
                     headers={'Content-Disposition': f'inline; filename="{r.nome_file}"'})


@monitor_bp.route('/api/totem/documenti/<int:did>', methods=['DELETE'])
def api_elimina_documento_tecnico(did):
    r = DocumentoTecnicoArticolo.query.get_or_404(did)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  FOTO LAVORAZIONE — scattate dall'operatore dal totem (come da cellulare),
#  legate a un OP + centro di costo. Salvate come base64 nel DB.
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/api/totem/<int:cid>/foto/<int:op_id>')
def api_lista_foto_lavorazione(cid, op_id):
    righe = (FotoLavorazioneMacchina.query
             .filter_by(centro_costo_id=cid, ordine_produzione_id=op_id)
             .order_by(FotoLavorazioneMacchina.caricato_il.desc()).all())
    return jsonify([{'id': r.id, 'nome_file': r.nome_file, 'caricato_il': r.caricato_il.isoformat()} for r in righe])


@monitor_bp.route('/api/totem/<int:cid>/foto', methods=['POST'])
def api_carica_foto_lavorazione(cid):
    """Body JSON: {ordine_produzione_id, nome_file, contenuto_base64} — contenuto_base64 SENZA il prefisso data:...;base64,"""
    CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    try:
        op_id = int(d['ordine_produzione_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'ordine_produzione_id obbligatorio'}), 400
    OrdineProduzione.query.get_or_404(op_id)
    contenuto = d.get('contenuto_base64') or ''
    nome_file = (d.get('nome_file') or 'foto.jpg').strip()
    if not contenuto:
        return jsonify({'errore': True, 'messaggio': 'contenuto_base64 obbligatorio'}), 400
    r = FotoLavorazioneMacchina(ordine_produzione_id=op_id, centro_costo_id=cid,
                                 nome_file=nome_file, contenuto_base64=contenuto)
    db.session.add(r)
    db.session.commit()
    return jsonify({'ok': True, 'id': r.id})


@monitor_bp.route('/api/totem/foto/file/<int:fid>')
def api_file_foto_lavorazione(fid):
    r = FotoLavorazioneMacchina.query.get_or_404(fid)
    return Response(base64.b64decode(r.contenuto_base64), mimetype='image/jpeg')


@monitor_bp.route('/api/totem/foto/<int:fid>', methods=['DELETE'])
def api_elimina_foto_lavorazione(fid):
    r = FotoLavorazioneMacchina.query.get_or_404(fid)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENTAZIONE ARTICOLO — pagina indipendente dal totem: qui si cerca un
#  codice articolo e si vede/carica sia la documentazione tecnica (stessi
#  endpoint /api/totem/documenti/... già usati dal totem — condivisi, non
#  duplicati) sia le foto di riferimento del prodotto (FotoArticolo, nuove).
#  Sempre disponibile, non serve nessun OP aperto né sessione di lavoro.
# ══════════════════════════════════════════════════════════════════════════════

@monitor_bp.route('/documentazione-articolo')
def pagina_documentazione_articolo():
    return render_template('monitor/documentazione_articolo.html', active='documentazione_articolo')


@monitor_bp.route('/api/foto_articolo/<codice_articolo>')
def api_lista_foto_articolo(codice_articolo):
    righe = (FotoArticolo.query.filter_by(codice_articolo=codice_articolo)
             .order_by(FotoArticolo.caricato_il.desc()).all())
    return jsonify([{'id': r.id, 'nome_file': r.nome_file, 'note': r.note or '',
                      'caricato_il': r.caricato_il.isoformat()} for r in righe])


@monitor_bp.route('/api/foto_articolo', methods=['POST'])
def api_carica_foto_articolo():
    """Body JSON: {codice_articolo, nome_file, contenuto_base64, note?} — contenuto_base64 SENZA il prefisso data:...;base64,"""
    d = request.get_json(force=True)
    codice = (d.get('codice_articolo') or '').strip()
    nome_file = (d.get('nome_file') or '').strip()
    contenuto = d.get('contenuto_base64') or ''
    if not codice or not nome_file or not contenuto:
        return jsonify({'errore': True, 'messaggio': 'codice_articolo, nome_file e contenuto_base64 sono obbligatori'}), 400
    r = FotoArticolo(codice_articolo=codice, nome_file=nome_file, contenuto_base64=contenuto, note=(d.get('note') or '').strip())
    db.session.add(r)
    db.session.commit()
    return jsonify({'ok': True, 'id': r.id})


@monitor_bp.route('/api/foto_articolo/file/<int:fid>')
def api_file_foto_articolo(fid):
    r = FotoArticolo.query.get_or_404(fid)
    return Response(base64.b64decode(r.contenuto_base64), mimetype='image/jpeg')


@monitor_bp.route('/api/foto_articolo/<int:fid>', methods=['DELETE'])
def api_elimina_foto_articolo(fid):
    r = FotoArticolo.query.get_or_404(fid)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})
