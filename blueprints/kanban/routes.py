from flask import Blueprint, render_template, jsonify, request
from models import (db, KanbanProdotto, KanbanGruppo, KanbanCiclo, FaseWip,
                    StoricoProduzione, storico_aggiungi_auto, storico_get,
                    kanban_to_dict, log, get_kanban_gruppi,
                    registra_ciclo_se_necessario, calcola_analisi_takt, PIN_ADMIN,
                    TIPI_APPROVVIGIONAMENTO, ArticoloApprovvigionamento,
                    GiacenzaWood, OrdineProduzione, LavorazioneTerzista)
from masterlogistic_client import carica_produzione, sku_da_nome_prodotto, ottieni_stock_kanban, ottieni_scheda_kanban, MasterLogisticError
from masterledgerlight_client import cerca_articolo, MasterLedgerLightError
from datetime import datetime
import re, json

kanban_bp = Blueprint('kanban', __name__)

def _aggiorna_residuo_produzione(p):
    """
    'Residuo da produrre' (KanbanProdotto.in_prod) NON è più un campo da
    inserire a mano: è sempre calcolabile da IronProduction stesso — somma
    di (qta_pianificata − qta_buona) sugli Ordini di Produzione ancora
    aperti (Creato/Rilasciato/In esecuzione) per il codice di questa scheda.
    Stessa fonte dati già usata per 'Commesse di Produzione Aperte' nella
    Scheda WMS (api_kanban_scheda) — qui si aggiorna il campo salvato così
    resta corretto ovunque venga letto (board, API, val_pv), non solo nella
    scheda di dettaglio.
    """
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    op_aperti = (OrdineProduzione.query
                 .filter(db.func.upper(OrdineProduzione.codice_articolo) == sku.upper(),
                         OrdineProduzione.stato.in_(['Creato', 'Rilasciato', 'In esecuzione']))
                 .all())
    p.in_prod = int(sum(max((o.qta_pianificata or 0) - (o.qta_buona or 0), 0) for o in op_aperti))


def _aggiorna_grezzi_e_trattamento(p):
    """
    'Grezzi' e 'In Trattamento' (VERN./ZINC.) NON sono più campi da inserire
    a mano: derivano da dati che IronProduction ha già.
    - Grezzi = totale prodotto dalle Dichiarazioni di Produzione (somma
      qta_buona su TUTTI gli Ordini di Produzione, aperti e chiusi, di
      questo codice — non solo quelli ancora aperti come in_prod) MENO
      quanto è già stato spedito a un terzista per il trattamento esterno
      (LavorazioneTerzista.qta, DDT uscita) — quello che è partito non è
      più "grezzo in casa", è "in trattamento".
    - In Trattamento = quanto di quello spedito NON è ancora rientrato
      (qta − qta_rientrata sulle lavorazioni non RIENTRATA), dagli stessi
      DDT (blueprints/terzisti — vedi _aggiorna_kanban_da_rientro_ddt per il
      lato "rientro" che alza invece 'verniciati'/Finiti IW).
    """
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    sku_upper = sku.upper()

    tutti_op = OrdineProduzione.query.filter(
        db.func.upper(OrdineProduzione.codice_articolo) == sku_upper).all()
    totale_prodotto = int(sum(o.qta_buona or 0 for o in tutti_op))

    lavorazioni = (LavorazioneTerzista.query
                   .filter(LavorazioneTerzista.note.ilike(f'%"codice": "{sku}"%'))
                   .all())
    totale_spedito = 0
    totale_in_viaggio = 0
    for lav in lavorazioni:
        try:
            note_j = json.loads(lav.note or '{}')
        except Exception:
            continue
        if (note_j.get('codice') or '').strip().upper() != sku_upper:
            continue
        totale_spedito += lav.qta or 0
        if lav.stato != 'RIENTRATA':
            totale_in_viaggio += max((lav.qta or 0) - int(note_j.get('qta_rientrata', 0)), 0)

    p.grezzi = max(totale_prodotto - totale_spedito, 0)
    p.in_vern = totale_in_viaggio


def _aggiorna_finiti_is_da_wms(p):
    """
    'Finiti IS' — stock reale che MasterLogistic-WMS ha per questo SKU
    (stesso dato di 'stock_verniciati' dell'endpoint /api/kanban-stock),
    scritto sul campo DEDICATO KanbanProdotto.finiti_is (mai su 'riserva',
    che è il buffer di sicurezza del Kanban — vedi models.py). Timeout
    breve apposta: gira per OGNI prodotto a ogni apertura board, non deve
    bloccare la pagina se WMS è lento — un fallimento qui lascia il valore
    precedente invariato, non lo azzera.
    """
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    try:
        wms = ottieni_scheda_kanban(sku, timeout=3)
    except MasterLogisticError:
        return
    p.finiti_is = int(wms.get('stock_verniciati') or 0)


def _url_key_from_label(label):
    key = re.sub(r'[^\w\s⌀]', '', label).strip()
    key = re.sub(r'\s+', '_', key)
    return key

def _gruppo_by_url_key(url_key):
    g = KanbanGruppo.query.filter_by(url_key=url_key).first()
    if g:
        return g, url_key.replace('_', ' ')
    return None, url_key

