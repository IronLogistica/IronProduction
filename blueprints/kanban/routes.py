from flask import Blueprint, render_template, jsonify, request
from models import (db, KanbanProdotto, KanbanGruppo, KanbanCiclo, FaseWip,
                    StoricoProduzione, storico_aggiungi_auto, storico_get,
                    kanban_to_dict, log, get_kanban_gruppi,
                    registra_ciclo_se_necessario, calcola_analisi_takt, PIN_ADMIN,
                    TIPI_APPROVVIGIONAMENTO, ArticoloApprovvigionamento,
                    GiacenzaWood, OrdineProduzione, LavorazioneTerzista)
from masterlogistic_client import carica_produzione, sku_da_nome_prodotto, ottieni_stock_kanban, ottieni_scheda_kanban, MasterLogisticError
from masterledgerlight_client import cerca_articolo, MasterLedgerLightError
from blueprints.magazzino.routes import _grezzo_iw_per_codici
from datetime import datetime, timedelta
import re, json

kanban_bp = Blueprint('kanban', __name__)

FINITI_IS_TTL_SECONDI = 180  # non richiamare WMS per lo stesso prodotto più spesso di così

def _mappa_op_per_sku():
    """Tutti gli Ordini di Produzione, raggruppati per codice_articolo
    (upper) in un unico dict — UNA query sola invece di una query per
    OGNI prodotto Kanban dentro il ciclo di index(). Era il principale
    motivo di lentezza della board: con N prodotti si facevano N query
    separate solo per questo dato."""
    per_sku = {}
    for o in OrdineProduzione.query.all():
        per_sku.setdefault((o.codice_articolo or '').upper(), []).append(o)
    return per_sku


def _mappa_giacenza_per_sku():
    """Tutta la giacenza locale (GiacenzaWood), come dict {codice_upper:
    quantita} — UNA query sola invece di una per ogni prodotto Kanban,
    stesso principio delle mappe qui sopra."""
    return {(g.codice or '').upper(): (g.quantita or 0) for g in GiacenzaWood.query.all()}


def _mappa_scorta_minima_per_sku():
    """
    Scorta minima WMS già sincronizzata in locale (GiacenzaWood.
    scorta_minima_wms), come dict {codice_upper: valore o None} — UNA query
    sola per tutta la board. MAI una chiamata WMS live qui: quel valore si
    aggiorna solo su azione esplicita (pulsante 'Sincronizza' in Materiali,
    o la risincronizzazione della singola scheda Kanban) — una pagina non
    deve mai dipendere da una chiamata di rete esterna per aprirsi, stesso
    principio già applicato al resto della board. Un codice mai
    sincronizzato risulta None (mostrato come '—' in tabella, mai zero:
    zero sarebbe un dato falso, diverso da 'non ancora sincronizzato').
    """
    return {(g.codice or '').upper(): g.scorta_minima_wms for g in GiacenzaWood.query.all()}


def _mappa_lavorazioni_terzisti_per_sku():
    """Tutte le LavorazioneTerzista con un 'codice' leggibile dal campo
    note, raggruppate per sku — UNA query invece di un LIKE con wildcard
    iniziale ('%\"codice\": \"...\"%') ripetuto per ogni prodotto Kanban:
    quel pattern non può usare nessun indice, quindi ogni chiamata era
    una scansione completa della tabella — l'altro grosso motivo di
    lentezza, che peggiora con la storia accumulata dei DDT."""
    per_sku = {}
    for lav in LavorazioneTerzista.query.all():
        try:
            note_j = json.loads(lav.note or '{}')
        except Exception:
            continue
        sku = (note_j.get('codice') or '').strip().upper()
        if sku:
            per_sku.setdefault(sku, []).append((lav, note_j))
    return per_sku


