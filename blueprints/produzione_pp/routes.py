import os
import uuid
from datetime import datetime, date
from flask import Blueprint, current_app, jsonify, render_template, request, Response
from sqlalchemy.exc import IntegrityError
from models import (db, log, OrdineProduzione, EventoConsuntivoPP, AuditPP,
                    STATI_ORDINE_PP, ASA_MASTERWORK, prossimo_codice_ordine_pp,
                    prossimo_numero_commessa, GiacenzaWood, MovimentoGiacenzaWood, CicloLavoroWood,
                    CentroCostoWood, VarianzaProduzioneWood, ArticoloApprovvigionamento,
                    CostoStandardVersioneWood, LegameCostoStandardOrdineWood, DistintaBaseWood,
                    CostoStandardVersioneDettaglioWood, OrdineAcquistoWood, RigaOrdineAcquistoWood,
                    CostoStandardVersioneFaseWood, ManodoperaRealeWood,
                    SogliaAllarmeVarianzaWood, ContoContabileMappaWood, MovimentoContabileWood,
                    VOCI_CONTABILI_WOOD, assicura_conti_contabili_wood, SchedaLavorazioneWood,
                    NumeroListaLavoroWood, AvvisoScostamentoWood, ArticoloML, DescrizioneCodiceWood,
                    FotoArticolo, KanbanProdotto, storico_aggiungi_auto, ModuloNonConformita8D)
from blueprints.magazzino.routes import (_esplodi_bom_wood, _flatten_componenti,
                    _registra_movimento_giacenza, _giacenza_residua_dopo_impegni,
                    _netta_e_esplodi_wood, _calcola_costo_standard, _crea_versione_costo_standard,
                    _overhead_pct, _esplodi_componenti_op, _righe_bom_attive_wood,
                    _residuo_giacenza_progressivo, _carica_mappa_distinta_base_wood, STATI_CHE_IMPEGNANO)
from blueprints.produzione_pp.varianze_calc import (varianza_quantita_materiale, varianza_prezzo_materiale,
                    varianza_efficienza_tempo, varianza_tariffa)

pp_bp = Blueprint("produzione_pp", __name__)

def _date(value):
    if not value: return None
    return datetime.strptime(str(value), "%Y-%m-%d").date()

def _integer(value, name, minimum=0):
    try: value = int(value)
    except (TypeError, ValueError): raise ValueError(f"{name} non valido")
    if value < minimum: raise ValueError(f"{name} non può essere negativo")
    return value

def _ordine(o):
    return {"id": o.id, "op_code": o.codice, "codice": o.codice,
      "codice_articolo": o.codice_articolo, "descrizione": o.descrizione,
      "cliente": o.cliente, "commessa": o.commessa,
      "cliente_commessa_esterna": o.cliente_commessa_esterna,
      "qta_pianificata": o.qta_pianificata, "qta_buona": o.qta_buona,
      "qta_scarto": o.qta_scarto, "tempo_consuntivo_minuti": o.tempo_consuntivo_minuti,
      "stato": o.stato, "asa": o.asa, "priorita": o.priorita,
      "data_inizio": o.data_inizio.isoformat() if o.data_inizio else None,
      "data_prevista": o.data_prevista.isoformat() if o.data_prevista else None,
      "avanzamento_pct": round(100 * o.qta_buona / o.qta_pianificata, 1) if o.qta_pianificata else 0}

def _api_auth():
    token = current_app.config.get("PP_API_TOKEN", "")
    if not token: return jsonify(ok=False, error="API PP disabilitata: configurare PP_API_TOKEN"), 503
    bearer = request.headers.get("Authorization", "")
    got = bearer[7:].strip() if bearer.lower().startswith("bearer ") else request.headers.get("X-PP-Token", "")
    if got != token: return jsonify(ok=False, error="non autorizzato"), 401

def _is_carpenteria(asa): return (asa or "").strip().casefold() == ASA_MASTERWORK.casefold()


@pp_bp.route('/ordini-produzione/<codice>/cartellino')
def pagina_cartellino_op(codice):
    """
    Cartellino di lavoro stampabile per un OP — il ciclo di lavoro completo
    (sequenza di reparti/monitor) con spazio firma, pensato per accompagnare
    fisicamente il lotto in produzione. Nessuna dipendenza da base.html
    (pagina standalone, stessa logica del cartellino Schede Trattamento).
    """
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    fasi = (CicloLavoroWood.query.filter_by(codice=o.codice_articolo)
            .order_by(CicloLavoroWood.sequenza).all())
    return render_template('produzione_pp/cartellino_stampa.html', o=o, fasi=fasi)


def _audit(o, action, detail="", event_id=""):
    db.session.add(AuditPP(op_code=o.codice, event_id=event_id, azione=action, dettaglio=detail))

@pp_bp.get('/ordini-produzione')
def pagina(): return render_template('produzione_pp/index.html', active='produzione_pp', stati=STATI_ORDINE_PP)

@pp_bp.get('/ordini-produzione/avanzamento')
def avanzamento(): return render_template('produzione_pp/avanzamento.html', active='produzione_pp')

@pp_bp.get('/api/ordini-produzione')
def lista_ui():
    return jsonify([_ordine(x) for x in OrdineProduzione.query.order_by(OrdineProduzione.creato_il.desc()).all()])

@pp_bp.post('/api/ordini-produzione')
def crea():
    d = request.get_json(silent=True) or {}
    article = str(d.get('codice_articolo', '')).strip()
    if not article: return jsonify(ok=False, error='Codice articolo obbligatorio'), 400
    try:
        qty = _integer(d.get('qta_pianificata', 0), 'Quantità pianificata')
        priority = _integer(d.get('priorita', 5), 'Priorità', 1)
        if priority > 9: raise ValueError('Priorità deve essere fra 1 e 9')
        client = str(d.get('cliente','')).strip()
        job = prossimo_numero_commessa()  # generato in automatico, ignora eventuale valore inviato dal client
        legacy = str(d.get('cliente_commessa_esterna','')).strip() or ' / '.join(x for x in (client, job) if x)
        o = OrdineProduzione(codice=prossimo_codice_ordine_pp(), codice_articolo=article,
            descrizione=str(d.get('descrizione','')).strip(), cliente=client, commessa=job,
            cliente_commessa_esterna=legacy, qta_pianificata=qty, asa=str(d.get('asa','')).strip(),
            priorita=priority, data_inizio=_date(d.get('data_inizio')), data_prevista=_date(d.get('data_prevista')))
        db.session.add(o); _audit(o, 'CREATO', 'Creato da pagina Ordini Produzione'); db.session.commit()

        # Controllo scorta: giacenza/impegnato/disponibile/mancante per questo OP
        # appena creato, rispetto agli OP già aperti (l'OP appena creato è in
        # stato "Creato", quindi NON impegna ancora nulla — è solo un'anteprima).
        controllo_scorta = _calcola_controllo_scorta(article, qty)

        return jsonify(ok=True, ordine=_ordine(o), controllo_scorta=controllo_scorta), 201
    except (ValueError, IntegrityError) as exc:
        db.session.rollback(); return jsonify(ok=False, error=str(exc)), 400

def _calcola_controllo_scorta(codice_articolo, qta, escludi_op_id=None):
    """
    Giacenza/impegnato/disponibile/mancante per un codice+quantità, rispetto
    agli altri OP già aperti — usata sia alla creazione (escludi_op_id=None,
    l'OP non esiste ancora) sia alla modifica (escludi_op_id=l'OP stesso, per
    non farlo contare come "già impegnato" verso se stesso).
    Non solleva mai eccezioni: ritorna None se il calcolo fallisce, per non
    bloccare mai la creazione/modifica dell'OP.
    """
    try:
        giacenza_iniziale = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
        giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=escludi_op_id)
        disponibile_prima = dict(giacenza_residua)
        righe_netting = {}
        _netta_e_esplodi_wood(codice_articolo, qta, giacenza_residua, righe_netting)
        controllo_scorta = []
        for cod, r in righe_netting.items():
            giacenza_tot = giacenza_iniziale.get(cod, 0.0)
            disponibile = disponibile_prima.get(cod, 0.0)
            controllo_scorta.append({
                'codice': cod, 'giacenza': round(giacenza_tot, 3),
                'gia_impegnato': round(max(giacenza_tot - disponibile, 0), 3),
                'disponibile': round(max(disponibile, 0), 3),
                'fabbisogno_nuovo_op': round(r['fabbisogno'], 3),
                'mancante': round(r['mancante'], 3),
            })
        controllo_scorta.sort(key=lambda x: (-x['mancante'], x['codice']))
        return controllo_scorta
    except Exception:
        return None  # non deve mai bloccare la creazione/modifica dell'OP


@pp_bp.put('/api/ordini-produzione/<int:oid>')
def modifica(oid):
    o = OrdineProduzione.query.get_or_404(oid); d = request.get_json(silent=True) or {}
    if o.stato not in ('Creato', 'Rilasciato'): return jsonify(ok=False, error='OP non modificabile nello stato corrente'), 409
    try:
        for k in ('codice_articolo','descrizione','cliente','asa'):
            if k in d: setattr(o, k, str(d[k]).strip())
        if 'qta_pianificata' in d: o.qta_pianificata = _integer(d['qta_pianificata'], 'Quantità pianificata')
        if 'priorita' in d: o.priorita = _integer(d['priorita'], 'Priorità', 1)
        for k in ('data_inizio','data_prevista'):
            if k in d: setattr(o, k, _date(d[k]))
        o.cliente_commessa_esterna = ' / '.join(x for x in (o.cliente, o.commessa) if x)
        _audit(o, 'MODIFICATO', 'Aggiornamento OP'); db.session.commit()
        saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
        controllo_scorta = _calcola_controllo_scorta(o.codice_articolo, saldo, escludi_op_id=o.id) if saldo > 0 else []
        return jsonify(ok=True, ordine=_ordine(o), controllo_scorta=controllo_scorta)
    except ValueError as exc: db.session.rollback(); return jsonify(ok=False, error=str(exc)), 400