def _wip_snapshot():
    """
    Ritorna {fase: {attivi, n_ordini, ore_stimate, limit, pct, colore}} per WIP Monitor.
    - attivi     = N° articoli Kanban con quantità in quella fase
    - n_ordini   = somma delle quantità (quanti pezzi totali in quella fase)
    - ore_stimate= stima ore basata su takt_time_min medio dei prodotti in fase
    """
    fasi = FaseWip.query.order_by(FaseWip.id).all()
    result = {}
    for fw in fasi:
        if fw.fase == 'collaudo':
            continue  # Collaudo rimosso dal monitor
        if fw.fase == 'verniciatura':
            # Prodotti in trattamento esterno
            prodotti_in_fase = KanbanProdotto.query.filter(KanbanProdotto.in_vern > 0).all()
            n_ordini = sum(p.in_vern for p in prodotti_in_fase)
        else:
            # Prodotti in produzione (taglio, sgola, piega, saldatura, finitura)
            prodotti_in_fase = KanbanProdotto.query.filter(KanbanProdotto.in_prod > 0).all()
            n_ordini = sum(p.in_prod for p in prodotti_in_fase)

        attivi = len(prodotti_in_fase)

        # Stima ore: somma (qta * takt_time_min) / 60 per i prodotti con takt impostato
        ore_stimate = None
        ore_tot = 0.0
        ha_takt = False
        for p in prodotti_in_fase:
            if p.takt_time_min and p.takt_time_min > 0:
                qta = p.in_vern if fw.fase == 'verniciatura' else p.in_prod
                ore_tot += (qta * p.takt_time_min) / 60.0
                ha_takt = True
        if ha_takt:
            ore_stimate = round(ore_tot, 1)

        lim = fw.wip_limit or 0
        pct = round(attivi / lim * 100) if lim > 0 else 0
        if lim == 0:                      colore = 'grigio'
        elif pct >= fw.soglia_rosso:      colore = 'rosso'
        elif pct >= fw.soglia_giallo:     colore = 'giallo'
        else:                             colore = 'verde'

        result[fw.fase] = {
            'label': fw.label,
            'attivi': attivi,
            'n_ordini': n_ordini,
            'ore_stimate': ore_stimate,
            'limit': lim,
            'limite_giornaliero': fw.limite_giornaliero,
            'pct': pct,
            'colore': colore,
        }
    return result

# ── PAGINA KANBAN ────────────────────────────────────────────────────────────
@kanban_bp.route('/kanban/<path:url_key>')
def index(url_key):
    g, sheet_key = _gruppo_by_url_key(url_key)
    if g:
        info = {'label': g.label, 'icona': g.icona, 'url_key': g.url_key, 'id': g.id}
    else:
        info = {'label': url_key.replace('_',' '), 'icona': '📋', 'url_key': url_key, 'id': None}

    prodotti = KanbanProdotto.query.filter(
        db.or_(KanbanProdotto.sheet_key == sheet_key,
               KanbanProdotto.sheet_key == url_key)
    ).order_by(KanbanProdotto.sort_order, KanbanProdotto.prodotto).all()
    prodotti = [p for p in prodotti if p.prodotto not in ('Totali',) and not p.prodotto.isdigit()]

    # Campi calcolati in automatico a ogni apertura della board — non più da
    # inserire a mano (vedi le funzioni sopra per le fonti dati).
    for p in prodotti:
        _aggiorna_residuo_produzione(p)
        _aggiorna_grezzi_e_trattamento(p)
        _aggiorna_finiti_is_da_wms(p)
    db.session.commit()

    tot    = len(prodotti)
    ok     = sum(1 for p in prodotti if p.stato == 'OK')
    warn   = tot - ok
    valore = sum(p.val_pv for p in prodotti)

    wip = _wip_snapshot()

    return render_template('kanban/index.html',
        active=f'kb-{url_key}',
        topbar_title=f"{info['icona']} {info['label']}",
        topbar_badge='Kanban Gruppi',
        info=info, url_key=url_key, sheet_key=sheet_key,
        prodotti=prodotti,
        stats={'tot': tot, 'ok': ok, 'warn': warn, 'valore': valore},
        wip=wip)


# ── API KANBAN PRODOTTI ──────────────────────────────────────────────────────
@kanban_bp.route('/api/kanban')
def api_lista():
    categoria = request.args.get('categoria', '')
    if categoria:
        prodotti = KanbanProdotto.query.filter_by(sheet_key=categoria)\
            .order_by(KanbanProdotto.sort_order, KanbanProdotto.prodotto).all()
    else:
        prodotti = KanbanProdotto.query.order_by(KanbanProdotto.categoria, KanbanProdotto.prodotto).all()
    return jsonify([kanban_to_dict(p) for p in prodotti])