def _aggiorna_residuo_produzione(p, op_per_sku=None):
    """
    'Residuo da produrre' (KanbanProdotto.in_prod) NON è più un campo da
    inserire a mano: è sempre calcolabile da IronProduction stesso — somma
    di (qta_pianificata − qta_buona) sugli Ordini di Produzione ancora
    aperti (Creato/Rilasciato/In esecuzione) per il codice di questa scheda.
    Stessa fonte dati già usata per 'Commesse di Produzione Aperte' nella
    Scheda WMS (api_kanban_scheda) — qui si aggiorna il campo salvato così
    resta corretto ovunque venga letto (board, API, val_pv), non solo nella
    scheda di dettaglio.

    op_per_sku: se passato (da _mappa_op_per_sku, es. dal ciclo su tutta
    la board), evita una query dedicata — altrimenti interroga il DB per
    questo solo prodotto (uso singolo, es. risincronizza-wms).
    """
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    if op_per_sku is not None:
        candidati = op_per_sku.get(sku.upper(), [])
        op_aperti = [o for o in candidati if o.stato in ('Creato', 'Rilasciato', 'In esecuzione')]
    else:
        op_aperti = (OrdineProduzione.query
                     .filter(db.func.upper(OrdineProduzione.codice_articolo) == sku.upper(),
                             OrdineProduzione.stato.in_(['Creato', 'Rilasciato', 'In esecuzione']))
                     .all())
    p.in_prod = int(sum(max((o.qta_pianificata or 0) - (o.qta_buona or 0), 0) for o in op_aperti))


def _aggiorna_grezzi_e_trattamento(p, op_per_sku=None, lav_per_sku=None, grezzo_iw_per_sku=None):
    """
    'Grezzi' e 'In Trattamento' (VERN./ZINC.) NON sono più campi da inserire
    a mano: derivano da dati che IronProduction ha già.
    - Grezzi = STESSA fonte di 'GREZZI IW' ovunque altro nell'app
      (_grezzo_iw_per_codici: solo dichiarazioni a Saldatura APPROVATE
      dalla Direzione, più le rettifiche cumulative — spedizione a
      terzista, vendita a Iron Segnaletica, ecc.). PRIMA questo campo
      usava una formula SUA (qta_buona di TUTTI gli OP a qualunque fase,
      senza richiedere approvazione Direzione) che poteva mostrare un
      numero diverso da 'GREZZI IW' per lo stesso prodotto — corretto per
      avere un'unica fonte di verità in tutta l'app. Può risultare
      NEGATIVO (spedito al terzista più di quanto risulti prodotto) —
      voluto, MAI clampato a 0: nasconderlo travestirebbe un segnale di
      sbilancio reale da controllare come se tutto fosse a posto.
    - In Trattamento = quanto è stato spedito a un terzista e NON ancora
      rientrato (qta − qta_rientrata sulle lavorazioni non RIENTRATA),
      dagli stessi DDT (blueprints/terzisti — vedi
      _aggiorna_kanban_da_rientro_ddt per il lato "rientro" che alza
      invece 'verniciati'/Finiti IW).

    op_per_sku/lav_per_sku/grezzo_iw_per_sku: mappe precalcolate (vedi
    sopra) per evitare più query per prodotto quando si scorre l'intera
    board — altrimenti interroga il DB solo per questo prodotto (uso
    singolo, es. risincronizza-wms).
    """
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    sku_upper = sku.upper()

    if grezzo_iw_per_sku is not None:
        p.grezzi = grezzo_iw_per_sku.get(sku, 0)
    else:
        p.grezzi = _grezzo_iw_per_codici([sku]).get(sku, 0)

    if lav_per_sku is not None:
        lavorazioni_con_note = lav_per_sku.get(sku_upper, [])
    else:
        lavorazioni_con_note = []
        for lav in (LavorazioneTerzista.query
                    .filter(LavorazioneTerzista.note.ilike(f'%"codice": "{sku}"%')).all()):
            try:
                note_j = json.loads(lav.note or '{}')
            except Exception:
                continue
            if (note_j.get('codice') or '').strip().upper() == sku_upper:
                lavorazioni_con_note.append((lav, note_j))

    totale_in_viaggio = 0
    for lav, note_j in lavorazioni_con_note:
        if lav.stato != 'RIENTRATA':
            totale_in_viaggio += max((lav.qta or 0) - int(note_j.get('qta_rientrata', 0)), 0)

    p.in_vern = totale_in_viaggio


