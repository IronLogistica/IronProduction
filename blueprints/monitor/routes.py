import base64
import uuid
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request, Response
from models import (db, log, CentroCostoWood, CicloLavoroWood, OrdineProduzione,
                    EventoConsuntivoPP, SequenzaMonitorMacchina, get_macchine_monitor,
                    SessioneLavoroMacchina, DocumentoTecnicoArticolo, FotoLavorazioneMacchina, FotoArticolo,
                    NumeroListaLavoroWood, SchedaLavorazioneWood, ArticoloML, DescrizioneCodiceWood,
                    ParametriLavorazioneWood)
from blueprints.magazzino.routes import (_giacenza_residua_dopo_impegni, _netta_e_esplodi_wood,
                    _righe_bom_attive_wood, _esplodi_componenti_op, _residuo_giacenza_progressivo,
                    _carica_mappa_distinta_base_wood, STATI_CHE_IMPEGNANO)
from blueprints.produzione_pp.routes import _registra_evento_consuntivo, _audit, _is_carpenteria

monitor_bp = Blueprint('monitor', __name__)

SEZIONI = {
    'da_iniziare': ('🛬 IN ATTESA / DA INIZIARE',           'da_iniziare-hdr'),
    'lavorazione': ('⚙️ IN ESECUZIONE',                     'lavorazione-hdr'),
    'terminati':   ('✅ APPENA TERMINATI (questa fase)',    'terminati-hdr'),
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

    # OP 'Creato' ma NON ANCORA rilasciato: non entra nel netting materiale
    # (non è ancora un impegno reale di produzione), ma l'utente deve poter
    # vederlo comunque in coda come promemoria — altrimenti un OP inserito e
    # mai rilasciato sparisce dal radar finché qualcuno non se lo ricorda da
    # solo. Righe semplificate (materiale/% non calcolati), sempre in
    # "IN ATTESA / DA INIZIARE", con 'non_rilasciato': True per il badge.
    ordini_non_rilasciati = (OrdineProduzione.query.filter_by(stato='Creato')
                              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())

    # Un OP passa a "IN ESECUZIONE" appena viene emesso il suo Ordine di
    # Lavoro su QUESTA macchina (numero di lista assegnato — vedi
    # NumeroListaLavoroWood/_numero_lista_lavoro), non solo quando arrivano i
    # primi pezzi consuntivati: l'emissione stessa è il segnale che il lavoro
    # è stato affidato agli operai.
    op_con_ordine_emesso = {r[0] for r in db.session.query(NumeroListaLavoroWood.op_code)
                            .filter_by(centro_costo_id=centro.id).distinct().all()}

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
    componenti_per_op_non_rilasciati = {o.id: _esplodi_componenti_op(o, mappa_distinta=mappa_distinta) for o in ordini_non_rilasciati}
    tutti_i_codici = ({c['codice'] for lista in componenti_per_op.values() for c in lista}
                       | {c['codice'] for lista in componenti_per_op_non_rilasciati.values() for c in lista})
    fasi_per_codice = {}
    if tutti_i_codici:
        for f in (CicloLavoroWood.query.filter(CicloLavoroWood.codice.in_(tutti_i_codici))
                  .order_by(CicloLavoroWood.codice, CicloLavoroWood.sequenza).all()):
            fasi_per_codice.setdefault(f.codice, []).append(f)

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

    # Disponibilità materiale — stesso motore "a residuo progressivo" di
    # Situazione Ordini di Produzione (priorità servita in ordine), ma qui
    # SOLO sul materiale che serve per lavorare QUESTA fase specifica (il
    # consumo diretto del componente in lavorazione su questa macchina), non
    # sull'intera distinta base del prodotto finito — un OP può benissimo
    # avere il tubo pronto per il Curvatubi anche se manca ancora, per dire,
    # la verniciatura finale: l'operaio alla macchina deve sapere se PUÒ
    # lavorare ORA, non se l'intero ordine è già completo di tutto.
    residuo_per_op, residuo_finale = _residuo_giacenza_progressivo(op_aperti=ordini, mappa=mappa_distinta)

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
            elif pezzi_fase > 0 or o.codice in op_con_ordine_emesso:
                # In lavorazione anche solo per aver EMESSO l'Ordine di
                # Lavoro (anche senza ancora nessun pezzo dichiarato) — non
                # più solo quando arrivano i primi pezzi consuntivati.
                sezione = 'lavorazione'
            else:
                sezione = 'da_iniziare'

            fase_ciclo = fasi_ciclo[idx]
            tempo_standard_min_pz = (60 / fase_ciclo.produttivita_oraria) if fase_ciclo.produttivita_oraria else None

            # Standard fisico di consumo materiale per 1 pezzo di QUESTO componente
            # (non dell'intero OP) — solo l'alternativa attiva per gruppo.
            consumi_standard = [{'codice': rb.codice_figlio, 'quantita': rb.quantita}
                                for rb in _righe_bom_attive_wood(codice_comp, mappa=mappa_distinta)]

            # Materiale per lavorare QUESTA riga ORA: giacenza residua (dopo
            # aver già servito chi ha priorità pari/superiore) sufficiente per
            # coprire il fabbisogno di ogni materiale diretto di questo
            # componente. Nessun consumo noto registrato → non blocca
            # l'evidenza (stessa tolleranza già usata nelle Liste di Lavoro
            # quando Parametri di Lavorazione non è ancora compilato).
            giacenza_di_op = residuo_per_op.get(o.id, residuo_finale)
            materiale_disponibile_riga = all(
                giacenza_di_op.get(cs['codice'], 0) >= round(qta_necessaria * cs['quantita'], 4)
                for cs in consumi_standard
            ) if consumi_standard else True

            posizione_manuale = sequenze_manuali.get((o.id, centro.id))
            chiave_ordine = (0, posizione_manuale) if posizione_manuale is not None else (
                1, o.priorita, o.data_prevista or datetime.max.date(), o.id, codice_comp)

            righe[sezione].append({
                'op_id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
                'codice_articolo': o.codice_articolo,
                # Pezzi di PRODOTTO FINITO pianificati per questo OP — NON i pezzi di
                # questa fase (vedi 'totale'): un OP con più componenti che passano
                # dalla stessa macchina fa sommare 'totale' più volte, mentre questo
                # resta il numero di unità finite che l'OP deve produrre.
                'qta_pianificata': o.qta_pianificata,
                'materiale_disponibile': materiale_disponibile_riga,
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
                'non_rilasciato': False,
                'completato': saldo_fase <= 0,
                '_chiave_ordine': chiave_ordine,
            })

    # ── righe promemoria per gli OP 'Creato' non ancora rilasciati ──
    for o in ordini_non_rilasciati:
        for comp in componenti_per_op_non_rilasciati[o.id]:
            codice_comp = comp['codice']
            componente_finale = (codice_comp == o.codice_articolo)
            fasi_ciclo = fasi_per_codice.get(codice_comp, [])
            idx = next((i for i, f in enumerate(fasi_ciclo) if f.centro_costo_id == centro.id), None)
            if idx is None:
                continue
            qta_necessaria = round((o.qta_pianificata or 0) * comp['moltiplicatore'], 4)
            fase_ciclo = fasi_ciclo[idx]
            tempo_standard_min_pz = (60 / fase_ciclo.produttivita_oraria) if fase_ciclo.produttivita_oraria else None
            righe['da_iniziare'].append({
                'op_id': o.id, 'op_codice': o.codice, 'commessa': o.commessa or '',
                'codice_articolo': o.codice_articolo, 'qta_pianificata': o.qta_pianificata,
                'materiale_disponibile': None,  # non calcolato: l'OP non è ancora un impegno reale
                'componente': None if componente_finale else codice_comp, 'componente_finale': componente_finale,
                'codice_lavorato': codice_comp, 'descrizione': o.descrizione or '',
                'priorita': o.priorita, 'stato_op': o.stato,
                'totale': qta_necessaria, 'saldo': qta_necessaria, 'pct': 0,
                'posizione_manuale': None, 'data_prevista': o.data_prevista.isoformat() if o.data_prevista else None,
                'tempo_standard_min_pz': round(tempo_standard_min_pz, 2) if tempo_standard_min_pz else None,
                'scarto_max_pct': fase_ciclo.scarto_max_pct, 'scarto_max_pezzi': None,
                'consumi_standard': [], 'non_rilasciato': True,
                '_chiave_ordine': (2, o.priorita, o.data_prevista or datetime.max.date(), o.id, codice_comp),
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
                'qta_pianificata': r['qta_pianificata'], 'materiale_disponibile': r['materiale_disponibile'],
                'non_rilasciato': r.get('non_rilasciato', False),
                'componenti': [], 'saldo_totale': 0, 'totale_totale': 0,
            })
        g = gruppi[indice_per_op[r['op_id']]]
        g['componenti'].append(r)
        g['saldo_totale'] += r['saldo']
        g['totale_totale'] += r['totale']
        g['materiale_disponibile'] = g['materiale_disponibile'] and r['materiale_disponibile']
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
    """
    Totem Live in Carpenteria — tabella in SOLA LETTURA: una riga per
    COMMESSA (non per singolo codice/figlio/nipote — tutti i pezzi da fare
    su questa macchina per quella commessa sono sommati insieme), col
    prodotto finito e la sua immagine di riferimento. I tempi arrivano da
    MasterWork; i pezzi fatti sono quelli già dichiarati altrove (dal
    saldatore o da Alessandro) in Dichiarazione di Produzione — qui non si
    dichiara nulla, si legge soltanto.
    """
    centro = CentroCostoWood.query.get_or_404(cid)
    if centro.esterno or centro.escluso_da_monitor_produzione:
        return render_template('monitor/nessuna_macchina.html', active='monitor',
            messaggio=f'"{centro.nome}" è marcato come {"esterno" if centro.esterno else "escluso dal Monitor Produzione"} — nessun totem qui.')
    righe = _righe_macchina(centro)
    # Le righe COMPLETATE (sezione 'terminati') restano visibili qui, non
    # spariscono più: prima venivano escluse del tutto dal Totem Live, quindi
    # un componente appena finito (es. il primo taglio di T200) scompariva
    # alla vista dell'operaio E — siccome _raggruppa_per_op raggruppa PER
    # SEZIONE — la percentuale complessiva dell'OP in cima non lo contava
    # più, restando ferma anche se in realtà quel pezzo dell'OP era fatto al
    # 100%. Ora tutte le righe dello stesso OP restano in UN solo gruppo,
    # qualunque sia il loro stato, e la percentuale aggregata le somma tutte.
    righe_tabella = righe['da_iniziare'] + righe['lavorazione'] + righe['terminati']
    righe_tabella.sort(key=lambda r: (r['posizione_manuale'] if r['posizione_manuale'] is not None else 999, r['priorita'], r['op_id']))

    gruppi = _raggruppa_per_op(righe_tabella)
    # Il Totem Live mostra SOLO le commesse pronte per essere lavorate ORA
    # (materiale disponibile — il bordo giallo lampeggiante) — quelle che
    # aspettano ancora materiale mancante ("non autorizzate" a partire)
    # restano fuori: l'operaio davanti alla macchina deve vedere solo cosa
    # può davvero prendere in mano adesso, non l'intera coda con anche i
    # lavori bloccati.
    gruppi = [g for g in gruppi if g['materiale_disponibile']]
    # Un gruppo (OP) completato al 100% — TUTTI i suoi componenti finiti,
    # non solo uno — non serve più qui: si toglie del tutto. Diverso dal
    # caso di completamento PARZIALE (un componente finito, altri ancora
    # da fare): quello resta visibile con il componente segnato ✅, proprio
    # perché serve a far tornare giusta la percentuale aggregata dell'OP —
    # qui invece non c'è più nessuna percentuale da seguire, è finito.
    gruppi = [g for g in gruppi if g['pct_aggregato'] < 100]

    codici_prodotto_finito = {g['codice_articolo'] for g in gruppi}
    foto_per_codice = {}
    if codici_prodotto_finito:
        for f in FotoArticolo.query.filter(FotoArticolo.codice_articolo.in_(codici_prodotto_finito)).order_by(FotoArticolo.caricato_il.desc()).all():
            foto_per_codice.setdefault(f.codice_articolo, f.id)
    for g in gruppi:
        g['foto_id'] = foto_per_codice.get(g['codice_articolo'])

    # Su Curvatubi, Satinatrice, Sgolatrice, Pressopiegatrice, Punzonatrice,
    # Trapani e Segatrice, sotto la riga dei totali va mostrato anche il
    # dettaglio dei singoli codici lavorati con i parametri di IMPOSTAZIONE
    # MACCHINA già compilati in Parametri di Lavorazione
    # (SchedaLavorazioneWood) — non le stesse colonne per tutte: ogni
    # macchina ha i suoi parametri fisici di setup.
    nome_l = centro.nome.lower()
    if 'curva' in nome_l:
        colonne_parametri = [{'chiave': 'sviluppo', 'etichetta': 'Sviluppo'},
                             {'chiave': 'matrice', 'etichetta': 'Matrice'},
                             {'chiave': 'punto_zero', 'etichetta': 'Punto Zero'},
                             {'chiave': 'indice_assorbimento', 'etichetta': 'Indice Assorbimento'}]
    elif 'satin' in nome_l:
        colonne_parametri = [{'chiave': 'punto_zero', 'etichetta': 'Punto Zero'}]
    elif 'sgola' in nome_l:
        colonne_parametri = [{'chiave': 'sviluppo', 'etichetta': 'Sviluppo'},
                             {'chiave': 'punto_zero', 'etichetta': 'Punto Zero'},
                             {'chiave': 'rullo', 'etichetta': 'Rullo Sgolatrice'}]
    elif 'piega' in nome_l:
        colonne_parametri = [{'chiave': 'sviluppo', 'etichetta': 'Sviluppo'}]
    elif 'punzon' in nome_l:
        colonne_parametri = [{'chiave': 'sviluppo', 'etichetta': 'Sviluppo'}]
    elif 'trapan' in nome_l:
        colonne_parametri = [{'chiave': 'sviluppo', 'etichetta': 'Sviluppo'}]
    elif 'sega' in nome_l:
        colonne_parametri = [{'chiave': 'sviluppo', 'etichetta': 'Sviluppo'},
                             {'chiave': 'lunghezza_barra_mm', 'etichetta': 'Lunghezza Barra'},
                             {'chiave': 'spessore_mm', 'etichetta': 'Spessore'},
                             {'chiave': 'pezzi_per_barra', 'etichetta': 'Pz/Barra'}]
    else:
        colonne_parametri = []

    # Descrizione di ogni codice lavorato (non quella del prodotto finito
    # dell'OP, che è un'altra cosa) — stessa fonte con riserva già usata
    # nell'Esploratore Prodotto: ArticoloML (magazzino condiviso di
    # MasterLogistic) prima, la tabella locale (da import Zucchetti/DESCOM)
    # solo per i codici che ArticoloML non conosce ancora.
    tutti_codici_lavorati = {c['codice_lavorato'] for g in gruppi for c in g['componenti']}
    descrizione_per_codice = {}
    if tutti_codici_lavorati:
        try:
            for a in ArticoloML.query.filter(ArticoloML.sku.in_(tutti_codici_lavorati)).all():
                if a.descrizione:
                    descrizione_per_codice[a.sku] = a.descrizione
        except Exception:
            db.session.rollback()
        codici_senza_descr = tutti_codici_lavorati - set(descrizione_per_codice.keys())
        if codici_senza_descr:
            for d in DescrizioneCodiceWood.query.filter(DescrizioneCodiceWood.codice.in_(codici_senza_descr)).all():
                if d.descrizione:
                    descrizione_per_codice[d.codice] = d.descrizione
    for g in gruppi:
        for c in g['componenti']:
            c['descrizione_lavorata'] = descrizione_per_codice.get(c['codice_lavorato'], '')

    if colonne_parametri:
        codici_lavorati = {c['codice_lavorato'] for g in gruppi for c in g['componenti']}
        scheda_per_codice = {}
        if codici_lavorati:
            for s in ParametriLavorazioneWood.query.filter(ParametriLavorazioneWood.codice.in_(codici_lavorati)).all():
                scheda_per_codice[s.codice] = s
        for g in gruppi:
            for c in g['componenti']:
                s = scheda_per_codice.get(c['codice_lavorato'])
                valori = {}
                for col in colonne_parametri:
                    chiave = col['chiave']
                    if chiave == 'matrice':
                        valori[chiave] = s.matrice.codice if (s and s.matrice) else ''
                    elif chiave == 'rullo':
                        valori[chiave] = s.rullo.codice if (s and s.rullo) else ''
                    elif chiave == 'lunghezza_barra_mm':
                        v = getattr(s, chiave, None) if s else None
                        valori[chiave] = f"{round(v / 1000, 2)} m" if v else ''
                    elif chiave == 'spessore_mm':
                        v = getattr(s, chiave, None) if s else None
                        valori[chiave] = f"{v} mm" if v else ''
                    else:
                        valori[chiave] = getattr(s, chiave, '') if s else ''
                c['parametri'] = valori

    # Saldatura assembla insieme PIÙ semilavorati/materie prime diversi in
    # UN prodotto finito: sommarli come fa _raggruppa_per_op per le altre
    # macchine (dove un solo componente passa più volte) qui gonfia "Pz da
    # Fare" contando ogni pezzo da unire invece dei pezzi finiti da saldare.
    # Qui SOLO il valore di "N° pz in produzione" (prodotto finito) ha senso.
    saldatura_nota = 'salda' in nome_l
    if saldatura_nota:
        for g in gruppi:
            g['totale_totale'] = g['qta_pianificata']
            finale = next((c for c in g['componenti'] if c['componente_finale']), None)
            g['saldo_totale'] = finale['saldo'] if finale else g['qta_pianificata']
            g['pct_aggregato'] = round(100 * (g['totale_totale'] - g['saldo_totale']) / g['totale_totale']) if g['totale_totale'] else 0

    return render_template('monitor/totem_tabella.html', centro=centro, gruppi=gruppi,
        righe_terminati=righe['terminati'][:8], macchine=get_macchine_monitor(),
        colonne_parametri=colonne_parametri, saldatura_nota=saldatura_nota,
        now=datetime.now().strftime('%d/%m/%Y'))


@monitor_bp.route('/api/totem_tabella/<int:cid>/dichiara', methods=['POST'])
def api_totem_tabella_dichiara(cid):
    """
    ⚠️ Endpoint NON più usato dal Totem (diventato sola lettura, la
    dichiarazione avviene solo in Dichiarazione di Produzione) — lasciato
    per compatibilità in caso qualche chiamata residua lo usasse ancora.
    """
    centro = CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    o = OrdineProduzione.query.get(d.get('op_id'))
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    try:
        good = int(d.get('pezzi_buoni') or 0)
        scrap = int(d.get('pezzi_scarto') or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Quantità non valida'), 400
    if good <= 0 and scrap <= 0:
        return jsonify(ok=False, error='Dichiara almeno un pezzo buono o di scarto'), 400
    componente = (d.get('componente') or '').strip() or None
    event_id = str(uuid.uuid4())
    _registra_evento_consuntivo(o, centro.nome, datetime.utcnow(), good, scrap, 0, event_id, componente=componente)
    db.session.commit()
    return jsonify(ok=True)


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


# ── TOTEM ALESSANDRO — postazione dedicata, verticale, senza via d'uscita ────
# Schermo fisicamente ruotato (uno schermo normale girato a fare da totem) con
# SOLO 3 destinazioni: Dichiarazione Produzione (come Angelo, ma senza Area
# Capo/Diagnostica/Direzione, che non competono ad Alessandro), Documentazione
# Articolo, e un selettore Monitor che apre — sempre dentro la stessa pagina —
# il Totem Live già esistente, centro di costo per centro di costo. Pagina
# standalone (non estende base.html): nessuna sidebar, nessun link verso il
# resto del programma.
@monitor_bp.route('/totem/alessandro')
def totem_alessandro():
    return render_template('monitor/totem_alessandro.html', macchine=get_macchine_monitor())


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