@kanban_bp.route('/api/kanban-scheda/<int:pid>')
def api_kanban_scheda(pid):
    """
    Dati per la Scheda WMS completa di un prodotto Kanban (modal "📊 Scheda
    WMS completa" — vedi templates/kanban/index.html::_caricaSchedaWms).
    Questo endpoint non esisteva ancora: il frontend lo chiamava già da
    tempo ma otteneva sempre un 404, quindi la scheda risultava vuota anche
    quando l'interrogazione WMS in fase di creazione funzionava — sono due
    chiamate distinte, questa mancava del tutto.

    Combina:
    - MasterLogistic-WMS (ottieni_scheda_kanban, endpoint /api/kanban-stock):
      stock verniciato/grezzo, riservato clienti CON l'elenco ordini per
      cliente (non solo il totale), ultimi evasi.
    - IronProduction stesso: 'In Trattamento' e 'Finiti IS' restano i
      contatori locali già tenuti a mano sulla scheda Kanban (KanbanProdotto.
      in_vern / .verniciati come fallback se WMS non risponde) — quei due
      campi non hanno una fonte WMS dedicata.
    - Commesse di Produzione APERTE su questo codice_articolo: dati
      IronProduction (OrdineProduzione), non WMS.
    Il saldo contabile riusa la property già esistente su KanbanProdotto
    (stessa formula ovunque nell'app); il saldo disponibile è quello meno
    il riservato clienti live da WMS, come indicato nel sottotitolo della card.
    """
    p = KanbanProdotto.query.get_or_404(pid)
    sku = sku_da_nome_prodotto(p.prodotto)

    risultato = {
        'sku': sku,
        'stock_verniciati': p.verniciati, 'stock_grezzi': p.grezzi, 'in_vern': p.in_vern,
        'stock_is': p.finiti_is,
        'riservato_clienti': p.riservato,
        'ordini_clienti': [], 'ultimi_evasi': [],
        'saldo_contabile': p.saldo_contabile,
        'saldo_contabile_breve_termine': p.saldo_contabile_breve_termine,
        # Saldo Disponibile = Finiti IW + Finiti IS − Impegni: quello che si
        # può evadere SUBITO forzando il sistema, non tutto il saldo
        # contabile (che include anche grezzi/in trattamento, non ancora
        # pronti alla vendita).
        'saldo_disponibile': p.verniciati + p.finiti_is - p.riservato,
        'wms_errore': None,
    }
    if sku:
        try:
            wms = ottieni_scheda_kanban(sku)
            risultato['stock_verniciati'] = p.verniciati   # Finiti IW resta il dato locale (DDT), mai sovrascritto da WMS qui
            risultato['stock_grezzi'] = p.grezzi           # idem Grezzi: locale (Dichiarazioni + DDT), non da WMS
            risultato['stock_is'] = wms['stock_verniciati']  # Finiti IS = stock reale WMS, questo sì
            risultato['riservato_clienti'] = wms['riservato_clienti']
            risultato['ordini_clienti'] = wms['ordini_clienti']
            risultato['ultimi_evasi'] = wms['ultimi_evasi']
            risultato['saldo_contabile_breve_termine'] = (
                p.grezzi + p.in_vern + p.verniciati + risultato['stock_is'] - wms['riservato_clienti'])
            risultato['saldo_disponibile'] = p.verniciati + risultato['stock_is'] - wms['riservato_clienti']
        except MasterLogisticError as e:
            risultato['wms_errore'] = str(e)

    op_aperti = []
    if sku:
        op_aperti = (OrdineProduzione.query
                     .filter(db.func.upper(OrdineProduzione.codice_articolo) == sku.upper(),
                             OrdineProduzione.stato.in_(['Creato', 'Rilasciato', 'In esecuzione']))
                     .order_by(OrdineProduzione.data_prevista).all())
    risultato['commesse_produzione'] = [{
        'numero': o.codice, 'qta_tot': o.qta_pianificata, 'qta_prod': o.qta_buona,
        'saldo': max((o.qta_pianificata or 0) - (o.qta_buona or 0), 0),
        'stato': o.stato,
        'data_consegna': o.data_prevista.strftime('%d/%m/%Y') if o.data_prevista else None,
    } for o in op_aperti]

    return jsonify(risultato)

