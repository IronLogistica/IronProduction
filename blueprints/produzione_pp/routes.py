from datetime import datetime
from flask import Blueprint, current_app, jsonify, render_template, request
from sqlalchemy.exc import IntegrityError
from models import (db, OrdineProduzione, EventoConsuntivoPP, AuditPP,
                    STATI_ORDINE_PP, ASA_MASTERWORK, prossimo_codice_ordine_pp,
                    prossimo_numero_commessa, GiacenzaWood, MovimentoGiacenzaWood, CicloLavoroWood,
                    CentroCostoWood, VarianzaProduzioneWood, ArticoloApprovvigionamento,
                    CostoStandardVersioneWood, LegameCostoStandardOrdineWood, DistintaBaseWood,
                    CostoStandardVersioneDettaglioWood, OrdineAcquistoWood, RigaOrdineAcquistoWood,
                    CostoStandardVersioneFaseWood, ManodoperaRealeWood)
from blueprints.magazzino.routes import (_esplodi_bom_wood, _flatten_componenti,
                    _registra_movimento_giacenza, _giacenza_residua_dopo_impegni,
                    _netta_e_esplodi_wood, _calcola_costo_standard, _crea_versione_costo_standard,
                    _overhead_pct)

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
        controllo_scorta = None
        try:
            giacenza_iniziale = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
            giacenza_residua = _giacenza_residua_dopo_impegni()
            disponibile_prima = dict(giacenza_residua)
            righe_netting = {}
            _netta_e_esplodi_wood(article, qty, giacenza_residua, righe_netting)
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
        except Exception:
            controllo_scorta = None  # non deve mai bloccare la creazione dell'OP

        return jsonify(ok=True, ordine=_ordine(o), controllo_scorta=controllo_scorta), 201
    except (ValueError, IntegrityError) as exc:
        db.session.rollback(); return jsonify(ok=False, error=str(exc)), 400

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
        return jsonify(ok=True, ordine=_ordine(o))
    except ValueError as exc: db.session.rollback(); return jsonify(ok=False, error=str(exc)), 400

@pp_bp.delete('/api/ordini-produzione/<int:oid>')
def elimina(oid):
    o = OrdineProduzione.query.get_or_404(oid)
    if o.stato != 'Creato':
        return jsonify(ok=False, error='Solo un OP ancora "Creato" (non rilasciato) può essere eliminato'), 409
    codice = o.codice
    _audit(o, 'ELIMINATO', 'Eliminazione manuale da pagina Ordini Produzione')
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
    return render_template('produzione_pp/situazione_cards.html', active='produzione_pp')


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
    risultato = []
    mancanti_per_ordine = []
    tutti_codici_mancanti = set()
    for o in ordini:
        saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
        righe = {}
        if saldo > 0:
            giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=o.id)
            _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, righe)
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