@pp_bp.delete('/api/ordini-produzione/<int:oid>')
def elimina(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato not in ('Creato', 'Rilasciato'):
        return jsonify(ok=False, error='OP non eliminabile in questo stato (produzione già avviata/completata)'), 409
    if o.stato == 'Rilasciato' and (o.qta_buona or 0) > 0:
        return jsonify(ok=False, error='Impossibile eliminare: risultano già pezzi buoni consuntivati su questo OP'), 409
    codice = o.codice
    _audit(o, 'ELIMINATO', 'Eliminazione manuale da pagina Ordini Produzione')
    legame = LegameCostoStandardOrdineWood.query.get(codice)
    if legame:
        db.session.delete(legame)
    db.session.delete(o); db.session.commit()
    return jsonify(ok=True, codice=codice)

@pp_bp.post('/api/ordini-produzione/<int:oid>/rilascia')
def rilascia(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Creato': return jsonify(ok=False, error='Solo un OP Creato può essere rilasciato'), 409
    o.stato, o.data_rilascio = 'Rilasciato', datetime.utcnow(); _audit(o, 'RILASCIATO', 'Rilascio manuale')

    # Aggancia l'OP alla versione di Costo Standard più recente disponibile per
    # il suo codice articolo — congelata da questo momento in poi: eventuali
    # ricalcoli successivi del costo standard NON la modificano più, per poter
    # calcolare le varianze rispetto a uno standard stabile. Se non esiste
    # ancora nessuna versione salvata, la calcola ora al volo (così ogni OP
    # rilasciato ha sempre un riferimento, anche se nessuno ha mai premuto
    # "Ricalcola e salva" a mano su quel prodotto).
    try:
        ultima = (CostoStandardVersioneWood.query.filter_by(codice=o.codice_articolo)
                  .order_by(CostoStandardVersioneWood.versione.desc()).first())
        if not ultima:
            _, ultima = _crea_versione_costo_standard(o.codice_articolo)
        legame = LegameCostoStandardOrdineWood.query.get(o.codice)
        if not legame:
            legame = LegameCostoStandardOrdineWood(op_code=o.codice)
            db.session.add(legame)
        legame.costo_standard_versione_id = ultima.id
        legame.agganciato_il = datetime.utcnow()
    except Exception:
        pass  # l'aggancio al costo standard non deve mai bloccare il rilascio dell'OP

    db.session.commit(); return jsonify(ok=True, ordine=_ordine(o))


@pp_bp.get('/ordini-produzione/situazione')
def pagina_ordini_situazione():
    return render_template('produzione_pp/situazione_cards.html', active='situazione_op')


@pp_bp.route('/api/ordini-produzione/riepilogo_disponibilita')
def api_ordini_riepilogo_disponibilita():
    """
    Sintesi disponibilità materiali per OGNI Ordine di Produzione ancora
    aperto (tutti tranne 'Chiuso CO') — alimenta le card della pagina
    Ordini di Produzione: dice se è tutto disponibile o quanti componenti
    mancano, senza il dettaglio completo (quello è nel popup, vedi
    /api/ordini-produzione/<codice>/situazione_completa).
    Ogni ordine è valutato ESCLUDENDO se stesso dagli "impegni già presi",
    coerente con /api/fabbisogno_disponibilita.
    """
    ordini = (OrdineProduzione.query.filter(OrdineProduzione.stato != 'Chiuso CO')
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())
    oggi = datetime.utcnow().date()
    # Un solo giro per sapere quali codici_articolo hanno ALMENO una fase di
    # Ciclo di Lavoro impostata — alimenta il flag diagnostico ha_ciclo_lavoro.
    codici_articolo_op = {o.codice_articolo for o in ordini}
    codici_con_ciclo = {c for c, in db.session.query(CicloLavoroWood.codice)
                         .filter(CicloLavoroWood.codice.in_(codici_articolo_op)).distinct().all()} if codici_articolo_op else set()
    risultato = []
    mancanti_per_ordine = []
    tutti_codici_mancanti = set()
    mappa_distinta = _carica_mappa_distinta_base_wood()
    residuo_per_op, residuo_finale = _residuo_giacenza_progressivo(mappa=mappa_distinta)
    for o in ordini:
        saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
        righe = {}
        if saldo > 0:
            giacenza_residua = dict(residuo_per_op.get(o.id, residuo_finale))
            _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, righe, mappa=mappa_distinta)
        mancanti = [codice for codice, r in righe.items() if r['mancante'] > 0]
        mancanti_per_ordine.append(mancanti)
        tutti_codici_mancanti.update(mancanti)
        risultato.append({
            'id': o.id, 'codice': o.codice, 'codice_articolo': o.codice_articolo,
            'descrizione': o.descrizione, 'cliente': o.cliente, 'commessa': o.commessa,
            'stato': o.stato, 'priorita': o.priorita,
            'qta_pianificata': o.qta_pianificata, 'qta_buona': o.qta_buona, 'qta_scarto': o.qta_scarto,
            'asa': o.asa,
            'data_inizio': o.data_inizio.isoformat() if o.data_inizio else None,
            'data_prevista': o.data_prevista.isoformat() if o.data_prevista else None,
            'n_componenti_mancanti': len(mancanti),
            'n_componenti_totali': len(righe),
            'scaduto': bool(o.data_prevista and o.data_prevista < oggi and o.stato != 'Tecnicamente completato'),
            'ha_ciclo_lavoro': o.codice_articolo in codici_con_ciclo,
        })

    # Solo i codici REALMENTE acquistabili contano per lo stato materiale di
    # un OP — un semilavorato (o il prodotto finito stesso) non si "ordina"
    # a un fornitore, quindi non deve mai far scattare DA_ORDINARE anche se
    # tecnicamente "manca" (perché non ancora prodotto).
    tutti_i_codici_coinvolti = tutti_codici_mancanti | {o.codice_articolo for o in ordini}
    tipo_per_codice = {a.codice: a.tipo_approvvigionamento for a in
                        ArticoloApprovvigionamento.query.filter(ArticoloApprovvigionamento.codice.in_(tutti_i_codici_coinvolti)).all()
                        } if tutti_i_codici_coinvolti else {}
    TIPI_ACQUISTABILI = ('LASERATO', 'COMPONENTE_ACQUISTO', 'MATERIA_PRIMA_FORNITORE')

    # Stato di acquisto per ogni codice mancante — scala a 4 livelli (dal
    # peggiore al migliore): DA_ORDINARE (nessun ordine) → ORDINATO (ordine
    # aperto non confermato) → ORDINI_CONFERMATI (confermato dal fornitore,
    # ma non ancora imminente) → IN_ARRIVO (confermato E la consegna prevista
    # è entro 48 ore da adesso). Con più ordini aperti sullo stesso codice, si
    # prende il migliore raggiunto (semplificazione: non verifica che la
    # quantità confermata basti da sola a coprire tutto il mancante).
    stato_per_codice = {}
    if tutti_codici_mancanti:
        adesso = datetime.utcnow()
        righe_oa = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                    .filter(RigaOrdineAcquistoWood.codice.in_(tutti_codici_mancanti),
                            OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
        SEVERITA = {'DA_ORDINARE': 4, 'ORDINATO': 3, 'ORDINI_CONFERMATI': 2, 'IN_ARRIVO': 1}
        for r_oa in righe_oa:
            if (r_oa.qta_originale or 0) <= (r_oa.qta_ricevuta or 0):
                continue
            ordine_oa = r_oa.ordine
            if not ordine_oa.confermato:
                stato_riga = 'ORDINATO'
            elif ordine_oa.data_consegna and (ordine_oa.data_consegna - adesso.date()).days <= 2:
                stato_riga = 'IN_ARRIVO'
            else:
                stato_riga = 'ORDINI_CONFERMATI'
            attuale = stato_per_codice.get(r_oa.codice)
            if attuale is None or SEVERITA[stato_riga] < SEVERITA[attuale]:
                stato_per_codice[r_oa.codice] = stato_riga

    SEVERITA = {'DA_ORDINARE': 4, 'ORDINATO': 3, 'ORDINI_CONFERMATI': 2, 'IN_ARRIVO': 1}
    for riga, mancanti in zip(risultato, mancanti_per_ordine):
        mancanti_acquistabili = [c for c in mancanti if tipo_per_codice.get(c) in TIPI_ACQUISTABILI]
        if not mancanti_acquistabili:
            riga['materiale_stato'] = 'PRODUCIBILE'
        else:
            peggiore = max((SEVERITA[stato_per_codice.get(c, 'DA_ORDINARE')] for c in mancanti_acquistabili), default=4)
            riga['materiale_stato'] = {v: k for k, v in SEVERITA.items()}[peggiore]
        riga['tutto_disponibile'] = (riga['materiale_stato'] == 'PRODUCIBILE')
        # "In esecuzione" è un flag INDIPENDENTE dal materiale: appena parte
        # anche un solo Ordine di Lavoro agli operai, resta vero a prescindere
        # da quanto ancora manca da ordinare — i due stati convivono in card.
        riga['in_esecuzione'] = riga['stato'] in ('In esecuzione', 'Tecnicamente completato')

    # Riepilogo Ordini di Lavoro emessi (per centro di costo) — avanzamento
    # pezzi e link diretto alla stampa, per la card principale. Non assegna
    # mai un numero di lista solo perché la card viene visualizzata: il
    # numero nasce solo quando l'Ordine di Lavoro viene davvero aperto/stampato.
    mappa_op_by_id = {o.id: o for o in ordini}
    for riga in risultato:
        o = mappa_op_by_id[riga['id']]
        componenti_op = _esplodi_componenti_op(o, mappa_distinta=mappa_distinta)
        codici_componenti = {c['codice'] for c in componenti_op}
        centri_coinvolti = (CentroCostoWood.query
                            .join(CicloLavoroWood, CicloLavoroWood.centro_costo_id == CentroCostoWood.id)
                            .filter(CicloLavoroWood.codice.in_(codici_componenti)).distinct().all()
                            ) if codici_componenti else []
        ordini_lavoro = []
        for centro in centri_coinvolti:
            dati_lista = _lista_lavoro_op(o, centro, assegna_numero=False)
            ordini_lavoro.append({
                'centro_id': centro.id, 'centro_nome': centro.nome,
                'totale_pz': dati_lista['totale_pz'], 'pz_effettuati': dati_lista['pz_effettuati'],
                'residuo_pz': dati_lista['residuo_pz'],
            })
        riga['ordini_lavoro'] = ordini_lavoro

    return jsonify(risultato)


@pp_bp.route('/api/ordini-produzione/<codice>/situazione_completa')
def api_ordine_situazione_completa(codice):
    """
    Dettaglio per il popup di una card: distinta base ESPLOSA multilivello
    (stessa gerarchia mostrata in Magazzino/Costo Standard) annotata con
    disponibilità (giacenza/impegnato/mancante) per ogni nodo, e — per ogni
    componente mancante — gli Ordini di Acquisto aperti che lo contengono
    (fornitore, numero ordine, data di consegna, quanto ne arriva).
    """
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404

    saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
    righe_disponibilita = {}
    if saldo > 0:
        giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=o.id)
        _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, righe_disponibilita)

    albero = _esplodi_bom_wood(o.codice_articolo, qta=saldo if saldo > 0 else 1.0)

    GRAVITA = {'DA_ORDINARE': 3, 'ORDINATO': 2, 'CONFERMATO': 1}   # più alto = più bloccante
    STATO_DA_GRAVITA = {3: 'DA_ORDINARE', 2: 'ORDINATO', 1: 'CONFERMATO'}

    def _stato_da_ordini_aperti(righe_oa_coprono):
        if not righe_oa_coprono:
            return 'DA_ORDINARE'
        if any(r_oa.ordine.confermato for r_oa in righe_oa_coprono):
            return 'CONFERMATO'
        return 'ORDINATO'

    def _annota_disponibilita(nodi):
        """Ritorna la gravità peggiore (0 = tutto ok) tra questi nodi e i loro discendenti, per farla risalire al nodo padre."""
        peggiore = 0
        for n in nodi:
            r = righe_disponibilita.get(n['codice'])
            if r:
                n['disponibile'] = round(max(r['fabbisogno'] - r['mancante'], 0), 3)
                n['mancante'] = round(r['mancante'], 3)
                n['fabbisogno'] = round(r['fabbisogno'], 3)
            else:
                n['disponibile'] = n['quantita_totale']; n['mancante'] = 0; n['fabbisogno'] = n['quantita_totale']

            approvv = ArticoloApprovvigionamento.query.filter_by(codice=n['codice']).first()
            tipo = approvv.tipo_approvvigionamento if approvv else 'DA_CLASSIFICARE'
            acquistabile = tipo in ('LASERATO', 'COMPONENTE_ACQUISTO', 'MATERIA_PRIMA_FORNITORE')

            n['ordini_acquisto'] = []
            righe_oa_coprono = []
            if n['mancante'] > 0:
                righe_oa = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                            .filter(RigaOrdineAcquistoWood.codice == n['codice'],
                                    OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
                righe_oa_coprono = [r_oa for r_oa in righe_oa if (r_oa.qta_originale or 0) > (r_oa.qta_ricevuta or 0)]
                n['ordini_acquisto'] = [{
                    'ordine_n': r_oa.ordine.ordine_n, 'fornitore': r_oa.ordine.fornitore,
                    'stato_label': r_oa.ordine.stato_label, 'confermato': r_oa.ordine.confermato,
                    'data_consegna': r_oa.ordine.data_consegna.isoformat() if r_oa.ordine.data_consegna else None,
                    'qta_in_arrivo': round((r_oa.qta_originale or 0) - (r_oa.qta_ricevuta or 0), 3),
                } for r_oa in righe_oa_coprono]

            gravita_figli = _annota_disponibilita(n.get('figli', []))

            if acquistabile:
                # Materia prima / laserato / componente d'acquisto: la SUA
                # gravità è la propria (mai ereditata dai figli — non ne ha).
                n['stato'] = _stato_da_ordini_aperti(righe_oa_coprono) if n['mancante'] > 0 else 'DISPONIBILE'
                gravita_nodo = GRAVITA.get(n['stato'], 0)
            else:
                # Semilavorato: MAI "da ordinare" per se stesso — non si
                # acquista, si produce. Il suo stato dipende SOLO da cosa
                # manca ancora, in profondità, tra i materiali acquistabili
                # che lo compongono: se sono tutti pronti è "PRODUCIBILE"
                # (si può lavorare ADESSO), altrimenti eredita il blocco più
                # grave tra i suoi discendenti acquistabili.
                if n['mancante'] <= 0:
                    n['stato'] = 'DISPONIBILE'
                    gravita_nodo = 0
                elif gravita_figli == 0:
                    n['stato'] = 'PRODUCIBILE'
                    gravita_nodo = 0
                else:
                    n['stato'] = STATO_DA_GRAVITA[gravita_figli]
                    gravita_nodo = gravita_figli

            peggiore = max(peggiore, gravita_nodo)
        return peggiore
    gravita_massima = _annota_disponibilita(albero)

    return jsonify(ok=True, codice=o.codice, codice_articolo=o.codice_articolo, descrizione=o.descrizione,
                   cliente=o.cliente, commessa=o.commessa, stato=o.stato, priorita=o.priorita,
                   qta_pianificata=o.qta_pianificata, qta_buona=o.qta_buona, qta_scarto=o.qta_scarto,
                   tutto_disponibile=(gravita_massima == 0), componenti=albero)


@pp_bp.route('/api/ordini-produzione/<codice>/dettaglio_per_categoria')
def api_ordine_dettaglio_per_categoria(codice):
    """
    Spacca TUTTI i componenti di un OP (mancanti o già coperti a scorta) nelle
    4 categorie operative, col quadro completo di ognuno — non solo quelli
    mancanti: anche un componente pienamente disponibile va mostrato, con
    quanto serve/quanto c'è/quanto è impegnato da altri OP, non solo "manca 0".
    1) Ordini ai Reparti — semilavorati interni (non classificati come
       acquisto/materia prima/laserato), con il/i reparto/i del loro Ciclo
       di Lavoro, per le liste taglio/piega/ecc.
    2) Laserati
    3) Componenti di Acquisto — "componenti esterni" (rosette, minuteria...)
    4) Materie Prime — tubi, barre di ferro, ecc.
    Quantità già aggregate per codice (stesso codice in più punti della
    distinta conta una volta sola, sommato) — stessa base dati di
    situazione_completa, così i due popup restano sempre coerenti tra loro.
    """
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404

    saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
    righe_disponibilita = {}
    if saldo > 0:
        giacenza_iniziale = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
        giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=o.id)
        disponibile_prima = dict(giacenza_residua)
        _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, righe_disponibilita)
    else:
        giacenza_iniziale, disponibile_prima = {}, {}

    reparti, laserati, acquisto, materie_prime = [], [], [], []
    for cod, r in righe_disponibilita.items():
        approvv = ArticoloApprovvigionamento.query.filter_by(codice=cod).first()
        tipo = approvv.tipo_approvvigionamento if approvv else 'DA_CLASSIFICARE'

        # Unità di misura: nessun campo dedicato nella distinta base — recupero
        # quella dell'ultimo Ordine di Acquisto registrato per questo codice
        # (se non è mai stato ordinato, resta sconosciuta: mai inventata).
        ultima_riga_oa_um = (RigaOrdineAcquistoWood.query.filter_by(codice=cod)
                             .join(OrdineAcquistoWood)
                             .order_by(OrdineAcquistoWood.caricato_il.desc()).first())
        unita_misura = (ultima_riga_oa_um.unita_misura or '') if ultima_riga_oa_um else ''

        righe_oa = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                    .filter(RigaOrdineAcquistoWood.codice == cod,
                            OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
        ordini_acquisto = [{
            'ordine_n': r_oa.ordine.ordine_n, 'fornitore': r_oa.ordine.fornitore,
            'stato_label': r_oa.ordine.stato_label,
            'data_consegna': r_oa.ordine.data_consegna.isoformat() if r_oa.ordine.data_consegna else None,
            'qta_in_arrivo': round((r_oa.qta_originale or 0) - (r_oa.qta_ricevuta or 0), 3),
        } for r_oa in righe_oa if (r_oa.qta_originale or 0) > (r_oa.qta_ricevuta or 0)]

        giacenza_tot = giacenza_iniziale.get(cod, 0.0)
        disponibile = disponibile_prima.get(cod, 0.0)
        base = {
            'codice': cod, 'unita_misura': unita_misura, 'ordini_acquisto': ordini_acquisto,
            'giacenza': round(giacenza_tot, 3),
            'gia_impegnato': round(max(giacenza_tot - disponibile, 0), 3),
            'disponibile': round(max(disponibile, 0), 3),
            'fabbisogno': round(r['fabbisogno'], 3),
            'mancante': round(r['mancante'], 3),
        }

        if tipo == 'LASERATO':
            laserati.append(base)
        elif tipo == 'COMPONENTE_ACQUISTO':
            acquisto.append(base)
        elif tipo == 'MATERIA_PRIMA_FORNITORE':
            materie_prime.append(base)
        else:
            fasi = (CicloLavoroWood.query.filter_by(codice=cod)
                    .order_by(CicloLavoroWood.sequenza).all())
            base['reparti'] = [{'sequenza': f.sequenza, 'nome': f.centro_costo.nome}
                               for f in fasi if f.centro_costo]
            base['ciclo_configurato'] = len(base['reparti']) > 0
            # Questi codici sono semilavorati che PRODUCIAMO internamente,
            # mai comprati da un fornitore: 'ordini_acquisto' qui non ha
            # senso (resta sempre vuoto) e faceva apparire "Non ancora
            # ordinato" anche quando l'Ordine di Lavoro interno era già
            # stato emesso/avviato al centro di costo. Lo stato va invece
            # letto dagli Ordini di Lavoro interni (stessa fonte dati della
            # dashboard "ORDINI DI LAVORO"): un Numero di Lista assegnato
            # per OP+centro, o pezzi già dichiarati su questo componente,
            # significano "avviato in produzione interna".
            base['ordini_acquisto'] = []
            centri_ids = [f.centro_costo_id for f in fasi]
            base['lista_lavoro_emessa'] = bool(
                centri_ids and NumeroListaLavoroWood.query.filter(
                    NumeroListaLavoroWood.op_code == o.codice,
                    NumeroListaLavoroWood.centro_costo_id.in_(centri_ids)).first())
            componente_param = None if cod == o.codice_articolo else cod
            base['pezzi_fatti'] = int((db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                                       .filter(EventoConsuntivoPP.op_code == o.codice,
                                               EventoConsuntivoPP.componente == componente_param if componente_param
                                               else EventoConsuntivoPP.componente.is_(None))
                                       .scalar()) or 0)
            base['avviato_produzione_interna'] = base['lista_lavoro_emessa'] or base['pezzi_fatti'] > 0
            reparti.append(base)

    for lista in (reparti, laserati, acquisto, materie_prime):
        lista.sort(key=lambda x: x['codice'])

    return jsonify(ok=True, codice=o.codice, ordini_reparti=reparti, laserati=laserati,
                   componenti_acquisto=acquisto, materie_prime=materie_prime)


@pp_bp.post('/api/ordini-produzione/<int:oid>/chiudi-co')
def chiudi_co(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Tecnicamente completato': return jsonify(ok=False, error='Chiusura CO possibile solo dopo completamento tecnico'), 409
    o.stato, o.data_chiusura_co = 'Chiuso CO', datetime.utcnow(); _audit(o, 'CHIUSO_CO', 'Chiusura CO manuale')
    db.session.commit(); return jsonify(ok=True, ordine=_ordine(o))

@pp_bp.post('/api/ordini-produzione/<int:oid>/chiudi-forzato')
def chiudi_forzato(oid):
    """
    Chiude un OP che non arriverà mai a completamento tecnico (qta_buona
    resterà sempre sotto qta_pianificata — es. 1496 di 1500, gli ultimi 4
    non verranno mai fatti) — quello che 'Chiudi CO' normale non permette,
    dato che richiede esplicitamente lo stato 'Tecnicamente completato'.
    Va bene sia per OP senza nessun movimento (l'alternativa più pulita
    resta comunque l'Eliminazione, che qui non tocchiamo) sia — soprattutto
    — per OP con dichiarazioni/movimenti già registrati, che quindi non
    sono più eliminabili: qui la storia resta intatta, cambia solo lo stato.

    Cosa succede al residuo (i 4 pezzi mai fatti) una volta chiuso:
    - L'OP esce da STATI_CHE_IMPEGNANO (Rilasciato/In esecuzione/Tecnicamente
      completato) appena passa a 'Chiuso CO': il fabbisogno lordo che
      teneva impegnata la materia prima per quei 4 pezzi (vedi Impegnato
      OP in Magazzino) si libera in automatico al prossimo ricalcolo —
      nessuna azione separata necessaria.
    - NON genera da sola nessuna Varianza Materiali: quella (Analisi Costo
      → Dettaglio varianza materiali) è già calcolata automaticamente dai
      movimenti di scarico REALMENTE avvenuti confrontati con lo standard
      per qta_buona — se la materia prima per quei 4 pezzi non è mai stata
      scaricata (solo impegnata, mai consumata), non c'è nessuna varianza
      da registrare: il materiale resta fisicamente a magazzino, libero.
      Se invece era già stata scaricata (es. tagliata e poi scartata), la
      varianza risulta GIÀ visibile in Analisi Costo prima ancora di
      chiudere qui — chiudere l'OP non cambia quel calcolo.
    """
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato not in ('Rilasciato', 'In esecuzione'):
        return jsonify(ok=False, error='Chiusura forzata possibile solo da Rilasciato o In esecuzione'), 409
    d = request.get_json(silent=True) or {}
    residuo = max((o.qta_pianificata or 0) - (o.qta_buona or 0), 0)
    nota = (d.get('nota') or '').strip()
    o.stato, o.data_chiusura_co = 'Chiuso CO', datetime.utcnow()
    _audit(o, 'CHIUSO_CO_FORZATO',
           f'Chiusura forzata: {o.qta_buona}/{o.qta_pianificata} realizzati, {residuo} pezzi mai completati.'
           + (f' Nota: {nota}' if nota else ''))
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine(o), residuo_non_completato=residuo)

@pp_bp.post('/api/ordini-produzione/<int:oid>/riapri')
def riapri(oid):
    """
    Riapre un OP 'Chiuso CO' (sia da chiusura forzata che da chiusura
    normale dopo completamento tecnico) — reversibile in entrambi i casi,
    non solo per la forzata: una chiusura fatta per errore va sempre
    corretta allo stesso modo. Lo stato in cui torna non è fisso: viene
    ricalcolato da quanto è stato REALMENTE prodotto finora (qta_buona vs
    qta_pianificata), esattamente come se non fosse mai stato chiuso —
    'Tecnicamente completato' se la quantità pianificata è già raggiunta,
    altrimenti 'In esecuzione' (o 'Rilasciato' se non è ancora partita
    nessuna dichiarazione). Rientrando in uno stato che impegna materiale,
    l'Impegnato OP in Magazzino torna a contarlo al prossimo ricalcolo —
    stesso motivo per cui la chiusura lo liberava.
    """
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Chiuso CO':
        return jsonify(ok=False, error="Riapertura possibile solo da uno stato 'Chiuso CO'"), 409
    if (o.qta_pianificata or 0) > 0 and (o.qta_buona or 0) >= o.qta_pianificata:
        nuovo_stato = 'Tecnicamente completato'
    elif (o.qta_buona or 0) > 0:
        nuovo_stato = 'In esecuzione'
    else:
        nuovo_stato = 'Rilasciato'
    o.stato, o.data_chiusura_co = nuovo_stato, None
    _audit(o, 'RIAPERTO', f"OP riaperto da 'Chiuso CO' — stato ricalcolato a '{nuovo_stato}'")
    db.session.commit()
    return jsonify(ok=True, ordine=_ordine(o))

@pp_bp.get('/api/pp/orders')
def api_ordini_attivi():
    auth = _api_auth()
    if auth: return auth
    rows = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(['Rilasciato','In esecuzione'])).all()
    rows = [o for o in rows if _is_carpenteria(o.asa)]
    return jsonify(orders=[_ordine(o) for o in sorted(rows, key=lambda o: (o.data_prevista is None, o.data_prevista, o.priorita, o.codice))])

def _e_prima_fase_del_ciclo(codice_lavorato, fase_nome):
    """
    Vero se fase_nome è la PRIMA fase del Ciclo di Lavoro di questo codice —
    quella che lo crea per la prima volta a partire dai suoi materiali
    diretti. Le fasi SUCCESSIVE (es. un pezzo prima piegato e poi forato)
    lavorano ulteriormente lo STESSO semilavorato già esistente: non devono
    scaricare di nuovo i materiali né ricaricare il semilavorato a
    magazzino, altrimenti lo stesso pezzo fisico verrebbe contato una volta
    per ogni fase dichiarata (es. 3 fasi = caricato/scaricato 3 volte).
    Se il codice non ha nessun Ciclo di Lavoro configurato, non possiamo
    saperlo: non blocchiamo, si comporta come prima (comportamento storico).
    """
    prima = (CicloLavoroWood.query.filter_by(codice=codice_lavorato)
             .order_by(CicloLavoroWood.sequenza).first())
    if not prima or not prima.centro_costo:
        return True
    return prima.centro_costo.nome.strip().lower() == (fase_nome or '').strip().lower()


def _e_ultima_fase_del_ciclo(codice_lavorato, fase_nome):
    """
    Vero se fase_nome è l'ULTIMA fase del Ciclo di Lavoro di questo codice —
    quella che lo rende davvero 'finito'. BUG REALE VERIFICATO IN
    PRODUZIONE che questa funzione esiste per correggere: per un articolo
    che passa per PIÙ fasi mantenendo sempre lo STESSO codice (es.
    Sega→Piega→Saldatura tutte su T200, mai un codice diverso in mezzo),
    il solo confronto codice_lavorato == o.codice_articolo risulta vero a
    OGNI fase, non solo all'ultima — quindi dichiarare la prima fase (Sega)
    faceva avanzare comunque qta_buona/stato dell'INTERO Ordine di
    Produzione, anche con Piega/Saldatura ancora da fare. Vedi uso in
    _registra_evento_consuntivo (avanza_op).
    Se il codice non ha nessun Ciclo di Lavoro configurato, non possiamo
    saperlo: non blocchiamo, si comporta come prima (stessa filosofia di
    _e_prima_fase_del_ciclo — un codice a fase singola è sia prima che
    ultima fase di se stesso).
    """
    ultima = (CicloLavoroWood.query.filter_by(codice=codice_lavorato)
              .order_by(CicloLavoroWood.sequenza.desc()).first())
    if not ultima or not ultima.centro_costo:
        return True
    return ultima.centro_costo.nome.strip().lower() == (fase_nome or '').strip().lower()


def _calcola_consumi_standard(o, componente_finale, componente, qta_tagliata):
    """
    Consumi standard (da distinta base) per QTA_TAGLIATA pezzi — stessa
    logica usata sia per la registrazione vera sia per l'anteprima pre-
    conferma. IMPORTANTE: qta_tagliata deve essere BUONI+SCARTO, non solo
    i pezzi buoni — il materiale grezzo (es. il ferro alla segatrice) si
    consuma per OGNI pezzo tagliato, compreso quello poi scartato: uno
    scarto non è "materiale mai toccato", è materiale già consumato che
    non è diventato un pezzo conforme. Solo il CARICO a magazzino del
    pezzo prodotto (più sotto in _registra_evento_consuntivo) resta sui
    soli pezzi buoni — quello sì che deve rappresentare solo scorta
    davvero utilizzabile.

    Riesplode la distinta SOLO fino ai figli che NON hanno un proprio Ciclo
    di Lavoro (materie prime/componenti d'acquisto puri, mai dichiarabili da
    soli): un figlio CON un proprio ciclo può essere stato dichiarato
    separatamente come semilavorato (scaricando già le SUE materie prime in
    quel momento) — consumarlo di nuovo qui vorrebbe dire scaricare le
    stesse materie prime due volte, e in più scaricare a sua volta il
    semilavorato appena caricato. Si consuma quindi LUI (il semilavorato,
    dal magazzino), non si riesplode sotto di lui.

    Ritorna (consumi, contestuali): 'consumi' sono gli scarichi normali
    (presumono un carico avvenuto altrove/prima, es. Taglio/Trapano);
    'contestuali' sono i figli marcati come "isola one-piece-flow" in
    distinta base (DistintaBaseWood.contestuale=True, es. FRONTE+RETRO di
    un cavalletto saldati insieme) — nascono e si consumano nello stesso
    istante del padre, quindi vanno caricati E scaricati in automatico qui
    (vedi _registra_evento_consuntivo), mai trattati come stock preesistente.
    """
    codice_base = o.codice_articolo if componente_finale else componente
    consumi = {}
    contestuali = {}
    _esplodi_fino_a_semilavorati_dichiarabili(codice_base, qta_tagliata, consumi, contestuali)
    return consumi, contestuali


def _esplodi_fino_a_semilavorati_dichiarabili(codice, qta, consumi, contestuali, _visitati=None):
    _visitati = _visitati if _visitati is not None else set()
    if codice in _visitati:
        return  # mai un ciclo infinito su una distinta configurata male per errore
    _visitati.add(codice)
    for rb in _righe_bom_attive_wood(codice):
        qta_figlio = rb.quantita * qta
        if rb.contestuale:
            # Isola one-piece-flow: questo figlio non è mai stato caricato a
            # magazzino da solo (nasce e si assembla nello stesso istante del
            # padre) — va accumulato a parte per il carico+scarico automatico,
            # MAI messo tra i 'consumi' normali (che presumono stock già
            # esistente). Si continua comunque a esplodere sotto di lui: anche
            # le SUE materie prime/componenti vanno consumate, a meno che pure
            # loro siano contestuali o abbiano un proprio ciclo dichiarato a parte.
            contestuali[rb.codice_figlio] = contestuali.get(rb.codice_figlio, 0) + qta_figlio
            _esplodi_fino_a_semilavorati_dichiarabili(rb.codice_figlio, qta_figlio, consumi, contestuali, _visitati)
            continue
        ha_proprio_ciclo = CicloLavoroWood.query.filter_by(codice=rb.codice_figlio).first() is not None
        figli_del_figlio = _righe_bom_attive_wood(rb.codice_figlio)
        if ha_proprio_ciclo or not figli_del_figlio:
            # Ci si ferma qui: o è un semilavorato con un proprio ciclo (può
            # essere stato dichiarato a parte, si consuma lui), oppure è una
            # foglia vera — materia prima/componente d'acquisto senza altra
            # distinta sotto — e va comunque consumata a questo livello.
            consumi[rb.codice_figlio] = consumi.get(rb.codice_figlio, 0) + qta_figlio
        else:
            _esplodi_fino_a_semilavorati_dichiarabili(rb.codice_figlio, qta_figlio, consumi, contestuali, _visitati)