@kanban_bp.route('/api/kanban/<int:kid>', methods=['PUT'])
def api_aggiorna(kid):
    try:
        p = KanbanProdotto.query.get_or_404(kid)
        stato_prima = p.stato
        d = request.get_json(force=True)
        # ── Accumulo storico produzione: intercetta aumento di verniciati ──
        verniciati_prima = p.verniciati
        # 'in_prod' NON è più qui: è sempre ricalcolato in automatico
        # all'apertura della board (_aggiorna_residuo_produzione), un valore
        # inserito a mano verrebbe sovrascritto al primo refresh — vedi
        # anche templates/kanban/index.html dove quella cella non è più
        # editabile.
        # 'grezzi'/'in_vern' ricalcolati in automatico da Dichiarazioni di
        # Produzione + DDT terzisti; 'verniciati' (Finiti IW) sale da solo a
        # ogni rientro DDT confermato (_aggiorna_kanban_da_rientro_ddt);
        # niente in questa lista è più "Finiti IS" (campo dedicato
        # finiti_is, mai manuale, pescato da WMS). 'riserva' resta
        # editabile: è il buffer di sicurezza del Kanban.
        for campo in ['lotto','riserva','riservato']:
            if campo in d: setattr(p, campo, int(d[campo]))
        delta_verniciati = p.verniciati - verniciati_prima
        # Solo dal 01/07/2026 in poi (data go-live accumulo automatico)
        ora_now = datetime.utcnow()
        go_live = datetime(2026, 7, 1)
        if ora_now >= go_live and delta_verniciati > 0:
            storico_aggiungi_auto(p.id, delta_verniciati)
        if 'val_medio'   in d: p.val_medio   = float(d['val_medio'])
        if 'lavorazioni' in d: p.lavorazioni = d['lavorazioni']
        if 'tipo_approvvigionamento' in d:
            tipo = d['tipo_approvvigionamento']
            if tipo not in TIPI_APPROVVIGIONAMENTO:
                return jsonify({'ok': False, 'error': 'Tipo di approvvigionamento non valido'}), 400
            p.tipo_approvvigionamento = tipo
        if 'lead_time_fornitura_giorni' in d:
            valore = d['lead_time_fornitura_giorni']
            p.lead_time_fornitura_giorni = float(valore) if valore not in (None, '') else None
        p.aggiornato_il = datetime.utcnow()
        stato_dopo = p.stato
        # ── Accumulazione dati cicli per Takt Time ──
        registra_ciclo_se_necessario(p, stato_prima, stato_dopo)
        log(f'Kanban: aggiornato {p.prodotto}')
        db.session.commit()

        # ── Notifica a MasterLogistic-WMS il prodotto finito pronto alla
        # vendita — DOPO il commit: il Kanban (fonte di verità della
        # produzione) resta registrato anche se WMS non risponde. Non
        # bloccante: se fallisce, l'utente viene avvisato nella risposta
        # ma l'aggiornamento Kanban non viene annullato — niente retry
        # automatico, l'endpoint di WMS non è idempotente (un retry alla
        # cieca rischierebbe di caricare la stessa produzione due volte).
        avviso_wms = None
        if delta_verniciati > 0:
            sku = sku_da_nome_prodotto(p.prodotto)
            try:
                carica_produzione(sku, delta_verniciati)
            except MasterLogisticError as e:
                avviso_wms = str(e)
                log(f'WARN: carico produzione NON notificato a MasterLogistic-WMS per {sku}: {e}')

        risposta = {'ok': True, **kanban_to_dict(p)}
        if avviso_wms:
            risposta['avviso_wms'] = avviso_wms
        return jsonify(risposta)
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>/risincronizza-wms', methods=['POST'])
def api_risincronizza_wms(kid):
    """
    Riallinea in blocco UNA scheda già esistente: riservato + Finiti IS da
    MasterLogistic-WMS, grezzi/in trattamento da Dichiarazioni di Produzione
    + DDT terzisti, residuo da produrre dalle Commesse aperte — stessa
    identica logica automatica usata a ogni apertura board (vedi le
    funzioni _aggiorna_* sopra), richiamabile qui a comando per una singola
    scheda (utile appena dopo un test, senza aspettare il prossimo giro).
    'Finiti IW' (verniciati) apposta NON viene toccato qui: sale solo
    all'evento di un rientro DDT confermato (_aggiorna_kanban_da_rientro_ddt)
    — ririchiamarlo da un valore raw WMS rischierebbe di contare due volte
    la stessa quantità.
    """
    p = KanbanProdotto.query.get_or_404(kid)
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return jsonify({'ok': False, 'error': 'Impossibile ricavare lo SKU dal nome prodotto'}), 400
    try:
        wms = ottieni_scheda_kanban(sku)
    except MasterLogisticError as e:
        return jsonify({'ok': False, 'error': str(e)}), 502
    p.riservato = int(wms.get('riservato_clienti') or 0)
    p.finiti_is = int(wms.get('stock_verniciati') or 0)
    _aggiorna_grezzi_e_trattamento(p)
    _aggiorna_residuo_produzione(p)
    p.aggiornato_il = datetime.utcnow()
    log(f'Kanban: risincronizzato {p.prodotto} — grezzi={p.grezzi}, in_vern={p.in_vern}, '
        f'riservato={p.riservato}, finiti_is={p.finiti_is}, in_prod={p.in_prod}')
    db.session.commit()
    return jsonify({'ok': True, **kanban_to_dict(p)})

