import uuid
from datetime import datetime, date
from flask import Blueprint, current_app, jsonify, render_template, request, Response
from sqlalchemy.exc import IntegrityError
from models import (db, OrdineProduzione, EventoConsuntivoPP, AuditPP,
                    STATI_ORDINE_PP, ASA_MASTERWORK, prossimo_codice_ordine_pp,
                    prossimo_numero_commessa, GiacenzaWood, MovimentoGiacenzaWood, CicloLavoroWood,
                    CentroCostoWood, VarianzaProduzioneWood, ArticoloApprovvigionamento,
                    CostoStandardVersioneWood, LegameCostoStandardOrdineWood, DistintaBaseWood,
                    CostoStandardVersioneDettaglioWood, OrdineAcquistoWood, RigaOrdineAcquistoWood,
                    CostoStandardVersioneFaseWood, ManodoperaRealeWood,
                    SogliaAllarmeVarianzaWood, ContoContabileMappaWood, MovimentoContabileWood,
                    VOCI_CONTABILI_WOOD, assicura_conti_contabili_wood, SchedaLavorazioneWood,
                    NumeroListaLavoroWood)
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
            'tutto_disponibile': len(mancanti) == 0,
            'n_componenti_mancanti': len(mancanti),
            'n_componenti_totali': len(righe),
            'scaduto': bool(o.data_prevista and o.data_prevista < oggi and o.stato != 'Tecnicamente completato'),
            'ha_ciclo_lavoro': o.codice_articolo in codici_con_ciclo,
        })

    # Un solo giro di query per sapere quali codici mancanti hanno GIA' un
    # Ordine di Acquisto aperto che li copre (evita N+1 query per ordine).
    codici_coperti = set()
    if tutti_codici_mancanti:
        righe_oa = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                    .filter(RigaOrdineAcquistoWood.codice.in_(tutti_codici_mancanti),
                            OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
        for r_oa in righe_oa:
            if (r_oa.qta_originale or 0) > (r_oa.qta_ricevuta or 0):
                codici_coperti.add(r_oa.codice)

    for riga, mancanti in zip(risultato, mancanti_per_ordine):
        if not mancanti:
            riga['categoria_materiale'] = 'disponibile'
        elif all(c in codici_coperti for c in mancanti):
            riga['categoria_materiale'] = 'in_arrivo'
        else:
            riga['categoria_materiale'] = 'da_ordinare'

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

    def _annota_disponibilita(nodi):
        for n in nodi:
            r = righe_disponibilita.get(n['codice'])
            if r:
                n['disponibile'] = round(max(r['fabbisogno'] - r['mancante'], 0), 3)
                n['mancante'] = round(r['mancante'], 3)
                n['fabbisogno'] = round(r['fabbisogno'], 3)
            else:
                n['disponibile'] = n['quantita_totale']; n['mancante'] = 0; n['fabbisogno'] = n['quantita_totale']
            if n['mancante'] > 0:
                righe_oa = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                            .filter(RigaOrdineAcquistoWood.codice == n['codice'],
                                    OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
                n['ordini_acquisto'] = [{
                    'ordine_n': r_oa.ordine.ordine_n, 'fornitore': r_oa.ordine.fornitore,
                    'stato_label': r_oa.ordine.stato_label,
                    'data_consegna': r_oa.ordine.data_consegna.isoformat() if r_oa.ordine.data_consegna else None,
                    'qta_in_arrivo': round((r_oa.qta_originale or 0) - (r_oa.qta_ricevuta or 0), 3),
                } for r_oa in righe_oa if (r_oa.qta_originale or 0) > (r_oa.qta_ricevuta or 0)]
            else:
                n['ordini_acquisto'] = []
            _annota_disponibilita(n.get('figli', []))
    _annota_disponibilita(albero)

    mancanti_totali = sum(1 for r in righe_disponibilita.values() if r['mancante'] > 0)
    return jsonify(ok=True, codice=o.codice, codice_articolo=o.codice_articolo, descrizione=o.descrizione,
                   cliente=o.cliente, commessa=o.commessa, stato=o.stato, priorita=o.priorita,
                   qta_pianificata=o.qta_pianificata, qta_buona=o.qta_buona, qta_scarto=o.qta_scarto,
                   tutto_disponibile=(mancanti_totali == 0), componenti=albero)


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

@pp_bp.get('/api/pp/orders')
def api_ordini_attivi():
    auth = _api_auth()
    if auth: return auth
    rows = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(['Rilasciato','In esecuzione'])).all()
    rows = [o for o in rows if _is_carpenteria(o.asa)]
    return jsonify(orders=[_ordine(o) for o in sorted(rows, key=lambda o: (o.data_prevista is None, o.data_prevista, o.priorita, o.codice))])

def _registra_evento_consuntivo(o, fase_nome, ts, good, scrap, tempo, event_id, componente=None):
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
    ⚠️ LIMITE NOTO: l'esplosione dell'assemblaggio finale non fa ancora
    netting contro lo stock di semilavorati creato qui — se si consuntivano
    ENTRAMBI i livelli per le stesse unità, il consumo di materia prima può
    risultare contato due volte. Da rifinire con un netting dedicato.
    """
    componente_finale = not componente or componente == o.codice_articolo
    codice_lavorato = o.codice_articolo if componente_finale else componente

    db.session.add(EventoConsuntivoPP(event_id=event_id, op_code=o.codice, fase=fase_nome,
                                       componente=None if componente_finale else componente,
                                       timestamp_evento=ts, pezzi_buoni=good, pezzi_scarto=scrap, tempo_minuti=tempo))
    o.tempo_consuntivo_minuti += tempo
    if componente_finale:
        o.qta_buona += good; o.qta_scarto += scrap
        if o.stato == 'Rilasciato': o.stato = 'In esecuzione'
        if o.qta_pianificata and o.qta_buona >= o.qta_pianificata:
            o.stato, o.data_completamento = 'Tecnicamente completato', datetime.utcnow()
    elif o.stato == 'Rilasciato':
        o.stato = 'In esecuzione'   # un componente ha iniziato la lavorazione: l'OP non è più solo "rilasciato"
    _audit(o, 'EVENTO_CONSUNTIVO', f'componente={codice_lavorato}; fase={fase_nome}; buoni={good}; scarto={scrap}; minuti={tempo}', event_id)

    # Scarico automatico giacenza Iron Wood in proporzione ai pezzi buoni
    # appena consuntivati. Per il prodotto finito: esplode l'INTERA distinta
    # (nessun netting qui, solo la quantità reale consumata) e scarica ogni
    # componente toccato, poi carica il PRODOTTO FINITO a magazzino. Per un
    # componente intermedio: scarica solo la SUA distinta diretta (un
    # livello) e carica LUI STESSO a magazzino come semilavorato pronto.
    # In entrambi i casi si registra anche la varianza di lavorazione (tempo
    # reale vs standard) quando la fase è abbinabile per nome a un reparto
    # del Ciclo di Lavoro del codice appena lavorato.
    if good > 0:
        try:
            if componente_finale:
                componenti_esplosi = _esplodi_bom_wood(o.codice_articolo, qta=good)
                consumi = {}
                _flatten_componenti(componenti_esplosi, consumi)
            else:
                consumi = {rb.codice_figlio: rb.quantita * good for rb in _righe_bom_attive_wood(componente)}
            for cod, qta_consumata in consumi.items():
                if qta_consumata:
                    costo_corrente = _calcola_costo_standard(cod)['costo_totale']
                    _registra_movimento_giacenza(cod, -qta_consumata, 'scarico_produzione',
                                                  riferimento=o.codice, note=f'Consuntivo {good} pz buoni ({codice_lavorato})',
                                                  costo_unitario=costo_corrente)
        except Exception:
            pass  # lo scarico giacenza non deve mai bloccare la registrazione del consuntivo

        try:
            costo = _calcola_costo_standard(codice_lavorato)
            if costo['codici_senza_costo']:
                nota_costo = (f'Consuntivo {good} pz buoni — ⚠️ COSTO INCOMPLETO: mancano prezzi per '
                               f'{", ".join(sorted(costo["codici_senza_costo"]))} — totale sottostimato')
            else:
                nota_costo = f'Consuntivo {good} pz buoni' + ('' if componente_finale else ' (semilavorato)')
            _registra_movimento_giacenza(codice_lavorato, good, 'carico_produzione',
                                          riferimento=o.codice, note=nota_costo,
                                          costo_unitario=costo['costo_totale'])
        except Exception:
            pass  # il carico a magazzino non deve mai bloccare la registrazione del consuntivo

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
        except Exception:
            pass  # nessuna varianza registrabile (fase non abbinabile) non deve bloccare il consuntivo


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
                                     componente=(str(d['componente']).strip() if d.get('componente') else None))
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


def _lista_lavoro_op(o, centro):
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
        pezzi_fatti = int((db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                          .filter(EventoConsuntivoPP.op_code == o.codice,
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
                'nota': '⚠️ Parametri non ancora compilati in Parametri di Lavorazione',
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

    return {
        'centro_id': centro.id, 'centro_nome': centro.nome,
        'op_codice': o.codice, 'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione or '',
        'commessa': o.commessa or '', 'cliente': o.cliente or '', 'stato': o.stato,
        'qta_pianificata': o.qta_pianificata,
        'data_inizio': o.data_inizio.strftime('%d/%m/%Y') if o.data_inizio else '',
        'numero_lista': _numero_lista_lavoro(o.codice, centro),
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
    return render_template('produzione_pp/liste_lavoro.html', active='liste_lavoro',
        centro=centro, macchine=get_macchine_monitor())


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


@pp_bp.get('/api/dichiarazione-produzione/centri')
def api_dichiarazione_centri():
    return jsonify(get_macchine_monitor())


@pp_bp.get('/api/dichiarazione-produzione/<int:cid>/op-aperti')
def api_dichiarazione_op_aperti(cid):
    centro = CentroCostoWood.query.get_or_404(cid)
    ordini = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
              .order_by(OrdineProduzione.priorita, OrdineProduzione.id).all())
    risultato = []
    for o in ordini:
        ha_fase = CicloLavoroWood.query.filter_by(codice=o.codice_articolo, centro_costo_id=centro.id).first()
        if ha_fase:
            risultato.append({
                'codice': o.codice, 'codice_articolo': o.codice_articolo, 'descrizione': o.descrizione,
                'commessa': o.commessa, 'qta_pianificata': o.qta_pianificata,
                'qta_buona': o.qta_buona, 'qta_scarto': o.qta_scarto,
                'saldo': max((o.qta_pianificata or 0) - (o.qta_buona or 0) - (o.qta_scarto or 0), 0),
            })
    return jsonify(risultato)


@pp_bp.post('/api/dichiarazione-produzione')
def api_dichiarazione_crea():
    """Il capo reparto dichiara: crea SOLO, nessun accesso a modifica/storico."""
    d = request.get_json(force=True)
    o = OrdineProduzione.query.filter_by(codice=(d.get('op_code') or '').strip()).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    centro = CentroCostoWood.query.get(d.get('centro_id'))
    if not centro:
        return jsonify(ok=False, error='Centro di costo non trovato'), 404
    try:
        good = _integer(d.get('pezzi_buoni', 0), 'Pezzi buoni')
        scrap = _integer(d.get('pezzi_scarto', 0), 'Pezzi scarto')
        tempo = _integer(d.get('tempo_minuti', 0), 'Tempo')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if good <= 0 and scrap <= 0:
        return jsonify(ok=False, error='Dichiara almeno un pezzo buono o di scarto'), 400
    event_id = str(uuid.uuid4())
    _registra_evento_consuntivo(o, centro.nome, datetime.utcnow(), good, scrap, tempo, event_id)
    db.session.commit()
    return jsonify(ok=True, event_id=event_id, ordine=_ordine(o))


def _verifica_pin_capo(d):
    pin = (d.get('pin') or '').strip()
    return pin and pin == current_app.config.get('CAPO_PIN', '')


@pp_bp.get('/api/dichiarazione-produzione/verifica-pin')
def api_dichiarazione_verifica_pin():
    if not _verifica_pin_capo(request.args):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    return jsonify(ok=True)


@pp_bp.get('/api/dichiarazione-produzione/<int:cid>/storico')
def api_dichiarazione_storico(cid):
    """Visibile SOLO al capo — richiede il PIN come query param."""
    if not _verifica_pin_capo(request.args):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    centro = CentroCostoWood.query.get_or_404(cid)
    data_str = request.args.get('data') or datetime.utcnow().strftime('%Y-%m-%d')
    try:
        giorno = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, error='Data non valida'), 400
    eventi = (EventoConsuntivoPP.query
              .filter(db.func.lower(EventoConsuntivoPP.fase) == centro.nome.lower(),
                      db.func.date(EventoConsuntivoPP.timestamp_evento) == giorno)
              .order_by(EventoConsuntivoPP.timestamp_evento.desc()).all())
    return jsonify(ok=True, eventi=[{
        'id': e.id, 'event_id': e.event_id, 'op_code': e.op_code, 'componente': e.componente,
        'timestamp': e.timestamp_evento.strftime('%d/%m/%Y %H:%M'),
        'pezzi_buoni': e.pezzi_buoni, 'pezzi_scarto': e.pezzi_scarto, 'tempo_minuti': e.tempo_minuti,
    } for e in eventi])


@pp_bp.post('/api/dichiarazione-produzione/eventi/<int:eid>/annulla')
def api_dichiarazione_annulla(eid):
    """
    Storno di una dichiarazione sbagliata — SOLO col PIN del capo. Non
    modifica i numeri sul posto: inverte esattamente quantità OP, giacenza
    (ri-carica i materiali scaricati, ri-scarica il prodotto caricato) e
    tempo, poi elimina l'evento. Il capo reparto ridichiara poi quella
    corretta con la dichiarazione normale.
    ⚠️ LIMITE: le eventuali righe di Varianza di Produzione legate a questo
    evento NON vengono rimosse (restano come residuo storico) — non alterano
    la giacenza, solo l'Analisi Costo di quell'OP potrebbe mostrare una
    varianza in più che non riflette più produzione reale.
    """
    d = request.get_json(force=True)
    if not _verifica_pin_capo(d):
        return jsonify(ok=False, error='PIN capo non valido'), 403
    e = EventoConsuntivoPP.query.get_or_404(eid)
    o = OrdineProduzione.query.filter_by(codice=e.op_code).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione collegato non trovato'), 404

    componente_finale = not e.componente or e.componente == o.codice_articolo
    codice_lavorato = o.codice_articolo if componente_finale else e.componente

    o.tempo_consuntivo_minuti = max((o.tempo_consuntivo_minuti or 0) - e.tempo_minuti, 0)
    if componente_finale:
        o.qta_buona = max((o.qta_buona or 0) - e.pezzi_buoni, 0)
        o.qta_scarto = max((o.qta_scarto or 0) - e.pezzi_scarto, 0)
        if o.stato == 'Tecnicamente completato':
            o.stato = 'In esecuzione'
    _audit(o, 'ANNULLO_CONSUNTIVO', f'storno evento {e.event_id}: componente={codice_lavorato}; '
           f'buoni={e.pezzi_buoni}; scarto={e.pezzi_scarto}; minuti={e.tempo_minuti}')

    if e.pezzi_buoni > 0:
        try:
            if componente_finale:
                componenti_esplosi = _esplodi_bom_wood(o.codice_articolo, qta=e.pezzi_buoni)
                consumi = {}
                _flatten_componenti(componenti_esplosi, consumi)
            else:
                consumi = {rb.codice_figlio: rb.quantita * e.pezzi_buoni for rb in _righe_bom_attive_wood(e.componente)}
            for cod, qta_consumata in consumi.items():
                if qta_consumata:
                    _registra_movimento_giacenza(cod, qta_consumata, 'rettifica_import',
                                                  riferimento=o.codice, note=f'STORNO consuntivo {e.event_id}')
        except Exception:
            pass
        try:
            _registra_movimento_giacenza(codice_lavorato, -e.pezzi_buoni, 'rettifica_import',
                                          riferimento=o.codice, note=f'STORNO consuntivo {e.event_id}')
        except Exception:
            pass

    db.session.delete(e)
    db.session.commit()
    return jsonify(ok=True)