def _registra_evento_consuntivo(o, fase_nome, ts, good, scrap, tempo, event_id, componente=None, consumi_override=None, operatore=None):
    """
    Nucleo di registrazione di un consuntivo per l'OP o (già lockato con
    with_for_update dal chiamante): crea l'EventoConsuntivoPP, aggiorna
    quantità/stato dell'OP, scarica la giacenza dei componenti consumati,
    carica il prodotto finito e registra la varianza di lavorazione.
    Condivisa da /api/pp/events (integrazione MasterWork) e dal totem a bordo
    macchina (inizio/fine lavoro) — stesso identico comportamento in entrambi
    i casi. NON fa il commit: il chiamante decide quando farlo.

    'componente': None (o uguale a o.codice_articolo) = si sta consuntivando
    il prodotto finito/assieme finale dell'OP — comportamento storico
    invariato (avanza qta_buona/qta_scarto dell'OP, può chiudere l'OP).
    Se invece è il codice di un COMPONENTE della distinta base (es. uno dei
    pezzi che passano dalla segatrice prima di diventare il prodotto finito),
    NON tocca qta_buona/qta_scarto dell'OP (quelle restano riservate al
    prodotto finito): scarica solo la distinta DIRETTA di quel componente,
    lo carica a magazzino come semilavorato pronto, e registra la sua
    varianza separatamente — l'assemblaggio finale (componente=None)
    continua a esplodere l'intera distinta come sempre.
    'consumi_override': dict {codice: quantità} opzionale — se il capo
    reparto ha corretto le quantità nella maschera di anteprima prima di
    confermare, si scarica QUESTO invece di ricalcolare dalla distinta
    standard (stesso principio della maschera "Evasione articoli composti"
    di Zucchetti: valori standard proposti, modificabili prima di confermare).
    ⚠️ LIMITE NOTO: l'esplosione dell'assemblaggio finale non fa ancora
    netting contro lo stock di semilavorati creato qui — se si consuntivano
    ENTRAMBI i livelli per le stesse unità, il consumo di materia prima può
    risultare contato due volte. Da rifinire con un netting dedicato.
    """
    componente_finale = not componente or componente == o.codice_articolo
    codice_lavorato = o.codice_articolo if componente_finale else componente

    # BUG CORRETTO: componente_finale da solo dice solo "sto lavorando lo
    # stesso codice del prodotto finito dell'OP" — vero anche per la PRIMA
    # fase di un articolo a codice unico multi-fase (es. Sega, quando poi
    # ci sono ancora Piega e Saldatura). L'OP deve avanzare (qta_buona,
    # stato, "Tecnicamente completato") solo quando è ANCHE l'ultima fase
    # del ciclo di quel codice — altrimenti dichiarare Sega faceva evadere
    # l'intero OP anche con le fasi successive ancora da fare.
    avanza_op = componente_finale and _e_ultima_fase_del_ciclo(codice_lavorato, fase_nome)

    db.session.add(EventoConsuntivoPP(event_id=event_id, op_code=o.codice, fase=fase_nome,
                                       componente=None if componente_finale else componente,
                                       timestamp_evento=ts, pezzi_buoni=good, pezzi_scarto=scrap, tempo_minuti=tempo,
                                       operatore=operatore))
    o.tempo_consuntivo_minuti += tempo
    if avanza_op:
        o.qta_buona += good; o.qta_scarto += scrap
        if o.stato == 'Rilasciato': o.stato = 'In esecuzione'
        if o.qta_pianificata and o.qta_buona >= o.qta_pianificata:
            o.stato, o.data_completamento = 'Tecnicamente completato', datetime.utcnow()
        # Sopra-produzione: si vede in tempo reale appena si supera la
        # tolleranza (10%), non serve aspettare una chiusura esplicita — un
        # 3-4% in più è normale, oltre il 10% è quasi certamente un errore
        # di dichiarazione da rivedere. Un avviso per OP: non ne accumula
        # altri se il primo non è stato ancora letto dalla Direzione.
        if o.qta_pianificata:
            pct_sopra = (o.qta_buona - o.qta_pianificata) / o.qta_pianificata * 100
            if pct_sopra > 10 and not AvvisoScostamentoWood.query.filter_by(
                    op_code=o.codice, tipo='SOPRA_PRODUZIONE', letto=False).first():
                db.session.add(AvvisoScostamentoWood(
                    op_code=o.codice, codice_articolo=o.codice_articolo, tipo='SOPRA_PRODUZIONE',
                    qta_pianificata=o.qta_pianificata, qta_buona=o.qta_buona, percentuale=round(pct_sopra, 1)))
    elif o.stato == 'Rilasciato':
        o.stato = 'In esecuzione'   # una fase (finale o intermedia) ha iniziato la lavorazione: l'OP non è più solo "rilasciato"
    _audit(o, 'EVENTO_CONSUNTIVO', f'componente={codice_lavorato}; fase={fase_nome}; buoni={good}; scarto={scrap}; minuti={tempo}', event_id)

    # Scarico automatico giacenza Iron Wood in proporzione ai pezzi buoni
    # appena consuntivati — SOLO alla PRIMA fase del ciclo di questo codice
    # (vedi _e_prima_fase_del_ciclo): un pezzo che passa da più fasi (es.
    # prima piegato poi forato) esiste come semilavorato UNA volta sola, non
    # una volta per ogni fase dichiarata. Per il prodotto finito: esplode
    # l'INTERA distinta (nessun netting qui, solo la quantità reale
    # consumata) e scarica ogni componente toccato, poi carica il PRODOTTO
    # FINITO a magazzino. Per un componente intermedio: scarica solo la SUA
    # distinta diretta (un livello) e carica LUI STESSO a magazzino come
    # semilavorato pronto. In entrambi i casi si registra comunque la
    # varianza di lavorazione (tempo reale vs standard) per OGNI fase
    # dichiarata — quella è per costruzione specifica di ogni singola fase,
    # non va mai saltata.
    prima_fase = _e_prima_fase_del_ciclo(codice_lavorato, fase_nome)
    # BUG CORRETTO: quando si completa la produzione del CODICE PADRE
    # (avanza_op=True, codice_lavorato == codice_articolo — l'esatta
    # dichiarazione di "fine produzione" che chiude l'OP), il carico a
    # GiacenzaWood del prodotto finito finiva nella STESSA giacenza che
    # alimenta "Finiti IW" sul Kanban — che invece deve rappresentare SOLO
    # merce rientrata dalla verniciatura/zincatura ESTERNA via DDT
    # confermato, mai la produzione interna grezza appena completata. Quel
    # dato resta comunque visibile tramite "Grezzo IW" (_grezzo_iw_per_codici,
    # letto direttamente dalle dichiarazioni approvate) — qui semplicemente
    # non tocchiamo più GiacenzaWood per il prodotto finito in questo caso
    # specifico. Per i semilavorati/componenti intermedi (codice_lavorato
    # diverso dal codice articolo) il carico resta invariato: quello
    # rappresenta davvero scorta fisica di un pezzo pronto, non il prodotto
    # finito in attesa di trattamento esterno.
    carica_prodotto_finito = not (codice_lavorato == o.codice_articolo and avanza_op)
    qta_tagliata = good + scrap  # il materiale si consuma per OGNI pezzo tagliato, buono o scarto
    avviso_magazzino = None
    if qta_tagliata > 0 and prima_fase:
        try:
            consumi_calcolati, contestuali = _calcola_consumi_standard(o, componente_finale, componente, qta_tagliata)
            consumi = consumi_override if consumi_override is not None else consumi_calcolati
            for cod, qta_consumata in consumi.items():
                if qta_consumata:
                    costo_corrente = _calcola_costo_standard(cod)['costo_totale']
                    _registra_movimento_giacenza(cod, -qta_consumata, 'scarico_produzione',
                                                  riferimento=o.codice,
                                                  note=f'Consuntivo {good} pz buoni + {scrap} pz scarto ({codice_lavorato})',
                                                  costo_unitario=costo_corrente)
            # Isole one-piece-flow (DistintaBaseWood.contestuale=True, es.
            # FRONTE+RETRO di un cavalletto saldati insieme): questi figli non
            # sono MAI stock preesistente, nascono e si consumano nello stesso
            # istante del padre — si carica il fabbisogno e lo si scarica
            # subito dopo, così restano tracciati (costo, storico movimenti)
            # senza mai lasciare una giacenza fantasma né andare in negativo.
            # Non passa da consumi_override: è un comportamento automatico
            # legato al flag di distinta, non un valore che il capo reparto
            # corregge a mano nell'anteprima.
            for cod, qta_consumata in contestuali.items():
                if qta_consumata:
                    costo_corrente = _calcola_costo_standard(cod)['costo_totale']
                    _registra_movimento_giacenza(cod, qta_consumata, 'carico_produzione',
                                                  riferimento=o.codice,
                                                  note=(f'Assemblato contestualmente in {fase_nome} ({codice_lavorato}) '
                                                        f'— isola one-piece-flow, mai dichiarato a parte'),
                                                  costo_unitario=costo_corrente)
                    _registra_movimento_giacenza(cod, -qta_consumata, 'scarico_produzione',
                                                  riferimento=o.codice,
                                                  note=(f'Consuntivo {good} pz buoni + {scrap} pz scarto '
                                                        f'— assemblato contestualmente in {codice_lavorato}'),
                                                  costo_unitario=costo_corrente)
        except Exception as e:
            # NON deve mai bloccare la registrazione del consuntivo — ma un
            # errore qui prima spariva nel nulla, senza traccia da nessuna
            # parte: ora finisce nei log e nell'audit dell'OP, così la
            # prossima volta si vede COSA è andato storto invece di doverlo
            # indovinare da un mancato scarico silenzioso.
            avviso_magazzino = f'Scarico materiale FALLITO: {e}'
            log(f'ERRORE scarico giacenza — OP {o.codice}, {codice_lavorato}, evento {event_id}: {e}')
            _audit(o, 'ERRORE_SCARICO_GIACENZA', f'{codice_lavorato}: {e}', event_id)

        try:
            costo = _calcola_costo_standard(codice_lavorato)
            if carica_prodotto_finito and good > 0:
                if costo['codici_senza_costo']:
                    nota_costo = (f'Consuntivo {good} pz buoni — ⚠️ COSTO INCOMPLETO: mancano prezzi per '
                                   f'{", ".join(sorted(costo["codici_senza_costo"]))} — totale sottostimato')
                else:
                    nota_costo = f'Consuntivo {good} pz buoni' + ('' if componente_finale else ' (semilavorato)')
                _registra_movimento_giacenza(codice_lavorato, good, 'carico_produzione',
                                              riferimento=o.codice, note=nota_costo,
                                              costo_unitario=costo['costo_totale'])
        except Exception as e:
            avviso_magazzino = (avviso_magazzino + ' | ' if avviso_magazzino else '') + f'Carico prodotto FALLITO: {e}'
            log(f'ERRORE carico giacenza — OP {o.codice}, {codice_lavorato}, evento {event_id}: {e}')
            _audit(o, 'ERRORE_CARICO_GIACENZA', f'{codice_lavorato}: {e}', event_id)

    # Varianza di lavorazione: SEMPRE per ogni fase dichiarata (tempo
    # reale vs standard), indipendentemente da _e_prima_fase_del_ciclo — a
    # differenza di scarico/carico materiali, ogni fase ha il proprio tempo
    # standard e va sempre tracciata, anche quella successiva alla prima.
    if good > 0:
        try:
            riga_ciclo = (CicloLavoroWood.query.filter_by(codice=codice_lavorato)
                          .join(CicloLavoroWood.centro_costo)
                          .filter(db.func.lower(CentroCostoWood.nome) == fase_nome.lower())
                          .first())
            if riga_ciclo and riga_ciclo.produttivita_oraria:
                fase_congelata = None
                if componente_finale:
                    legame = LegameCostoStandardOrdineWood.query.get(o.codice)
                    if legame and legame.costo_standard_versione_id:
                        fase_congelata = (CostoStandardVersioneFaseWood.query
                                          .filter_by(versione_id=legame.costo_standard_versione_id)
                                          .filter(db.func.lower(CostoStandardVersioneFaseWood.nome_reparto) == fase_nome.lower())
                                          .first())
                # Per i componenti intermedi non esiste ancora uno snapshot di
                # costo standard congelato dedicato: si usano le tariffe
                # CORRENTI sia per lo standard sia per il reale (varianza di
                # tariffa sempre 0 in quel caso — solo efficienza è significativa).

                produttivita_std = fase_congelata.produttivita_oraria_congelata if fase_congelata else riga_ciclo.produttivita_oraria
                tariffa_macchina_std = fase_congelata.costo_orario_congelato if fase_congelata else (riga_ciclo.centro_costo.costo_orario or 0)
                tariffa_mdo_std = fase_congelata.tariffa_manodopera_congelata if fase_congelata else (riga_ciclo.centro_costo.tariffa_manodopera_diretta_oraria or 0)
                tariffa_macchina_reale = riga_ciclo.centro_costo.costo_orario or 0     # sempre quella corrente
                tariffa_mdo_reale = riga_ciclo.centro_costo.tariffa_manodopera_diretta_oraria or 0

                tempo_standard_min = (good / produttivita_std) * 60 if produttivita_std else 0
                ore_standard = tempo_standard_min / 60
                ore_reali_masterwork = (ManodoperaRealeWood.query
                                        .filter_by(ordine_produzione_id=o.id, fonte='masterwork')
                                        .filter(ManodoperaRealeWood.ore_reali.isnot(None))
                                        .order_by(ManodoperaRealeWood.creato_il.desc()).first())
                ore_reali = ore_reali_masterwork.ore_reali if ore_reali_masterwork else tempo / 60

                costo_standard_lav = ore_standard * tariffa_macchina_std
                costo_reale_lav = ore_reali * tariffa_macchina_reale
                costo_standard_mdo = ore_standard * tariffa_mdo_std
                costo_reale_mdo = ore_reali * tariffa_mdo_reale

                var_tariffa_lav = varianza_tariffa(tariffa_macchina_reale, tariffa_macchina_std, ore_reali)
                var_tariffa_mdo = varianza_tariffa(tariffa_mdo_reale, tariffa_mdo_std, ore_reali)

                db.session.add(VarianzaProduzioneWood(
                    op_code=o.codice, codice_articolo=codice_lavorato, fase=fase_nome, quantita=good,
                    tempo_standard_minuti=round(tempo_standard_min, 2), tempo_reale_minuti=tempo,
                    costo_standard_lavorazione=round(costo_standard_lav, 4),
                    costo_reale_lavorazione=round(costo_reale_lav, 4),
                    costo_standard_manodopera=round(costo_standard_mdo, 4),
                    costo_reale_manodopera=round(costo_reale_mdo, 4),
                    varianza=round(costo_reale_lav - costo_standard_lav, 4),
                    varianza_manodopera=round(costo_reale_mdo - costo_standard_mdo, 4),
                    varianza_tariffa_lavorazione=round(var_tariffa_lav, 4) if fase_congelata else 0,
                    varianza_tariffa_manodopera=round(var_tariffa_mdo, 4) if fase_congelata else 0,
                ))

                # Auto-apprendimento del tempo standard: media cumulativa
                # pezzi/minuti osservati (non sovrascrive di colpo col dato
                # di un solo evento, si stabilizza mano a mano che arrivano
                # più consuntivi) — salta le righe che il capo ha bloccato
                # a mano con produttivita_oraria_manuale.
                if not riga_ciclo.produttivita_oraria_manuale and ore_reali > 0:
                    riga_ciclo.produttivita_pezzi_osservati = (riga_ciclo.produttivita_pezzi_osservati or 0) + good
                    riga_ciclo.produttivita_minuti_osservati = (riga_ciclo.produttivita_minuti_osservati or 0) + tempo
                    if riga_ciclo.produttivita_minuti_osservati >= 15:  # almeno 15 minuti di storico prima di fidarsi
                        riga_ciclo.produttivita_oraria = round(
                            riga_ciclo.produttivita_pezzi_osservati / (riga_ciclo.produttivita_minuti_osservati / 60), 3)
        except Exception:
            pass  # nessuna varianza registrabile (fase non abbinabile) non deve bloccare il consuntivo

    return avviso_magazzino


@pp_bp.post('/api/pp/events')
def api_evento():
    auth = _api_auth()
    if auth: return auth
    d = request.get_json(silent=True) or {}
    required = ('event_id', 'op_code', 'fase', 'timestamp')
    if any(not str(d.get(k, '')).strip() for k in required): return jsonify(ok=False, error='Campi obbligatori: '+', '.join(required)), 400
    if EventoConsuntivoPP.query.filter_by(event_id=str(d['event_id']).strip()).first(): return jsonify(ok=True, deduplicated=True), 200
    try:
        ts = datetime.fromisoformat(str(d['timestamp']).replace('Z', '+00:00')).replace(tzinfo=None)
        good = _integer(d.get('pezzi_buoni', 0), 'Pezzi buoni'); scrap = _integer(d.get('pezzi_scarto', 0), 'Scarto')
        tempo = _integer(d.get('tempo_minuti', d.get('tempo', 0)), 'Tempo')
        o = OrdineProduzione.query.filter_by(codice=str(d['op_code']).strip()).with_for_update().first()
        if not o: return jsonify(ok=False, error='OP non trovato'), 404
        if not _is_carpenteria(o.asa) or o.stato not in ('Rilasciato','In esecuzione'):
            return jsonify(ok=False, error='OP non attivo o non disponibile per Carpenteria Propria'), 409
        _registra_evento_consuntivo(o, str(d['fase']).strip(), ts, good, scrap, tempo, str(d['event_id']).strip(),
                                     componente=(str(d['componente']).strip() if d.get('componente') else None),
                                     operatore=(str(d['operatore']).strip() if d.get('operatore') else None))
        db.session.commit(); return jsonify(ok=True, deduplicated=False, ordine=_ordine(o)), 201
    except ValueError as exc: return jsonify(ok=False, error=str(exc)), 400
    except IntegrityError:
        db.session.rollback(); return jsonify(ok=True, deduplicated=True), 200


# ══════════════════════════════════════════════════════════════════════════════
#  ANALISI COSTO / VARIANZE PER ORDINE DI PRODUZIONE — confronta lo standard
#  CONGELATO al rilascio (LegameCostoStandardOrdineWood) con l'effettivo
#  ricavato da: movimenti di scarico materiali valorizzati (MovimentoGiacenzaWood),
#  varianze di lavorazione già registrate ad ogni consuntivo (VarianzaProduzioneWood).
#  Dove un dato "vero" non è disponibile con l'architettura attuale, lo
#  dichiara esplicitamente invece di fingere un valore.
# ══════════════════════════════════════════════════════════════════════════════
@pp_bp.get('/ordini-produzione/<codice>/analisi-costo')
def pagina_analisi_costo(codice):
    return render_template('produzione_pp/analisi_costo.html', active='produzione_pp', codice=codice)


@pp_bp.get('/impostazioni-contabili')
def pagina_impostazioni_contabili():
    return render_template('produzione_pp/impostazioni_contabili.html', active='impostazioni_contabili')