@kanban_bp.route('/api/kanban', methods=['POST'])
def api_crea():
    """Crea nuovo prodotto Kanban — richiede PIN autorizzativo."""
    try:
        d = request.get_json(force=True)
        # Verifica PIN
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        prodotto = d.get('prodotto','').strip()
        if not prodotto:
            return jsonify({'ok': False, 'error': 'Nome prodotto obbligatorio'}), 400
        tipo_approvvigionamento = d.get('tipo_approvvigionamento', 'DA_CLASSIFICARE')
        if tipo_approvvigionamento not in TIPI_APPROVVIGIONAMENTO:
            return jsonify({'ok': False, 'error': 'Tipo di approvvigionamento non valido'}), 400
        lead_time_fornitura = d.get('lead_time_fornitura_giorni')
        p = KanbanProdotto(
            prodotto=prodotto,
            categoria=d.get('categoria', d.get('sheet_key','').replace('_',' ')),
            sheet_key=d.get('sheet_key',''),
            icona=d.get('icona','📦'),
            lotto=int(d.get('lotto',0)),
            riserva=int(d.get('riserva',0)),
            val_medio=float(d.get('val_medio',0)),
            lavorazioni=d.get('lavorazioni',''),
            scorta_sicurezza=float(d.get('scorta_sicurezza',0.15)),
            lead_time_giorni=float(d.get('lead_time_giorni',7.0)),
            tipo_approvvigionamento=tipo_approvvigionamento,
            lead_time_fornitura_giorni=float(lead_time_fornitura) if lead_time_fornitura not in (None, '') else None,
            # Istantanea WMS presa dall'interrogazione automatica al momento
            # della creazione (vedi templates/base.html::interrogaCodiceEsistente)
            # — così la scheda parte già con i numeri reali. 'riservato' e
            # 'finiti_is' sono dati diretti da WMS (stock reale, impegni);
            # 'grezzi'/'in_vern'/'in_prod' verranno comunque ricalcolati al
            # primo caricamento della board dalle fonti locali (Dichiarazioni
            # di Produzione, DDT terzisti, Commesse aperte) — 'verniciati'
            # (Finiti IW) NON si inizializza da WMS: sale solo a un rientro
            # DDT confermato, mai da uno stock generico.
            grezzi=int(d.get('grezzi', 0) or 0),
            riservato=int(d.get('riservato', 0) or 0),
            finiti_is=int(d.get('finiti_is', d.get('verniciati', 0)) or 0),
        )
        db.session.add(p)
        log(f'Kanban: creato prodotto {prodotto} (PIN autorizzato)')
        db.session.commit()
        return jsonify({'ok': True, 'id': p.id, **kanban_to_dict(p)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/interroga-codice')
def api_interroga_codice():
    """
    Interroga in automatico gli altri programmi dell'ecosistema per un
    codice che si sta per inserire in Kanban Gruppi — così chi crea la
    scheda non deve andare a controllare a mano MasterLogistic-WMS e
    IronProduction. Cerca il codice (case-insensitive) su:
    1) MasterLogistic-WMS — endpoint dedicato /api/kanban-stock (già pensato
       lì per popolare la scheda Kanban): SOLO magazzino Iron Segnaletica
       (stock verniciato/grezzo), riservato clienti (impegni) e ultimi
       evasi. Niente ordinati/fornitore/incoming — quella è roba di
       acquisto a fornitore, fuori scopo qui: saldo disponibile e saldo
       contabile restano calcolati da IronProduction stesso.
    2) IronProduction stesso: classificazione già fatta in Approvvigionamento
       (tipo, lead time, costo standard, UoM), giacenza Iron Wood locale, e
       Ordini di Produzione ancora aperti su questo codice_articolo.
    3) MasterLedgerLight — anagrafica articolo (costo standard, prezzo
       vendita, tipo/classificazione) via masterledgerlight_client.py,
       stesso schema Bearer già usato dalle altre integrazioni.
    Nessuna interrogazione è bloccante: se un sistema non risponde (bind
    non configurato, errore di rete) quella sezione torna vuota con
    l'errore, senza far fallire le altre.
    """
    codice = (request.args.get('codice') or '').strip()
    if not codice:
        return jsonify(ok=False, error='Codice mancante'), 400
    cod_upper = codice.upper()
    trovato = False

    # 1) MasterLogistic-WMS — solo magazzino/impegni/evasi, mai acquisti
    wms = None
    try:
        stock_kanban = ottieni_stock_kanban(codice)
        if (stock_kanban['stock_iron_segnaletica'] or stock_kanban['stock_grezzi']
                or stock_kanban['riservato_clienti'] or stock_kanban['ultimi_evasi']):
            wms = stock_kanban
            trovato = True
    except MasterLogisticError as e:
        wms = {'errore': str(e)}

    # 2) IronProduction stesso
    approvv = ArticoloApprovvigionamento.query.filter(
        db.func.upper(ArticoloApprovvigionamento.codice) == cod_upper).first()
    giacenza = GiacenzaWood.query.filter(
        db.func.upper(GiacenzaWood.codice) == cod_upper).first()
    op_aperti = (OrdineProduzione.query
                 .filter(db.func.upper(OrdineProduzione.codice_articolo) == cod_upper,
                         OrdineProduzione.stato.in_(['Creato', 'Rilasciato', 'In esecuzione']))
                 .order_by(OrdineProduzione.data_prevista).all())
    ironproduction = {
        'tipo_approvvigionamento': approvv.tipo_approvvigionamento if approvv else None,
        'lead_time_fornitura_giorni': approvv.lead_time_fornitura_giorni if approvv else None,
        'costo_acquisto_standard': approvv.costo_acquisto_standard if approvv else None,
        'unita_misura': approvv.unita_misura if approvv else None,
        'giacenza_wood': giacenza.quantita if giacenza else None,
        'ordini_produzione_aperti': [{'codice': o.codice, 'stato': o.stato,
                                       'qta_pianificata': o.qta_pianificata,
                                       'qta_buona': o.qta_buona} for o in op_aperti],
    }
    if approvv or giacenza or op_aperti:
        trovato = True

    # 3) MasterLedgerLight
    try:
        ml = cerca_articolo(codice)
        if ml.get('trovato'):
            masterledgerlight = {'collegato': True, 'trovato': True,
                'codice': ml.get('codice'), 'descrizione': ml.get('descrizione'),
                'tipo_label': ml.get('tipo_label'), 'uom': ml.get('uom'),
                'costo_standard': ml.get('costo_standard'), 'prezzo_vendita': ml.get('prezzo_vendita'),
                'carpenteria_propria': ml.get('carpenteria_propria'),
                'destinazione_acquisto': ml.get('destinazione_acquisto')}
            trovato = True
        else:
            masterledgerlight = {'collegato': True, 'trovato': False,
                'nota': f'Nessun riscontro per "{codice}" in anagrafica MasterLedgerLight.'}
    except MasterLedgerLightError as e:
        masterledgerlight = {'collegato': False, 'nota': str(e)}

    # Schede Kanban già esistenti con questo codice — utile per evitare doppioni
    kanban_esistenti = (KanbanProdotto.query
                        .filter(db.func.upper(KanbanProdotto.prodotto).like(f'{cod_upper}%')).all())
    if kanban_esistenti:
        trovato = True

    return jsonify(ok=True, codice=codice, trovato=trovato,
                    masterlogistic_wms=wms, ironproduction=ironproduction,
                    masterledgerlight=masterledgerlight,
                    kanban_esistenti=[{'id': k.id, 'prodotto': k.prodotto, 'categoria': k.categoria}
                                      for k in kanban_esistenti])


@kanban_bp.route('/api/kanban/<int:kid>', methods=['DELETE'])
def api_elimina(kid):
    try:
        # PIN può arrivare come query param (DELETE non supporta body in Flask di default)
        pin = request.args.get('pin', '') or (request.get_json(force=True, silent=True) or {}).get('pin', '')
        if pin != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        p = KanbanProdotto.query.get_or_404(kid)
        nome = p.prodotto
        db.session.delete(p)
        log(f'Kanban: eliminato {nome} (PIN autorizzato)')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>/sposta', methods=['POST'])
def api_sposta(kid):
    """Sposta un prodotto in un altro gruppo Kanban (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin', '') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        nuovo_sheet_key = d.get('nuovo_sheet_key', '').strip()
        if not nuovo_sheet_key:
            return jsonify({'ok': False, 'error': 'Gruppo destinazione obbligatorio'}), 400
        p = KanbanProdotto.query.get_or_404(kid)
        vecchio = p.sheet_key
        # Trova il gruppo destinazione per ricavare label e categoria
        g = KanbanGruppo.query.filter_by(url_key=nuovo_sheet_key).first()
        p.sheet_key = nuovo_sheet_key
        p.categoria = g.label if g else nuovo_sheet_key.replace('_', ' ')
        log(f'Kanban: spostato {p.prodotto} da "{vecchio}" a "{nuovo_sheet_key}" (PIN autorizzato)')
        db.session.commit()
        return jsonify({'ok': True, 'nuovo_sheet_key': nuovo_sheet_key})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── API TAKT TIME / ANALISI CICLI ────────────────────────────────────────────
@kanban_bp.route('/api/kanban/<int:kid>/analisi-takt')
def api_analisi_takt(kid):
    """Calcola e suggerisce Takt Time basandosi sui cicli accumulati."""
    return jsonify(calcola_analisi_takt(kid))

@kanban_bp.route('/api/kanban/<int:kid>/imposta-takt', methods=['POST'])
def api_imposta_takt(kid):
    """Imposta ufficialmente il Takt Time (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        p = KanbanProdotto.query.get_or_404(kid)
        p.takt_time_min = float(d.get('takt_time_min', 0)) or None
        if 'lead_time_giorni'  in d: p.lead_time_giorni  = float(d['lead_time_giorni'])
        if 'scorta_sicurezza'  in d: p.scorta_sicurezza  = float(d['scorta_sicurezza'])
        db.session.commit()
        log(f'Kanban: Takt Time {p.prodotto} impostato a {p.takt_time_min} min/pz (PIN)')
        return jsonify({'ok': True, 'takt_time_min': p.takt_time_min})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>/cicli')
def api_cicli(kid):
    """Lista ultimi 50 cicli registrati per questo prodotto."""
    cicli = KanbanCiclo.query.filter_by(kanban_id=kid)\
        .order_by(KanbanCiclo.data_inizio.desc()).limit(50).all()
    return jsonify([{
        'id': c.id,
        'data_inizio': c.data_inizio.strftime('%d/%m/%Y %H:%M') if c.data_inizio else '—',
        'data_fine':   c.data_fine.strftime('%d/%m/%Y %H:%M')   if c.data_fine   else '—',
        'lead_time_ore': c.lead_time_ore,
        'qta_prodotta':  c.qta_prodotta,
        'aperto': c.aperto,
    } for c in cicli])


# ── API WIP SNAPSHOT ─────────────────────────────────────────────────────────
@kanban_bp.route('/api/wip')
def api_wip():
    return jsonify(_wip_snapshot())

@kanban_bp.route('/api/wip', methods=['PUT'])
def api_wip_aggiorna():
    """Aggiorna limiti WIP per una fase (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        fase = d.get('fase','').strip()
        fw = FaseWip.query.filter_by(fase=fase).first()
        if not fw:
            return jsonify({'ok': False, 'error': f'Fase "{fase}" non trovata'}), 404
        if 'wip_limit'          in d: fw.wip_limit          = int(d['wip_limit'])
        if 'limite_giornaliero' in d: fw.limite_giornaliero = int(d['limite_giornaliero'])
        if 'soglia_giallo'      in d: fw.soglia_giallo      = int(d['soglia_giallo'])
        if 'soglia_rosso'       in d: fw.soglia_rosso       = int(d['soglia_rosso'])
        db.session.commit()
        log(f'FaseWip: aggiornata {fase} limit={fw.wip_limit} giorn={fw.limite_giornaliero}')
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── API KANBAN GRUPPI ────────────────────────────────────────────────────────
@kanban_bp.route('/api/kanban-gruppi', methods=['GET'])
def api_gruppi_lista():
    return jsonify(get_kanban_gruppi())

@kanban_bp.route('/api/kanban-gruppi', methods=['POST'])
def api_gruppi_crea():
    try:
        d = request.get_json(force=True)
        label = d.get('label','').strip()
        if not label:
            return jsonify({'ok': False, 'error': 'Nome gruppo obbligatorio'}), 400
        icona   = d.get('icona','📦').strip() or '📦'
        url_key = _url_key_from_label(label)
        base_key, counter = url_key, 2
        while KanbanGruppo.query.filter_by(url_key=url_key).first():
            url_key = f"{base_key}_{counter}"; counter += 1
        max_order = db.session.query(db.func.max(KanbanGruppo.sort_order)).scalar() or 0
        g = KanbanGruppo(label=label, icona=icona, url_key=url_key, sort_order=max_order+1)
        db.session.add(g)
        log(f'KanbanGruppo: creato "{label}"')
        db.session.commit()
        return jsonify({'ok': True, 'url_key': url_key, 'label': label})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban-gruppi/<path:url_key>', methods=['PUT'])
def api_gruppi_rinomina(url_key):
    """Rinomina label e/o icona di un gruppo (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        g = KanbanGruppo.query.filter_by(url_key=url_key).first()
        if not g:
            return jsonify({'ok': False, 'error': 'Gruppo non trovato'}), 404
        if 'label' in d and d['label'].strip():
            g.label = d['label'].strip()
        if 'icona' in d and d['icona'].strip():
            g.icona = d['icona'].strip()
        db.session.commit()
        log(f'KanbanGruppo: rinominato "{url_key}" → "{g.label}"')
        return jsonify({'ok': True, 'label': g.label, 'icona': g.icona})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban-gruppi/<path:url_key>', methods=['DELETE'])
def api_gruppi_elimina(url_key):
    try:
        g = KanbanGruppo.query.filter_by(url_key=url_key).first()
        if not g:
            return jsonify({'ok': False, 'error': 'Gruppo non trovato'}), 404
        sheet_k    = url_key.replace('_', ' ')
        n_prodotti = KanbanProdotto.query.filter(
            db.or_(KanbanProdotto.sheet_key == sheet_k,
                   KanbanProdotto.sheet_key == url_key)
        ).count()
        if n_prodotti > 0:
            return jsonify({'ok': False,
                'error': f'Impossibile eliminare: contiene {n_prodotti} prodotti. Rimuovili prima.'}), 400
        nome = g.label
        db.session.delete(g)
        log(f'KanbanGruppo: eliminato "{nome}"')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── API STORICO PRODUZIONE ───────────────────────────────────────────────────
@kanban_bp.route('/api/kanban/<int:kid>/storico')
def api_storico(kid):
    """Ritorna storico produzione per la scheda articolo."""
    return jsonify(storico_get(kid))

@kanban_bp.route('/api/kanban/<int:kid>/storico/import-csv', methods=['POST'])
def api_storico_import(kid):
    """
    Import CSV storico produzione per un articolo.
    Body JSON: { pin, righe: [{anno, mese, qta}] }
    Modalità: somma a qta_import esistente (non sovrascrive).
    Per azzerare e reimportare usare reset=true.
    """
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        p = KanbanProdotto.query.get_or_404(kid)
        righe = d.get('righe', [])
        reset = d.get('reset', False)
        if reset:
            StoricoProduzione.query.filter_by(kanban_id=kid).delete()
        n_inserite = 0
        for r in righe:
            try:
                anno = int(r['anno']); mese = int(r['mese']); qta = int(r['qta'])
                if not (1 <= mese <= 12) or anno < 2000 or qta < 0:
                    continue
                riga = StoricoProduzione.query.filter_by(kanban_id=kid, anno=anno, mese=mese).first()
                if riga:
                    riga.qta_import = (riga.qta_import or 0) + qta
                    riga.aggiornato_il = datetime.utcnow()
                else:
                    db.session.add(StoricoProduzione(
                        kanban_id=kid, anno=anno, mese=mese, qta_import=qta, qta_auto=0
                    ))
                n_inserite += 1
            except (KeyError, ValueError, TypeError):
                continue
        log(f'Storico: importate {n_inserite} righe per {p.prodotto}')
        db.session.commit()
        return jsonify({'ok': True, 'n_inserite': n_inserite, **storico_get(kid)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/kanban/<int:kid>/storico/reset', methods=['POST'])
def api_storico_reset(kid):
    """Azzera tutto lo storico di un articolo (richiede PIN)."""
    try:
        d = request.get_json(force=True)
        if d.get('pin','') != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403
        p = KanbanProdotto.query.get_or_404(kid)
        StoricoProduzione.query.filter_by(kanban_id=kid).delete()
        db.session.commit()
        log(f'Storico: azzerato per {p.prodotto}')
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

@kanban_bp.route('/api/storico/upload-csv', methods=['POST'])
def api_storico_upload_csv():
    """
    Upload CSV globale — importa storico per più articoli in una volta.
    Formato CSV: codice_articolo,anno,mese,qta
    Header obbligatorio. Separatore virgola o punto e virgola.
    Richiede PIN nel form field 'pin'.
    """
    import csv, io
    try:
        pin = request.form.get('pin','')
        if pin != PIN_ADMIN:
            return jsonify({'ok': False, 'error': 'PIN non valido', 'pin_error': True}), 403

        file = request.files.get('file')
        if not file:
            return jsonify({'ok': False, 'error': 'Nessun file caricato'}), 400

        raw = file.read().decode('utf-8-sig')  # gestisce BOM Excel
        # Rileva separatore
        sep = ';' if raw.count(';') > raw.count(',') else ','
        reader = csv.DictReader(io.StringIO(raw), delimiter=sep)

        n_ok = n_err = 0
        errori = []
        # Cache prodotti per codice (case-insensitive, strip)
        cache = {}
        for riga_num, row in enumerate(reader, start=2):
            try:
                # Supporta header flessibili
                codice = (row.get('codice_articolo') or row.get('codice') or row.get('prodotto','') ).strip()
                anno   = int((row.get('anno') or '').strip())
                mese   = int((row.get('mese') or '').strip())
                qta    = int((row.get('qta') or row.get('quantita','0')).strip())

                if not codice or not (1 <= mese <= 12) or anno < 2000 or qta < 0:
                    errori.append(f'Riga {riga_num}: dati non validi ({codice},{anno},{mese},{qta})')
                    n_err += 1
                    continue

                # Trova KanbanProdotto per codice (cerca nel campo prodotto)
                key = codice.lower()
                if key not in cache:
                    # Cerca prima corrispondenza esatta, poi parziale
                    p = KanbanProdotto.query.filter(
                        db.func.lower(KanbanProdotto.prodotto).like(f'%{key}%')
                    ).first()
                    cache[key] = p
                p = cache[key]

                if not p:
                    errori.append(f'Riga {riga_num}: articolo "{codice}" non trovato')
                    n_err += 1
                    continue

                storico_r = StoricoProduzione.query.filter_by(
                    kanban_id=p.id, anno=anno, mese=mese
                ).first()
                if storico_r:
                    storico_r.qta_import = (storico_r.qta_import or 0) + qta
                    storico_r.aggiornato_il = datetime.utcnow()
                else:
                    db.session.add(StoricoProduzione(
                        kanban_id=p.id, anno=anno, mese=mese,
                        qta_import=qta, qta_auto=0
                    ))
                n_ok += 1
            except Exception as e:
                errori.append(f'Riga {riga_num}: {e}')
                n_err += 1

        db.session.commit()
        log(f'Storico CSV: {n_ok} righe importate, {n_err} errori')
        return jsonify({
            'ok': True, 'n_ok': n_ok, 'n_err': n_err,
            'errori': errori[:20]  # max 20 errori mostrati
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── API WMS ARTICOLI (autocomplete da MasterLogistic) ────────────────────────
@kanban_bp.route('/api/wms-articoli')
def api_wms_articoli():
    """
    Proxy verso MasterLogistic-WMS per l'autocomplete del form Kanban. A
    differenza di prima, NON inghiotte più l'errore in una lista vuota:
    ritorna sempre {articoli, errore} così il frontend può mostrare il
    motivo vero (URL non configurato / WMS irraggiungibile / risposta
    inattesa) invece del generico "non connesso" che nascondeva tutto.
    """
    import os, requests as http_req
    base = os.environ.get('MASTERLOGISTIC_URL', '').rstrip('/')
    if not base:
        return jsonify(articoli=[], errore='MASTERLOGISTIC_URL non configurato su IronProduction (Railway → Variables).')
    try:
        resp = http_req.get(f"{base}/api/articoli-lista", timeout=8)
    except Exception as e:
        return jsonify(articoli=[], errore=f'MasterLogistic-WMS non raggiungibile ({base}): {e}')
    if resp.status_code != 200:
        return jsonify(articoli=[], errore=f'MasterLogistic-WMS ha risposto {resp.status_code} su /api/articoli-lista')
    try:
        dati = resp.json()
    except ValueError:
        return jsonify(articoli=[], errore='MasterLogistic-WMS ha risposto in un formato inatteso (non JSON).')
    if isinstance(dati, dict) and dati.get('error'):
        return jsonify(articoli=[], errore=f'MasterLogistic-WMS: {dati["error"]}')
    return jsonify(articoli=dati if isinstance(dati, list) else [], errore=None)