def _aggiorna_finiti_is_da_wms(p, forza=False):
    """
    'Finiti IS' — stock reale che MasterLogistic-WMS ha per questo SKU
    (stesso dato di 'stock_verniciati' dell'endpoint /api/kanban-stock),
    scritto sul campo DEDICATO KanbanProdotto.finiti_is (mai su 'riserva',
    che è il buffer di sicurezza del Kanban — vedi models.py).

    Chiamata via HTTP a WMS per OGNI prodotto della board a OGNI apertura
    pagina era il motivo più pesante di lentezza (N chiamate esterne
    sequenziali, anche con timeout breve): ora si salta la chiamata se
    già aggiornato negli ultimi FINITI_IS_TTL_SECONDI, il valore resta
    comunque corretto per l'uso pratico (lo stock WMS non cambia al
    secondo) e la board carica quasi subito dopo il primo giro.
    Un fallimento qui lascia il valore precedente invariato, non lo azzera.
    """
    ora = datetime.utcnow()
    if not forza and p.finiti_is_aggiornato_il and (ora - p.finiti_is_aggiornato_il).total_seconds() < FINITI_IS_TTL_SECONDI:
        return
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    try:
        wms = ottieni_scheda_kanban(sku, timeout=3)
    except MasterLogisticError:
        return
    p.finiti_is = int(wms.get('stock_verniciati') or 0)
    p.finiti_is_aggiornato_il = ora