def _analisi_costo_ordine_dict(o):
    """
    Calcolo COMPLETO standard-vs-effettivo per un OP (già recuperato e
    validato dal chiamante): unica fonte di verità, usata sia dalla route
    JSON /analisi-costo sia dalla generazione dei movimenti contabili
    (_genera_movimenti_contabili_ordine) — così i numeri del report e quelli
    scritti in contabilità sono SEMPRE gli stessi. Ritorna un dict "grezzo"
    (senza jsonify) con tutti i campi già usati dal report.
    """

    limiti = []

    # ── Standard congelato al rilascio ──────────────────────────────────
    legame = LegameCostoStandardOrdineWood.query.get(o.codice)
    versione = legame.versione if legame else None
    if not versione:
        limiti.append('Nessuna versione di Costo Standard agganciata a questo OP (probabilmente non ancora rilasciato, '
                       'o l\'aggancio non è riuscito): i confronti sotto usano 0 come standard e vanno considerati inattendibili.')
        std_unit = {'costo_materiali': 0, 'costo_lavorazione': 0, 'costo_manodopera': 0, 'costo_overhead': 0, 'costo_totale': 0}
        versione_info = None
    else:
        std_unit = {'costo_materiali': versione.costo_materiali, 'costo_lavorazione': versione.costo_lavorazione,
                    'costo_manodopera': versione.costo_manodopera,
                    'costo_overhead': versione.costo_overhead, 'costo_totale': versione.costo_totale}
        versione_info = {'versione': versione.versione, 'calcolato_il': versione.calcolato_il.strftime('%d/%m/%Y %H:%M') if versione.calcolato_il else None,
                          'completo': versione.completo, 'codici_senza_costo': versione.codici_senza_costo.split(',') if versione.codici_senza_costo else []}
        if not versione.completo:
            limiti.append(f'Lo standard agganciato (versione {versione.versione}) era già incompleto al momento del rilascio '
                           f'(mancava il prezzo di: {", ".join(versione.codici_senza_costo.split(","))}) — i totali standard sotto sono sottostimati.')

    qta_buona = o.qta_buona or 0
    qta_scarto = o.qta_scarto or 0

    std_materiali_tot = round(std_unit['costo_materiali'] * qta_buona, 2)
    std_lavorazione_tot = round(std_unit['costo_lavorazione'] * qta_buona, 2)
    std_manodopera_tot = round(std_unit['costo_manodopera'] * qta_buona, 2)
    std_overhead_tot = round(std_unit['costo_overhead'] * qta_buona, 2)
    std_totale_tot = round(std_unit['costo_totale'] * qta_buona, 2)
    std_totale_pianificato = round(std_unit['costo_totale'] * (o.qta_pianificata or 0), 2)

    # ── Materiali effettivi (dai movimenti di scarico valorizzati) ──────
    movimenti = (MovimentoGiacenzaWood.query
                 .filter_by(riferimento=o.codice, tipo='scarico_produzione').all())
    per_componente = {}
    materiali_senza_costo = set()
    for m in movimenti:
        riga = per_componente.setdefault(m.codice, {'quantita': 0.0, 'valore': 0.0, 'valore_disponibile': True})
        riga['quantita'] += abs(m.quantita or 0)
        if m.costo_unitario is None:
            riga['valore_disponibile'] = False
            materiali_senza_costo.add(m.codice)
        else:
            riga['valore'] += (m.valore or 0)

    eff_materiali_tot = round(sum(r['valore'] for r in per_componente.values()), 2)
    if materiali_senza_costo:
        limiti.append(f'Movimenti di scarico senza costo registrato (dati precedenti a questa funzione) per: '
                       f'{", ".join(sorted(materiali_senza_costo))} — il loro valore NON è incluso nel totale effettivo materiali, che quindi è sottostimato.')
    if not movimenti:
        limiti.append('Nessun movimento di scarico materiali trovato per questo OP: il costo effettivo materiali è 0 per mancanza di dati, non perché sia davvero zero.')

    # Dettaglio quantità/prezzo per componente — usa lo snapshot CONGELATO
    # (CostoStandardVersioneDettaglioWood) legato alla stessa versione dello
    # standard usato sopra: quantità e prezzo sono esattamente quelli in
    # vigore al momento del rilascio, quindi la somma delle varianze per
    # componente riconcilia sempre col totale materiali qui sopra.
    dettaglio_materiali = []
    if per_componente or versione:
        dettaglio_standard = {}
        if versione:
            for det in CostoStandardVersioneDettaglioWood.query.filter_by(versione_id=versione.id).all():
                dettaglio_standard[det.codice_componente] = det
        tutti_i_codici = set(per_componente.keys()) | set(dettaglio_standard.keys())
        for cod in sorted(tutti_i_codici):
            riga_eff = per_componente.get(cod)
            det_std = dettaglio_standard.get(cod)
            qty_standard = round(det_std.quantita_standard * qta_buona, 4) if det_std else None
            prezzo_standard = det_std.prezzo_standard_unitario if det_std else None
            qty_effettiva = round(riga_eff['quantita'], 4) if riga_eff else 0.0
            prezzo_effettivo = (round(riga_eff['valore'] / riga_eff['quantita'], 4)
                                 if riga_eff and riga_eff['valore_disponibile'] and riga_eff['quantita'] else None)
            var_quantita = (round(varianza_quantita_materiale(qty_standard, qty_effettiva, prezzo_standard), 2)
                             if qty_standard is not None and prezzo_standard is not None else None)
            var_prezzo = (round(varianza_prezzo_materiale(prezzo_standard, prezzo_effettivo, qty_effettiva), 2)
                          if prezzo_effettivo is not None and prezzo_standard is not None else None)
            dettaglio_materiali.append({
                'codice': cod, 'quantita_standard': qty_standard, 'quantita_effettiva': qty_effettiva,
                'prezzo_standard': prezzo_standard, 'prezzo_effettivo': prezzo_effettivo,
                'valore_disponibile': bool(riga_eff and riga_eff['valore_disponibile']),
                'standard_disponibile': det_std is not None and prezzo_standard is not None,
                'varianza_quantita': var_quantita, 'varianza_prezzo': var_prezzo,
                'tipo': det_std.tipo if det_std else '',
            })
    if versione is None:
        limiti.append('Nessuno snapshot standard per componente disponibile (nessuna versione agganciata): '
                       'il dettaglio materiali sotto mostra solo i consumi effettivi, senza confronto.')

    # ── Lavorazione effettiva (da varianze già registrate ad ogni consuntivo) ──
    varianze_lav = VarianzaProduzioneWood.query.filter_by(op_code=o.codice).all()
    eff_lavorazione_tot = round(sum(v.costo_reale_lavorazione for v in varianze_lav), 2)
    std_lavorazione_da_eventi = round(sum(v.costo_standard_lavorazione for v in varianze_lav), 2)
    var_tariffa_lav_tot = round(sum(v.varianza_tariffa_lavorazione for v in varianze_lav), 2)
    var_efficienza_tot = round(sum(v.varianza - v.varianza_tariffa_lavorazione for v in varianze_lav), 2)
    eff_manodopera_tot = round(sum(v.costo_reale_manodopera for v in varianze_lav), 2)
    var_tariffa_mdo_tot = round(sum(v.varianza_tariffa_manodopera for v in varianze_lav), 2)
    var_efficienza_manodopera_tot = round(sum(v.varianza_manodopera - v.varianza_tariffa_manodopera for v in varianze_lav), 2)

    eventi = EventoConsuntivoPP.query.filter_by(op_code=o.codice).all()
    fasi_tracciate = {v.fase for v in varianze_lav}
    fasi_non_tracciate = sorted({e.fase for e in eventi if e.fase not in fasi_tracciate})
    if fasi_non_tracciate:
        limiti.append(f'Fasi consuntivate ma non abbinabili a un reparto del Ciclo di Lavoro (nome fase diverso dal nome '
                       f'reparto): {", ".join(fasi_non_tracciate)} — il loro tempo/costo NON è incluso nella lavorazione/manodopera effettiva.')
    versione_id_corrente = legame.costo_standard_versione_id if legame else None
    fasi_con_tariffa_congelata = ({f.nome_reparto.lower() for f in
                                   CostoStandardVersioneFaseWood.query.filter_by(versione_id=versione_id_corrente).all()}
                                  if versione_id_corrente else set())
    fasi_senza_tariffa_congelata = sorted({f for f in fasi_tracciate if f.lower() not in fasi_con_tariffa_congelata})
    if fasi_senza_tariffa_congelata:
        limiti.append(f'Varianza di tariffa non disponibile per: {", ".join(fasi_senza_tariffa_congelata)} — nessuna '
                       f'tariffa congelata trovata per quella fase al momento del rilascio (probabilmente il Costo '
                       f'Standard non era ancora stato ricalcolato con le tariffe attuali). La varianza mostrata per '
                       f'quella fase è quindi solo di tempo/efficienza.')

    # ── Scarto ──────────────────────────────────────────────────────────
    impatto_scarto = round(std_unit['costo_totale'] * qta_scarto, 2)

    # ── Overhead ─────────────────────────────────────────────────────────
    overhead_pct_corrente = _overhead_pct()
    eff_overhead_tot = round((eff_materiali_tot + eff_lavorazione_tot) * (overhead_pct_corrente / 100.0), 2)
    var_overhead = round(eff_overhead_tot - std_overhead_tot, 2)
    if versione and versione.overhead_pct_usata != overhead_pct_corrente:
        limiti.append(f'Aliquota overhead cambiata dopo il rilascio: era {versione.overhead_pct_usata}% allo standard, '
                       f'ora è {overhead_pct_corrente}% — parte della varianza overhead deriva da questo cambio, non da un vero scostamento operativo.')

    # ── Totali ───────────────────────────────────────────────────────────
    eff_totale_tot = round(eff_materiali_tot + eff_lavorazione_tot + eff_manodopera_tot + eff_overhead_tot, 2)
    var_materiali_tot = round(eff_materiali_tot - std_materiali_tot, 2)
    var_lavorazione_tot = round(eff_lavorazione_tot - std_lavorazione_tot, 2)
    var_manodopera_tot = round(eff_manodopera_tot - std_manodopera_tot, 2)
    var_totale = round(eff_totale_tot - std_totale_tot, 2)
    var_totale_pct = round((var_totale / std_totale_tot) * 100, 2) if std_totale_tot else None
    var_scarto_pct = round((qta_scarto / (qta_buona + qta_scarto)) * 100, 2) if (qta_buona + qta_scarto) else None

    # ── Soglie di allarme (configurabili) e segnalazioni automatiche ──────
    soglie = SogliaAllarmeVarianzaWood.query.first()
    if not soglie:
        soglie = SogliaAllarmeVarianzaWood()
        db.session.add(soglie); db.session.commit()

    def _pct(varr, std):
        return round((varr / std) * 100, 2) if std else None

    pct_materiali = _pct(var_materiali_tot, std_materiali_tot)
    pct_lavorazione = _pct(var_lavorazione_tot, std_lavorazione_tot)
    pct_manodopera = _pct(var_manodopera_tot, std_manodopera_tot)

    segnalazioni = []
    if pct_materiali is not None and abs(pct_materiali) >= soglie.soglia_materiali_pct:
        segnalazioni.append({'categoria': 'materiali', 'livello': 'sfavorevole' if var_materiali_tot > 0 else 'favorevole',
                              'messaggio': f'Varianza materiali {"+"if var_materiali_tot>0 else ""}{var_materiali_tot} € ({pct_materiali}%) oltre la soglia configurata di {soglie.soglia_materiali_pct}%'})
    if pct_lavorazione is not None and abs(pct_lavorazione) >= soglie.soglia_lavorazione_pct:
        segnalazioni.append({'categoria': 'lavorazione', 'livello': 'sfavorevole' if var_lavorazione_tot > 0 else 'favorevole',
                              'messaggio': f'Varianza lavorazione {"+"if var_lavorazione_tot>0 else ""}{var_lavorazione_tot} € ({pct_lavorazione}%) oltre la soglia configurata di {soglie.soglia_lavorazione_pct}%'})
    if pct_manodopera is not None and abs(pct_manodopera) >= soglie.soglia_manodopera_pct:
        segnalazioni.append({'categoria': 'manodopera', 'livello': 'sfavorevole' if var_manodopera_tot > 0 else 'favorevole',
                              'messaggio': f'Varianza manodopera {"+"if var_manodopera_tot>0 else ""}{var_manodopera_tot} € ({pct_manodopera}%) oltre la soglia configurata di {soglie.soglia_manodopera_pct}%'})
    if var_totale_pct is not None and abs(var_totale_pct) >= soglie.soglia_totale_pct:
        segnalazioni.append({'categoria': 'totale', 'livello': 'sfavorevole' if var_totale > 0 else 'favorevole',
                              'messaggio': f'Varianza totale commessa {"+"if var_totale>0 else ""}{var_totale} € ({var_totale_pct}%) oltre la soglia configurata di {soglie.soglia_totale_pct}%'})
    if var_scarto_pct is not None and var_scarto_pct >= soglie.soglia_scarto_pct:
        segnalazioni.append({'categoria': 'scarto', 'livello': 'sfavorevole',
                              'messaggio': f'Scarto {var_scarto_pct}% oltre la soglia configurata di {soglie.soglia_scarto_pct}%'})

    # ── Riepilogo manageriale in linguaggio naturale ──────────────────────
    riepilogo_manageriale = []
    materiali_peggiori = sorted([r for r in dettaglio_materiali if r['varianza_quantita']], key=lambda r: abs(r['varianza_quantita']), reverse=True)[:3]
    for r in materiali_peggiori:
        delta_q = round(r['quantita_effettiva'] - (r['quantita_standard'] or 0), 3)
        if delta_q:
            riepilogo_manageriale.append(
                f"Materiale {r['codice']} consumato {'oltre' if delta_q > 0 else 'sotto'} lo standard: "
                f"{'+' if delta_q > 0 else ''}{delta_q} — varianza {'sfavorevole' if r['varianza_quantita'] > 0 else 'favorevole'} € {r['varianza_quantita']}")
    if var_efficienza_tot:
        ore_delta = round(sum((v.tempo_reale_minuti - v.tempo_standard_minuti) for v in varianze_lav) / 60, 2)
        riepilogo_manageriale.append(
            f"Tempo di lavorazione {'superiore' if ore_delta > 0 else 'inferiore'} allo standard: "
            f"{'+' if ore_delta > 0 else ''}{ore_delta} ore — varianza {'sfavorevole' if var_efficienza_tot > 0 else 'favorevole'} € {var_efficienza_tot}")
    if var_totale:
        riepilogo_manageriale.append(
            f"Costo effettivo commessa {'superiore' if var_totale > 0 else 'inferiore'} al costo standard di € {abs(var_totale)}"
            + (f" ({var_totale_pct}%)" if var_totale_pct is not None else ''))
    if impatto_scarto:
        riepilogo_manageriale.append(f"Impatto economico dello scarto ({qta_scarto} pz): € {impatto_scarto}")
    if not riepilogo_manageriale:
        riepilogo_manageriale.append("Nessuno scostamento rilevante rispetto allo standard.")

    return dict(ok=True, codice=o.codice, codice_articolo=o.codice_articolo, stato=o.stato,
        qta_pianificata=o.qta_pianificata, qta_buona=qta_buona, qta_scarto=qta_scarto,
        data_rilascio=o.data_rilascio.strftime('%d/%m/%Y %H:%M') if o.data_rilascio else None,
        versione_standard=versione_info,
        standard={'materiali': std_materiali_tot, 'lavorazione': std_lavorazione_tot, 'manodopera': std_manodopera_tot,
                   'overhead': std_overhead_tot, 'totale': std_totale_tot, 'totale_su_pianificata': std_totale_pianificato,
                   'unitario': {'materiali': std_unit['costo_materiali'], 'lavorazione': std_unit['costo_lavorazione'],
                                'manodopera': std_unit['costo_manodopera'],
                                'overhead': std_unit['costo_overhead'], 'totale': std_unit['costo_totale']}},
        effettivo={'materiali': eff_materiali_tot, 'lavorazione': eff_lavorazione_tot, 'manodopera': eff_manodopera_tot,
                    'overhead': eff_overhead_tot, 'totale': eff_totale_tot},
        varianza={'materiali': var_materiali_tot, 'lavorazione': var_lavorazione_tot, 'manodopera': var_manodopera_tot,
                   'overhead': var_overhead, 'totale': var_totale, 'totale_pct': var_totale_pct,
                   'scarto_impatto': impatto_scarto, 'scarto_pct': var_scarto_pct,
                   'lavorazione_efficienza': var_efficienza_tot, 'manodopera_efficienza': var_efficienza_manodopera_tot,
                   'lavorazione_tariffa': var_tariffa_lav_tot, 'manodopera_tariffa': var_tariffa_mdo_tot},
        dettaglio_materiali=dettaglio_materiali,
        dettaglio_lavorazione=[{
            'fase': v.fase, 'tempo_standard_minuti': v.tempo_standard_minuti, 'tempo_reale_minuti': v.tempo_reale_minuti,
            'costo_standard': v.costo_standard_lavorazione, 'costo_reale': v.costo_reale_lavorazione, 'varianza': v.varianza,
            'costo_standard_manodopera': v.costo_standard_manodopera, 'costo_reale_manodopera': v.costo_reale_manodopera,
            'varianza_manodopera': v.varianza_manodopera,
            'varianza_tariffa_lavorazione': v.varianza_tariffa_lavorazione,
            'varianza_tariffa_manodopera': v.varianza_tariffa_manodopera,
        } for v in varianze_lav],
        soglie={'materiali_pct': soglie.soglia_materiali_pct, 'lavorazione_pct': soglie.soglia_lavorazione_pct,
                'manodopera_pct': soglie.soglia_manodopera_pct, 'totale_pct': soglie.soglia_totale_pct,
                'scarto_pct': soglie.soglia_scarto_pct},
        segnalazioni=segnalazioni,
        riepilogo_manageriale=riepilogo_manageriale,
        limiti=limiti,
    )


@pp_bp.get('/api/ordini-produzione/<codice>/analisi-costo')
def api_analisi_costo_ordine(codice):
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    return jsonify(**_analisi_costo_ordine_dict(o))


# ══════════════════════════════════════════════════════════════════════════════
#  MANODOPERA REALE — PLACEHOLDER in attesa dell'integrazione con MasterWork.
#  Oggi non c'è nessun collegamento automatico: queste API permettono solo di
#  inserire/consultare a mano un consuntivo ore con fonte='masterwork' per un
#  OP, che (se presente) viene già usato al posto del tempo auto-riportato
#  nel calcolo della varianza manodopera (vedi sopra). Quando l'integrazione
#  vera sarà collegata, basterà farla scrivere qui con fonte='masterwork'
#  invece di passare da un inserimento manuale.
# ══════════════════════════════════════════════════════════════════════════════

@pp_bp.route('/api/ordini_produzione/<int:oid>/manodopera_reale', methods=['GET'])
def api_lista_manodopera_reale(oid):
    OrdineProduzione.query.get_or_404(oid)
    righe = ManodoperaRealeWood.query.filter_by(ordine_produzione_id=oid).order_by(ManodoperaRealeWood.creato_il.desc()).all()
    return jsonify([{
        'id': r.id, 'ore_reali': r.ore_reali, 'fonte': r.fonte, 'note': r.note or '',
        'creato_il': r.creato_il.isoformat() if r.creato_il else None,
    } for r in righe])


@pp_bp.route('/api/ordini_produzione/<int:oid>/manodopera_reale', methods=['POST'])
def api_add_manodopera_reale(oid):
    OrdineProduzione.query.get_or_404(oid)
    d = request.get_json(force=True)
    try:
        ore_reali = float(d['ore_reali']) if d.get('ore_reali') not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Ore reali non valide'}), 400
    fonte = d.get('fonte') or 'placeholder'
    if fonte not in ('placeholder', 'masterwork'):
        return jsonify({'errore': True, 'messaggio': 'Fonte non valida'}), 400
    db.session.add(ManodoperaRealeWood(
        ordine_produzione_id=oid, ore_reali=ore_reali, fonte=fonte, note=(d.get('note') or '').strip(),
    ))
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  PREDISPOSIZIONE CONTABILE PER MASTERLEDGER (non ancora integrato)
#  ---------------------------------------------------------------
#  Questo modulo NON trasmette nulla a nessun sistema esterno: genera solo
#  righe strutturate in partita doppia a partire dagli stessi numeri già
#  mostrati in Analisi Costo (_analisi_costo_ordine_dict è l'unica fonte di
#  verità condivisa), pronte per una futura integrazione. Finché
#  MasterLedger non è collegato, l'unica "uscita" è l'export CSV qui sotto.
#
#  Logica di ogni riga generata (vedi _genera_movimenti_contabili_ordine):
#   - Consumo materiali: DARE Consumo materiali standard / AVERE Magazzino,
#     valorizzato a QUANTITÀ STANDARD × PREZZO STANDARD; le differenze reali
#     (quantità consumata ed eventuale prezzo diverso) finiscono su righe di
#     varianza separate, sempre in contropartita al Magazzino — così il
#     Magazzino si muove per il valore ESATTO realmente scaricato, mentre a
#     conto economico restano visibili standard e varianze separati.
#   - Lavorazione/manodopera: assorbimento a costo standard in Produzione in
#     Corso (WIP), con varianze di efficienza e tariffa separate.
#   - Chiusura: il prodotto finito entra a magazzino al costo standard pieno
#     (mai al costo effettivo), il residuo del WIP diventa Varianza di
#     Produzione — esattamente lo schema SAP richiesto.
# ══════════════════════════════════════════════════════════════════════════════

def _riga_varianza_contabile(rows, data, op, conti, voce_varianza, voce_contropartita, importo, causale, centro_costo='', tipo_varianza=''):
    """
    Aggiunge una coppia DARE/AVERE per una varianza: se importo > 0 (sfavorevole)
    la varianza va a DARE (costo aggiuntivo) e la contropartita ad AVERE;
    se importo < 0 (favorevole) è il contrario. Non scrive nulla se importo è ~0.
    """
    importo = round(importo, 2)
    if abs(importo) < 0.005:
        return
    conto_varianza = conti.get(voce_varianza, '')
    conto_contro = conti.get(voce_contropartita, '')
    if importo > 0:
        rows.append(MovimentoContabileWood(data=data, causale=causale, op_code=op.codice, codice_articolo=op.codice_articolo,
                                            conto=conto_varianza, centro_costo=centro_costo, dare_avere='DARE', importo=abs(importo), tipo_varianza=tipo_varianza))
        rows.append(MovimentoContabileWood(data=data, causale=causale, op_code=op.codice, codice_articolo=op.codice_articolo,
                                            conto=conto_contro, centro_costo=centro_costo, dare_avere='AVERE', importo=abs(importo), tipo_varianza=tipo_varianza))
    else:
        rows.append(MovimentoContabileWood(data=data, causale=causale, op_code=op.codice, codice_articolo=op.codice_articolo,
                                            conto=conto_contro, centro_costo=centro_costo, dare_avere='DARE', importo=abs(importo), tipo_varianza=tipo_varianza))
        rows.append(MovimentoContabileWood(data=data, causale=causale, op_code=op.codice, codice_articolo=op.codice_articolo,
                                            conto=conto_varianza, centro_costo=centro_costo, dare_avere='AVERE', importo=abs(importo), tipo_varianza=tipo_varianza))