def _registra_evento_consuntivo(o, fase_nome, ts, good, scrap, tempo, event_id):
    """
    Nucleo di registrazione di un consuntivo per l'OP o (già lockato con
    with_for_update dal chiamante): crea l'EventoConsuntivoPP, aggiorna
    quantità/stato dell'OP, scarica la giacenza dei componenti consumati,
    carica il prodotto finito e registra la varianza di lavorazione.
    Condivisa da /api/pp/events (integrazione MasterWork) e dal totem a bordo
    macchina (inizio/fine lavoro) — stesso identico comportamento in entrambi
    i casi. NON fa il commit: il chiamante decide quando farlo.
    """
    db.session.add(EventoConsuntivoPP(event_id=event_id, op_code=o.codice, fase=fase_nome,
                                       timestamp_evento=ts, pezzi_buoni=good, pezzi_scarto=scrap, tempo_minuti=tempo))
    o.qta_buona += good; o.qta_scarto += scrap; o.tempo_consuntivo_minuti += tempo
    if o.stato == 'Rilasciato': o.stato = 'In esecuzione'
    if o.qta_pianificata and o.qta_buona >= o.qta_pianificata:
        o.stato, o.data_completamento = 'Tecnicamente completato', datetime.utcnow()
    _audit(o, 'EVENTO_CONSUNTIVO', f'fase={fase_nome}; buoni={good}; scarto={scrap}; minuti={tempo}', event_id)

    # Scarico automatico giacenza Iron Wood in proporzione ai pezzi buoni
    # appena consuntivati: esplode l'intera distinta (nessun netting qui,
    # solo la quantità reale consumata) e scarica ogni componente toccato.
    # Poi carica il PRODOTTO FINITO stesso a magazzino, valorizzato al suo
    # costo standard (materiali + lavorazione + overhead), e registra la
    # varianza di lavorazione (tempo reale vs standard) quando la fase
    # dell'evento è abbinabile per nome a un reparto del suo Ciclo di Lavoro.
    if good > 0:
        try:
            componenti = _esplodi_bom_wood(o.codice_articolo, qta=good)
            consumi = {}
            _flatten_componenti(componenti, consumi)
            for cod, qta_consumata in consumi.items():
                if qta_consumata:
                    costo_corrente = _calcola_costo_standard(cod)['costo_totale']
                    _registra_movimento_giacenza(cod, -qta_consumata, 'scarico_produzione',
                                                  riferimento=o.codice, note=f'Consuntivo {good} pz buoni',
                                                  costo_unitario=costo_corrente)
        except Exception:
            pass  # lo scarico giacenza non deve mai bloccare la registrazione del consuntivo

        try:
            costo = _calcola_costo_standard(o.codice_articolo)
            if costo['codici_senza_costo']:
                nota_costo = (f'Consuntivo {good} pz buoni — ⚠️ COSTO INCOMPLETO: mancano prezzi per '
                               f'{", ".join(sorted(costo["codici_senza_costo"]))} — totale sottostimato')
            else:
                nota_costo = f'Consuntivo {good} pz buoni'
            _registra_movimento_giacenza(o.codice_articolo, good, 'carico_produzione',
                                          riferimento=o.codice, note=nota_costo,
                                          costo_unitario=costo['costo_totale'])
        except Exception:
            pass  # il carico a magazzino non deve mai bloccare la registrazione del consuntivo

        try:
            riga_ciclo = (CicloLavoroWood.query.filter_by(codice=o.codice_articolo)
                          .join(CicloLavoroWood.centro_costo)
                          .filter(db.func.lower(CentroCostoWood.nome) == fase_nome.lower())
                          .first())
            if riga_ciclo and riga_ciclo.produttivita_oraria:
                fase_congelata = None
                legame = LegameCostoStandardOrdineWood.query.get(o.codice)
                if legame and legame.costo_standard_versione_id:
                    fase_congelata = (CostoStandardVersioneFaseWood.query
                                      .filter_by(versione_id=legame.costo_standard_versione_id)
                                      .filter(db.func.lower(CostoStandardVersioneFaseWood.nome_reparto) == fase_nome.lower())
                                      .first())

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

                var_tariffa_lav = (tariffa_macchina_reale - tariffa_macchina_std) * ore_reali
                var_tariffa_mdo = (tariffa_mdo_reale - tariffa_mdo_std) * ore_reali

                db.session.add(VarianzaProduzioneWood(
                    op_code=o.codice, codice_articolo=o.codice_articolo, fase=fase_nome, quantita=good,
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
        _registra_evento_consuntivo(o, str(d['fase']).strip(), ts, good, scrap, tempo, str(d['event_id']).strip())
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


@pp_bp.get('/api/ordini-produzione/<codice>/analisi-costo')
def api_analisi_costo_ordine(codice):
    o = OrdineProduzione.query.filter_by(codice=codice).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404

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
            var_quantita = (round((qty_effettiva - qty_standard) * prezzo_standard, 2)
                             if qty_standard is not None and prezzo_standard is not None else None)
            var_prezzo = (round((prezzo_effettivo - prezzo_standard) * qty_effettiva, 2)
                          if prezzo_effettivo is not None and prezzo_standard is not None else None)
            dettaglio_materiali.append({
                'codice': cod, 'quantita_standard': qty_standard, 'quantita_effettiva': qty_effettiva,
                'prezzo_standard': prezzo_standard, 'prezzo_effettivo': prezzo_effettivo,
                'valore_disponibile': bool(riga_eff and riga_eff['valore_disponibile']),
                'standard_disponibile': det_std is not None and prezzo_standard is not None,
                'varianza_quantita': var_quantita, 'varianza_prezzo': var_prezzo,
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

    return jsonify(ok=True, codice=o.codice, codice_articolo=o.codice_articolo, stato=o.stato,
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
        limiti=limiti,
    )


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