def _sincronizza_finiti_iw_da_magazzino(p, giacenza_per_sku=None):
    """
    'Finiti IW' (KanbanProdotto.verniciati) deve SEMPRE rispecchiare il
    Magazzino locale (GiacenzaWood), non il contrario. Prima veniva solo
    incrementato a evento (rientro DDT terzista, +N) — comodo per
    l'aggiornamento immediato, ma se Angelo poi RETTIFICA il magazzino a
    mano su Materiali (es. dopo un inventario fisico di fine mese, dove il
    conteggio reale può differire da quello accumulato via eventi), quella
    correzione non arrivava mai al Kanban: i due numeri restavano
    disallineati indefinitamente. Da qui in poi, a ogni apertura board, il
    Kanban si RIALLINEA al Magazzino — in caso di divergenza vince sempre
    lui, perché una rettifica manuale rappresenta la verità fisica reale,
    mentre l'accumulo a eventi è solo una stima costruita nel tempo.
    'giacenza_per_sku' opzionale: dict {codice: quantita} già caricato in
    blocco, per non fare una query per prodotto a ogni apertura board.
    """
    sku = sku_da_nome_prodotto(p.prodotto)
    if not sku:
        return
    if giacenza_per_sku is not None:
        sku_upper = sku.upper()
        if sku_upper not in giacenza_per_sku:
            return  # nessuna riga di magazzino ancora per questo SKU: niente da riallineare
        p.verniciati = int(round(giacenza_per_sku[sku_upper]))
    else:
        g = GiacenzaWood.query.get(sku)
        if not g:
            return
        p.verniciati = int(round(g.quantita or 0))


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

    Carica TUTTI i KanbanProdotto una volta sola (2 query totali) invece di
    ripetere 'in_vern > 0' / 'in_prod > 0' per ogni fase — con N fasi erano
    fino a 2×N query per la stessa manciata di righe.
    """
    fasi = FaseWip.query.order_by(FaseWip.id).all()
    tutti_prodotti = KanbanProdotto.query.all()
    in_vern_attivi = [p for p in tutti_prodotti if p.in_vern > 0]
    in_prod_attivi = [p for p in tutti_prodotti if p.in_prod > 0]
    result = {}
    for fw in fasi:
        if fw.fase == 'collaudo':
            continue  # Collaudo rimosso dal monitor
        if fw.fase == 'verniciatura':
            prodotti_in_fase = in_vern_attivi
            n_ordini = sum(p.in_vern for p in prodotti_in_fase)
        else:
            prodotti_in_fase = in_prod_attivi
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


def _calcola_alert_scorte():
    """
    Per ogni prodotto Kanban (codice padre/prodotto finito), incrocia:
    - scorta minima  = domanda_effettiva × lead_time_giorni × (1+scorta_sicurezza)
      (stessa identica formula già usata per n_kanban_suggerito — 'scorte
      minime' e RT/SS sono gli stessi parametri Lean già in uso altrove,
      non un concetto nuovo)
    - domanda_effettiva = il PIÙ ALTO tra domanda_giornaliera (storico
      cicli chiusi ultimi 90gg) e budget_mensile_pz/30 (previsione
      commerciale manuale, se impostata) — usare il massimo evita di
      sottostimare il rischio quando il budget prevede più di quanto lo
      storico racconti ancora
    - stock pronto ora = saldo_disponibile (Finiti IW + Finiti IS − riservato
      clienti — la stessa cifra già mostrata in Scheda WMS come "quello che
      si può evadere SUBITO")
    - tempo di attraversamento = lead_time_giorni (RT) — quanto ci vuole
      storicamente a rimpiazzare lo stock

    Ritorna una lista ordinata (rosso prima), con giorni di copertura
    residui e la data presunta in cui si resterebbe a terra.
    """
    oggi = datetime.utcnow()
    prodotti = KanbanProdotto.query.filter(
        ~KanbanProdotto.prodotto.in_(['Totali'])).order_by(KanbanProdotto.prodotto).all()

    risultati = []
    for p in prodotti:
        if p.prodotto.isdigit():
            continue
        domanda_storica = p.domanda_giornaliera or 0
        domanda_budget = (p.budget_mensile_pz / 30) if p.budget_mensile_pz else 0
        domanda_effettiva = max(domanda_storica, domanda_budget)

        stock_pronto = max((p.verniciati or 0) + (p.finiti_is or 0) - (p.riservato or 0), 0)
        rt = p.lead_time_giorni or 0
        ss = p.scorta_sicurezza if p.scorta_sicurezza is not None else 0.15

        if domanda_effettiva <= 0:
            risultati.append({
                'prodotto': p.prodotto, 'sheet_key': p.sheet_key, 'id': p.id,
                'stock_pronto': stock_pronto, 'domanda_gg': 0, 'fonte_domanda': 'nessun dato',
                'scorta_minima_pz': None, 'giorni_copertura': None, 'lead_time_giorni': rt,
                'data_a_terra': None, 'livello': 'grigio',
            })
            continue

        scorta_minima_pz = round(domanda_effettiva * rt * (1 + ss), 1)
        giorni_copertura = round(stock_pronto / domanda_effettiva, 1)
        data_a_terra = oggi + timedelta(days=giorni_copertura)

        if stock_pronto < scorta_minima_pz and giorni_copertura <= rt:
            livello = 'rosso'   # sotto scorta minima E si esaurirà prima che un rifornimento possa arrivare
        elif stock_pronto < scorta_minima_pz:
            livello = 'giallo'  # sotto scorta minima ma con margine prima di restare a terra
        else:
            livello = 'verde'

        risultati.append({
            'prodotto': p.prodotto, 'sheet_key': p.sheet_key, 'id': p.id,
            'stock_pronto': stock_pronto, 'domanda_gg': round(domanda_effettiva, 2),
            'fonte_domanda': 'budget' if domanda_budget > domanda_storica else 'storico',
            'scorta_minima_pz': scorta_minima_pz, 'giorni_copertura': giorni_copertura,
            'lead_time_giorni': rt, 'data_a_terra': data_a_terra.strftime('%d/%m/%Y'),
            'livello': livello,
        })

    ordine_livello = {'rosso': 0, 'giallo': 1, 'verde': 2, 'grigio': 3}
    risultati.sort(key=lambda r: (ordine_livello[r['livello']], r['giorni_copertura'] if r['giorni_copertura'] is not None else 9999))
    return risultati


# ── PAGINA ALERT SCORTE ──────────────────────────────────────────────────────
@kanban_bp.route('/alert-scorte')
def pagina_alert_scorte():
    from blueprints.magazzino.routes import calcola_alert_fabbisogno_codici_padre
    return render_template('kanban/alert_scorte.html', active='alert-scorte',
                            topbar_title='🚨 Alert Scorte Codici Padre', righe=_calcola_alert_scorte(),
                            righe_fabbisogno=calcola_alert_fabbisogno_codici_padre())


@kanban_bp.route('/api/alert-scorte')
def api_alert_scorte():
    return jsonify(_calcola_alert_scorte())


@kanban_bp.route('/api/alert-scorte/fabbisogno-padre')
def api_alert_fabbisogno_padre():
    from blueprints.magazzino.routes import calcola_alert_fabbisogno_codici_padre
    return jsonify(calcola_alert_fabbisogno_codici_padre())


@kanban_bp.route('/api/alert-scorte/chiedi-ai', methods=['POST'])
def api_alert_chiedi_ai():
    """
    Manda lo stato di Alert Scorte (fabbisogno codici padre + rischio
    stockout Kanban) a ChatGPT con un prompt studiato per farsi suggerire
    concretamente come comportarsi — priorità, cosa produrre/ordinare
    prima, dove concentrare l'attenzione. Richiede la variabile d'ambiente
    OPENAI_API_KEY (da impostare su Railway); se manca, errore chiaro
    invece di un crash silenzioso.
    """
    from blueprints.magazzino.routes import calcola_alert_fabbisogno_codici_padre
    import os, requests as req

    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'errore': True,
                         'messaggio': "Manca la variabile d'ambiente OPENAI_API_KEY su Railway — chiedi a Maurizio di impostarla."}), 400

    righe_padre = calcola_alert_fabbisogno_codici_padre()
    righe_scorte = _calcola_alert_scorte()

    if not righe_padre and not righe_scorte:
        return jsonify({'ok': True, 'analisi': "Nessun alert attivo al momento — stock, scorte minime e fabbisogni risultano tutti a posto."})

    def fmt_padre(r):
        return (f"- {r['codice']} ({r['descrizione'] or 'senza descrizione'}): fabbisogno {r['fabbisogno']}, "
                f"scorta minima {r['scorta_minima']}, disponibile allargata {r['disponibile_allargata']}, "
                f"stock {r['stock']}, grezzo IW {r['grezzo_iw']}, ordinato in produzione {r['ordinato_produzione']}, "
                f"ordinato a fornitori {r['ordinato_fornitori']}")

    def fmt_scorta(r):
        if r['livello'] == 'grigio':
            return f"- {r['prodotto']}: nessuno storico di vendita né budget, non calcolabile"
        return (f"- {r['prodotto']} [{r['livello'].upper()}]: stock pronto {r['stock_pronto']}, "
                f"scorta minima {r['scorta_minima_pz']}, domanda media/gg {r['domanda_gg']}, "
                f"giorni di copertura {r['giorni_copertura']}, lead time rifornimento {r['lead_time_giorni']}gg")

    blocco_padre = '\n'.join(fmt_padre(r) for r in righe_padre) if righe_padre else '(nessun codice padre con fabbisogno > 0)'
    blocco_scorte = '\n'.join(fmt_scorta(r) for r in righe_scorte) if righe_scorte else '(nessun prodotto in tabella)'

    prompt = f"""Sei un consulente di produzione e supply chain per una piccola/media azienda di carpenteria metallica (arredo urbano e segnaletica stradale). Ti fornisco lo stato attuale di due tabelle di allarme scorte del loro sistema gestionale (IronProduction). Il tuo compito è dare consigli PRATICI e AZIONABILI, non una descrizione dei dati.

═══ TABELLA 1 — CODICI PADRE (prodotti finiti/assiemi) CON FABBISOGNO > 0 ═══
Il "fabbisogno" è quanto manca rispetto alla scorta minima impostata, tenendo conto di stock fisico, grezzo assemblato non ancora verniciato, ordinato a fornitori e ordinato in produzione interna. Fabbisogno > 0 = servirebbe intervenire.
{blocco_padre}

═══ TABELLA 2 — RISCHIO DI RIMANERE A TERRA (stock finito vs domanda storica/budget) ═══
Livello ROSSO = si esaurirà prima che un nuovo lotto possa arrivare (rischio reale di stockout). GIALLO = sotto scorta minima ma con margine. GRIGIO = dati insufficienti per calcolare.
{blocco_scorte}

Dammi una risposta strutturata in italiano, con questi punti:
1. **Priorità immediate** — quali 2-4 codici richiedono attenzione OGGI e perché (incrocia le due tabelle se un codice compare in entrambe: è più urgente).
2. **Per ciascun codice prioritario** — un suggerimento concreto: produrre di più, sollecitare un fornitore, aumentare la scorta minima se sembra sottostimata, o altro.
3. **Pattern generali** — se noti che più codici hanno lo stesso tipo di problema (es. tutti in attesa dello stesso fornitore, o tutti sulla stessa macchina collo di bottiglia), segnalalo.
4. **Cosa NON è urgente** — rassicura su cosa può aspettare, per non far percepire tutto come un'emergenza.

Sii diretto, concreto, e concentrati su AZIONI da fare, non su ripetere i numeri che hai già visto sopra. Massimo 350 parole."""

    try:
        r = req.post('https://api.openai.com/v1/chat/completions',
                      headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                      json={'model': 'gpt-4o', 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0.3},
                      timeout=45)
        r.raise_for_status()
        analisi = r.json()['choices'][0]['message']['content']
        return jsonify({'ok': True, 'analisi': analisi})
    except req.exceptions.RequestException as e:
        return jsonify({'errore': True, 'messaggio': f'Errore durante la chiamata a ChatGPT: {e}'}), 502
    except (KeyError, IndexError):
        return jsonify({'errore': True, 'messaggio': 'Risposta inattesa da ChatGPT.'}), 502


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
    # inserire a mano (vedi le funzioni sopra per le fonti dati). Le mappe
    # op_per_sku/lav_per_sku sono UNA query sola in tutto, condivisa da
    # tutti i prodotti — prima erano 2-3 query per OGNI prodotto (N+1),
    # il motivo principale per cui la board era lenta.
    #
    # 'Finiti IS' NON viene più aggiornato da WMS qui: anche con la cache
    # di 3 minuti, ogni volta che scadeva la board tornava a fare fino a N
    # chiamate HTTP sequenziali a WMS, bastava un attimo di lentezza della
    # rete o di WMS per sembrare bloccata ('non funziona'). Ora la board
    # apre SEMPRE istantanea con l'ultimo valore salvato — l'aggiornamento
    # da WMS è un'azione a parte (pulsante '🔄 Aggiorna stock WMS' o la
    # risincronizzazione su singola scheda), mai qualcosa che blocca la
    # navigazione normale.
    op_per_sku = _mappa_op_per_sku()
    lav_per_sku = _mappa_lavorazioni_terzisti_per_sku()
    giacenza_per_sku = _mappa_giacenza_per_sku()
    scorta_minima_per_sku = _mappa_scorta_minima_per_sku()
    skus_board = list({sku_da_nome_prodotto(p.prodotto) for p in prodotti if sku_da_nome_prodotto(p.prodotto)})
    grezzo_iw_per_sku = _grezzo_iw_per_codici(skus_board) if skus_board else {}
    for p in prodotti:
        _aggiorna_residuo_produzione(p, op_per_sku)
        _aggiorna_grezzi_e_trattamento(p, op_per_sku, lav_per_sku, grezzo_iw_per_sku)
        _sincronizza_finiti_iw_da_magazzino(p, giacenza_per_sku)
        # Attributo dinamico (non un campo del modello — mai salvato): solo
        # per il rendering di questa pagina, letto dal template come
        # p.scorta_minima_locale per la colonna SALDO C/SCORTA.
        sku = sku_da_nome_prodotto(p.prodotto)
        p.scorta_minima_locale = scorta_minima_per_sku.get(sku.upper()) if sku else None
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

    # Grezzi IW — stessa fonte condivisa di Magazzino/Alert Scorte Codici
    # Padre (_grezzo_iw_per_codici): quantità di prodotto finito dichiarata
    # completa in Saldatura E approvata dalla Direzione, più eventuali
    # rettifiche manuali. Distinto da 'Grezzi' (p.grezzi, box più sotto
    # nella scheda): quello è "prodotti lavorati, non ancora trattati"
    # calcolato da Dichiarazioni+DDT; questo è il dato ufficiale di
    # magazzino, la stessa identica fonte che vede Angelo su Materiali.
    grezzi_iw = _grezzo_iw_per_codici([sku]).get(sku, 0) if sku else 0

    risultato = {
        'sku': sku,
        'stock_verniciati': p.verniciati, 'stock_grezzi': p.grezzi, 'in_vern': p.in_vern,
        'stock_is': p.finiti_is, 'grezzi_iw': grezzi_iw,
        'riservato_clienti': p.riservato, 'scorta_minima': None,
        'ordini_clienti': [], 'ultimi_evasi': [],
        'saldo_contabile': p.saldo_contabile,
        'saldo_contabile_breve_termine': p.saldo_contabile_breve_termine,
        # Saldo Disponibile = Finiti IW + Finiti IS − Impegni: quello che si
        # può evadere SUBITO forzando il sistema, non tutto il saldo
        # contabile (che include anche grezzi/in trattamento, non ancora
        # pronti alla vendita).
        'saldo_disponibile': p.verniciati + p.finiti_is - p.riservato,
        # Saldo C/Scorta = Saldo LT meno la scorta minima configurata su
        # MasterLogistic-WMS (colonna "SCORTA MIN." della sua scheda
        # Articolo) — quanto REALMENTE si può impegnare oltre il buffer di
        # sicurezza. None finché non si riesce a leggere la scorta minima
        # da WMS (nessun valore locale di fallback: è un dato che vive solo
        # là, mai stato un campo modificabile qui).
        'saldo_scorta': None,
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
            risultato['scorta_minima'] = wms.get('scorta_minima')
            risultato['saldo_contabile'] = (
                p.grezzi + p.in_vern + p.verniciati + risultato['stock_is'] + p.in_prod - wms['riservato_clienti'])
            risultato['saldo_contabile_breve_termine'] = (
                p.in_vern + p.verniciati + risultato['stock_is'] - wms['riservato_clienti'])
            risultato['saldo_disponibile'] = p.verniciati + risultato['stock_is'] - wms['riservato_clienti']
            if wms.get('scorta_minima') is not None:
                risultato['saldo_scorta'] = risultato['saldo_contabile'] - wms['scorta_minima']
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
        if 'budget_mensile_pz' in d:
            valore = d['budget_mensile_pz']
            p.budget_mensile_pz = float(valore) if valore not in (None, '') else None
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
    + DDT terzisti, residuo da produrre dalle Commesse aperte, Finiti IW dal
    Magazzino locale — stessa identica logica automatica usata a ogni
    apertura board (vedi le funzioni _aggiorna_*/_sincronizza_* sopra),
    richiamabile qui a comando per una singola scheda (utile appena dopo un
    test, senza aspettare il prossimo giro).
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
    p.finiti_is_aggiornato_il = datetime.utcnow()
    _aggiorna_grezzi_e_trattamento(p)
    _aggiorna_residuo_produzione(p)
    _sincronizza_finiti_iw_da_magazzino(p)
    p.aggiornato_il = datetime.utcnow()
    log(f'Kanban: risincronizzato {p.prodotto} — grezzi={p.grezzi}, in_vern={p.in_vern}, '
        f'riservato={p.riservato}, finiti_is={p.finiti_is}, in_prod={p.in_prod}')
    db.session.commit()
    return jsonify({'ok': True, **kanban_to_dict(p)})


@kanban_bp.route('/api/kanban/sincronizza-wms-tutti', methods=['POST'])
def api_sincronizza_wms_tutti():
    """
    Aggiorna 'Finiti IS' da MasterLogistic-WMS per TUTTI i prodotti di una
    board (o di tutte, se sheet_key non passato) — azione ESPLICITA
    richiamata dal pulsante '🔄 Aggiorna stock WMS', mai dal caricamento
    normale della pagina (vedi index(): la board apre sempre istantanea
    con l'ultimo valore salvato, WMS non deve mai bloccare la navigazione).
    Qui invece l'utente ha scelto consapevolmente di aspettare, quindi le
    chiamate WMS si possono fare — restano comunque soggette alla cache di
    FINITI_IS_TTL_SECONDI, per non richiamare WMS su un prodotto già
    aggiornato pochi minuti fa.
    """
    sheet_key = request.args.get('sheet_key', '')
    q = KanbanProdotto.query
    if sheet_key:
        q = q.filter(db.or_(KanbanProdotto.sheet_key == sheet_key,
                             KanbanProdotto.sheet_key == sheet_key.replace('_', ' ')))
    prodotti = q.all()
    aggiornati = 0
    for p in prodotti:
        prima = p.finiti_is
        _aggiorna_finiti_is_da_wms(p)
        if p.finiti_is != prima:
            aggiornati += 1
    db.session.commit()
    log(f'Kanban: sincronizzazione WMS massiva — {aggiornati}/{len(prodotti)} prodotti aggiornati')
    return jsonify({'ok': True, 'totale': len(prodotti), 'aggiornati': aggiornati})

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

@kanban_bp.route('/api/kanban/riordina', methods=['POST'])
def api_riordina():
    """
    Ordine manuale a piacimento delle righe prodotto in un gruppo Kanban —
    Angelo trascina una riga su/giù e questa lista qui salva il nuovo ordine
    così com'è: niente logica automatica dietro, è una preferenza personale
    di lettura, non un dato calcolato. Riceve la lista COMPLETA degli id
    del gruppo nell'ordine desiderato e riscrive KanbanProdotto.sort_order
    in sequenza (0, 1, 2, ...) — bastano gli id del gruppo trascinato, non
    serve toccare gli altri gruppi.
    """
    d = request.get_json(force=True)
    ordine = d.get('ordine') or []
    if not isinstance(ordine, list) or not ordine:
        return jsonify({'ok': False, 'error': 'Lista ordine mancante o vuota'}), 400
    try:
        ids = [int(x) for x in ordine]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Id non validi nella lista ordine'}), 400
    prodotti = {p.id: p for p in KanbanProdotto.query.filter(KanbanProdotto.id.in_(ids)).all()}
    for posizione, pid in enumerate(ids):
        p = prodotti.get(pid)
        if p:
            p.sort_order = posizione
    db.session.commit()
    log(f'Kanban: riordinate manualmente {len(ids)} righe')
    return jsonify({'ok': True, 'aggiornate': len(ids)})


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