def _genera_movimenti_contabili_ordine(o):
    """
    Genera (rigenerando le sole righe NON ancora esportate) i movimenti
    contabili strutturati per l'OP o, a partire da _analisi_costo_ordine_dict.
    Ritorna (righe_create: int, avvisi: list[str]).
    """
    assicura_conti_contabili_wood()
    conti = {c.voce: c.conto for c in ContoContabileMappaWood.query.all()}
    conti_mancanti = sorted(v for v, conto in conti.items() if not conto)

    d = _analisi_costo_ordine_dict(o)
    oggi = date.today()
    rows = []

    # ── Materiali ──────────────────────────────────────────────────────
    for r in d['dettaglio_materiali']:
        if r['quantita_standard'] is None or r['prezzo_standard'] is None:
            continue
        voce_magazzino = 'MAGAZZINO_SEMILAVORATI' if r['tipo'] == 'SEMILAVORATO' else 'MAGAZZINO_MP'
        base_std = round(r['quantita_standard'] * r['prezzo_standard'], 2)
        if base_std:
            rows.append(MovimentoContabileWood(data=oggi, causale=f"Consumo standard {r['codice']} — {o.codice}",
                                                op_code=o.codice, codice_articolo=o.codice_articolo,
                                                conto=conti.get('CONSUMO_MATERIALI_STD', ''), dare_avere='DARE', importo=base_std))
            rows.append(MovimentoContabileWood(data=oggi, causale=f"Consumo standard {r['codice']} — {o.codice}",
                                                op_code=o.codice, codice_articolo=o.codice_articolo,
                                                conto=conti.get(voce_magazzino, ''), dare_avere='AVERE', importo=base_std))
        if r['varianza_quantita']:
            _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_QUANTITA_MATERIALI', voce_magazzino,
                                      r['varianza_quantita'], f"Varianza quantità {r['codice']} — {o.codice}", tipo_varianza='quantita_materiale')
        if r['varianza_prezzo']:
            _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_PREZZO_MATERIALI', voce_magazzino,
                                      r['varianza_prezzo'], f"Varianza prezzo {r['codice']} — {o.codice}", tipo_varianza='prezzo_materiale')

    # ── Lavorazione (macchina) + Manodopera, per fase ───────────────────
    for v in d['dettaglio_lavorazione']:
        if v['costo_standard']:
            rows.append(MovimentoContabileWood(data=oggi, causale=f"Assorbimento macchina std. {v['fase']} — {o.codice}",
                                                op_code=o.codice, codice_articolo=o.codice_articolo, centro_costo=v['fase'],
                                                conto=conti.get('PRODUZIONE_IN_CORSO', ''), dare_avere='DARE', importo=v['costo_standard']))
            rows.append(MovimentoContabileWood(data=oggi, causale=f"Assorbimento macchina std. {v['fase']} — {o.codice}",
                                                op_code=o.codice, codice_articolo=o.codice_articolo, centro_costo=v['fase'],
                                                conto=conti.get('ASSORBIMENTO_MACCHINA_STD', ''), dare_avere='AVERE', importo=v['costo_standard']))
        var_efficienza_macch = round(v['varianza'] - v['varianza_tariffa_lavorazione'], 2)
        _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_EFFICIENZA_MACCHINA', 'ASSORBIMENTO_MACCHINA_STD',
                                  var_efficienza_macch, f"Varianza efficienza macchina {v['fase']} — {o.codice}", v['fase'], 'efficienza_macchina')
        _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_TARIFFA_MACCHINA', 'ASSORBIMENTO_MACCHINA_STD',
                                  v['varianza_tariffa_lavorazione'], f"Varianza tariffa macchina {v['fase']} — {o.codice}", v['fase'], 'tariffa_macchina')

        if v['costo_standard_manodopera']:
            rows.append(MovimentoContabileWood(data=oggi, causale=f"Assorbimento manodopera std. {v['fase']} — {o.codice}",
                                                op_code=o.codice, codice_articolo=o.codice_articolo, centro_costo=v['fase'],
                                                conto=conti.get('PRODUZIONE_IN_CORSO', ''), dare_avere='DARE', importo=v['costo_standard_manodopera']))
            rows.append(MovimentoContabileWood(data=oggi, causale=f"Assorbimento manodopera std. {v['fase']} — {o.codice}",
                                                op_code=o.codice, codice_articolo=o.codice_articolo, centro_costo=v['fase'],
                                                conto=conti.get('ASSORBIMENTO_MANODOPERA_STD', ''), dare_avere='AVERE', importo=v['costo_standard_manodopera']))
        var_efficienza_mdo = round(v['varianza_manodopera'] - v['varianza_tariffa_manodopera'], 2)
        _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_EFFICIENZA_MANODOPERA', 'ASSORBIMENTO_MANODOPERA_STD',
                                  var_efficienza_mdo, f"Varianza efficienza manodopera {v['fase']} — {o.codice}", v['fase'], 'efficienza_manodopera')
        _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_TARIFFA_MANODOPERA', 'ASSORBIMENTO_MANODOPERA_STD',
                                  v['varianza_tariffa_manodopera'], f"Varianza tariffa manodopera {v['fase']} — {o.codice}", v['fase'], 'tariffa_manodopera')

    # ── Carico prodotto finito a costo standard + chiusura varianza di produzione ──
    std_tot = d['standard']['totale']
    if std_tot:
        rows.append(MovimentoContabileWood(data=oggi, causale=f"Carico PF a costo standard — {o.codice}",
                                            op_code=o.codice, codice_articolo=o.codice_articolo,
                                            conto=conti.get('MAGAZZINO_PF', ''), dare_avere='DARE', importo=std_tot))
        rows.append(MovimentoContabileWood(data=oggi, causale=f"Carico PF a costo standard — {o.codice}",
                                            op_code=o.codice, codice_articolo=o.codice_articolo,
                                            conto=conti.get('PRODUZIONE_IN_CORSO', ''), dare_avere='AVERE', importo=std_tot))
    _riga_varianza_contabile(rows, oggi, o, conti, 'VARIANZA_PRODUZIONE', 'PRODUZIONE_IN_CORSO',
                              d['varianza']['totale'], f"Varianza di produzione — {o.codice}", tipo_varianza='produzione')

    # Rigenerazione idempotente: cancella solo le righe NON ancora esportate di questo OP
    MovimentoContabileWood.query.filter_by(op_code=o.codice, esportato=False).delete()
    for r in rows:
        db.session.add(r)
    db.session.commit()

    avvisi = []
    if conti_mancanti:
        avvisi.append(f"Conti non ancora mappati (righe generate con conto vuoto): {', '.join(conti_mancanti)} — "
                       f"compilali in Impostazioni → Mappa Conti Contabili.")
    return len(rows), avvisi


@pp_bp.route('/api/ordini-produzione/<codice>/genera-movimenti-contabili', methods=['POST'])
def api_genera_movimenti_contabili(codice):
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify({'errore': True, 'messaggio': 'Ordine di produzione non trovato'}), 404
    n, avvisi = _genera_movimenti_contabili_ordine(o)
    return jsonify({'ok': True, 'righe_generate': n, 'avvisi': avvisi})


@pp_bp.route('/api/ordini-produzione/<codice>/movimenti-contabili')
def api_lista_movimenti_contabili(codice):
    righe = (MovimentoContabileWood.query.filter_by(op_code=codice)
             .order_by(MovimentoContabileWood.id).all())
    return jsonify([{
        'id': r.id, 'data': r.data.isoformat(), 'causale': r.causale, 'conto': r.conto,
        'centro_costo': r.centro_costo, 'dare_avere': r.dare_avere, 'importo': r.importo,
        'tipo_varianza': r.tipo_varianza, 'esportato': r.esportato,
    } for r in righe])


@pp_bp.route('/api/movimenti-contabili/export.csv')
def api_export_movimenti_contabili():
    """
    Export CSV di tutti i movimenti NON ancora esportati (o tutti, con
    ?tutti=1) — marca esportato=True quelli inclusi, PRONTI per una futura
    trasmissione a MasterLedger (oggi solo scaricabili a mano).
    """
    solo_non_esportati = request.args.get('tutti') != '1'
    q = MovimentoContabileWood.query
    if solo_non_esportati:
        q = q.filter_by(esportato=False)
    righe = q.order_by(MovimentoContabileWood.data, MovimentoContabileWood.op_code, MovimentoContabileWood.id).all()

    intestazione = 'data;causale;commessa;codice_articolo;conto;centro_costo;dare_avere;importo;tipo_varianza;riferimento\n'
    corpo = ''.join(
        f"{r.data.isoformat()};{r.causale};{r.op_code};{r.codice_articolo};{r.conto};{r.centro_costo};"
        f"{r.dare_avere};{r.importo:.2f};{r.tipo_varianza};MOV-{r.id}\n"
        for r in righe
    )
    now = datetime.utcnow()
    for r in righe:
        r.esportato = True
        r.esportato_il = now
    db.session.commit()
    return Response(intestazione + corpo, mimetype='text/csv',
                     headers={'Content-Disposition': 'attachment; filename="movimenti_contabili_wood.csv"'})


# ══════════════════════════════════════════════════════════════════════════════
#  IMPOSTAZIONI: soglie di allarme varianza + mappa conti contabili
# ══════════════════════════════════════════════════════════════════════════════

@pp_bp.route('/api/impostazioni/soglie_varianza', methods=['GET'])
def api_get_soglie_varianza():
    s = SogliaAllarmeVarianzaWood.query.first()
    if not s:
        s = SogliaAllarmeVarianzaWood(); db.session.add(s); db.session.commit()
    return jsonify({'materiali_pct': s.soglia_materiali_pct, 'lavorazione_pct': s.soglia_lavorazione_pct,
                     'manodopera_pct': s.soglia_manodopera_pct, 'totale_pct': s.soglia_totale_pct,
                     'scarto_pct': s.soglia_scarto_pct})


@pp_bp.route('/api/impostazioni/soglie_varianza', methods=['POST'])
def api_set_soglie_varianza():
    s = SogliaAllarmeVarianzaWood.query.first()
    if not s:
        s = SogliaAllarmeVarianzaWood(); db.session.add(s)
    d = request.get_json(force=True)
    try:
        for campo, chiave in (('soglia_materiali_pct', 'materiali_pct'), ('soglia_lavorazione_pct', 'lavorazione_pct'),
                               ('soglia_manodopera_pct', 'manodopera_pct'), ('soglia_totale_pct', 'totale_pct'),
                               ('soglia_scarto_pct', 'scarto_pct')):
            if chiave in d:
                setattr(s, campo, float(d[chiave]))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Valori soglia non validi'}), 400
    db.session.commit()
    return jsonify({'ok': True})


@pp_bp.route('/api/impostazioni/conti_contabili', methods=['GET'])
def api_get_conti_contabili():
    assicura_conti_contabili_wood()
    righe = ContoContabileMappaWood.query.order_by(ContoContabileMappaWood.voce).all()
    return jsonify([{'id': r.id, 'voce': r.voce, 'conto': r.conto, 'descrizione_conto': r.descrizione_conto} for r in righe])


@pp_bp.route('/api/impostazioni/conti_contabili/<int:cid>', methods=['PUT'])
def api_set_conto_contabile(cid):
    r = ContoContabileMappaWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    r.conto = (d.get('conto') or '').strip()
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  LISTE DI LAVORO — ordini cartacei generati dal programma, uno per OGNI
#  centro di costo ("Lista Tagli", "Lista Piega", "Lista Punzonatrice", "Lista
#  Trapani", ecc. — il nome è sempre "Lista " + nome del centro di costo,
#  niente di scritto a mano nel codice). Stessa impostazione del foglio Excel
#  di Angelo:
#    - MATERIALE = codice_figlio, CODICE = codice_padre, MISURA = sviluppo
#    - lunghezza barra da Lunghezza Barra, pezzi per barra dai Parametri di
#      Lavorazione, Nr Barre = pezzi da tagliare / pezzi per barra
#    - raggruppato per codice_figlio (materiale), perché si lavora un
#      materiale alla volta (es. prima le barre da ø32, poi quelle da ø25)
#  Le colonne mostrate si adattano al TIPO di parametri compilati per quel
#  centro (barra per il taglio, matrice/punto zero/rullo per la piega,
#  impostazione per la satinatura) — se per un centro non è stato ancora
#  compilato nulla in Parametri di Lavorazione, la lista resta comunque
#  utilizzabile con le sole quantità, in attesa che vengano aggiunti.
# ══════════════════════════════════════════════════════════════════════════════

def _prefisso_lista(nome_centro):
    """Prefisso del numero di lista (es. CUT/001) in base al tipo di macchina."""
    n = (nome_centro or '').strip().lower()
    if 'sega' in n: return 'CUT'
    if 'punzon' in n: return 'PUNZ'
    if 'piega' in n: return 'PRES'
    if 'curva' in n: return 'CURV'
    if 'trapan' in n: return 'TRAPA'
    if 'satin' in n: return 'SAT'
    return 'LIST'


def _numero_lista_lavoro(op_code, centro):
    """Numero di lista idempotente per la coppia OP+centro — vedi NumeroListaLavoroWood."""
    prefisso = _prefisso_lista(centro.nome)
    esistente = NumeroListaLavoroWood.query.filter_by(op_code=op_code, centro_costo_id=centro.id).first()
    if esistente:
        return f'{esistente.prefisso}/{esistente.numero:03d}'
    ultimo = (db.session.query(db.func.max(NumeroListaLavoroWood.numero))
              .filter_by(prefisso=prefisso).scalar()) or 0
    nuovo = NumeroListaLavoroWood(op_code=op_code, centro_costo_id=centro.id, prefisso=prefisso, numero=ultimo + 1)
    db.session.add(nuovo)
    db.session.commit()
    return f'{prefisso}/{nuovo.numero:03d}'


def _lista_lavoro_op(o, centro, assegna_numero=True):
    """
    Costruisce la Lista di Lavoro per l'OP o SU QUESTO centro di costo: una
    riga per ogni componente della sua distinta base il cui Ciclo di Lavoro
    passa da questo centro, raggruppate per materiale (codice_figlio) — con
    le specifiche macchina di Parametri di Lavorazione già compilate (o vuote
    se non ancora inserite per quel tipo di lavorazione).

    Le colonne mostrate dipendono dal TIPO di macchina (nome del centro di
    costo), NON da quali campi risultano compilati nella Scheda di Lavorazione
    — la stessa coppia padre/figlio può avere sia dati di taglio (barra) sia
    dati di piega, perché lo stesso componente passa da entrambe le macchine
    in sequenza: mostrare le colonne di piega sulla lista Segatrice (o
    viceversa) sarebbe fuorviante, anche se il dato esiste nel database.
    """
    nome_l = (centro.nome or '').strip().lower()
    mostra_barra = any(k in nome_l for k in ('sega', 'punzon'))
    mostra_piega = any(k in nome_l for k in ('piega', 'curva'))
    mostra_rullo = 'piega' in nome_l   # il Rullo è specifico della Pressopiegatrice, non del Curvatubi
    mostra_satinatura = 'satin' in nome_l

    nodi = _esplodi_componenti_op(o)
    moltiplicatore_per_codice = {n['codice']: n['moltiplicatore'] for n in nodi}

    # Solo i componenti il cui Ciclo di Lavoro passa DAVVERO da questo centro.
    componenti_di_centro = [c for c in moltiplicatore_per_codice
                            if CicloLavoroWood.query.filter_by(codice=c, centro_costo_id=centro.id).first()]

    schede = (SchedaLavorazioneWood.query
              .filter(SchedaLavorazioneWood.codice_padre.in_(componenti_di_centro)).all()) if componenti_di_centro else []
    schede_per_padre = {}
    for s in schede:
        schede_per_padre.setdefault(s.codice_padre, []).append(s)

    righe_per_materiale = {}
    for codice_comp in componenti_di_centro:
        moltiplicatore = moltiplicatore_per_codice[codice_comp]
        nr_pz_da_fare = round((o.qta_pianificata or 0) * moltiplicatore)
        componente_param = None if codice_comp == o.codice_articolo else codice_comp
        # BUG REALE TROVATO E CORRETTO: mancava il filtro per fase — sommava
        # i pezzi buoni dichiarati per questo componente su QUALUNQUE
        # centro, non solo su QUESTO. Un codice che passa da più macchine
        # (es. Segatrice poi Trapani) mostrava "già fatto" su Trapani non
        # appena Segatrice aveva dichiarato la SUA parte — anche se Trapani
        # non aveva ancora lavorato nulla. Ogni centro deve vedere SOLO
        # quello che è stato dichiarato su di lui, indipendente dagli altri.
        pezzi_fatti = int((db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                          .filter(EventoConsuntivoPP.op_code == o.codice,
                                  db.func.lower(EventoConsuntivoPP.fase) == centro.nome.strip().lower(),
                                  EventoConsuntivoPP.componente == componente_param if componente_param
                                  else EventoConsuntivoPP.componente.is_(None))
                          .scalar()) or 0)
        saldo = max(nr_pz_da_fare - pezzi_fatti, 0)

        schede_padre = schede_per_padre.get(codice_comp)
        if schede_padre:
            for s in schede_padre:
                pz_per_barra = s.pezzi_per_barra if mostra_barra else None
                nr_barre = int((saldo + pz_per_barra - 1) // pz_per_barra) if (mostra_barra and pz_per_barra) else None
                riga = {
                    'codice': codice_comp, 'materiale': s.codice_figlio, 'misura': s.sviluppo or '',
                    'lunghezza_barra_mm': s.lunghezza_barra_mm if mostra_barra else None,
                    'spessore_mm': s.spessore_mm if mostra_barra else None,
                    'matrice': (s.matrice.codice if s.matrice else '') if mostra_piega else '',
                    'punto_zero': (s.punto_zero or '') if mostra_piega else '',
                    'indice_assorbimento': (s.indice_assorbimento or '') if mostra_piega else '',
                    'rullo': (s.rullo.codice if s.rullo else '') if mostra_rullo else '',
                    'impostazione_satinatrice': (s.impostazione_satinatrice or '') if mostra_satinatura else '',
                    'nr_pz_da_fare': nr_pz_da_fare, 'pezzi_fatti': pezzi_fatti, 'saldo': saldo,
                    'pz_per_barra': pz_per_barra, 'nr_barre': nr_barre, 'nota': '',
                }
                righe_per_materiale.setdefault(s.codice_figlio, []).append(riga)
        else:
            # Nessun parametro compilato ancora per questo componente in Parametri
            # di Lavorazione — materiale di riserva preso dal primo figlio diretto
            # in Distinta Base, così la riga compare comunque nella lista.
            figli = _righe_bom_attive_wood(codice_comp)
            materiale = figli[0].codice_figlio if figli else '—'
            riga = {
                'codice': codice_comp, 'materiale': materiale, 'misura': '', 'lunghezza_barra_mm': None,
                'matrice': '', 'punto_zero': '', 'indice_assorbimento': '', 'rullo': '',
                'impostazione_satinatrice': '', 'nr_pz_da_fare': nr_pz_da_fare, 'pezzi_fatti': pezzi_fatti,
                'saldo': saldo, 'pz_per_barra': None, 'nr_barre': None,
                # In Saldatura non si compilano mai (né servono) i parametri di
                # Scheda Lavorazione (matrice/punto zero/rullo/satinatura, tutti
                # per macchine di piega/taglio) — l'avviso lì è sempre falso
                # allarme, va mostrato solo dove quei parametri hanno senso.
                'nota': '' if 'sald' in nome_l else '⚠️ Parametri non ancora compilati in Parametri di Lavorazione',
            }
            righe_per_materiale.setdefault(materiale, []).append(riga)

    codici_materiale = list(righe_per_materiale.keys())
    giacenze = {g.codice: g.quantita for g in GiacenzaWood.query.filter(GiacenzaWood.codice.in_(codici_materiale)).all()} if codici_materiale else {}
    unita_misura = {a.codice: a.unita_misura for a in ArticoloApprovvigionamento.query.filter(ArticoloApprovvigionamento.codice.in_(codici_materiale)).all()} if codici_materiale else {}
    ordinato = {}
    if codici_materiale:
        for cod, qta_orig, qta_ric in (db.session.query(RigaOrdineAcquistoWood.codice,
                                                          RigaOrdineAcquistoWood.qta_originale,
                                                          RigaOrdineAcquistoWood.qta_ricevuta)
                                        .join(OrdineAcquistoWood)
                                        .filter(RigaOrdineAcquistoWood.codice.in_(codici_materiale),
                                                OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all()):
            residuo = (qta_orig or 0) - (qta_ric or 0)
            if residuo > 0:
                ordinato[cod] = ordinato.get(cod, 0) + residuo

    gruppi = []
    totale_pz = totale_fatti = 0
    for materiale, righe in sorted(righe_per_materiale.items()):
        ha_barra_gruppo = any(r['lunghezza_barra_mm'] for r in righe)
        if ha_barra_gruppo:
            nr_barre_tot = sum(r['nr_barre'] or 0 for r in righe)
            lunghezza_m = (next((r['lunghezza_barra_mm'] for r in righe if r['lunghezza_barra_mm']), 0) or 0) / 1000
            necessario_qta = round(nr_barre_tot * lunghezza_m, 3)
            um_default = 'mt'
        else:
            nr_barre_tot = None
            necessario_qta = sum(r['saldo'] for r in righe)  # senza distinta barra: quantità pezzi come proxy generico
            um_default = 'pz'
        um = unita_misura.get(materiale, um_default)
        disponibile_qta = giacenze.get(materiale, 0)
        if disponibile_qta >= necessario_qta:
            stato_materiale = {'stato': 'disponibile', 'label': '✅ OK Disponibile'}
        elif ordinato.get(materiale, 0) > 0:
            stato_materiale = {'stato': 'ordinato', 'label': '🟠 Ordinato'}
        else:
            stato_materiale = {'stato': 'da_ordinare', 'label': '🔴 Non disponibile / da ordinare'}
        gruppi.append({
            'materiale': materiale, 'righe': righe, 'nr_barre_totale': nr_barre_tot,
            'quantita_necessaria': necessario_qta, 'unita_misura': um, 'disponibile': disponibile_qta,
            'stato_materiale': stato_materiale, 'ha_barra': ha_barra_gruppo,
        })
        totale_pz += sum(r['nr_pz_da_fare'] for r in righe)
        totale_fatti += sum(r['pezzi_fatti'] for r in righe)

    # Miniatura di riferimento del prodotto finito (la più recente caricata in
    # Documentazione Articolo per questo codice) — mostrata in testa alla
    # Lista di Lavoro al posto della Data commessa, poco utile qui.
    foto = (FotoArticolo.query.filter_by(codice_articolo=o.codice_articolo)
            .order_by(FotoArticolo.caricato_il.desc()).first())

    return {
        'centro_id': centro.id, 'centro_nome': centro.nome,
        'op_codice': o.codice, 'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione or '',
        'commessa': o.commessa or '', 'cliente': o.cliente or '', 'stato': o.stato,
        'qta_pianificata': o.qta_pianificata,
        'data_inizio': o.data_inizio.strftime('%d/%m/%Y') if o.data_inizio else '',
        'numero_lista': _numero_lista_lavoro(o.codice, centro) if assegna_numero else None,
        'foto_id': foto.id if foto else None,
        'gruppi': gruppi, 'ha_barra': mostra_barra, 'ha_piega': mostra_piega,
        'ha_rullo': mostra_rullo, 'ha_satinatura': mostra_satinatura,
        'totale_pz': totale_pz, 'pz_effettuati': totale_fatti,
        'residuo_pz': max(totale_pz - totale_fatti, 0),
    }


@pp_bp.get('/liste-lavoro')
def pagina_liste_lavoro_index():
    """Se non specificato un centro, va sul primo disponibile (stesso principio del Monitor Macchina)."""
    from flask import redirect, url_for
    from models import get_macchine_monitor
    macchine = get_macchine_monitor()
    if not macchine:
        return render_template('monitor/nessuna_macchina.html', active='liste_lavoro',
            messaggio='Nessun centro di costo disponibile: configuralo prima in Centri di Costo e Ciclo di Lavoro.')
    return redirect(url_for('produzione_pp.pagina_liste_lavoro', cid=macchine[0]['id']))


@pp_bp.get('/liste-tagli')
def pagina_liste_tagli_legacy():
    """Vecchio indirizzo — redirect di cortesia verso la Lista Lavoro generalizzata (stesso centro se è una segatrice, altrimenti la prima disponibile)."""
    from flask import redirect, url_for
    from models import get_macchine_monitor
    macchine = get_macchine_monitor()
    sega = next((m for m in macchine if 'sega' in m['nome'].lower() or 'tagli' in m['nome'].lower()), None)
    if sega:
        return redirect(url_for('produzione_pp.pagina_liste_lavoro', cid=sega['id']))
    return redirect(url_for('produzione_pp.pagina_liste_lavoro_index'))


@pp_bp.get('/liste-lavoro/<int:cid>')
def pagina_liste_lavoro(cid):
    from models import get_macchine_monitor
    centro = CentroCostoWood.query.get_or_404(cid)
    macchine = get_macchine_monitor()
    # Centri interni attivi che ESISTONO (li vedi es. in Dichiarazione
    # Produzione) ma non compaiono qui perché nessun articolo ha ancora una
    # fase di Ciclo di Lavoro assegnata a loro — senza questo avviso sembra
    # un pezzo di programma mancante, mentre è solo un dato da configurare.
    id_mostrati = {m['id'] for m in macchine}
    centri_senza_ciclo = (CentroCostoWood.query
                          .filter_by(esterno=False, attivo=True)
                          .filter(~CentroCostoWood.id.in_(id_mostrati)).order_by(CentroCostoWood.nome).all()
                          ) if id_mostrati else []
    return render_template('produzione_pp/liste_lavoro.html', active='liste_lavoro',
        centro=centro, macchine=macchine, centri_senza_ciclo=centri_senza_ciclo)


@pp_bp.get('/api/liste-lavoro/<int:cid>')
def api_liste_lavoro(cid):
    """Riepilogo per commessa su QUESTO centro: totale / effettuati / saldo — solo per gli OP con almeno una riga in questo centro."""
    centro = CentroCostoWood.query.get_or_404(cid)
    ordini = (OrdineProduzione.query.filter(OrdineProduzione.stato != 'Chiuso CO')
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())
    risultato = []
    for o in ordini:
        dati = _lista_lavoro_op(o, centro)
        if not dati['gruppi']:
            continue
        risultato.append({
            'op_codice': dati['op_codice'], 'codice_articolo': dati['codice_articolo'],
            'descrizione': dati['descrizione'], 'commessa': dati['commessa'], 'stato': dati['stato'],
            'qta_pianificata': dati['qta_pianificata'],
            'totale_pz': dati['totale_pz'], 'pz_effettuati': dati['pz_effettuati'], 'residuo_pz': dati['residuo_pz'],
        })
    return jsonify(risultato)


@pp_bp.get('/ordini-produzione/<codice>/lista-lavoro/<int:cid>')
def pagina_lista_lavoro_op(codice, cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    return render_template('produzione_pp/lista_lavoro_stampa.html',
        dati=(_lista_lavoro_op(o, centro) if o else None), codice=codice, centro=centro)


@pp_bp.get('/api/ordini-produzione/<codice>/lista-lavoro/<int:cid>')
def api_lista_lavoro_op(codice, cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify({'errore': True, 'messaggio': 'Ordine di produzione non trovato'}), 404
    return jsonify(_lista_lavoro_op(o, centro))


@pp_bp.get('/dichiarazione-produzione')
def pagina_dichiarazione_produzione():
    return render_template('produzione_pp/dichiarazione_produzione.html', active='dichiarazione_produzione')


@pp_bp.get('/dichiarazione-produzione/app')
def pagina_dichiarazione_produzione_app():
    """
    Pagina AUTONOMA (nessuna sidebar/menu del programma) per il capo reparto
    — pensata per essere aperta su un tablet/PC in reparto a fine giornata,
    stesso stile visivo del Totem live a bordo macchina. Nessun accesso allo
    storico/correzioni da qui: solo dichiarare, come nella pagina completa.
    """
    return render_template('produzione_pp/dichiarazione_produzione_app.html')


@pp_bp.get('/api/dichiarazione-produzione/centri')
def api_dichiarazione_centri():
    """
    Tutti i centri di costo INTERNI (non esterno), non solo quelli già usati
    in un Ciclo di Lavoro come per il Monitor — qui il capo reparto deve
    poter scegliere il proprio centro anche se non è ancora stato assegnato
    a nessun articolo, altrimenti il menu risulterebbe vuoto o incompleto.
    """
    centri = (CentroCostoWood.query.filter_by(esterno=False, attivo=True)
              .order_by(CentroCostoWood.nome).all())
    return jsonify([{'id': c.id, 'nome': c.nome} for c in centri])


@pp_bp.get('/api/dichiarazione-produzione/diagnostica-fasi-op/<path:op_code>')
def api_diagnostica_fasi_op(op_code):
    """
    Diagnostica GREZZA per un OP — o per TUTTI gli OP di un articolo, se
    quello che viene cercato non è un codice OP ma un codice articolo (es.
    "T200" invece di "OP-2026-000004", facile confonderli: la tabella
    sopra li mostra fianco a fianco). Ogni EventoConsuntivoPP registrato
    con la sua 'fase' esatta (stringa così com'è nel database, senza
    nessuna elaborazione) — con l'OP di provenienza ben visibile quando ce
    n'è più di uno — più l'elenco di tutti i Centri di Costo (id, nome
    esatto). Serve per beccare sul fatto un caso in cui dichiarare su un
    centro sembra far salire 'Fatti' anche su un altro: il confronto
    carattere per carattere fra fase salvata e nome centro lo mostra subito.
    """
    cercato = op_code.strip()
    ordini = OrdineProduzione.query.filter_by(codice=cercato).all()
    cercato_per = 'op_code'
    if not ordini:
        ordini = OrdineProduzione.query.filter(
            db.func.upper(OrdineProduzione.codice_articolo) == cercato.upper()).all()
        cercato_per = 'codice_articolo'
    if not ordini:
        return jsonify(ok=False, error=f'Nessun OP trovato né come codice OP né come codice articolo per "{cercato}"'), 404

    codici_op = [o.codice for o in ordini]
    eventi = (EventoConsuntivoPP.query.filter(EventoConsuntivoPP.op_code.in_(codici_op))
              .order_by(EventoConsuntivoPP.timestamp_evento.desc()).all())
    centri = CentroCostoWood.query.order_by(CentroCostoWood.nome).all()
    return jsonify(ok=True, cercato=cercato, cercato_per=cercato_per,
        ordini=[{'op_code': o.codice, 'codice_articolo': o.codice_articolo,
                 'qta_buona': o.qta_buona, 'qta_pianificata': o.qta_pianificata, 'stato': o.stato} for o in ordini],
        eventi=[{'id': e.id, 'op_code': e.op_code, 'fase_esatta': e.fase, 'componente': e.componente,
                 'pezzi_buoni': e.pezzi_buoni, 'pezzi_scarto': e.pezzi_scarto,
                 'timestamp': e.timestamp_evento.strftime('%d/%m/%Y %H:%M:%S')} for e in eventi],
        centri=[{'id': c.id, 'nome_esatto': c.nome} for c in centri])


@pp_bp.get('/api/dichiarazione-produzione/reintegro-scarti')
def api_reintegro_scarti():
    """
    Elenco di TUTTI i codici (su qualunque centro di costo interno) dove è
    stato dichiarato dello scarto E mancano ancora pezzi buoni per
    completare il pianificato — cioè "ho scartato dei pezzi, li devo
    rifare". Il meccanismo per dichiararli è lo STESSO di sempre (dichiara
    di nuovo sulla stessa riga, in Dichiarazione Produzione) — questo
    endpoint serve solo a renderlo impossibile da perdere: senza, un
    reintegro da fare si confondeva nella lista generale di tutti gli OP
    aperti, indipendentemente dal perché mancava ancora qualcosa.
    """
    ordini = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())
    if not ordini:
        return jsonify([])

    centri_interni = {c.id: c for c in CentroCostoWood.query.filter_by(esterno=False).all()}
    mappa_distinta = _carica_mappa_distinta_base_wood()
    componenti_per_op = {o.id: _esplodi_componenti_op(o, mappa_distinta=mappa_distinta) for o in ordini}
    tutti_i_codici = {c['codice'] for lista in componenti_per_op.values() for c in lista}

    fasi_per_codice = {}
    if tutti_i_codici:
        for f in CicloLavoroWood.query.filter(CicloLavoroWood.codice.in_(tutti_i_codici)).all():
            if f.centro_costo_id in centri_interni:
                fasi_per_codice.setdefault(f.codice, []).append(f)

    codici_op = [o.codice for o in ordini]
    dati_per_riga = {}  # (op_code, componente, fase_lower) -> {buoni, scarto}
    if codici_op:
        for op_code, componente, fase, buoni, scarto in (
                db.session.query(EventoConsuntivoPP.op_code, EventoConsuntivoPP.componente,
                                  EventoConsuntivoPP.fase, db.func.sum(EventoConsuntivoPP.pezzi_buoni),
                                  db.func.sum(EventoConsuntivoPP.pezzi_scarto))
                .filter(EventoConsuntivoPP.op_code.in_(codici_op))
                .group_by(EventoConsuntivoPP.op_code, EventoConsuntivoPP.componente, EventoConsuntivoPP.fase).all()):
            dati_per_riga[(op_code, componente, fase.strip().lower())] = {
                'buoni': buoni or 0, 'scarto': scarto or 0}

    risultato = []
    for o in ordini:
        for comp in componenti_per_op[o.id]:
            codice_comp = comp['codice']
            componente_finale = (codice_comp == o.codice_articolo)
            componente_param = None if componente_finale else codice_comp
            for ciclo in fasi_per_codice.get(codice_comp, []):
                centro = centri_interni.get(ciclo.centro_costo_id)
                if not centro:
                    continue
                chiave = (o.codice, componente_param, centro.nome.strip().lower())
                dati = dati_per_riga.get(chiave)
                if not dati or dati['scarto'] <= 0:
                    continue  # nessuno scarto dichiarato qui: non è un reintegro
                qta_necessaria = round((o.qta_pianificata or 0) * comp['moltiplicatore'], 4)
                saldo = max(qta_necessaria - dati['buoni'], 0)
                if saldo <= 0:
                    continue  # lo scarto c'è stato ma è già stato rifatto: niente da reintegrare
                risultato.append({
                    'op_code': o.codice, 'commessa': o.commessa or '', 'codice_articolo': o.codice_articolo,
                    'componente': componente_param, 'codice_lavorato': codice_comp,
                    'centro_id': centro.id, 'centro_nome': centro.nome,
                    'qta_necessaria': qta_necessaria, 'buoni': dati['buoni'], 'scarto_totale': dati['scarto'],
                    'saldo_da_reintegrare': saldo,
                })
    risultato.sort(key=lambda r: -r['saldo_da_reintegrare'])
    return jsonify(risultato)


@pp_bp.get('/api/dichiarazione-produzione/<int:cid>/op-aperti')
def api_dichiarazione_op_aperti(cid):
    """
    Tutti i CODICI dichiarabili su questo centro di costo, raggruppati per
    OP/commessa — non solo il prodotto finito dell'OP (limite del vecchio
    comportamento: un centro attraversato solo da un componente intermedio,
    es. Segatrice prima dell'assemblaggio finale, risultava sempre vuoto).
    Stesso motore di risoluzione già usato in Monitor/LIVE: esplode la
    distinta base di ogni OP aperto e tiene solo i codici il cui Ciclo di
    Lavoro passa da QUESTO centro, con il saldo calcolato sui consuntivi già
    dichiarati per quel componente specifico.
    """
    centro = CentroCostoWood.query.get_or_404(cid)
    ordini = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())
    if not ordini:
        return jsonify([])

    mappa_distinta = _carica_mappa_distinta_base_wood()
    componenti_per_op = {o.id: _esplodi_componenti_op(o, mappa_distinta=mappa_distinta) for o in ordini}
    tutti_i_codici = {c['codice'] for lista in componenti_per_op.values() for c in lista}
    fasi_per_codice = {}
    if tutti_i_codici:
        for f in (CicloLavoroWood.query.filter(CicloLavoroWood.codice.in_(tutti_i_codici))
                  .filter_by(centro_costo_id=centro.id).all()):
            fasi_per_codice.setdefault(f.codice, []).append(f)

    codici_op = [o.codice for o in ordini]
    fatti_per_componente = {}
    if codici_op:
        for op_code, componente, tot in (db.session.query(
                EventoConsuntivoPP.op_code, EventoConsuntivoPP.componente,
                db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                .filter(EventoConsuntivoPP.op_code.in_(codici_op),
                        db.func.lower(EventoConsuntivoPP.fase) == centro.nome.lower())
                .group_by(EventoConsuntivoPP.op_code, EventoConsuntivoPP.componente).all()):
            fatti_per_componente[(op_code, componente)] = tot or 0

    # Descrizione di ogni codice dichiarabile — ArticoloML (magazzino
    # condiviso MasterLogistic) prima, riserva locale (da import
    # Zucchetti/DESCOM) solo per i codici che ArticoloML non conosce.
    descrizione_per_codice = {}
    if tutti_i_codici:
        try:
            for a in ArticoloML.query.filter(ArticoloML.sku.in_(tutti_i_codici)).all():
                if a.descrizione:
                    descrizione_per_codice[a.sku] = a.descrizione
        except Exception:
            db.session.rollback()
        codici_senza_descr = tutti_i_codici - set(descrizione_per_codice.keys())
        if codici_senza_descr:
            for d in DescrizioneCodiceWood.query.filter(DescrizioneCodiceWood.codice.in_(codici_senza_descr)).all():
                if d.descrizione:
                    descrizione_per_codice[d.codice] = d.descrizione

    risultato = []
    for o in ordini:
        gruppo_componenti = []
        for comp in componenti_per_op[o.id]:
            codice_comp = comp['codice']
            if codice_comp not in fasi_per_codice:
                continue  # questo codice non passa da questo centro
            # 'componente_finale' qui decide SOLO cosa mandare come
            # 'componente' nella creazione (None = storicamente sempre
            # stato "stesso codice dell'articolo", per compatibilità con
            # come viene salvato/cercato EventoConsuntivoPP — MAI cambiarlo
            # in base alla fase, altrimenti la ricerca di 'fatti già
            # dichiarati' sopra smette di trovare le righe già salvate).
            # 'es_ultima_fase' è SOLO per l'etichetta mostrata all'operaio:
            # dice se questa riga è davvero l'ultima fase del ciclo (quella
            # che fa avanzare l'OP) o solo una fase intermedia con lo
            # stesso codice (es. Sega, quando poi c'è ancora Saldatura) —
            # il bug per cui dichiarare Sega evadeva l'intero OP è
            # corretto in _registra_evento_consuntivo (avanza_op), non qui.
            componente_finale = (codice_comp == o.codice_articolo)
            componente_param = None if componente_finale else codice_comp
            es_ultima_fase = componente_finale and _e_ultima_fase_del_ciclo(codice_comp, centro.nome)
            qta_necessaria = round((o.qta_pianificata or 0) * comp['moltiplicatore'], 4)
            fatti = fatti_per_componente.get((o.codice, componente_param), 0)
            saldo = max(qta_necessaria - fatti, 0)
            if saldo <= 0:
                continue  # già completato su questo centro: non dichiarabile
            gruppo_componenti.append({
                'componente': componente_param, 'componente_finale': es_ultima_fase,
                'codice_lavorato': codice_comp, 'descrizione': descrizione_per_codice.get(codice_comp, ''),
                'qta_necessaria': qta_necessaria, 'fatti': fatti, 'saldo': saldo,
            })
        if not gruppo_componenti:
            continue
        risultato.append({
            'codice': o.codice, 'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione,
            'commessa': o.commessa, 'priorita': o.priorita,
            'qta_pianificata': o.qta_pianificata, 'qta_buona': o.qta_buona, 'qta_scarto': o.qta_scarto,
            'saldo': max((o.qta_pianificata or 0) - (o.qta_buona or 0), 0),
            'componenti': gruppo_componenti,
        })
    return jsonify(risultato)


@pp_bp.post('/api/dichiarazione-produzione')
def api_dichiarazione_crea():
    """Il capo reparto dichiara: crea SOLO, nessun accesso a modifica/storico.
    'componente' opzionale: quale codice della distinta base si sta
    dichiarando (None/assente = prodotto finito dell'OP).
    'consumi' opzionale: {codice: quantità} — se il capo ha corretto le
    quantità nella maschera di anteprima, si scarica quello invece dello
    standard di distinta (vedi _registra_evento_consuntivo).

    Blocco eccedenza produzione: se i pezzi buoni dichiarati (sommati a
    quelli già fatti su questa riga) superano la quantità pianificata di
    oltre SOGLIA_ECCEDENZA_PCT_DICHIARAZIONE, la dichiarazione viene
    RIFIUTATA — un'eccedenza così grande quasi certamente segnala scarti
    o un errore, non va lasciata passare in automatico. Sblocco solo con
    un Modulo Non Conformità 8D (semplificato) APPROVATO da Angelo per
    ESATTAMENTE questa quantità — vedi ModuloNonConformita8D."""
    d = request.get_json(force=True)
    o = OrdineProduzione.query.filter_by(codice=(d.get('op_code') or '').strip()).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    centro = CentroCostoWood.query.get(d.get('centro_id'))
    if not centro:
        return jsonify(ok=False, error='Centro di costo non trovato'), 404
    componente = (d.get('componente') or '').strip() or None
    try:
        good = _integer(d.get('pezzi_buoni', 0), 'Pezzi buoni')
        scrap = _integer(d.get('pezzi_scarto', 0), 'Pezzi scarto')
        tempo = _integer(d.get('tempo_minuti', 0), 'Tempo')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if good <= 0 and scrap <= 0:
        return jsonify(ok=False, error='Dichiara almeno un pezzo buono o di scarto'), 400

    if good > 0:
        blocco = _verifica_eccedenza_dichiarazione(o, centro, componente, good)
        if blocco:
            return jsonify(blocco), 409

    consumi_override = None
    if 'consumi' in d and isinstance(d['consumi'], dict):
        consumi_override = {}
        for cod, qta in d['consumi'].items():
            try:
                qta_f = float(qta)
            except (TypeError, ValueError):
                continue
            if qta_f > 0:
                consumi_override[cod] = qta_f
    event_id = str(uuid.uuid4())
    avviso_magazzino = _registra_evento_consuntivo(o, centro.nome, datetime.utcnow(), good, scrap, tempo, event_id,
                                                     componente=componente, consumi_override=consumi_override)

    # Se questa dichiarazione era autorizzata da un 8D approvato, lo consuma:
    # non autorizza più nessuna dichiarazione futura, un'eccedenza successiva
    # richiede un nuovo modulo.
    if good > 0:
        modulo = (ModuloNonConformita8D.query
                  .filter_by(op_code=o.codice, centro_costo_id=centro.id, componente=componente,
                             stato='APPROVATO', qta_richiesta=good).order_by(ModuloNonConformita8D.id.desc()).first())
        if modulo:
            modulo.stato = 'CONSUMATO'
            modulo.event_id_consumato = event_id

    db.session.commit()
    # La dichiarazione è comunque registrata (mai bloccata da un problema di
    # magazzino) — ma se lo scarico/carico giacenza è fallito, l'operatore
    # deve saperlo SUBITO invece che scoprirlo da una giacenza sbagliata
    # settimane dopo: 'avviso' non blocca nulla, è solo visibile.
    return jsonify(ok=True, event_id=event_id, ordine=_ordine(o), avviso_magazzino=avviso_magazzino)


SOGLIA_ECCEDENZA_PCT_DICHIARAZIONE = 20  # oltre questa % sopra il pianificato, blocco automatico


def _qta_necessaria_riga(o, componente):
    """Quantità pianificata per QUESTA riga (prodotto finito o componente
    specifico) — moltiplicatore di distinta × qta_pianificata dell'OP.
    None se il componente non è riconosciuto nella distinta (non blocca:
    senza un pianificato di riferimento non si può giudicare un'eccedenza)."""
    try:
        comp_list = _esplodi_componenti_op(o)
    except Exception:
        return None
    target = componente or o.codice_articolo
    match = next((c for c in comp_list if c['codice'] == target), None)
    if not match:
        return None
    return round((o.qta_pianificata or 0) * match['moltiplicatore'], 4)


def _verifica_eccedenza_dichiarazione(o, centro, componente, good):
    """
    Controlla se GOOD pezzi (sommati a quelli già dichiarati su questa
    riga) eccedono il pianificato di oltre SOGLIA_ECCEDENZA_PCT_DICHIARAZIONE.
    Ritorna None se si può procedere, altrimenti il dict di errore da
    restituire al chiamante (già pronto per jsonify).
    """
    qta_necessaria = _qta_necessaria_riga(o, componente)
    if not qta_necessaria or qta_necessaria <= 0:
        return None  # nessun pianificato di riferimento: non blocchiamo alla cieca

    gia_fatti = (db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                 .filter(EventoConsuntivoPP.op_code == o.codice,
                         db.func.lower(EventoConsuntivoPP.fase) == centro.nome.strip().lower(),
                         EventoConsuntivoPP.componente == componente if componente
                         else EventoConsuntivoPP.componente.is_(None))
                 .scalar()) or 0
    nuovo_totale = gia_fatti + good
    eccedenza_pct = round(max(0, (nuovo_totale - qta_necessaria) / qta_necessaria * 100), 1)
    if eccedenza_pct <= SOGLIA_ECCEDENZA_PCT_DICHIARAZIONE:
        return None

    # Un 8D approvato per ESATTAMENTE questa quantità la sblocca UNA volta.
    modulo = (ModuloNonConformita8D.query
              .filter_by(op_code=o.codice, centro_costo_id=centro.id, componente=componente,
                         stato='APPROVATO', qta_richiesta=good).order_by(ModuloNonConformita8D.id.desc()).first())
    if modulo:
        return None

    return {
        'ok': False,
        'error': (f'Dichiarazione bloccata: {good} pezzi (totale {nuovo_totale} su questa riga) '
                  f'superano il pianificato ({qta_necessaria}) del {eccedenza_pct}%, oltre la soglia '
                  f'del {SOGLIA_ECCEDENZA_PCT_DICHIARAZIONE}% — probabile scarto/errore da segnalare. '
                  f'Serve un Modulo Non Conformità 8D approvato da Angelo per procedere.'),
        'richiede_8d': True,
        'qta_necessaria': qta_necessaria, 'gia_fatti': gia_fatti,
        'qta_richiesta': good, 'eccedenza_pct': eccedenza_pct,
    }


@pp_bp.post('/api/non-conformita-8d')
def api_non_conformita_8d_crea():
    """Apre un Modulo Non Conformità 8D (semplificato) per un'eccedenza di
    produzione bloccata — va in coda per l'autorizzazione di Angelo."""
    d = request.get_json(force=True)
    o = OrdineProduzione.query.filter_by(codice=(d.get('op_code') or '').strip()).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    centro = CentroCostoWood.query.get(d.get('centro_id'))
    if not centro:
        return jsonify(ok=False, error='Centro di costo non trovato'), 404
    componente = (d.get('componente') or '').strip() or None
    try:
        good = _integer(d.get('pezzi_buoni', 0), 'Pezzi buoni')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if good <= 0:
        return jsonify(ok=False, error='Quantità non valida'), 400

    qta_necessaria = _qta_necessaria_riga(o, componente)
    if not qta_necessaria or qta_necessaria <= 0:
        return jsonify(ok=False, error='Riga non riconosciuta nella distinta: nessun pianificato di riferimento'), 400
    gia_fatti = (db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                 .filter(EventoConsuntivoPP.op_code == o.codice,
                         db.func.lower(EventoConsuntivoPP.fase) == centro.nome.strip().lower(),
                         EventoConsuntivoPP.componente == componente if componente
                         else EventoConsuntivoPP.componente.is_(None))
                 .scalar()) or 0
    eccedenza_pct = round(max(0, (gia_fatti + good - qta_necessaria) / qta_necessaria * 100), 1)

    modulo = ModuloNonConformita8D(
        op_code=o.codice, centro_costo_id=centro.id, componente=componente,
        qta_pianificata_riga=qta_necessaria, qta_gia_fatta=gia_fatti, qta_richiesta=good,
        eccedenza_pct=eccedenza_pct,
        descrizione_problema=(d.get('descrizione_problema') or '').strip(),
        causa_probabile=(d.get('causa_probabile') or '').strip(),
        azione_immediata=(d.get('azione_immediata') or '').strip(),
    )
    db.session.add(modulo)
    db.session.commit()
    return jsonify(ok=True, id=modulo.id)


@pp_bp.get('/api/non-conformita-8d')
def api_non_conformita_8d_lista():
    """Coda per Angelo — SOLO PIN capo. Di default solo IN_ATTESA; ?tutti=1
    per vedere anche le decise (storico)."""
    if not _verifica_pin_capo(request.args):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    q = ModuloNonConformita8D.query
    if request.args.get('tutti') != '1':
        q = q.filter_by(stato='IN_ATTESA')
    moduli = q.order_by(ModuloNonConformita8D.creato_il.desc()).limit(100).all()
    op_codes = {m.op_code for m in moduli}
    articolo_per_op = {o.codice: o.codice_articolo for o in
                       OrdineProduzione.query.filter(OrdineProduzione.codice.in_(op_codes)).all()}
    centri = {c.id: c.nome for c in CentroCostoWood.query.filter(
        CentroCostoWood.id.in_({m.centro_costo_id for m in moduli})).all()}
    return jsonify(ok=True, moduli=[{
        'id': m.id, 'op_code': m.op_code, 'codice_articolo': articolo_per_op.get(m.op_code, '—'),
        'centro_nome': centri.get(m.centro_costo_id, '—'), 'componente': m.componente,
        'qta_pianificata_riga': m.qta_pianificata_riga, 'qta_gia_fatta': m.qta_gia_fatta,
        'qta_richiesta': m.qta_richiesta, 'eccedenza_pct': m.eccedenza_pct,
        'descrizione_problema': m.descrizione_problema, 'causa_probabile': m.causa_probabile,
        'azione_immediata': m.azione_immediata, 'stato': m.stato,
        'creato_il': m.creato_il.strftime('%d/%m/%Y %H:%M'),
        'deciso_il': m.deciso_il.strftime('%d/%m/%Y %H:%M') if m.deciso_il else None,
        'nota_decisione': m.nota_decisione,
    } for m in moduli])


@pp_bp.post('/api/non-conformita-8d/<int:mid>/decidi')
def api_non_conformita_8d_decidi(mid):
    """Angelo approva o respinge — SOLO PIN capo. 'approva': true/false,
    'nota' opzionale."""
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    m = ModuloNonConformita8D.query.get_or_404(mid)
    if m.stato != 'IN_ATTESA':
        return jsonify(ok=False, error=f'Modulo già deciso ({m.stato})'), 409
    m.stato = 'APPROVATO' if d.get('approva') else 'RESPINTO'
    m.nota_decisione = (d.get('nota') or '').strip()
    m.deciso_il = datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True, stato=m.stato)


@pp_bp.get('/api/dichiarazione-produzione/anteprima')
def api_dichiarazione_anteprima():
    """
    Maschera di anteprima pre-conferma (stesso principio di "Evasione
    articoli composti" di Zucchetti): cosa si CARICA a magazzino (il
    prodotto — finito o semilavorato, secondo il componente dichiarato,
    SOLO i pezzi buoni) e cosa si SCARICA (i suoi componenti diretti,
    quantità standard di distinta PER BUONI+SCARTO — il materiale si
    consuma per ogni pezzo tagliato, anche quello poi scartato) —
    proposti come default, modificabili prima di confermare.
    """
    op_code = (request.args.get('op_code') or '').strip()
    componente = (request.args.get('componente') or '').strip() or None
    centro_id = request.args.get('centro_id')
    try:
        good = _integer(request.args.get('pezzi_buoni', 0), 'Pezzi buoni')
        scrap = _integer(request.args.get('pezzi_scarto', 0), 'Pezzi scarto')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    o = OrdineProduzione.query.filter_by(codice=op_code).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    if good <= 0 and scrap <= 0:
        return jsonify(ok=True, carica=None, componenti=[])

    componente_finale = not componente or componente == o.codice_articolo
    codice_caricato = o.codice_articolo if componente_finale else componente

    # Se questa NON è la prima fase del Ciclo di Lavoro di questo codice
    # (es. un pezzo già piegato che passa ora in foratura), il magazzino non
    # verrà toccato — vedi _e_prima_fase_del_ciclo — quindi l'anteprima deve
    # dirlo chiaramente invece di mostrare un carico/scarico che poi non
    # avverrà davvero: mostrarlo comunque confonde e allarma inutilmente.
    centro = CentroCostoWood.query.get(centro_id) if centro_id else None
    if centro and not _e_prima_fase_del_ciclo(codice_caricato, centro.nome):
        return jsonify(ok=True, carica=None, componenti=[], gia_caricato=True,
            messaggio=(f'"{codice_caricato}" è già stato caricato a magazzino alla prima fase del suo '
                       f'ciclo di lavoro — questa è una fase successiva sullo STESSO pezzo: nessun materiale '
                       f'verrà scaricato né ricaricato, si registrano solo i pezzi fatti e il tempo.'))

    # Scarico basato su BUONI+SCARTO: il materiale grezzo si consuma per
    # ogni pezzo tagliato, anche quello poi scartato — non solo per i buoni.
    consumi, contestuali = _calcola_consumi_standard(o, componente_finale, componente, good + scrap)

    tutti_codici = {codice_caricato} | set(consumi.keys()) | set(contestuali.keys())
    descr_map = {}
    try:
        for a in ArticoloML.query.filter(ArticoloML.sku.in_(tutti_codici)).all():
            if a.descrizione:
                descr_map[a.sku] = a.descrizione
    except Exception:
        db.session.rollback()
    codici_senza = tutti_codici - set(descr_map.keys())
    if codici_senza:
        for dsc in DescrizioneCodiceWood.query.filter(DescrizioneCodiceWood.codice.in_(codici_senza)).all():
            if dsc.descrizione:
                descr_map[dsc.codice] = dsc.descrizione

    # Il CARICO a magazzino resta SOLO sui pezzi buoni — uno scarto non
    # diventa mai scorta utilizzabile. Se good=0 (solo scarto dichiarato),
    # niente da caricare. I 'contestuali' (isole one-piece-flow, es.
    # FRONTE+RETRO) si mostrano a parte: verranno caricati E scaricati in
    # automatico alla conferma, non sono uno scarico da stock preesistente.
    return jsonify(ok=True,
        carica=({'codice': codice_caricato, 'descrizione': descr_map.get(codice_caricato, ''), 'quantita': good}
                if good > 0 else None),
        componenti=[{'codice': cod, 'descrizione': descr_map.get(cod, ''), 'quantita': round(qta, 4)}
                    for cod, qta in consumi.items()],
        componenti_contestuali=[{'codice': cod, 'descrizione': descr_map.get(cod, ''), 'quantita': round(qta, 4)}
                                 for cod, qta in contestuali.items()])


def _verifica_pin_capo(d):
    pin = (d.get('pin') or '').strip()
    return pin and pin == current_app.config.get('CAPO_PIN', '')


# PIN Direzione per l'approvazione delle Dichiarazioni di Produzione — fisso,
# separato dal PIN capo (che serve solo per storico/correzioni). La
# dichiarazione resta comunque registrata subito in OP/giacenza appena il
# capo la dichiara: l'approvazione Direzione è un controllo successivo, non
# un blocco.
PIN_DIREZIONE = os.environ.get('PIN_DIREZIONE', '1234')

def _verifica_pin_direzione(d):
    pin = (d.get('pin') or '').strip()
    return pin == PIN_DIREZIONE


@pp_bp.get('/api/dichiarazione-produzione/verifica-pin')
def api_dichiarazione_verifica_pin():
    if not _verifica_pin_capo(request.args):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    return jsonify(ok=True)


@pp_bp.get('/api/dichiarazione-produzione/pin-interno')
def api_dichiarazione_pin_interno():
    """Area Capo e Area Direzione, in questa pagina, sono di fatto riservate
    a chi già ha accesso alla pagina stessa (Angelo): niente più sblocco
    manuale col PIN, la sezione resta sempre aperta. Il PIN continua però a
    essere richiesto e verificato lato server su ogni chiamata — qui viene
    solo fornito al frontend per auto-popolare le richieste successive."""
    return jsonify(ok=True, capo=current_app.config.get('CAPO_PIN', ''), direzione=PIN_DIREZIONE)


@pp_bp.get('/api/dichiarazione-produzione/<int:cid>/storico')
def api_dichiarazione_storico(cid):
    """Visibile SOLO al capo — richiede il PIN come query param."""
    if not _verifica_pin_capo(request.args):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    centro = CentroCostoWood.query.get_or_404(cid)
    oggi_str = datetime.utcnow().strftime('%Y-%m-%d')
    data_da_str = request.args.get('data_da') or request.args.get('data') or oggi_str
    data_a_str = request.args.get('data_a') or data_da_str
    try:
        giorno_da = datetime.strptime(data_da_str, '%Y-%m-%d').date()
        giorno_a = datetime.strptime(data_a_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, error='Data non valida'), 400
    eventi = (EventoConsuntivoPP.query
              .filter(db.func.lower(EventoConsuntivoPP.fase) == centro.nome.lower(),
                      db.func.date(EventoConsuntivoPP.timestamp_evento) >= giorno_da,
                      db.func.date(EventoConsuntivoPP.timestamp_evento) <= giorno_a)
              .order_by(EventoConsuntivoPP.timestamp_evento.desc()).all())
    return jsonify(ok=True, eventi=[{
        'id': e.id, 'event_id': e.event_id, 'op_code': e.op_code, 'componente': e.componente,
        'timestamp': e.timestamp_evento.strftime('%d/%m/%Y %H:%M'),
        'pezzi_buoni': e.pezzi_buoni, 'pezzi_scarto': e.pezzi_scarto, 'tempo_minuti': e.tempo_minuti,
        'approvato_direzione': e.approvato_direzione,
        'approvato_il': e.approvato_il.strftime('%d/%m/%Y %H:%M') if e.approvato_il else None,
    } for e in eventi])


@pp_bp.post('/api/dichiarazione-produzione/ordini/<codice>/chiudi-forzato')
def api_chiudi_commessa_forzato(codice):
    """
    Chiusura DELIBERATA di una commessa sotto target (tolleranza commerciale)
    — riservata alla Direzione (stesso PIN). A differenza della chiusura
    tecnica automatica (che scatta da sola quando qta_buona raggiunge il
    pianificato), questa serve per i casi in cui si accetta di consegnare
    meno del previsto: se lo scostamento supera il 3%, genera comunque un
    avviso — la chiusura avviene lo stesso, l'avviso è solo un controllo
    successivo, mai un blocco.
    """
    d = request.get_json(force=True)
    if not _verifica_pin_direzione(d):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    if o.stato == 'Tecnicamente completato':
        return jsonify(ok=False, error='Commessa già chiusa'), 400

    o.stato, o.data_completamento = 'Tecnicamente completato', datetime.utcnow()
    _audit(o, 'CHIUSURA_FORZATA_DIREZIONE', f'chiusa sotto target: qta_buona={o.qta_buona}/{o.qta_pianificata}')

    avviso_creato = False
    if o.qta_pianificata:
        pct_sotto = (o.qta_pianificata - o.qta_buona) / o.qta_pianificata * 100
        if pct_sotto > 3:
            db.session.add(AvvisoScostamentoWood(
                op_code=o.codice, codice_articolo=o.codice_articolo, tipo='SOTTO_PRODUZIONE',
                qta_pianificata=o.qta_pianificata, qta_buona=o.qta_buona, percentuale=round(pct_sotto, 1)))
            avviso_creato = True
    db.session.commit()
    return jsonify(ok=True, avviso_creato=avviso_creato)


@pp_bp.get('/api/dichiarazione-produzione/avvisi-scostamento')
def api_avvisi_scostamento():
    """Avvisi di scostamento quantità non ancora letti — richiede il PIN Direzione."""
    if not _verifica_pin_direzione(request.args):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    avvisi = (AvvisoScostamentoWood.query.filter_by(letto=False)
              .order_by(AvvisoScostamentoWood.creato_il.desc()).all())
    return jsonify(ok=True, avvisi=[{
        'id': a.id, 'op_code': a.op_code, 'codice_articolo': a.codice_articolo, 'tipo': a.tipo,
        'qta_pianificata': a.qta_pianificata, 'qta_buona': a.qta_buona, 'percentuale': a.percentuale,
        'creato_il': a.creato_il.strftime('%d/%m/%Y %H:%M'),
    } for a in avvisi])


@pp_bp.post('/api/dichiarazione-produzione/avvisi-scostamento/<int:aid>/letto')
def api_avviso_scostamento_letto(aid):
    d = request.get_json(force=True)
    if not _verifica_pin_direzione(d):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    a = AvvisoScostamentoWood.query.get_or_404(aid)
    a.letto = True
    db.session.commit()
    return jsonify(ok=True)


@pp_bp.get('/api/dichiarazione-produzione/verifica-pin-direzione')
def api_dichiarazione_verifica_pin_direzione():
    if not _verifica_pin_direzione(request.args):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    return jsonify(ok=True)


@pp_bp.get('/api/dichiarazione-produzione/approvazioni')
def api_dichiarazione_approvazioni():
    """Dichiarazioni NON ancora approvate dalla Direzione, su tutti i centri
    — richiede il PIN Direzione. Sono comunque già registrate/valide in
    OP e giacenza: questa è solo la coda di controllo successivo."""
    if not _verifica_pin_direzione(request.args):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    eventi = (EventoConsuntivoPP.query.filter_by(approvato_direzione=False)
              .order_by(EventoConsuntivoPP.timestamp_evento.desc()).limit(200).all())
    # Codice articolo: preso dall'OP collegato, così la Direzione vede COSA
    # sta approvando (prima si vedeva solo l'OP, non il prodotto). Una sola
    # query per tutti gli OP coinvolti, non una per riga.
    op_codes = {e.op_code for e in eventi}
    articolo_per_op = {o.codice: o.codice_articolo for o in
                        OrdineProduzione.query.filter(OrdineProduzione.codice.in_(op_codes)).all()}
    return jsonify(ok=True, eventi=[{
        'id': e.id, 'event_id': e.event_id, 'op_code': e.op_code, 'fase': e.fase, 'componente': e.componente,
        'codice_articolo': articolo_per_op.get(e.op_code, '—'),
        'timestamp': e.timestamp_evento.strftime('%d/%m/%Y %H:%M'),
        'pezzi_buoni': e.pezzi_buoni, 'pezzi_scarto': e.pezzi_scarto, 'tempo_minuti': e.tempo_minuti,
        'operatore': e.operatore,
    } for e in eventi])


@pp_bp.post('/api/dichiarazione-produzione/eventi/<int:eid>/approva')
def api_dichiarazione_approva(eid):
    d = request.get_json(force=True)
    if not _verifica_pin_direzione(d):
        return jsonify(ok=False, error='PIN Direzione non valido'), 403
    e = EventoConsuntivoPP.query.get_or_404(eid)
    e.approvato_direzione = True
    e.approvato_il = datetime.utcnow()
    db.session.add(AuditPP(op_code=e.op_code, event_id=e.event_id, azione='APPROVAZIONE_DIREZIONE',
                            dettaglio=f'dichiarazione {e.event_id} approvata dalla Direzione'))
    # Aggancio Storico Produzione Kanban: questa approvazione è ESATTAMENTE
    # la stessa condizione che fa salire "Grezzo IW" in Magazzino
    # (componente NULL, fase Saldatura, approvato dalla Direzione — vedi
    # _calcola_campi_giacenza) — quindi lo Storico Produzione del Kanban
    # deve contarla nello stesso istante, non restare fermo ai soli rientri
    # DDT dalla verniciatura.
    if e.componente is None and 'sald' in (e.fase or '').strip().lower() and e.pezzi_buoni > 0:
        o = OrdineProduzione.query.filter_by(codice=e.op_code).first()
        if o:
            p = (KanbanProdotto.query
                 .filter(db.func.upper(KanbanProdotto.prodotto).like(f'{(o.codice_articolo or "").upper()}%'))
                 .first())
            if p:
                storico_aggiungi_auto(p.id, e.pezzi_buoni)
    db.session.commit()
    return jsonify(ok=True)


def _storna_evento_consuntivo(e, o):
    """
    Nucleo dello storno di UNA dichiarazione — condiviso dallo storno
    singolo (api_dichiarazione_annulla) e dallo storno di massa per periodo
    (api_dichiarazione_annulla_periodo). Non fa commit: il chiamante decide
    quando. Solleva eccezione se il ripristino giacenza fallisce — il
    chiamante deve rollback, mai lasciare uno storno a metà.
    """
    componente_finale = not e.componente or e.componente == o.codice_articolo
    codice_lavorato = o.codice_articolo if componente_finale else e.componente
    # Stesso bug corretto in _registra_evento_consuntivo: solo l'identità di
    # codice non basta per sapere se questa dichiarazione aveva fatto
    # avanzare l'OP — doveva ANCHE essere l'ultima fase del ciclo. Stornare
    # con la sola identità di codice toglierebbe qta_buona anche da una
    # dichiarazione intermedia che (dopo la correzione) non l'aveva mai
    # aggiunta, portando l'OP sotto zero o a numeri sbagliati.
    avanza_op = componente_finale and _e_ultima_fase_del_ciclo(codice_lavorato, e.fase)

    o.tempo_consuntivo_minuti = max((o.tempo_consuntivo_minuti or 0) - e.tempo_minuti, 0)
    if avanza_op:
        o.qta_buona = max((o.qta_buona or 0) - e.pezzi_buoni, 0)
        o.qta_scarto = max((o.qta_scarto or 0) - e.pezzi_scarto, 0)
        if o.stato == 'Tecnicamente completato':
            o.stato = 'In esecuzione'
    _audit(o, 'ANNULLO_CONSUNTIVO', f'storno evento {e.event_id}: componente={codice_lavorato}; '
           f'buoni={e.pezzi_buoni}; scarto={e.pezzi_scarto}; minuti={e.tempo_minuti}')

    prima_fase_originale = _e_prima_fase_del_ciclo(codice_lavorato, e.fase)
    # Stessa esclusione della dichiarazione originale: se questo evento era
    # il completamento del CODICE PADRE (avanza_op), il carico a
    # GiacenzaWood del prodotto finito non era mai avvenuto (vedi
    # carica_prodotto_finito in _registra_evento_consuntivo) — lo storno non
    # deve inventarsi uno scarico di qualcosa che non è mai stato caricato.
    carica_prodotto_finito = not (codice_lavorato == o.codice_articolo and avanza_op)
    qta_tagliata_originale = e.pezzi_buoni + e.pezzi_scarto
    if qta_tagliata_originale > 0 and prima_fase_originale:
        # Stessa identica logica di esplosione usata per dichiarare —
        # fondamentale che coincidano: se dichiaro consumando SOLO il
        # semilavorato (perché ha un ciclo proprio) ma stornassi
        # esplodendo fino alle materie prime, ripristinerei i codici
        # sbagliati, lasciando il magazzino disallineato. E se la
        # dichiarazione originale era una fase SUCCESSIVA alla prima
        # (non aveva toccato il magazzino — vedi _e_prima_fase_del_ciclo
        # nella dichiarazione), lo storno non deve inventarsi un
        # ripristino di qualcosa che non era mai stato scaricato.
        # Buoni+scarto: il materiale scaricato in dichiarazione copriva
        # ANCHE i pezzi di scarto (consumano ferro come quelli buoni),
        # quindi lo storno deve ripristinare la stessa quantità totale.
        # 'contestuali' (isole one-piece-flow) non richiedono nessun
        # ripristino: alla dichiarazione originale erano stati caricati E
        # scaricati nello stesso istante (net zero sulla giacenza) — non
        # c'è nessuno stock fantasma da restituire, lo storno riguarda
        # solo i 'consumi' normali (presunto stock preesistente).
        consumi, _contestuali_storno = _calcola_consumi_standard(o, componente_finale, e.componente, qta_tagliata_originale)
        for cod, qta_consumata in consumi.items():
            if qta_consumata:
                _registra_movimento_giacenza(cod, qta_consumata, 'rettifica_import',
                                              riferimento=o.codice, note=f'STORNO consuntivo {e.event_id}')
        if carica_prodotto_finito and e.pezzi_buoni > 0:
            _registra_movimento_giacenza(codice_lavorato, -e.pezzi_buoni, 'rettifica_import',
                                          riferimento=o.codice, note=f'STORNO consuntivo {e.event_id}')

    op_code_evento, fase_evento = e.op_code, e.fase
    db.session.delete(e)

    # Storno anche l'Ordine di Lavoro (NumeroListaLavoroWood) — il
    # documento numerato che Angelo manda agli operai, generato PRIMA che
    # eseguano il lavoro. Se dopo aver tolto questa dichiarazione non
    # rimane più NESSUNA dichiarazione per questo OP su questo centro, la
    # lista di lavoro non riflette più niente di realmente eseguito: va
    # tolta anche lei, non solo la dichiarazione. Se invece restano altre
    # dichiarazioni sullo stesso OP+centro (es. storno parziale), il
    # documento resta valido per quelle e non va toccato.
    centro_evento = CentroCostoWood.query.filter(
        db.func.lower(CentroCostoWood.nome) == (fase_evento or '').strip().lower()).first()
    if centro_evento:
        restano_dichiarazioni = (EventoConsuntivoPP.query
                                  .filter_by(op_code=op_code_evento, fase=fase_evento).first())
        if not restano_dichiarazioni:
            numero_lista = (NumeroListaLavoroWood.query
                             .filter_by(op_code=op_code_evento, centro_costo_id=centro_evento.id).first())
            if numero_lista:
                _audit(o, 'STORNO_ORDINE_LAVORO',
                       f'tolto Ordine di Lavoro {numero_lista.prefisso}/{numero_lista.numero:03d} '
                       f'su {centro_evento.nome} (nessuna dichiarazione rimasta)')
                db.session.delete(numero_lista)


@pp_bp.post('/api/dichiarazione-produzione/eventi/<int:eid>/annulla')
def api_dichiarazione_annulla(eid):
    """
    Storno di una dichiarazione sbagliata — col PIN del capo OPPURE della
    Direzione (chi trova l'errore prima interviene: la Direzione lo vede
    tipicamente qui, nella coda di approvazione, prima ancora che il capo
    se ne accorga nello storico). Non modifica i numeri sul posto: inverte
    esattamente quantità OP, giacenza (ri-carica i materiali scaricati,
    ri-scarica il prodotto caricato) e tempo, poi elimina l'evento. La
    correzione via MasterWork (es. il saldatore aveva segnato 100 invece
    di 80) resta comunque un intervento SEPARATO che Angelo fa di là, per
    conto suo — qui si toglie solo l'errore da IronProduction.
    ⚠️ LIMITE: le eventuali righe di Varianza di Produzione legate a questo
    evento NON vengono rimosse (restano come residuo storico) — non alterano
    la giacenza, solo l'Analisi Costo di quell'OP potrebbe mostrare una
    varianza in più che non riflette più produzione reale.
    """
    d = request.get_json(force=True)
    if not (_verifica_pin_capo(d) or _verifica_pin_direzione(d)):
        return jsonify(ok=False, error='PIN capo o Direzione non valido'), 403
    e = EventoConsuntivoPP.query.get_or_404(eid)
    o = OrdineProduzione.query.filter_by(codice=e.op_code).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione collegato non trovato'), 404
    try:
        # NESSUN except-pass qui: se il ripristino del magazzino fallisce
        # per qualunque motivo, TUTTA l'operazione deve fallire — mai
        # cancellare l'evento lasciando intendere "storno riuscito" mentre
        # in realtà la giacenza non è stata corretta. Nessun commit fatto
        # finora in questa richiesta, quindi un errore qui non lascia stato
        # parziale: il rollback che segue riporta tutto come prima.
        _storna_evento_consuntivo(e, o)
    except Exception as exc:
        db.session.rollback()
        log(f"⚠️ STORNO FALLITO per evento {e.event_id} (OP {o.codice}): {exc}")
        return jsonify(ok=False, error=f'Storno non riuscito, nessuna modifica applicata: {exc}'), 500

    db.session.commit()
    return jsonify(ok=True)


@pp_bp.post('/api/dichiarazione-produzione/eventi/azzera-periodo')
def api_dichiarazione_annulla_periodo():
    """
    Storno di MASSA: tutte le dichiarazioni (EventoConsuntivoPP) nel periodo
    indicato (data_da/data_a, default oggi), opzionalmente filtrate per
    codice — SOLO PIN capo. Ogni evento viene stornato con la STESSA logica
    esatta dello storno singolo (_storna_evento_consuntivo): quantità OP,
    tempo e giacenza tutti correttamente ripristinati, non solo i movimenti
    di magazzino "a parte" scollegati dalle dichiarazioni che li hanno
    generati (quello lo fa invece /movimenti/azzera-periodo, pensato per un
    caso diverso: pulire dati di test senza nessuna dichiarazione dietro).
    Un errore su UN evento non blocca gli altri già stornati con successo
    — ogni storno ha il proprio commit indipendente; il riepilogo finale
    dice quanti sono andati a buon fine e quali sono falliti.
    """
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    oggi_str = datetime.utcnow().strftime('%Y-%m-%d')
    data_da_str = (d.get('data_da') or oggi_str)
    data_a_str = (d.get('data_a') or data_da_str)
    codice_filtro = (d.get('codice') or '').strip().upper()
    try:
        giorno_da = datetime.strptime(data_da_str, '%Y-%m-%d').date()
        giorno_a = datetime.strptime(data_a_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, error='Data non valida'), 400

    query = EventoConsuntivoPP.query.filter(
        db.func.date(EventoConsuntivoPP.timestamp_evento) >= giorno_da,
        db.func.date(EventoConsuntivoPP.timestamp_evento) <= giorno_a)
    if codice_filtro:
        query = query.join(OrdineProduzione, EventoConsuntivoPP.op_code == OrdineProduzione.codice).filter(
            db.func.upper(OrdineProduzione.codice_articolo) == codice_filtro)
    eventi = query.all()

    stornati, falliti = 0, []
    for e in eventi:
        o = OrdineProduzione.query.filter_by(codice=e.op_code).first()
        if not o:
            falliti.append({'event_id': e.event_id, 'errore': 'Ordine di produzione collegato non trovato'})
            continue
        try:
            _storna_evento_consuntivo(e, o)
            db.session.commit()
            stornati += 1
        except Exception as exc:
            db.session.rollback()
            log(f"⚠️ STORNO DI MASSA FALLITO per evento {e.event_id} (OP {o.codice}): {exc}")
            falliti.append({'event_id': e.event_id, 'op_code': e.op_code, 'errore': str(exc)})
    return jsonify(ok=True, stornati=stornati, falliti=falliti)


@pp_bp.get('/api/dichiarazione-produzione/<int:cid>/movimenti')
def api_dichiarazione_movimenti(cid):
    """
    Movimenti di carico/scarico giacenza (MovimentoGiacenzaWood, tipi
    carico_produzione/scarico_produzione) generati dalle dichiarazioni di
    QUESTO centro in QUESTO giorno — stesso PIN capo dello storico, sono la
    stessa area di correzione.
    """
    if not _verifica_pin_capo(request.args):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    centro = CentroCostoWood.query.get_or_404(cid)
    oggi_str = datetime.utcnow().strftime('%Y-%m-%d')
    data_da_str = request.args.get('data_da') or request.args.get('data') or oggi_str
    data_a_str = request.args.get('data_a') or data_da_str
    try:
        giorno_da = datetime.strptime(data_da_str, '%Y-%m-%d').date()
        giorno_a = datetime.strptime(data_a_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, error='Data non valida'), 400
    # AuditPP (mai cancellato, nemmeno dallo storno) invece di
    # EventoConsuntivoPP (che lo storno CANCELLA): usare quest'ultimo per
    # decidere quali OP mostrare faceva sparire e poi RIAPPARIRE i movimenti
    # già stornati, appena l'OP tornava ad avere un evento attivo per un
    # altro motivo (es. una nuova dichiarazione) — il movimento vecchio non
    # era mai stato cancellato, solo temporaneamente nascosto dal filtro.
    fase_pattern = f'fase={centro.nome};'
    righe_audit = (AuditPP.query
                   .filter(AuditPP.azione.in_(('EVENTO_CONSUNTIVO', 'ANNULLO_CONSUNTIVO')),
                           AuditPP.dettaglio.ilike(f'%{fase_pattern}%'),
                           db.func.date(AuditPP.creato_il) >= giorno_da,
                           db.func.date(AuditPP.creato_il) <= giorno_a).all())
    op_codici = {a.op_code for a in righe_audit if a.op_code}
    if not op_codici:
        return jsonify(ok=True, movimenti=[])
    movimenti = (MovimentoGiacenzaWood.query
                 .filter(MovimentoGiacenzaWood.tipo.in_(('carico_produzione', 'scarico_produzione', 'rettifica_import')),
                         MovimentoGiacenzaWood.riferimento.in_(op_codici),
                         db.func.date(MovimentoGiacenzaWood.creato_il) >= giorno_da,
                         db.func.date(MovimentoGiacenzaWood.creato_il) <= giorno_a)
                 .order_by(MovimentoGiacenzaWood.creato_il.desc()).all())
    return jsonify(ok=True, movimenti=[{
        'id': m.id, 'codice': m.codice, 'tipo': m.tipo, 'quantita': m.quantita,
        'costo_unitario': m.costo_unitario, 'valore': m.valore, 'riferimento': m.riferimento,
        'note': m.note, 'timestamp': m.creato_il.strftime('%d/%m/%Y %H:%M'),
    } for m in movimenti])


@pp_bp.post('/api/dichiarazione-produzione/movimenti/<int:mid>/modifica')
def api_dichiarazione_movimento_modifica(mid):
    """
    Corregge la quantità di un movimento già registrato — SOLO PIN capo.
    La giacenza (saldo corrente) è un contatore progressivo, non ricalcolato
    dai movimenti: qui si applica solo la DIFFERENZA fra vecchia e nuova
    quantità, così il saldo resta coerente con lo storico.
    """
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    m = MovimentoGiacenzaWood.query.get_or_404(mid)
    try:
        nuova_quantita = float(d.get('quantita'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='Quantità non valida'), 400
    vecchia_quantita = m.quantita
    diff = nuova_quantita - vecchia_quantita
    g = GiacenzaWood.query.get(m.codice)
    if g:
        g.quantita = (g.quantita or 0) + diff
        g.aggiornato_il = datetime.utcnow()
    m.quantita = nuova_quantita
    if m.costo_unitario is not None:
        m.valore = round(m.costo_unitario * abs(nuova_quantita), 4)
    if 'note' in d:
        m.note = (d.get('note') or '').strip()
    db.session.add(AuditPP(op_code=m.riferimento or '', azione='MODIFICA_MOVIMENTO_GIACENZA',
                            dettaglio=f'movimento {m.id} ({m.codice}, {m.tipo}): '
                                      f'{vecchia_quantita} → {nuova_quantita}'))
    db.session.commit()
    return jsonify(ok=True)


@pp_bp.post('/api/dichiarazione-produzione/movimenti/<int:mid>/elimina')
def api_dichiarazione_movimento_elimina(mid):
    """Elimina un movimento di carico/scarico errato — SOLO PIN capo — e ne
    riporta indietro l'effetto sulla giacenza corrente."""
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    m = MovimentoGiacenzaWood.query.get_or_404(mid)
    g = GiacenzaWood.query.get(m.codice)
    if g:
        g.quantita = (g.quantita or 0) - m.quantita
        g.aggiornato_il = datetime.utcnow()
    db.session.add(AuditPP(op_code=m.riferimento or '', azione='ELIMINA_MOVIMENTO_GIACENZA',
                            dettaglio=f'eliminato movimento {m.id} ({m.codice}, {m.tipo}, {m.quantita})'))
    db.session.delete(m)
    db.session.commit()
    return jsonify(ok=True)


@pp_bp.post('/api/dichiarazione-produzione/movimenti/azzera-periodo')
def api_dichiarazione_movimenti_azzera_periodo():
    """
    Azzeramento massivo dei movimenti di carico/scarico giacenza (Movimento-
    GiacenzaWood, tipi carico_produzione/scarico_produzione/rettifica_import)
    nel periodo indicato (data_da/data_a, default oggi) — SOLO PIN capo.
    Stessa identica logica dell'eliminazione singola, ripetuta riga per riga:
    ogni movimento viene riportato indietro sulla giacenza (g.quantita -=
    m.quantita) prima di essere cancellato, poi tolto. Non tocca
    EventoConsuntivoPP, OrdineProduzione, AuditPP pregresso né altro — solo
    le righe di movimento di magazzino nel range scelto.
    """
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    oggi_str = datetime.utcnow().strftime('%Y-%m-%d')
    data_da_str = (d.get('data_da') or oggi_str)
    data_a_str = (d.get('data_a') or data_da_str)
    try:
        giorno_da = datetime.strptime(data_da_str, '%Y-%m-%d').date()
        giorno_a = datetime.strptime(data_a_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, error='Data non valida'), 400
    movimenti = (MovimentoGiacenzaWood.query
                 .filter(MovimentoGiacenzaWood.tipo.in_(('carico_produzione', 'scarico_produzione', 'rettifica_import')),
                         db.func.date(MovimentoGiacenzaWood.creato_il) >= giorno_da,
                         db.func.date(MovimentoGiacenzaWood.creato_il) <= giorno_a)
                 .all())
    n = len(movimenti)
    for m in movimenti:
        g = GiacenzaWood.query.get(m.codice)
        if g:
            g.quantita = (g.quantita or 0) - m.quantita
            g.aggiornato_il = datetime.utcnow()
        db.session.delete(m)
    db.session.add(AuditPP(op_code='', azione='AZZERA_MOVIMENTI_PERIODO',
                            dettaglio=f'azzerati {n} movimenti di carico/scarico dal {giorno_da.isoformat()} al {giorno_a.isoformat()}'))
    db.session.commit()
    return jsonify(ok=True, eliminati=n)


@pp_bp.get('/api/dichiarazione-produzione/diagnostica-bug-fasi')
def api_diagnostica_bug_fasi():
    """
    Trova gli Ordini di Produzione colpiti dal bug corretto in
    _registra_evento_consuntivo (vedi _e_ultima_fase_del_ciclo): un
    EventoConsuntivoPP con componente NULL (storicamente sempre trattato
    come "prodotto finito", solo per identità di codice) dichiarato su una
    fase che NON è davvero l'ultima del Ciclo di Lavoro di quel codice —
    prima della correzione, avrebbe fatto avanzare qta_buona/stato
    dell'intero OP anche se non era la fase che lo completava per davvero.
    Sola lettura: non modifica nulla, elenca solo cosa sembra colpito.
    """
    eventi_sospetti = (db.session.query(EventoConsuntivoPP, OrdineProduzione)
                       .join(OrdineProduzione, EventoConsuntivoPP.op_code == OrdineProduzione.codice)
                       .filter(EventoConsuntivoPP.componente.is_(None))
                       .all())
    per_op = {}
    for e, o in eventi_sospetti:
        if _e_ultima_fase_del_ciclo(o.codice_articolo, e.fase):
            continue  # questa era davvero l'ultima fase: dichiarazione legittima
        voce = per_op.setdefault(o.codice, {
            'op_code': o.codice, 'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione,
            'commessa': o.commessa, 'stato': o.stato, 'qta_pianificata': o.qta_pianificata,
            'qta_buona': o.qta_buona, 'qta_scarto': o.qta_scarto,
            'eventi_sospetti': [], 'pezzi_buoni_sospetti': 0, 'pezzi_scarto_sospetti': 0,
        })
        voce['eventi_sospetti'].append({
            'fase': e.fase, 'timestamp': e.timestamp_evento.strftime('%d/%m/%Y %H:%M'),
            'pezzi_buoni': e.pezzi_buoni, 'pezzi_scarto': e.pezzi_scarto,
        })
        voce['pezzi_buoni_sospetti'] += e.pezzi_buoni or 0
        voce['pezzi_scarto_sospetti'] += e.pezzi_scarto or 0
    risultato = sorted(per_op.values(), key=lambda v: v['op_code'])
    return jsonify(ok=True, op_colpiti=risultato, totale=len(risultato))


@pp_bp.post('/api/dichiarazione-produzione/diagnostica-bug-fasi/azzera-movimenti')
def api_diagnostica_bug_fasi_azzera_movimenti():
    """
    Per gli OP colpiti dal bug (vedi api_diagnostica_bug_fasi): azzera SOLO
    i movimenti di magazzino (MovimentoGiacenzaWood, carico_produzione/
    scarico_produzione) legati al loro codice_articolo — stessa logica di
    reversal di api_dichiarazione_movimenti_azzera_periodo, qui filtrata
    per codice invece che per data. NON tocca EventoConsuntivoPP (restano
    per l'audit e per il tracciamento "già dichiarato" di ogni fase, es.
    Sega continua a sapere cosa è già stato fatto), NON tocca qta_buona/
    stato dell'OP — solo i movimenti di carico/scarico a magazzino.
    Richiede il PIN capo, come le altre operazioni di azzeramento massivo.
    """
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    op_codes = d.get('op_codes')
    if not op_codes or not isinstance(op_codes, list):
        return jsonify(ok=False, error='Nessun OP indicato'), 400

    codici_articolo = {o.codice_articolo for o in
                        OrdineProduzione.query.filter(OrdineProduzione.codice.in_(op_codes)).all()}
    if not codici_articolo:
        return jsonify(ok=True, eliminati=0)

    movimenti = (MovimentoGiacenzaWood.query
                 .filter(MovimentoGiacenzaWood.codice.in_(codici_articolo),
                         MovimentoGiacenzaWood.tipo.in_(('carico_produzione', 'scarico_produzione')),
                         MovimentoGiacenzaWood.riferimento.in_(op_codes))
                 .all())
    n = len(movimenti)
    for m in movimenti:
        g = GiacenzaWood.query.get(m.codice)
        if g:
            g.quantita = (g.quantita or 0) - m.quantita
            g.aggiornato_il = datetime.utcnow()
        db.session.delete(m)
    db.session.add(AuditPP(op_code='', azione='AZZERA_MOVIMENTI_BUG_FASI',
                            dettaglio=f'azzerati {n} movimenti di magazzino su {len(op_codes)} OP colpiti dal bug fasi: {", ".join(op_codes)}'))
    db.session.commit()
    return jsonify(ok=True, eliminati=n)

