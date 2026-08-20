from flask import Blueprint, render_template, jsonify, request, redirect, current_app
from datetime import datetime, date
import json
from masterlogistic_client import ottieni_scheda_kanban, MasterLogisticError
from models import (db, ArticoloML, DistintaBaseML, DistintaBaseWood, Commessa, RigaCommessa,
                    CentroCostoWood, CicloLavoroWood, ArticoloApprovvigionamento,
                    TIPI_APPROVVIGIONAMENTO, GiacenzaWood, MovimentoGiacenzaWood, OrdineProduzione,
                    ImpostazioneCostoWood, CostoStandardWood, VarianzaProduzioneWood,
                    CostoPianificatoCentroWood, DRIVER_ATTIVITA_WOOD, VOCI_COSTO_PIANIFICATO_WOOD,
                    CostoStandardVersioneWood, LegameCostoStandardOrdineWood,
                    CostoStandardVersioneDettaglioWood, CostoStandardVersioneFaseWood,
                    MatriceWood, RulloWood, LunghezzaBarraWood, SchedaLavorazioneWood,
                    DescrizioneCodiceWood,
                    ScortaMinimaWood, OrdineAcquistoWood, RigaOrdineAcquistoWood, LavorazioneTerzista,
                    EventoConsuntivoPP, RettificaGrezzoIW)

magazzino_bp = Blueprint('magazzino', __name__)

STATI_CHIUSI_COMMESSA = {"COMPLETATA", "SPEDITA", "ANNULLATA"}


@magazzino_bp.route('/magazzino')
def index():
    return render_template('magazzino/index.html', active='magazzino')


@magazzino_bp.route('/centri-costo-wood')
def pagina_centri_costo():
    return render_template('centri_costo_wood.html', active='centri_costo')


@magazzino_bp.route('/giacenza-wood')
def pagina_giacenza():
    return render_template('giacenza_wood.html', active='giacenza_wood')


@magazzino_bp.route('/costo-standard-wood')
def pagina_costo_standard():
    return render_template('costo_standard_wood.html', active='costo_standard')


# ══════════════════════════════════════════════════════════════════════════════
#  Nessuna anagrafica locale: si legge in tempo reale la tabella 'articoli'
#  di MasterLogistic tramite il bind Postgres 'masterlogistic' (stesso
#  progetto Railway, DB separato — vedi config.py e models.ArticoloML).
#  Sola lettura: nessuna route qui crea/modifica/elimina articoli.
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/api/materiali')
def api_lista():
    try:
        articoli = ArticoloML.query.order_by(ArticoloML.sku).all()
    except Exception as e:
        return jsonify({
            'errore': True,
            'messaggio': f'Connessione al database di MasterLogistic non disponibile ({e})',
        }), 503

    return jsonify([{
        'codice':         a.sku,
        'codice_esterno': a.codice_esterno,
        'descrizione':    a.descrizione,
        'stock':          a.stock or 0,
        'ordinati':       a.ordinati or 0,
        'incoming':       a.incoming or 0,
        'scorta_min':     a.scorta_minima or 0,
        'fornitore':      a.fornitore,
        'ordine_n':       a.ordine_n,
        'stato':          a.stato,
    } for a in articoli])


# ══════════════════════════════════════════════════════════════════════════════
#  CLASSIFICAZIONE APPROVVIGIONAMENTO — locale e separata dal Kanban
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/api/articoli/<path:codice>/approvvigionamento', methods=['GET', 'PUT'])
def api_approvvigionamento_articolo(codice):
    codice = codice.strip()
    if not codice:
        return jsonify({'ok': False, 'error': 'Codice articolo obbligatorio'}), 400
    record = ArticoloApprovvigionamento.query.filter_by(codice=codice).first()
    if request.method == 'GET':
        return jsonify({
            'ok': True, 'codice': codice,
            'tipo_approvvigionamento': record.tipo_approvvigionamento if record else 'DA_CLASSIFICARE',
            'lead_time_fornitura_giorni': record.lead_time_fornitura_giorni if record else None,
            'costo_acquisto_standard': record.costo_acquisto_standard if record else 0,
            'unita_misura': record.unita_misura if record else '',
        })
    try:
        d = request.get_json(force=True)
        tipo = d.get('tipo_approvvigionamento', 'DA_CLASSIFICARE')
        if tipo not in TIPI_APPROVVIGIONAMENTO:
            return jsonify({'ok': False, 'error': 'Tipo di approvvigionamento non valido'}), 400
        if record is None:
            record = ArticoloApprovvigionamento(codice=codice)
            db.session.add(record)
        record.tipo_approvvigionamento = tipo
        valore = d.get('lead_time_fornitura_giorni')
        record.lead_time_fornitura_giorni = float(valore) if valore not in (None, '') else None
        if 'costo_acquisto_standard' in d:
            costo = d.get('costo_acquisto_standard')
            record.costo_acquisto_standard = float(costo) if costo not in (None, '') else 0
        if 'unita_misura' in d:
            record.unita_misura = (d.get('unita_misura') or '').strip().upper()[:20]
        record.aggiornato_il = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'codice': codice,
                        'tipo_approvvigionamento': record.tipo_approvvigionamento,
                        'lead_time_fornitura_giorni': record.lead_time_fornitura_giorni,
                        'costo_acquisto_standard': record.costo_acquisto_standard,
                        'unita_misura': record.unita_misura})
    except (TypeError, ValueError):
        db.session.rollback()
        return jsonify({'ok': False, 'error': 'Lead time non valido'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  DISTINTA BASE (BOM) — anch'essa letta in sola lettura da MasterLogistic
#  (tabella distinta_base, bind 'masterlogistic' — vedi models.DistintaBaseML).
#  Esplosione ricorsiva multilivello con protezione anti-ciclo e profondità
#  massima; nessuna scrittura, MasterLogistic resta l'unica fonte di verità.
# ══════════════════════════════════════════════════════════════════════════════
def _esplodi_bom(codice, qta=1.0, _visitati=None, _profondita=0, _max_profondita=12):
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita:
        return []
    _visitati = _visitati | {codice}

    righe = DistintaBaseML.query.filter_by(codice_padre=codice).order_by(DistintaBaseML.livello).all()
    componenti = []
    for r in righe:
        art = ArticoloML.query.filter_by(sku=r.codice_figlio).first()
        qta_totale = (r.quantita or 1.0) * qta
        componenti.append({
            'codice':            r.codice_figlio,
            'descrizione':       art.descrizione if art else '',
            'quantita_unitaria': r.quantita,
            'quantita_totale':   round(qta_totale, 3),
            'stock':             art.stock if art else None,
            'fornitore':         art.fornitore if art else None,
            'note':              r.note or '',
            'figli':             _esplodi_bom(r.codice_figlio, qta_totale, _visitati, _profondita + 1, _max_profondita),
        })
    return componenti


def _raccogli_approvvigionamenti(codici):
    """Restituisce le classificazioni locali per gli SKU richiesti, in una sola query."""
    records = ArticoloApprovvigionamento.query.filter(
        ArticoloApprovvigionamento.codice.in_(codici)
    ).all() if codici else []
    return {r.codice: {
        'tipo_approvvigionamento': r.tipo_approvvigionamento,
        'lead_time_fornitura_giorni': r.lead_time_fornitura_giorni,
    } for r in records}


def _applica_approvvigionamenti(componenti, approvv_map):
    for c in componenti:
        c['approvvigionamento'] = approvv_map.get(c['codice'], {
            'tipo_approvvigionamento': 'DA_CLASSIFICARE',
            'lead_time_fornitura_giorni': None,
        })
        _applica_approvvigionamenti(c.get('figli', []), approvv_map)


@magazzino_bp.route('/api/distinta_base/<codice>')
def api_distinta_base(codice):
    try:
        art = ArticoloML.query.filter_by(sku=codice).first()
        componenti = _esplodi_bom(codice)
        codici = {codice}
        _raccogli_codici_bom(componenti, codici)
        approvv_map = _raccogli_approvvigionamenti(codici)
        _applica_approvvigionamenti(componenti, approvv_map)
    except Exception as e:
        return jsonify({
            'errore': True,
            'messaggio': f'Connessione al database di MasterLogistic non disponibile ({e})',
        }), 503

    return jsonify({
        'codice':      codice,
        'descrizione': art.descrizione if art else '',
        'trovato':     art is not None,
        'ha_bom':      len(componenti) > 0,
        'approvvigionamento': approvv_map.get(codice, {
            'tipo_approvvigionamento': 'DA_CLASSIFICARE',
            'lead_time_fornitura_giorni': None,
        }),
        'componenti':  componenti,
    })


# ══════════════════════════════════════════════════════════════════════════════
#  DISTINTA BASE IRON WOOD — copia LOCALE (tabella distinta_base_wood, nel
#  database di IronProduction, vedi models.DistintaBaseWood). A differenza
#  della sezione sopra, qui si legge E si scrive: MasterLogistic non c'entra,
#  gli articoli/stock restano quelli condivisi (ArticoloML), ma le righe di
#  distinta sono gestite qui per non toccare mai la tabella di MasterLogistic.
# ══════════════════════════════════════════════════════════════════════════════
def _righe_bom_attive_wood(codice_padre, query=None, mappa=None):
    """
    Righe di distinta_base_wood da usare per un dato codice_padre in TUTTE le
    esplosioni di default (albero BOM, fabbisogno/netting, costo standard):
    - righe senza gruppo_alternativa → sempre incluse (comportamento invariato);
    - righe con lo stesso gruppo_alternativa → mutuamente esclusive, si tiene
      SOLO quella con preferita=True (se per errore ce ne fosse più di una
      preferita nello stesso gruppo, si tiene la prima per id, per determinismo).
    Il parametro 'query' opzionale permette di riusare una query già filtrata
    (es. con order_by) invece di ripartire da DistintaBaseWood.query.
    Il parametro 'mappa' opzionale (vedi _carica_mappa_distinta_base_wood) evita
    del tutto la query: usa un dict {codice_padre: [righe]} già in memoria —
    indispensabile quando questa funzione viene chiamata molte volte di
    seguito (esplosione di più OP), altrimenti ogni nodo dell'albero costa
    una query separata e con distinte larghe/profonde diventa lentissimo.
    """
    if mappa is not None:
        righe = mappa.get(codice_padre, [])
    else:
        righe = (query if query is not None
                 else DistintaBaseWood.query.filter_by(codice_padre=codice_padre)).all()
    per_gruppo = {}
    risultato = []
    for r in righe:
        if not r.gruppo_alternativa:
            risultato.append(r)
            continue
        scelta_attuale = per_gruppo.get(r.gruppo_alternativa)
        if scelta_attuale is None or (r.preferita and not scelta_attuale.preferita) or \
           (r.preferita == scelta_attuale.preferita and r.id < scelta_attuale.id):
            per_gruppo[r.gruppo_alternativa] = r
    risultato.extend(per_gruppo.values())
    return risultato


def _carica_mappa_distinta_base_wood():
    """
    Carica TUTTA la distinta_base_wood in un solo colpo, raggruppata per
    codice_padre: {codice_padre: [righe]}. Da passare a _righe_bom_attive_wood/
    _esplodi_componenti_op quando si devono esplodere le distinte di MOLTI
    OP nella stessa richiesta (Monitor Macchina, Lista Lavoro) — una singola
    query invece di una per ogni nodo di ogni albero, che con distinte reali
    (non solo 4-5 componenti come nei test) può voler dire centinaia di
    query sequenziali e tempi di risposta di decine di secondi.
    """
    mappa = {}
    for r in DistintaBaseWood.query.all():
        mappa.setdefault(r.codice_padre, []).append(r)
    return mappa


def _esplodi_componenti_op(o, _max_profondita=15, mappa_distinta=None):
    """
    Ritorna un nodo per OGNI codice della distinta base di o.codice_articolo
    — l'articolo stesso (moltiplicatore 1) più TUTTI i suoi componenti a
    qualunque livello, ciascuno con il moltiplicatore di quantità cumulato
    dalla radice — usando solo l'alternativa attiva per gruppo (vedi
    _righe_bom_attive_wood). Un codice riusato in più punti dell'albero
    compare una sola volta (al moltiplicatore del primo punto in cui viene
    incontrato) — protezione anti-ciclo/doppio conteggio.
    Condivisa da Monitor Macchina e Lista Tagli (vive qui, non in un blueprint
    specifico, per evitare import circolari fra monitor e produzione_pp).
    'mappa_distinta' opzionale (vedi _carica_mappa_distinta_base_wood): se
    fornita, esplode SENZA query aggiuntive — indispensabile quando la si
    chiama per molti OP di seguito nella stessa richiesta.
    """
    risultato = [{'codice': o.codice_articolo, 'moltiplicatore': 1.0}]
    visitati = {o.codice_articolo}

    def walk(codice, moltiplicatore, profondita):
        if profondita > _max_profondita:
            return
        for r in _righe_bom_attive_wood(codice, mappa=mappa_distinta):
            if r.codice_figlio in visitati:
                continue
            visitati.add(r.codice_figlio)
            m = moltiplicatore * (r.quantita or 1.0)
            risultato.append({'codice': r.codice_figlio, 'moltiplicatore': m})
            walk(r.codice_figlio, m, profondita + 1)

    walk(o.codice_articolo, 1.0, 0)
    return risultato


def _esplodi_bom_wood(codice, qta=1.0, _visitati=None, _profondita=0, _max_profondita=12):
    """
    Costruisce l'albero grezzo dalla sola distinta_base_wood locale (nessuna
    query remota qui dentro — vedi _arricchisci_bom_wood per descrizioni/cicli,
    fatte in blocco DOPO aver costruito l'intero albero, non riga per riga).
    'livello_effettivo' = profondità reale nell'albero rispetto al codice
    padre esploso (1 = Sottogruppo 1° livello, 2 = Sottogruppo 2° livello...),
    calcolato qui — NON il campo 'livello' grezzo inserito a mano nell'import,
    che è solo un dato di riga e può non corrispondere alla vera profondità.
    """
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita:
        return []
    _visitati = _visitati | {codice}

    righe = _righe_bom_attive_wood(
        codice, query=DistintaBaseWood.query.filter_by(codice_padre=codice).order_by(DistintaBaseWood.livello))
    componenti = []
    for r in righe:
        qta_totale = (r.quantita or 1.0) * qta
        componenti.append({
            'id':                r.id,
            'codice':            r.codice_figlio,
            'descrizione':       '',
            'quantita_unitaria': r.quantita,
            'quantita_totale':   round(qta_totale, 3),
            'stock':             None,
            'fornitore':         None,
            'note':              r.note or '',
            'livello_effettivo': _profondita + 1,
            'cicli_lavoro':      [],
            'figli':             _esplodi_bom_wood(r.codice_figlio, qta_totale, _visitati, _profondita + 1, _max_profondita),
        })
    return componenti


def _raccogli_codici_bom(componenti, acc):
    for c in componenti:
        acc.add(c['codice'])
        _raccogli_codici_bom(c.get('figli', []), acc)


def _arricchisci_bom_wood(componenti, descr_map, fasi_map, approvv_map):
    for c in componenti:
        info = descr_map.get(c['codice'])
        if info:
            c['descrizione'] = info['descrizione']
            c['stock']       = info['stock']
            c['fornitore']   = info['fornitore']
        c['cicli_lavoro'] = fasi_map.get(c['codice'], [])
        c['approvvigionamento'] = approvv_map.get(c['codice'], {
            'tipo_approvvigionamento': 'DA_CLASSIFICARE',
            'lead_time_fornitura_giorni': None,
        })
        _arricchisci_bom_wood(c.get('figli', []), descr_map, fasi_map, approvv_map)


@magazzino_bp.route('/api/distinta_base_wood/<codice>')
def api_distinta_base_wood(codice):
    componenti = _esplodi_bom_wood(codice)

    tutti_codici = {codice}
    _raccogli_codici_bom(componenti, tutti_codici)

    descr_map = {}
    try:
        for a in ArticoloML.query.filter(ArticoloML.sku.in_(tutti_codici)).all():
            descr_map[a.sku] = {'descrizione': a.descrizione, 'stock': a.stock, 'fornitore': a.fornitore}
    except Exception:
        db.session.rollback()

    # Riserva locale (da caricamento massivo Zucchetti, colonna DESCOM) per i
    # codici che ArticoloML non conosce ancora — mai in sovrascrittura.
    codici_senza_descr = tutti_codici - set(descr_map.keys())
    if codici_senza_descr:
        for d in DescrizioneCodiceWood.query.filter(DescrizioneCodiceWood.codice.in_(codici_senza_descr)).all():
            if d.descrizione:
                descr_map[d.codice] = {'descrizione': d.descrizione, 'stock': None, 'fornitore': None}

    fasi_map = {}
    for f in (CicloLavoroWood.query.filter(CicloLavoroWood.codice.in_(tutti_codici))
              .order_by(CicloLavoroWood.codice, CicloLavoroWood.sequenza).all()):
        fasi_map.setdefault(f.codice, []).append({
            'sequenza':             f.sequenza,
            'centro_costo_nome':    f.centro_costo.nome if f.centro_costo else '',
            'centro_costo_esterno': f.centro_costo.esterno if f.centro_costo else False,
            'produttivita_oraria':  f.produttivita_oraria,
        })

    approvv_map = _raccogli_approvvigionamenti(tutti_codici)
    _arricchisci_bom_wood(componenti, descr_map, fasi_map, approvv_map)

    info_root = descr_map.get(codice)
    return jsonify({
        'codice':       codice,
        'descrizione':  info_root['descrizione'] if info_root else '',
        'trovato':      info_root is not None,
        'ha_bom':       len(componenti) > 0,
        'cicli_lavoro': fasi_map.get(codice, []),
        'approvvigionamento': approvv_map.get(codice, {
            'tipo_approvvigionamento': 'DA_CLASSIFICARE',
            'lead_time_fornitura_giorni': None,
        }),
        'componenti':   componenti,
    })


@magazzino_bp.route('/api/distinta_base_wood', methods=['GET'])
def api_lista_distinta_wood():
    """
    Restituisce le righe della distinta base Iron Wood per la tabella di
    gestione. Con un import massivo la tabella può avere migliaia di righe:
    per non bloccare il browser (fetch enorme + migliaia di <tr> nel DOM),
    di default ne restituisce al massimo LIMITE_DEFAULT, più recenti prima,
    e richiede una ricerca (?q=) per vedere righe specifiche.
    """
    LIMITE_DEFAULT = 200
    q = (request.args.get('q') or '').strip()
    query = DistintaBaseWood.query
    totale = query.count()
    if q:
        pattern = f'%{q}%'
        query = query.filter(
            db.or_(DistintaBaseWood.codice_padre.ilike(pattern),
                   DistintaBaseWood.codice_figlio.ilike(pattern))
        )
    righe = query.order_by(DistintaBaseWood.id.desc()).limit(LIMITE_DEFAULT).all()
    return jsonify({
        'righe': [{
            'id':            r.id,
            'codice_padre':  r.codice_padre,
            'codice_figlio': r.codice_figlio,
            'quantita':      r.quantita,
            'livello':       r.livello,
            'note':          r.note or '',
            'gruppo_alternativa': r.gruppo_alternativa or '',
            'preferita':          bool(r.preferita) if r.gruppo_alternativa else None,
        } for r in righe],
        'totale':      totale,
        'mostrate':    len(righe),
        'filtrato':    bool(q),
        'limite':      LIMITE_DEFAULT,
    })


@magazzino_bp.route('/api/distinta_base_wood', methods=['POST'])
def api_add_distinta_wood():
    """Aggiunge (o aggiorna se già esiste la stessa coppia padre+figlio) una riga alla distinta base Iron Wood."""
    try:
        data = request.get_json(force=True)
        padre  = (data.get('codice_padre')  or '').strip().upper()
        figlio = (data.get('codice_figlio') or '').strip().upper()
        if not padre or not figlio:
            return jsonify({'errore': True, 'messaggio': 'Codice padre e codice figlio sono obbligatori.'}), 400
        if padre == figlio:
            return jsonify({'errore': True, 'messaggio': 'Un articolo non può essere componente di se stesso.'}), 400

        quantita = float(data.get('quantita') or 1.0)
        livello  = int(data.get('livello') or 1)
        note     = (data.get('note') or '').strip()
        # gruppo_alternativa: vuoto/assente = riga normale (nessuna alternativa).
        # Se valorizzato, la riga entra in un gruppo di componenti mutuamente
        # esclusivi per lo stesso codice_padre (es. barra 7m vs barra 6m) — solo
        # quella con preferita=True viene usata nell'esplosione BOM di default.
        gruppo_alternativa = (data.get('gruppo_alternativa') or '').strip().upper() or None
        preferita = bool(data.get('preferita', True)) if gruppo_alternativa else True

        esistente = DistintaBaseWood.query.filter_by(codice_padre=padre, codice_figlio=figlio).first()
        if esistente:
            esistente.quantita = quantita
            esistente.livello  = livello
            esistente.note     = note
            esistente.gruppo_alternativa = gruppo_alternativa
            esistente.preferita = preferita
        else:
            db.session.add(DistintaBaseWood(
                codice_padre=padre, codice_figlio=figlio,
                quantita=quantita, livello=livello, note=note,
                gruppo_alternativa=gruppo_alternativa, preferita=preferita,
                creato_il=datetime.utcnow()
            ))
        # Se questa riga diventa la preferita di un gruppo, tutte le altre righe
        # dello stesso codice_padre+gruppo_alternativa smettono di esserlo — un
        # solo componente attivo per gruppo, sempre.
        if gruppo_alternativa and preferita:
            (DistintaBaseWood.query
             .filter_by(codice_padre=padre, gruppo_alternativa=gruppo_alternativa)
             .filter(DistintaBaseWood.codice_figlio != figlio)
             .update({'preferita': False}))
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500


@magazzino_bp.route('/api/distinta_base_wood/<int:id_riga>/seleziona_alternativa', methods=['POST'])
def api_seleziona_alternativa_wood(id_riga):
    """
    Rende questa riga la componente ATTIVA (preferita) del suo gruppo di
    alternative, disattivando le altre righe dello stesso codice_padre +
    gruppo_alternativa — es. per passare da 'barra 7m' a 'barra 6m' su un
    articolo quando cambia la disponibilità del materiale in magazzino.
    """
    riga = DistintaBaseWood.query.get_or_404(id_riga)
    if not riga.gruppo_alternativa:
        return jsonify({'errore': True, 'messaggio': 'Questa riga non fa parte di un gruppo di alternative.'}), 400
    (DistintaBaseWood.query
     .filter_by(codice_padre=riga.codice_padre, gruppo_alternativa=riga.gruppo_alternativa)
     .update({'preferita': False}))
    riga.preferita = True
    db.session.commit()
    return jsonify({'ok': True})


@magazzino_bp.route('/api/distinta_base_wood/<int:id_riga>', methods=['DELETE'])
def api_del_distinta_wood(id_riga):
    """Elimina una riga dalla distinta base Iron Wood."""
    try:
        riga = DistintaBaseWood.query.get_or_404(id_riga)
        db.session.delete(riga)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500


@magazzino_bp.route('/api/distinta_base_wood/<int:id_riga>', methods=['PUT'])
def api_modifica_distinta_wood(id_riga):
    """Modifica esplicitamente una riga BOM identificata dal suo ID."""
    try:
        riga = DistintaBaseWood.query.get_or_404(id_riga)
        data = request.get_json(force=True) or {}
        padre = (data.get('codice_padre') or riga.codice_padre).strip().upper()
        figlio = (data.get('codice_figlio') or riga.codice_figlio).strip().upper()
        if not padre or not figlio or padre == figlio:
            return jsonify({'errore': True, 'messaggio': 'Padre e figlio sono obbligatori e devono essere diversi.'}), 400
        try:
            quantita = float(data.get('quantita', riga.quantita))
            livello = int(data.get('livello', riga.livello or 1))
        except (ValueError, TypeError):
            return jsonify({'errore': True, 'messaggio': 'Quantità e livello devono essere numerici.'}), 400
        if quantita <= 0 or livello < 1:
            return jsonify({'errore': True, 'messaggio': 'La quantità deve essere maggiore di zero e il livello almeno 1.'}), 400
        doppia = DistintaBaseWood.query.filter_by(codice_padre=padre, codice_figlio=figlio).filter(DistintaBaseWood.id != id_riga).first()
        if doppia:
            return jsonify({'errore': True, 'messaggio': 'Esiste già una riga con lo stesso padre e figlio.'}), 400
        # Verifica ciclo indiretto: dopo padre -> figlio, figlio non deve poter raggiungere padre.
        archi = {}
        for x in DistintaBaseWood.query.filter(DistintaBaseWood.id != id_riga).all():
            archi.setdefault(x.codice_padre, set()).add(x.codice_figlio)
        archi.setdefault(padre, set()).add(figlio)
        da_visitare, visitati = [figlio], set()
        while da_visitare:
            c = da_visitare.pop()
            if c == padre:
                return jsonify({'errore': True, 'messaggio': 'Modifica non consentita: creerebbe un ciclo nella distinta base.'}), 400
            if c not in visitati:
                visitati.add(c); da_visitare.extend(archi.get(c, set()) - visitati)
        riga.codice_padre, riga.codice_figlio = padre, figlio
        riga.quantita, riga.livello = quantita, livello
        riga.note = (data.get('note') or '').strip()
        riga.gruppo_alternativa = (data.get('gruppo_alternativa') or '').strip().upper() or None
        riga.preferita = bool(data.get('preferita', True)) if riga.gruppo_alternativa else True
        if riga.gruppo_alternativa and riga.preferita:
            (DistintaBaseWood.query.filter_by(codice_padre=padre, gruppo_alternativa=riga.gruppo_alternativa)
             .filter(DistintaBaseWood.id != riga.id).update({'preferita': False}))
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500


def _sostituisci_sottoalbero_distinta(radici):
    """
    Cancella TUTTE le righe di distinta base (DistintaBaseWood) che
    discendono da una di queste radici — attraversando ricorsivamente
    padre→figlio, non solo il primo livello — così un caricamento massivo
    SOSTITUISCE davvero la struttura vecchia invece di accumularsi sopra
    (che lasciava rami con la vecchia numerazione appesi per sempre quando
    Zucchetti rinominava i codici intermedi).

    NON tocca SchedaLavorazioneWood (i "Parametri" impostati a mano) — è una
    tabella separata, indicizzata per la STESSA coppia padre/figlio: se la
    coppia ricompare identica nel nuovo file, i suoi parametri restano
    collegati automaticamente; se non ricompare più, restano semplicemente
    orfani (mai cancellati, per non perdere dati per errore).

    Ritorna il numero di righe cancellate.
    """
    da_visitare = list(radici)
    visitati = set()
    cancellate = 0
    while da_visitare:
        padre = da_visitare.pop()
        if padre in visitati:
            continue
        visitati.add(padre)
        figli = [r.codice_figlio for r in DistintaBaseWood.query.filter_by(codice_padre=padre).all()]
        if figli:
            cancellate += DistintaBaseWood.query.filter_by(codice_padre=padre).delete(synchronize_session=False)
            da_visitare.extend(figli)
    return cancellate


@magazzino_bp.route('/api/distinta_base_wood/importa', methods=['POST'])
def api_importa_distinta_wood():
    """
    Caricamento massivo da file Excel/CSV per la distinta Iron Wood.
    SOSTITUISCE la distinta base delle radici (CODART / codice_padre di primo
    livello) presenti nel file — cancella l'intero sottoalbero vecchio di ogni
    radice PRIMA di ricaricare quello nuovo, così una ristrutturazione dei
    codici intermedi lato Zucchetti (es. rinominati PINX1-x → PINX100-x) non
    lascia i rami vecchi appesi accanto ai nuovi. I Parametri di Lavorazione
    (SchedaLavorazioneWood) NON vengono mai toccati da questa cancellazione —
    tabella separata, indicizzata per la stessa coppia padre/figlio.
    """
    file = request.files.get('file_excel')
    if not file:
        return jsonify({'errore': True, 'messaggio': 'Nessun file selezionato.'}), 400
    try:
        import io
        import pandas as pd
        filename = file.filename.lower()
        raw = file.read()
        df = None
        if filename.endswith('.xls'):
            try:
                df = pd.read_excel(io.BytesIO(raw), engine='xlrd')
            except Exception:
                try:
                    df = pd.read_excel(io.BytesIO(raw), engine='openpyxl')
                except Exception as e:
                    return jsonify({'errore': True, 'messaggio': f'Errore lettura XLS: {e}'}), 400
        elif filename.endswith('.xlsx'):
            try:
                df = pd.read_excel(io.BytesIO(raw), engine='openpyxl')
            except Exception as e:
                return jsonify({'errore': True, 'messaggio': f'Errore lettura XLSX: {e}'}), 400
        elif filename.endswith('.csv'):
            for enc in ['utf-8', 'latin-1', 'cp1252']:
                try:
                    df = pd.read_csv(io.BytesIO(raw), encoding=enc, sep=None, engine='python')
                    break
                except Exception:
                    pass
        if df is None:
            return jsonify({'errore': True, 'messaggio': 'Formato file non supportato o illeggibile (usare .xlsx, .xls o .csv).'}), 400

        df.columns = [str(c).strip().lower() for c in df.columns]

        # ── FORMATO A: export Zucchetti multilivello (CODART, CODCOM, NUMLEV, QTAMOV, ...) ──
        # Un codice prodotto (CODART) ripetuto su più righe; NUMLEV = livello di
        # annidamento; CODCOM contiene il codice del componente preceduto da un
        # prefisso di punti/numero d'ordine (es. ". . 3 P6025" → componente "P6025").
        # Il padre di una riga di livello N è l'ultimo componente visto al livello
        # N-1 (o il CODART stesso se N=1). NUMLEV=0 è la riga di intestazione del
        # prodotto stesso, non una relazione BOM reale: si salta.
        if {'codart', 'codcom', 'numlev', 'qtamov'}.issubset(set(df.columns)):
            nuovi = aggiornati = scartate = 0
            righe_scartate = []

            radici_nel_file = {str(v).strip().upper() for v in df['codart'].dropna().unique()
                               if str(v).strip() and str(v).strip().lower() != 'nan'}
            righe_cancellate = _sostituisci_sottoalbero_distinta(radici_nel_file)
            db.session.flush()

            esistenti = {(r.codice_padre, r.codice_figlio): r for r in DistintaBaseWood.query.all()}
            descrizioni_da_salvare = {}

            root_corrente = None
            stack = {}
            for i, row in df.iterrows():
                codart = str(row['codart']).strip().upper()
                if not codart or codart.lower() == 'nan':
                    continue
                if codart != root_corrente:
                    root_corrente = codart
                    stack = {}

                try:
                    numlev = int(row['numlev'])
                except (ValueError, TypeError):
                    continue

                # DESCOM è la descrizione del codice di QUESTA riga (radice
                # compresa, quando numlev==0) — salvata a parte nella riserva
                # locale, mai in ArticoloML (magazzino condiviso di un'altra
                # azienda, mai scritto da qui).
                descom_raw = str(row.get('descom', '')).strip()
                if descom_raw and descom_raw.lower() != 'nan':
                    codice_di_questa_riga = codart if numlev == 0 else None  # il figlio si calcola sotto
                    if codice_di_questa_riga:
                        descrizioni_da_salvare[codice_di_questa_riga] = descom_raw

                if numlev == 0:
                    continue  # riga di intestazione del prodotto, non un componente

                codcom_raw = str(row['codcom']).strip()
                child = codcom_raw.split()[-1].strip().upper() if codcom_raw else ''
                if not child or child.lower() == 'nan':
                    scartate += 1
                    righe_scartate.append(f"riga {i+2}: componente illeggibile in CODCOM ('{codcom_raw}')")
                    continue
                if descom_raw and descom_raw.lower() != 'nan':
                    descrizioni_da_salvare[child] = descom_raw

                padre = root_corrente if numlev == 1 else stack.get(numlev - 1, root_corrente)
                if padre == child:
                    scartate += 1
                    righe_scartate.append(f"riga {i+2}: {child} risulterebbe componente di se stesso, saltata")
                    stack[numlev] = child
                    continue

                try:
                    qta = float(row['qtamov']) if pd.notna(row['qtamov']) else 1.0
                except (ValueError, TypeError):
                    qta = 1.0
                note = str(row.get('db__note', '')).strip()
                if note.lower() in ('nan', 'none'):
                    note = ''

                chiave = (padre, child)
                if chiave in esistenti:
                    r = esistenti[chiave]
                    r.quantita, r.livello, r.note = qta, numlev, note
                    aggiornati += 1
                else:
                    nr = DistintaBaseWood(codice_padre=padre, codice_figlio=child,
                                          quantita=qta, livello=numlev, note=note,
                                          creato_il=datetime.utcnow())
                    db.session.add(nr)
                    esistenti[chiave] = nr  # evita duplicati se la stessa coppia ricompare nel file
                    nuovi += 1

                stack[numlev] = child

            if descrizioni_da_salvare:
                esistenti_descr = {d.codice: d for d in
                                   DescrizioneCodiceWood.query.filter(DescrizioneCodiceWood.codice.in_(descrizioni_da_salvare.keys())).all()}
                for cod, descr in descrizioni_da_salvare.items():
                    if cod in esistenti_descr:
                        esistenti_descr[cod].descrizione = descr
                    else:
                        db.session.add(DescrizioneCodiceWood(codice=cod, descrizione=descr))

            db.session.commit()
            return jsonify({
                'ok': True, 'nuovi': nuovi, 'aggiornati': aggiornati,
                'cancellate': righe_cancellate, 'radici_sostituite': sorted(radici_nel_file),
                'descrizioni_salvate': len(descrizioni_da_salvare),
                'scartate': scartate, 'righe_scartate': righe_scartate[:30]
            })

        # ── FORMATO B: colonne semplici dirette (codice_padre, codice_figlio, ...) ──
        col_padre  = next((c for c in ['codice_padre', 'padre', 'codice padre'] if c in df.columns), None)
        col_figlio = next((c for c in ['codice_figlio', 'figlio', 'codice figlio'] if c in df.columns), None)
        if not col_padre or not col_figlio:
            return jsonify({'errore': True,
                             'messaggio': f'Colonne codice_padre/codice_figlio non trovate. Colonne nel file: {list(df.columns)}'}), 400
        col_qta    = next((c for c in ['quantita', 'quantità', 'qta'] if c in df.columns), None)
        col_liv    = next((c for c in ['livello', 'liv'] if c in df.columns), None)
        col_note   = next((c for c in ['note', 'nota'] if c in df.columns), None)

        radici_nel_file_b = {str(v).strip().upper() for v in df[col_padre].dropna().unique()
                             if str(v).strip() and str(v).strip().lower() != 'nan'}
        righe_cancellate_b = _sostituisci_sottoalbero_distinta(radici_nel_file_b)
        db.session.flush()

        nuovi = aggiornati = scartate = 0
        righe_scartate = []
        for i, row in df.iterrows():
            padre  = str(row[col_padre]).strip().upper()
            figlio = str(row[col_figlio]).strip().upper()
            if not padre or not figlio or padre.lower() == 'nan' or figlio.lower() == 'nan':
                scartate += 1
                righe_scartate.append(f"riga {i+2}: codice padre o figlio mancante")
                continue
            if padre == figlio:
                scartate += 1
                righe_scartate.append(f"riga {i+2}: {padre} non può essere componente di se stesso")
                continue

            try:
                qta = float(str(row.get(col_qta, 1) if col_qta else 1).replace(',', '.') or 1)
            except (ValueError, TypeError):
                qta = 1.0
            try:
                liv = int(float(row.get(col_liv, 1))) if col_liv and pd.notna(row.get(col_liv)) else 1
            except (ValueError, TypeError):
                liv = 1
            note = str(row.get(col_note, '')).strip() if col_note else ''
            if note.lower() in ('nan', 'none'):
                note = ''

            esistente = DistintaBaseWood.query.filter_by(codice_padre=padre, codice_figlio=figlio).first()
            if esistente:
                esistente.quantita = qta
                esistente.livello  = liv
                esistente.note     = note
                aggiornati += 1
            else:
                db.session.add(DistintaBaseWood(
                    codice_padre=padre, codice_figlio=figlio,
                    quantita=qta, livello=liv, note=note,
                    creato_il=datetime.utcnow()
                ))
                nuovi += 1

        db.session.commit()
        return jsonify({
            'ok': True, 'nuovi': nuovi, 'aggiornati': aggiornati,
            'cancellate': righe_cancellate_b, 'radici_sostituite': sorted(radici_nel_file_b),
            'scartate': scartate, 'righe_scartate': righe_scartate[:30]
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  FABBISOGNO PRODUZIONE — pensato per essere interrogato da MasterLogistic
#  (pull, non push): un canale SEPARATO dagli ordini clienti via PDF, per non
#  mescolare mai le due cose nello stesso motore/dashboard.
# ══════════════════════════════════════════════════════════════════════════════
def _flatten_componenti(componenti, aggregato):
    """Somma ricorsivamente le quantità totali di ogni componente (a tutti i
    livelli) in un unico dizionario {codice: quantita_totale}."""
    for c in componenti:
        aggregato[c['codice']] = aggregato.get(c['codice'], 0) + (c['quantita_totale'] or 0)
        if c.get('figli'):
            _flatten_componenti(c['figli'], aggregato)


@magazzino_bp.route('/api/fabbisogno_produzione')
def api_fabbisogno_produzione():
    """
    Fabbisogno di materie prime/semilavorati per TUTTI gli Ordini di
    Produzione Iron Wood ancora aperti (stato in STATI_CHE_IMPEGNANO, saldo
    da produrre > 0): per ognuno esplode la distinta base locale
    (distinta_base_wood) e aggrega i componenti necessari a ogni livello.
    Nessuna scrittura qui: solo lettura, per essere interrogato da MasterLogistic.

    NOTA: prima leggeva dalla tabella Commesse/RigaCommessa (sistema Iron
    Segnaletica, mai popolato nel flusso reale — gli ordini clienti vivono
    in Iron Segnaletica/MasterLogistic-WMS), che lasciava questo endpoint
    sempre vuoto. Ora legge OrdineProduzione, il flusso Iron Wood realmente
    usato — stessa fonte del motore di netting giacenza/impegni.
    """
    aggregato = {}
    origine = {}  # codice -> lista di {commessa, riga_codice, quantita, op_code}

    ordini = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO)).all()
    for o in ordini:
        saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
        if saldo <= 0:
            continue
        componenti = _esplodi_bom_wood(o.codice_articolo, qta=saldo)
        if not componenti:
            continue
        locale = {}
        _flatten_componenti(componenti, locale)
        for codice, qta in locale.items():
            aggregato[codice] = aggregato.get(codice, 0) + qta
            origine.setdefault(codice, []).append({
                'commessa': o.commessa, 'riga_codice': o.codice_articolo,
                'quantita': round(qta, 3), 'op_code': o.codice,
            })

    risultato = []
    for codice, qta in aggregato.items():
        try:
            art = ArticoloML.query.filter_by(sku=codice).first()
        except Exception:
            db.session.rollback()
            art = None
        risultato.append({
            'codice':              codice,
            'descrizione':         art.descrizione if art else '',
            'quantita_necessaria': round(qta, 3),
            'stock_noto':          art.stock if art else None,
            'origine':             origine.get(codice, []),
        })
    risultato.sort(key=lambda x: x['codice'])
    return jsonify({'fabbisogno': risultato, 'generato_il': datetime.utcnow().isoformat()})


# ══════════════════════════════════════════════════════════════════════════════
#  GIACENZA FISICA IRON WOOD — magazzino LOCALE (non più letto da altrove):
#  carico iniziale/aggiornamento da Excel/CSV, rettifiche manuali, storico
#  movimenti, e motore di netting multilivello per il controllo scorta ad
#  ogni Ordine di Produzione (giacenza − impegni già presi da altri OP aperti
#  = disponibile; solo il mancante genera fabbisogno dei componenti).
# ══════════════════════════════════════════════════════════════════════════════
STATI_CHE_IMPEGNANO = ('Rilasciato', 'In esecuzione', 'Tecnicamente completato')


def _registra_movimento_giacenza(codice, delta, tipo, riferimento='', note='', costo_unitario=None):
    """Applica un delta (positivo=carico, negativo=scarico) alla giacenza di un
    codice e registra il movimento in storico. Non fa il commit (lo fa il chiamante).
    costo_unitario è facoltativo: se dato, il movimento viene valorizzato
    (valore = costo_unitario × |delta|) — usato per i carichi a costo standard."""
    g = GiacenzaWood.query.get(codice)
    if not g:
        g = GiacenzaWood(codice=codice, quantita=0)
        db.session.add(g)
    g.quantita = (g.quantita or 0) + delta
    g.aggiornato_il = datetime.utcnow()
    valore = round(costo_unitario * abs(delta), 4) if costo_unitario is not None else None
    db.session.add(MovimentoGiacenzaWood(codice=codice, tipo=tipo, quantita=delta,
                                          costo_unitario=costo_unitario, valore=valore,
                                          riferimento=riferimento, note=note))


ORDINATO_CLIENTE_WMS_TTL_SECONDI = 600  # non richiamare WMS per lo stesso codice più spesso di così


def _aggiorna_dati_wms(g, forza=False):
    """
    Aggiorna GiacenzaWood.ordinato_cliente_wms E .scorta_minima_wms
    leggendo da MasterLogistic-WMS in UNA sola chiamata (ottieni_scheda_
    kanban restituisce entrambi i campi insieme) — chiamata SOLO su
    richiesta esplicita (pulsante), MAI durante il caricamento normale di
    una pagina: una pagina non deve mai dipendere da una chiamata di rete
    esterna per aprirsi (stesso principio già applicato al Kanban).
    Rispetta un TTL per non richiamare WMS inutilmente su un codice
    appena aggiornato. Un fallimento lascia i valori precedenti invariati.
    """
    ora = datetime.utcnow()
    aggiornato_di_recente = (g.ordinato_cliente_wms_aggiornato_il and
        (ora - g.ordinato_cliente_wms_aggiornato_il).total_seconds() < ORDINATO_CLIENTE_WMS_TTL_SECONDI)
    if not forza and aggiornato_di_recente:
        return
    try:
        scheda = ottieni_scheda_kanban(g.codice)
    except MasterLogisticError:
        return
    # 'riservato_clienti' — stesso campo già confermato e usato per il
    # Kanban ('Riservato'/Impegnato Clienti), qui riletto come "Ordinato da
    # Cliente" per Iron Wood: quanto è impegnato per i clienti su questo
    # SKU nella scheda WMS.
    ordinato = scheda.get('riservato_clienti')
    if ordinato is not None:
        g.ordinato_cliente_wms = ordinato
        g.ordinato_cliente_wms_aggiornato_il = ora
    scorta_min = scheda.get('scorta_minima')
    if scorta_min is not None:
        g.scorta_minima_wms = scorta_min
        g.scorta_minima_wms_aggiornato_il = ora


@magazzino_bp.route('/api/giacenza_wood/<codice>/wms-raw')
def api_giacenza_wms_raw(codice):
    """
    DIAGNOSTICA — mostra la risposta GREZZA e completa di MasterLogistic-WMS
    per questo SKU (tutti i campi, non solo quelli che ottieni_scheda_kanban
    già usa) — serve a trovare il nome vero del campo 'scorta minima' (e di
    qualunque altro), dato che il nome usato oggi ('scorta_minima') è
    un'ipotesi non confermata e a quanto pare sbagliata. Da togliere una
    volta chiarito il contratto reale dell'API WMS con chi l'ha costruita.
    """
    from masterlogistic_client import _kanban_stock_grezzo, MasterLogisticError
    try:
        dati = _kanban_stock_grezzo(codice.strip())
    except MasterLogisticError as e:
        return jsonify({'errore': True, 'messaggio': str(e)}), 502
    return jsonify(dati)


@magazzino_bp.route('/api/giacenza_wood/sincronizza-ordinato-cliente-wms', methods=['POST'])
def api_sincronizza_ordinato_cliente_wms():
    """
    Aggiorna 'Ordinato da Cliente (WMS)' e 'Scorta Minima (WMS)' per un
    elenco di codici passato nel body (JSON: {"codici": [...]}), o per
    TUTTI i codici padre (quelli con almeno un Ordine di Produzione) se
    non specificato — azione ESPLICITA dal pulsante dedicato, mai automatica.
    """
    d = request.get_json(silent=True) or {}
    codici = d.get('codici')
    if not codici:
        codici = [r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
    if not codici:
        return jsonify({'ok': True, 'totale': 0, 'aggiornati': 0})

    righe_gz = GiacenzaWood.query.filter(GiacenzaWood.codice.in_(codici)).all()
    presenti = {g.codice for g in righe_gz}
    for c in sorted(set(codici) - presenti):
        nuova = GiacenzaWood(codice=c, quantita=0)
        db.session.add(nuova)
        righe_gz.append(nuova)

    aggiornati = 0
    for g in righe_gz:
        prima = (g.ordinato_cliente_wms, g.scorta_minima_wms)
        _aggiorna_dati_wms(g, forza=True)
        if (g.ordinato_cliente_wms, g.scorta_minima_wms) != prima:
            aggiornati += 1
    db.session.commit()
    return jsonify({'ok': True, 'totale': len(righe_gz), 'aggiornati': aggiornati})


def _calcola_campi_giacenza(righe):
    """
    Funzione CONDIVISA che calcola Impegnato/Ordinato/Grezzo IW/Ordinato in
    Produzione/Disponibilità (stretta e Allargata*)/Fabbisogno per un
    elenco di righe GiacenzaWood (vere o sintetiche, es. i codici della
    Distinta Base senza ancora una riga di giacenza propria) — usata SIA
    da /api/giacenza_wood (Magazzino) SIA da Alert Scorte Codici Padre,
    così le due pagine non possono MAI più mostrare un fabbisogno diverso
    per lo stesso codice: prima ognuna aveva la sua copia della formula e
    rischiavano (ed è successo) di disallinearsi.
    """
    codici = [g.codice for g in righe]
    if not codici:
        return []

    giacenza_residua = _giacenza_residua_dopo_impegni()
    impegnato = {g.codice: round((g.quantita or 0) - giacenza_residua.get(g.codice, g.quantita or 0), 4) for g in righe}

    ordinato = {}
    for cod, qta_orig, qta_ric in (db.session.query(RigaOrdineAcquistoWood.codice,
                                    RigaOrdineAcquistoWood.qta_originale, RigaOrdineAcquistoWood.qta_ricevuta)
                                    .join(OrdineAcquistoWood)
                                    .filter(RigaOrdineAcquistoWood.codice.in_(codici),
                                            OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all()):
        residuo = (qta_orig or 0) - (qta_ric or 0)
        if residuo > 0:
            ordinato[cod] = ordinato.get(cod, 0) + residuo

    # Scorta minima: SOLO da MasterLogistic-WMS (non più un campo locale
    # editabile — la scorta minima "vera" vive già dentro WMS, qui è solo
    # una lettura di quel valore; None = non ancora sincronizzato, 0 non è
    # un'assunzione sicura da fare al posto suo).
    grezzo_iw = {}
    for cod, tot in (db.session.query(OrdineProduzione.codice_articolo,
                      db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                      .join(EventoConsuntivoPP, EventoConsuntivoPP.op_code == OrdineProduzione.codice)
                      .filter(OrdineProduzione.codice_articolo.in_(codici),
                              EventoConsuntivoPP.componente.is_(None),
                              db.func.lower(EventoConsuntivoPP.fase) == 'saldatura',
                              EventoConsuntivoPP.approvato_direzione.is_(True))
                      .group_by(OrdineProduzione.codice_articolo).all()):
        grezzo_iw[cod] = tot or 0
    for cod, delta_tot in (db.session.query(RettificaGrezzoIW.codice, db.func.sum(RettificaGrezzoIW.delta))
                            .filter(RettificaGrezzoIW.codice.in_(codici))
                            .group_by(RettificaGrezzoIW.codice).all()):
        grezzo_iw[cod] = grezzo_iw.get(cod, 0) + (delta_tot or 0)

    ordinato_produzione = {}
    for cod, qta_pian, qta_buona in (db.session.query(OrdineProduzione.codice_articolo,
                                      OrdineProduzione.qta_pianificata, OrdineProduzione.qta_buona)
                                      .filter(OrdineProduzione.codice_articolo.in_(codici),
                                              OrdineProduzione.stato.in_(['Creato', 'Rilasciato', 'In esecuzione'])).all()):
        residuo = max((qta_pian or 0) - (qta_buona or 0), 0)
        if residuo > 0:
            ordinato_produzione[cod] = ordinato_produzione.get(cod, 0) + residuo

    descrizioni = {}
    for cod, descr in (db.session.query(RigaOrdineAcquistoWood.codice, RigaOrdineAcquistoWood.descrizione)
                        .filter(RigaOrdineAcquistoWood.codice.in_(codici))
                        .order_by(RigaOrdineAcquistoWood.id.desc()).all()):
        descrizioni.setdefault(cod, descr)

    approvvigionamenti = {a.codice: a for a in ArticoloApprovvigionamento.query.filter(ArticoloApprovvigionamento.codice.in_(codici)).all()}

    # Tipologia — incrocia le stesse fonti già usate nel resto dell'app:
    # Codice Padre = ha almeno un Ordine di Produzione proprio (stessa
    # definizione di calcola_alert_fabbisogno_codici_padre). Se non lo è,
    # guarda tipo_approvvigionamento (Parametri di Lavorazione). Se manca
    # anche quello ma ha un Ciclo di Lavoro proprio, è un semilavorato
    # realizzato internamente (lavorato ma mai venduto/prodotto come OP
    # a sé). Altrimenti non ancora classificato.
    codici_padre = {r[0] for r in db.session.query(OrdineProduzione.codice_articolo)
                     .filter(OrdineProduzione.codice_articolo.in_(codici)).distinct().all()}
    codici_con_ciclo = {r[0] for r in db.session.query(CicloLavoroWood.codice)
                         .filter(CicloLavoroWood.codice.in_(codici)).distinct().all()}
    LABEL_TIPO_APPROVVIGIONAMENTO = {
        'MATERIA_PRIMA_FORNITORE': 'Materia Prima (fornitore)',
        'COMPONENTE_ACQUISTO': "Componente d'Acquisto",
        'LASERATO': 'Laserato',
    }

    def _tipologia(codice):
        if codice in codici_padre:
            return 'Codice Padre'
        art = approvvigionamenti.get(codice)
        if art and art.tipo_approvvigionamento in LABEL_TIPO_APPROVVIGIONAMENTO:
            return LABEL_TIPO_APPROVVIGIONAMENTO[art.tipo_approvvigionamento]
        if codice in codici_con_ciclo:
            return 'Semilavorato (centri di costo)'
        return 'Da classificare'

    righe_out = []
    for g in righe:
        imp = impegnato.get(g.codice, 0)
        ord_ = ordinato.get(g.codice, 0)
        grz = grezzo_iw.get(g.codice, 0)
        ord_prod = ordinato_produzione.get(g.codice, 0)
        scorta_min = getattr(g, 'scorta_minima_wms', None) or 0
        ord_cliente_wms = getattr(g, 'ordinato_cliente_wms', None) or 0
        disp_contabile = round((g.quantita or 0) - imp + ord_, 4)
        disp_allargata = round((g.quantita or 0) + grz - imp + ord_ + ord_prod, 4)
        # Fabbisogno = MAX(Scorta Minima (da WMS) + Ordinato da Cliente
        # (WMS) − Disp. Contabile Allargata, 0): un ordine cliente reale
        # letto da WMS pesa di più della sola scorta di sicurezza — non la
        # sostituisce, si somma. Se un campo WMS non è mai stato
        # aggiornato (None), conta 0, non blocca il calcolo.
        fabbisogno = round(max(scorta_min + ord_cliente_wms - disp_allargata, 0), 4)
        righe_out.append({
            'codice': g.codice, 'descrizione': descrizioni.get(g.codice, ''), 'quantita': g.quantita,
            'unita_misura': approvvigionamenti.get(g.codice).unita_misura if approvvigionamenti.get(g.codice) else '',
            'tipologia': _tipologia(g.codice),
            'impegnato': imp, 'ordinato': ord_, 'disponibile_contabile': disp_contabile,
            'grezzo_iw': grz, 'ordinato_produzione': ord_prod, 'disponibile_allargata': disp_allargata,
            'scorta_minima': getattr(g, 'scorta_minima_wms', None),
            'scorta_minima_wms_aggiornato_il': (g.scorta_minima_wms_aggiornato_il.strftime('%d/%m/%Y %H:%M')
                if getattr(g, 'scorta_minima_wms_aggiornato_il', None) else None),
            'ordinato_cliente_wms': getattr(g, 'ordinato_cliente_wms', None),
            'ordinato_cliente_wms_aggiornato_il': (g.ordinato_cliente_wms_aggiornato_il.strftime('%d/%m/%Y %H:%M')
                if getattr(g, 'ordinato_cliente_wms_aggiornato_il', None) else None),
            'fabbisogno': fabbisogno,
            'aggiornato_il': g.aggiornato_il.strftime('%d/%m/%Y %H:%M') if getattr(g, 'aggiornato_il', None) else '',
        })
    return righe_out


def calcola_alert_fabbisogno_codici_padre():
    """
    Codici PADRE = codici per cui esiste ALMENO UN Ordine di Produzione
    (codice_articolo) — cioè prodotti finiti gestiti come articolo
    autonomo, non semilavorati (i semilavorati non hanno mai un proprio
    OP: vengono lavorati DENTRO l'OP del prodotto finito, tracciati per
    componente — vedi EventoConsuntivoPP.componente). Più solido della
    posizione nella Distinta Base: un codice padre può benissimo comparire
    ANCHE come componente di un altro assieme più grande (es. un kit) e
    restare comunque un prodotto finito a sé — la Distinta Base da sola
    non basta a distinguerlo.
    Il cui Fabbisogno è diverso da zero. Riusa _calcola_campi_giacenza —
    LA STESSA funzione di /api/giacenza_wood (Magazzino) — così non può
    mai dare un numero diverso da quello che vedi in Magazzino per lo
    stesso codice.
    """
    from models import GiacenzaWood

    codici_padre = sorted({r[0] for r in db.session.query(OrdineProduzione.codice_articolo).distinct().all()})
    if not codici_padre:
        return []

    righe_gz = GiacenzaWood.query.filter(GiacenzaWood.codice.in_(codici_padre)).all()
    presenti = {g.codice for g in righe_gz}
    # Un codice padre senza ancora nessuna riga di giacenza propria (mai
    # movimentato) deve comunque poter avere fabbisogno visibile — stessa
    # regola già usata in /api/giacenza_wood per non nasconderlo.
    righe_gz = list(righe_gz) + [GiacenzaWood(codice=c, quantita=0) for c in sorted(set(codici_padre) - presenti)]

    righe_out = _calcola_campi_giacenza(righe_gz)
    risultati = [r for r in righe_out if r['fabbisogno'] > 0]
    risultati.sort(key=lambda r: -r['fabbisogno'])
    return risultati


@magazzino_bp.route('/api/giacenza_wood', methods=['GET'])
def api_giacenza_lista():
    """Elenco giacenze, con limite di default (200) + ricerca — stesso principio
    già usato per distinta_base_wood, per non appesantire la pagina con import enormi.
    Arricchito con la metodologia MasterLogistic-WMS: Impegnato (da OP aperti),
    Ordinato (da OA ancora da ricevere), Disponibile Contabile e Fabbisogno
    rispetto alla Scorta Minima configurabile.
    ?solo_bom=1 restringe ai codici che compaiono davvero nella Distinta Base
    Iron Wood (come padre o come figlio) — usato dalla pagina Magazzino per
    escludere qualunque codice estraneo (es. rimasto da un vecchio import)."""
    LIMITE_DEFAULT = 200
    q = (request.args.get('q') or '').strip()
    solo_bom = request.args.get('solo_bom') == '1'
    query = GiacenzaWood.query
    if solo_bom:
        codici_bom = {r[0] for r in db.session.query(DistintaBaseWood.codice_padre).distinct().all()} | \
                     {r[0] for r in db.session.query(DistintaBaseWood.codice_figlio).distinct().all()}
        query = query.filter(GiacenzaWood.codice.in_(codici_bom))
    totale = query.count()
    codici_bom_ricerca = set()
    if q:
        codici_per_descrizione = {c for c, in db.session.query(RigaOrdineAcquistoWood.codice)
                                   .filter(RigaOrdineAcquistoWood.descrizione.ilike(f'%{q}%')).distinct().all()}
        query = query.filter(db.or_(GiacenzaWood.codice.ilike(f'%{q}%'),
                                     GiacenzaWood.codice.in_(codici_per_descrizione)))
        # Un codice della Distinta Base senza NESSUNA giacenza registrata non
        # esiste come riga GiacenzaWood: senza questo, cercandolo per nome non
        # lo si troverebbe MAI, impedendo di inserirne il primo carico.
        if solo_bom:
            ql = q.lower()
            codici_bom_ricerca = {c for c in codici_bom if ql in c.lower()}
    righe = query.order_by(GiacenzaWood.aggiornato_il.desc()).limit(LIMITE_DEFAULT).all()
    # Giacenza Iron Wood è guidata dalla distinta impostata in Parametri: anche
    # un componente senza movimento/stock registrato deve comparire a zero, per
    # rendere visibile il fabbisogno invece di nasconderlo.
    if solo_bom and not q:
        presenti = {g.codice for g in righe}
        mancanti = sorted(codici_bom - presenti)
        spazio = max(LIMITE_DEFAULT - len(righe), 0)
        righe.extend(GiacenzaWood(codice=codice, quantita=0) for codice in mancanti[:spazio])
        totale = len(codici_bom)
    elif solo_bom and q and codici_bom_ricerca:
        presenti = {g.codice for g in righe}
        mancanti = sorted(codici_bom_ricerca - presenti)
        righe.extend(GiacenzaWood(codice=codice, quantita=0) for codice in mancanti)
        totale += len(mancanti)
    codici = [g.codice for g in righe]

    righe_out = _calcola_campi_giacenza(righe)

    return jsonify({
        'righe': righe_out,
        'totale': totale, 'mostrate': len(righe), 'filtrato': bool(q), 'limite': LIMITE_DEFAULT,
    })


@magazzino_bp.route('/api/giacenza_wood/<codice>/impegni-op')
def api_giacenza_impegni_op(codice):
    """
    Per un codice specifico: QUALI Ordini di Produzione aperti generano
    l'"Impegnato OP" mostrato nella pagina Materiali, e quanto ciascuno —
    stesso popup pattern già presente in MasterLogistic-WMS per gli Impegni
    Clienti, qui applicato agli OP invece che agli ordini di vendita.
    Ripete la STESSA simulazione priorità-based di _giacenza_residua_dopo_impegni
    (stessa giacenza condivisa, consumata in ordine di priorità), ma invece
    di buttare via il dettaglio per-OP (che quella funzione scarta), lo
    tiene: per ogni OP che tocca questo codice nella sua esplosione di
    distinta, quanto ne ha effettivamente consumato dalla giacenza
    (net_usato) prima che passasse al successivo.
    """
    codice = codice.strip()
    giacenza_residua = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    op_aperti = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
                 .order_by(OrdineProduzione.priorita.asc(), OrdineProduzione.id.asc()).all())
    mappa = _carica_mappa_distinta_base_wood()
    righe = []
    for op in op_aperti:
        saldo = (op.qta_pianificata or 0) - (op.qta_buona or 0)
        if saldo <= 0:
            continue
        out = {}
        _netta_e_esplodi_wood(op.codice_articolo, saldo, giacenza_residua, out, mappa=mappa)
        tocco = out.get(codice)
        if tocco and tocco['usato'] > 0:
            righe.append({
                'op_code': op.codice, 'codice_articolo': op.codice_articolo,
                'commessa': op.commessa or '', 'priorita': op.priorita,
                'stato': op.stato, 'consumato': round(tocco['usato'], 4),
            })
    return jsonify(ok=True, codice=codice, impegni=righe,
                    totale=round(sum(r['consumato'] for r in righe), 4))


@magazzino_bp.route('/api/ordini-produzione/<op_code>/diagnostica-esplosione')
def api_diagnostica_esplosione_op(op_code):
    """
    Diagnostica GREZZA dell'esplosione distinta base di UN OP specifico:
    ogni nodo toccato (fabbisogno, quanto trovato in giacenza, quanto
    mancante che continua a scendere sotto), PIÙ la giacenza fisica di ogni
    nodo così com'è nel database in questo momento. Serve a vedere ESATTAMENTE
    a quale livello l'esplosione smette di trovare scorta (es. il semilavorato
    che una fase ha già prodotto: se qui risulta 0, il carico a magazzino di
    quella dichiarazione non è mai avvenuto o è finito da qualche altra parte;
    se risulta ok ma l'esplosione lo ignora comunque, il problema è nel
    netting). ATTENZIONE: gira l'esplosione ISOLATA per questo solo OP,
    partendo dalla giacenza attuale — non replica l'ordine di priorità
    condiviso con gli altri OP aperti (quello lo fa /impegni-op), qui serve
    solo a vedere l'albero di QUESTO OP da solo.
    """
    o = OrdineProduzione.query.filter_by(codice=op_code.strip()).first()
    if not o:
        return jsonify(ok=False, error='Ordine di produzione non trovato'), 404
    saldo = (o.qta_pianificata or 0) - (o.qta_buona or 0)
    giacenza_residua = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    out = {}
    if saldo > 0:
        _netta_e_esplodi_wood(o.codice_articolo, saldo, giacenza_residua, out)
    nodi = [{
        'codice': cod, 'fabbisogno': round(r['fabbisogno'], 4), 'usato_da_giacenza': round(r['usato'], 4),
        'mancante_sceso_sotto': round(r['mancante'], 4),
        'giacenza_attuale_nel_db': GiacenzaWood.query.get(cod).quantita if GiacenzaWood.query.get(cod) else 0,
    } for cod, r in out.items()]
    nodi.sort(key=lambda n: -n['fabbisogno'])
    return jsonify(ok=True, op_code=o.codice, codice_articolo=o.codice_articolo,
                    qta_pianificata=o.qta_pianificata, qta_buona=o.qta_buona, saldo_op=saldo, nodi=nodi)


@magazzino_bp.route('/api/distinta-base-wood/<codice_padre>/righe-grezze')
def api_distinta_righe_grezze(codice_padre):
    """
    Righe GREZZE di DistintaBaseWood per un codice_padre, così come sono
    salvate — nessuna esplosione, nessun netting, solo quello che c'è
    scritto nel database. Con un parametro ?profondita=N (default 2) scende
    anche nei figli, per vedere se lo STESSO codice compare come figlio di
    PIÙ padri diversi lungo l'albero (possibile, per via del vincolo di
    unicità su codice_padre+codice_figlio: non può essere una riga
    duplicata, ma può essere raggiunto da due punti diversi dell'albero —
    doppio conteggio nel fabbisogno se i due percorsi non sono davvero
    entrambi reali).
    """
    profondita_max = min(int(request.args.get('profondita', 2)), 4)

    def esplora(padre, liv):
        righe = DistintaBaseWood.query.filter_by(codice_padre=padre).order_by(DistintaBaseWood.id).all()
        out = []
        for r in righe:
            riga = {
                'id': r.id, 'codice_figlio': r.codice_figlio, 'quantita': r.quantita, 'livello': r.livello,
                'gruppo_alternativa': r.gruppo_alternativa, 'preferita': r.preferita, 'note': r.note or '',
            }
            if liv < profondita_max:
                riga['figli'] = esplora(r.codice_figlio, liv + 1)
            out.append(riga)
        return out

    albero = esplora(codice_padre.strip(), 0)
    return jsonify(ok=True, codice_padre=codice_padre.strip(), albero=albero)


@magazzino_bp.route('/api/giacenza_wood/<codice>/scorta_minima', methods=['PUT'])
def api_giacenza_scorta_minima(codice):
    """
    DISATTIVATO su richiesta esplicita: la Scorta Minima adesso è SOLO una
    lettura da MasterLogistic-WMS (dove è già modificabile alla fonte),
    non più un campo locale editabile qui — vedi _calcola_campi_giacenza
    e la route /api/giacenza_wood/sincronizza-ordinato-cliente-wms.
    Route lasciata qui (non rimossa) solo per compatibilità di eventuali
    chiamate residue, ma risponde sempre con errore esplicativo.
    """
    return jsonify({'errore': True,
                     'messaggio': "La Scorta Minima non è più modificabile qui: si legge automaticamente da MasterLogistic-WMS. Modificala là."}), 410


@magazzino_bp.route('/api/giacenza_wood/<codice>/rettifica-grezzo-iw', methods=['POST'])
def api_rettifica_grezzo_iw(codice):
    """
    Rettifica manuale (Angelo) al Grezzo IW calcolato in automatico da
    MasterWork: scarti, rotture in trasporto tra produzione e magazzino,
    o qualunque altra correzione che l'automatico non vede. Cumulativa —
    non sovrascrive, si somma alla serie storica (come i movimenti di
    giacenza), quindi resta tracciabile chi ha corretto cosa e perché.
    """
    codice = codice.strip()
    d = request.get_json(force=True)
    try:
        delta = float(d.get('delta'))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Il valore deve essere un numero (positivo o negativo)'}), 400
    if delta == 0:
        return jsonify({'errore': True, 'messaggio': 'Inserisci un valore diverso da zero'}), 400
    note = (d.get('note') or '').strip()
    db.session.add(RettificaGrezzoIW(codice=codice, delta=delta, note=note))
    db.session.commit()
    return jsonify({'ok': True})


@magazzino_bp.route('/api/giacenza_wood/<codice>/rettifiche-grezzo-iw')
def api_lista_rettifiche_grezzo_iw(codice):
    righe = (RettificaGrezzoIW.query.filter_by(codice=codice.strip())
             .order_by(RettificaGrezzoIW.creato_il.desc()).all())
    return jsonify([{
        'delta': r.delta, 'note': r.note,
        'creato_il': r.creato_il.strftime('%d/%m/%Y %H:%M') if r.creato_il else '',
    } for r in righe])


@magazzino_bp.route('/api/giacenza_wood/<codice>/movimenti')
def api_giacenza_movimenti(codice):
    righe = (MovimentoGiacenzaWood.query.filter_by(codice=codice)
             .order_by(MovimentoGiacenzaWood.creato_il.desc()).limit(100).all())
    return jsonify([{
        'id': m.id, 'tipo': m.tipo, 'quantita': m.quantita, 'riferimento': m.riferimento,
        'note': m.note, 'creato_il': m.creato_il.strftime('%d/%m/%Y %H:%M') if m.creato_il else '',
    } for m in righe])


@magazzino_bp.route('/api/giacenza_wood/rettifica', methods=['POST'])
def api_giacenza_rettifica():
    """Carico/scarico manuale su un singolo codice (es. rettifica da inventario fisico)."""
    d = request.get_json(force=True)
    codice = (d.get('codice') or '').strip()
    if not codice:
        return jsonify({'errore': True, 'messaggio': 'Codice obbligatorio'}), 400
    try:
        delta = float(d.get('quantita'))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Quantità non valida'}), 400
    if delta == 0:
        return jsonify({'errore': True, 'messaggio': 'La quantità non può essere zero'}), 400
    tipo = 'carico_manuale' if delta > 0 else 'scarico_manuale'
    _registra_movimento_giacenza(codice, delta, tipo, note=(d.get('note') or '').strip())
    unita = (d.get('unita_misura') or '').strip().upper()[:20]
    if unita:
        articolo = ArticoloApprovvigionamento.query.filter_by(codice=codice).first()
        if articolo is None:
            articolo = ArticoloApprovvigionamento(codice=codice)
            db.session.add(articolo)
        articolo.unita_misura = unita
    db.session.commit()
    g = GiacenzaWood.query.get(codice)
    return jsonify({'ok': True, 'codice': codice, 'quantita': g.quantita})


@magazzino_bp.route('/api/giacenza_wood/importa', methods=['POST'])
def api_importa_giacenza():
    """
    Caricamento massivo/aggiornamento giacenza da Excel/CSV — colonne lette per
    nome: 'codice' (o 'sku'/'articolo') e 'quantita' (o 'giacenza'/'qta').
    UPSERT: imposta la giacenza al valore del file (non somma) e registra la
    differenza rispetto al valore precedente come movimento 'rettifica_import'
    — così il primo caricamento fa da carico iniziale e i successivi da
    aggiornamento, senza mai perdere lo storico di cosa è cambiato.
    """
    file = request.files.get('file_excel')
    if not file:
        return jsonify({'errore': True, 'messaggio': 'Nessun file selezionato.'}), 400
    try:
        import io
        import pandas as pd
        filename = file.filename.lower()
        raw = file.read()
        df = None
        if filename.endswith('.xls'):
            try:
                df = pd.read_excel(io.BytesIO(raw), engine='xlrd')
            except Exception:
                df = pd.read_excel(io.BytesIO(raw), engine='openpyxl')
        elif filename.endswith('.xlsx'):
            df = pd.read_excel(io.BytesIO(raw), engine='openpyxl')
        elif filename.endswith('.csv'):
            for enc in ['utf-8', 'latin-1', 'cp1252']:
                try:
                    df = pd.read_csv(io.BytesIO(raw), encoding=enc, sep=None, engine='python')
                    break
                except Exception:
                    pass
        if df is None:
            return jsonify({'errore': True, 'messaggio': 'Formato file non supportato o illeggibile (usare .xlsx, .xls o .csv).'}), 400

        df.columns = [str(c).strip().lower() for c in df.columns]
        col_codice = next((c for c in ('codice', 'sku', 'articolo', 'codart') if c in df.columns), None)
        col_qta    = next((c for c in ('quantita', 'quantità', 'giacenza', 'qta') if c in df.columns), None)
        col_unita  = next((c for c in ('unita_misura', 'unità di misura', 'unita', 'u.m.', 'um') if c in df.columns), None)
        if not col_codice or not col_qta:
            return jsonify({'errore': True,
                             'messaggio': f'Colonne non trovate. Attese "codice" e "quantita", trovate: {", ".join(df.columns)}'}), 400

        nuovi = aggiornati = scartate = 0
        for _, row in df.iterrows():
            codice = str(row[col_codice]).strip()
            if not codice or codice.lower() == 'nan':
                scartate += 1
                continue
            try:
                nuova_qta = float(row[col_qta])
            except (TypeError, ValueError):
                scartate += 1
                continue
            # L'unità è anagrafica del codice, non del singolo movimento: se presente
            # nell'import la aggiorniamo nella stessa tabella usata da Parametri.
            if col_unita and str(row[col_unita]).lower() != 'nan':
                unita = str(row[col_unita]).strip().upper()[:20]
                if unita:
                    articolo = ArticoloApprovvigionamento.query.filter_by(codice=codice).first()
                    if articolo is None:
                        articolo = ArticoloApprovvigionamento(codice=codice)
                        db.session.add(articolo)
                    articolo.unita_misura = unita
            g = GiacenzaWood.query.get(codice)
            if g is None:
                _registra_movimento_giacenza(codice, nuova_qta, 'carico_iniziale', note='Import massivo')
                nuovi += 1
            else:
                delta = nuova_qta - (g.quantita or 0)
                if delta != 0:
                    _registra_movimento_giacenza(codice, delta, 'rettifica_import', note='Import massivo (aggiornamento)')
                    aggiornati += 1
        db.session.commit()
        return jsonify({'ok': True, 'nuovi': nuovi, 'aggiornati': aggiornati, 'scartate': scartate,
                        'totale_righe_file': len(df)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': f'Errore durante l\'import: {e}'}), 500


def _netta_e_esplodi_wood(codice, qta, giacenza_residua, out, _visitati=None, _profondita=0, _max_profondita=12, mappa=None):
    """
    Esplode la distinta Iron Wood da `codice` per una quantità `qta`, NETTANDO
    ad ogni nodo contro `giacenza_residua` (dict mutabile {codice: qta libera},
    CONSUMATO in-place: chiamate successive vedono meno disponibilità — così
    più OP processati in sequenza si contendono realisticamente la stessa
    scorta, primo arrivato primo servito).
    Solo la quota MANCANTE (non coperta da giacenza) cascata sui componenti:
    un semilavorato già pronto a scorta non genera fabbisogno inutile dei
    suoi figli (ferro, laserati, ecc.).
    Accumula in `out` (dict) una riga per ogni codice toccato in QUESTA
    esplosione: {fabbisogno, usato, mancante} — sommati se il codice compare
    più volte nell'albero (es. la stessa vite in più punti).
    'mappa' opzionale (vedi _carica_mappa_distinta_base_wood): se fornita,
    evita una query per nodo — indispensabile quando si esplodono le
    distinte di molti OP nella stessa richiesta (es. Situazione Ordini di
    Produzione, Monitor Macchina).
    """
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita or qta <= 0:
        return
    _visitati = _visitati | {codice}

    disponibile_prima = giacenza_residua.get(codice, 0.0)
    # BUG CORRETTO: se la giacenza di questo codice è NEGATIVA (es. da
    # scorrettezze storiche pregresse), min(negativo, qta) restituiva un
    # 'usato' negativo — che invece di ridurre il mancante lo FACEVA
    # AUMENTARE oltre il fabbisogno reale (600 diventava 1060, poi 1200,
    # raddoppiando via via scendendo nell'albero). Giacenza negativa deve
    # sempre contare come "zero disponibile", mai come disponibilità
    # negativa che si propaga amplificando il fabbisogno a valle.
    usato = min(max(disponibile_prima, 0.0), qta)
    giacenza_residua[codice] = disponibile_prima - usato
    mancante = qta - usato

    riga = out.setdefault(codice, {'fabbisogno': 0.0, 'usato': 0.0, 'mancante': 0.0})
    riga['fabbisogno'] += qta
    riga['usato']      += usato
    riga['mancante']   += mancante

    if mancante <= 0:
        return
    righe_bom = _righe_bom_attive_wood(codice, mappa=mappa)
    for r in righe_bom:
        qta_figlio = (r.quantita or 1.0) * mancante
        _netta_e_esplodi_wood(r.codice_figlio, qta_figlio, giacenza_residua, out, _visitati, _profondita + 1, _max_profondita, mappa=mappa)


def _giacenza_residua_dopo_impegni(escludi_op_id=None, mappa=None):
    """
    Parte dalla giacenza fisica attuale e simula il consumo di TUTTI gli OP
    aperti (stato in STATI_CHE_IMPEGNANO), servendo PRIMA gli OP con priorità
    più alta (numero più basso), e a parità di priorità i più vecchi — così
    quando il materiale non basta per tutti, a decidere chi lo riceve per
    primo è la priorità che il capo ha impostato sull'OP (colonna PRIO in
    Ordini di Produzione), non semplicemente chi è stato creato prima.
    Ritorna il dict {codice: qta rimasta} DOPO aver tolto quanto già
    impegnato da quegli OP. 'mappa' opzionale: vedi _carica_mappa_distinta_base_wood.
    """
    giacenza_residua = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    op_aperti = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
                 .order_by(OrdineProduzione.priorita.asc(), OrdineProduzione.id.asc()).all())
    for op in op_aperti:
        if escludi_op_id and op.id == escludi_op_id:
            continue
        saldo = (op.qta_pianificata or 0) - (op.qta_buona or 0)
        if saldo > 0:
            _netta_e_esplodi_wood(op.codice_articolo, saldo, giacenza_residua, {}, mappa=mappa)
    return giacenza_residua


def _residuo_giacenza_progressivo(op_aperti=None, mappa=None):
    """
    Versione efficiente di _giacenza_residua_dopo_impegni pensata per essere
    chiamata UNA VOLTA SOLA per ottenere il residuo di TUTTI gli OP aperti
    insieme, invece che una volta per OP dentro un ciclo — il pattern
    "for o in ordini: _giacenza_residua_dopo_impegni(escludi_op_id=o.id)"
    è O(N²) (per ogni OP riesplode la distinta di TUTTI gli altri OP da
    zero) e con qualche decina di OP aperti porta i tempi di risposta da
    millisecondi a decine di secondi.

    Un solo passaggio in ordine di priorità: per ogni OP salva la giacenza
    residua PRIMA di servirlo (cioè dopo aver servito solo chi ha priorità
    pari o superiore ed è quindi davanti a lui in coda), poi lo consuma e
    passa al successivo. Semanticamente equivalente a "quanto materiale mi
    aspetto di trovare disponibile quando tocca a me", coerente con la stessa
    logica di priorità già usata ovunque nell'app (P1 più urgente di P9).

    Ritorna (residuo_per_op, residuo_finale): il primo è {op.id: {codice: qta
    residua prima di servire quell'OP}} per gli OP che impegnano davvero
    materiale (STATI_CHE_IMPEGNANO); il secondo è il residuo DOPO aver
    servito tutti — da usare per un OP che non impegna nulla (es. ancora
    'Creato', non rilasciato): per lui escludere se stesso è un no-op, quindi
    il suo residuo coincide con quello finale.
    """
    if op_aperti is None:
        op_aperti = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
                     .order_by(OrdineProduzione.priorita.asc(), OrdineProduzione.id.asc()).all())
    giacenza = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    residuo_per_op = {}
    for op in op_aperti:
        residuo_per_op[op.id] = dict(giacenza)  # snapshot prima di servire QUESTO OP
        saldo = (op.qta_pianificata or 0) - (op.qta_buona or 0)
        if saldo > 0:
            _netta_e_esplodi_wood(op.codice_articolo, saldo, giacenza, {}, mappa=mappa)
    return residuo_per_op, giacenza


@magazzino_bp.route('/api/fabbisogno_disponibilita')
def api_fabbisogno_disponibilita():
    """
    Controllo scorta per un codice+quantità (tipicamente l'OP che si sta per
    creare, o un OP esistente da verificare): per ogni componente toccato
    dall'esplosione, mostra Giacenza fisica, Già impegnato da altri OP aperti,
    Disponibile, Fabbisogno di QUESTO OP, Mancante da acquistare/produrre.
    Parametri: ?codice=...&qta=...  oppure ?op_id=... (usa l'OP esistente,
    escludendolo dal calcolo di "già impegnato" per non contare se stesso).
    """
    codice = (request.args.get('codice') or '').strip()
    qta_str = request.args.get('qta')
    op_id = request.args.get('op_id', type=int)

    if op_id:
        op = OrdineProduzione.query.get_or_404(op_id)
        codice = op.codice_articolo
        qta = (op.qta_pianificata or 0) - (op.qta_buona or 0)
    else:
        if not codice:
            return jsonify({'errore': True, 'messaggio': 'Specificare "codice" e "qta", oppure "op_id"'}), 400
        try:
            qta = float(qta_str)
        except (TypeError, ValueError):
            return jsonify({'errore': True, 'messaggio': 'Quantità non valida'}), 400

    giacenza_iniziale = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    giacenza_residua = _giacenza_residua_dopo_impegni(escludi_op_id=op_id)
    disponibile_prima_di_questo = dict(giacenza_residua)

    righe = {}
    if qta > 0 and codice:
        _netta_e_esplodi_wood(codice, qta, giacenza_residua, righe)

    risultato = []
    for cod, r in righe.items():
        giacenza_tot = giacenza_iniziale.get(cod, 0.0)
        disponibile = disponibile_prima_di_questo.get(cod, 0.0)
        gia_impegnato = giacenza_tot - disponibile
        risultato.append({
            'codice':              cod,
            'giacenza':            round(giacenza_tot, 3),
            'gia_impegnato':       round(max(gia_impegnato, 0), 3),
            'disponibile':         round(max(disponibile, 0), 3),
            'fabbisogno_nuovo_op': round(r['fabbisogno'], 3),
            'mancante':            round(r['mancante'], 3),
        })
    risultato.sort(key=lambda x: (-x['mancante'], x['codice']))
    return jsonify({'codice': codice, 'qta': qta, 'righe': risultato})


# ══════════════════════════════════════════════════════════════════════════════
#  CODICI PADRE DISPONIBILI — per il widget di selezione nella pagina Ordini di
#  Produzione: elenca SOLO i codici che sono "padre" in distinta_base_wood
#  (cioè prodotti finiti Iron Wood con una distinta associata), non un
#  qualsiasi codice del magazzino condiviso.
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/api/codici_padre_wood')
def api_codici_padre_wood():
    """Solo codici PADRE della distinta Iron Wood — presi da IronProduction, nessuna dipendenza da MasterLogistic."""
    codici = [row[0] for row in db.session.query(DistintaBaseWood.codice_padre).distinct()
              .order_by(DistintaBaseWood.codice_padre).all()]
    return jsonify([{'codice': c} for c in codici])


# ══════════════════════════════════════════════════════════════════════════════
#  TUTTI I CODICI IRON WOOD (padre + figlio) — usati per il popup Ciclo di
#  Lavoro. Presi solo da IronProduction, nessuna dipendenza da MasterLogistic.
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/api/codici_wood_tutti')
def api_codici_wood_tutti():
    padri  = {row[0] for row in db.session.query(DistintaBaseWood.codice_padre).distinct().all()}
    figli  = {row[0] for row in db.session.query(DistintaBaseWood.codice_figlio).distinct().all()}
    codici = sorted(padri | figli)
    return jsonify([{'codice': c, 'e_padre': c in padri} for c in codici])


# ══════════════════════════════════════════════════════════════════════════════
#  CENTRI DI COSTO / REPARTI IRON WOOD — macchine (piegatrice, sega,
#  foratura...) o lavorazioni esterne (verniciatura esterna, piega esterna).
#  Prerequisito dei Cicli di Lavoro: ogni fase del ciclo punta a uno di questi.
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/api/centri_costo_wood')
def api_centri_costo_lista():
    righe = CentroCostoWood.query.order_by(CentroCostoWood.nome).all()
    return jsonify([{
        'id': c.id, 'nome': c.nome, 'esterno': c.esterno,
        'costo_orario': c.costo_orario or 0, 'note': c.note,
        'attivo': c.attivo if c.attivo is not None else True,
        'reparto_gruppo': c.reparto_gruppo or '',
        'escluso_da_monitor_produzione': c.escluso_da_monitor_produzione or False,
        'tariffa_manodopera_diretta_oraria': c.tariffa_manodopera_diretta_oraria or 0,
    } for c in righe])


@magazzino_bp.route('/api/centri_costo_wood', methods=['POST'])
def api_centri_costo_crea():
    d = request.get_json(force=True)
    nome = (d.get('nome') or '').strip()
    if not nome:
        return jsonify({'errore': True, 'messaggio': 'Il nome del reparto/centro di costo è obbligatorio'}), 400
    esistente = CentroCostoWood.query.filter(db.func.lower(CentroCostoWood.nome) == nome.lower()).first()
    if esistente:
        return jsonify({'errore': True, 'messaggio': f'Esiste già un centro di costo chiamato "{nome}"'}), 409
    try:
        costo_orario = float(d.get('costo_orario') or 0)
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Costo orario non valido'}), 400
    c = CentroCostoWood(nome=nome, esterno=bool(d.get('esterno')), costo_orario=costo_orario,
                         note=(d.get('note') or '').strip())
    db.session.add(c)
    db.session.commit()
    return jsonify({'ok': True, 'id': c.id})


@magazzino_bp.route('/api/centri_costo_wood/<int:cid>', methods=['PUT'])
def api_centri_costo_modifica(cid):
    c = CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    if 'costo_orario' in d:
        try:
            c.costo_orario = float(d.get('costo_orario') or 0)
        except (TypeError, ValueError):
            return jsonify({'errore': True, 'messaggio': 'Costo orario non valido'}), 400
    if 'esterno' in d:
        c.esterno = bool(d.get('esterno'))
    if 'escluso_da_monitor_produzione' in d:
        c.escluso_da_monitor_produzione = bool(d.get('escluso_da_monitor_produzione'))
    if 'note' in d:
        c.note = (d.get('note') or '').strip()
    db.session.commit()
    return jsonify({'ok': True})


@magazzino_bp.route('/api/centri_costo_wood/<int:cid>', methods=['DELETE'])
def api_centri_costo_elimina(cid):
    c = CentroCostoWood.query.get_or_404(cid)
    in_uso = CicloLavoroWood.query.filter_by(centro_costo_id=cid).count()
    if in_uso:
        return jsonify({'errore': True, 'messaggio': f'Impossibile eliminare: usato in {in_uso} fase/i di ciclo di lavoro'}), 409
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAZIONE DETTAGLIATA CENTRO DI COSTO — anagrafica estesa, capacità/
#  driver, costi pianificati storicizzati, tariffa calcolata. Il valore
#  calcolato viene scritto in CentroCostoWood.costo_orario /
#  tariffa_manodopera_diretta_oraria: tutto il resto del programma (Costo
#  Standard, Cicli di Lavoro) continua a leggere solo quei due campi, senza
#  bisogno di modifiche altrove — retrocompatibile con l'inserimento manuale
#  rapido già esistente, che resta comunque possibile in qualunque momento.
# ══════════════════════════════════════════════════════════════════════════════
def _ore_pratiche_pianificate(c):
    """Capacità pratica: ore teoriche × risorse equivalenti × efficienza%. None se non calcolabile (per non dividere per zero)."""
    if not c.ore_teoriche_periodo or c.ore_teoriche_periodo <= 0:
        return None
    n_risorse = c.n_risorse_equivalenti if c.n_risorse_equivalenti and c.n_risorse_equivalenti > 0 else 1
    pct = c.pct_efficienza if c.pct_efficienza is not None else 100
    return c.ore_teoriche_periodo * n_risorse * (pct / 100.0)


def _costi_pianificati_correnti(centro_costo_id):
    """Le voci di costo pianificato ATTUALMENTE valide (valido_al IS NULL) per un centro."""
    return (CostoPianificatoCentroWood.query
            .filter_by(centro_costo_id=centro_costo_id, valido_al=None)
            .order_by(CostoPianificatoCentroWood.voce).all())


@magazzino_bp.route('/api/centri_costo_wood/<int:cid>/configurazione')
def api_centro_costo_configurazione(cid):
    c = CentroCostoWood.query.get_or_404(cid)
    correnti = _costi_pianificati_correnti(cid)
    storico = (CostoPianificatoCentroWood.query.filter_by(centro_costo_id=cid)
               .order_by(CostoPianificatoCentroWood.voce, CostoPianificatoCentroWood.valido_dal.desc()).all())
    ore_pratiche = _ore_pratiche_pianificate(c)
    totale_costi_centro = sum(v.importo for v in correnti if v.voce != 'manodopera_diretta')
    costo_manodopera = sum(v.importo for v in correnti if v.voce == 'manodopera_diretta')
    tariffa_centro_calcolata = round(totale_costi_centro / ore_pratiche, 4) if ore_pratiche else None
    tariffa_manodopera_calcolata = round(costo_manodopera / ore_pratiche, 4) if ore_pratiche else None

    return jsonify({
        'id': c.id, 'nome': c.nome, 'esterno': c.esterno, 'note': c.note,
        'reparto_gruppo': c.reparto_gruppo, 'attivo': c.attivo,
        'escluso_da_monitor_produzione': c.escluso_da_monitor_produzione,
        'fornitore_esterno': c.fornitore_esterno, 'tariffa_esterna': c.tariffa_esterna,
        'lead_time_esterno_giorni': c.lead_time_esterno_giorni,
        'lead_time_esterno_manuale': bool(c.lead_time_esterno_manuale),
        'lead_time_esterno_n_osservazioni': c.lead_time_esterno_n_osservazioni or 0,
        'driver_attivita': c.driver_attivita, 'n_risorse_equivalenti': c.n_risorse_equivalenti,
        'ore_teoriche_periodo': c.ore_teoriche_periodo, 'pct_efficienza': c.pct_efficienza,
        'periodo_riferimento': c.periodo_riferimento,
        'costo_orario_attuale': c.costo_orario,
        'tariffa_manodopera_diretta_oraria_attuale': c.tariffa_manodopera_diretta_oraria,
        'ore_pratiche_pianificate': round(ore_pratiche, 4) if ore_pratiche else None,
        'totale_costi_centro_correnti': round(totale_costi_centro, 4),
        'totale_manodopera_diretta_corrente': round(costo_manodopera, 4),
        'tariffa_centro_calcolata': tariffa_centro_calcolata,
        'tariffa_manodopera_calcolata': tariffa_manodopera_calcolata,
        'costi_pianificati_correnti': [{
            'id': v.id, 'voce': v.voce, 'importo': v.importo,
            'valido_dal': v.valido_dal.isoformat() if v.valido_dal else None, 'note': v.note,
        } for v in correnti],
        'storico_costi_pianificati': [{
            'id': v.id, 'voce': v.voce, 'importo': v.importo,
            'valido_dal': v.valido_dal.isoformat() if v.valido_dal else None,
            'valido_al': v.valido_al.isoformat() if v.valido_al else None, 'note': v.note,
        } for v in storico],
        'voci_disponibili': VOCI_COSTO_PIANIFICATO_WOOD,
        'driver_disponibili': DRIVER_ATTIVITA_WOOD,
    })


@magazzino_bp.route('/api/centri_costo_wood/<int:cid>/configurazione', methods=['PUT'])
def api_centro_costo_configurazione_salva(cid):
    c = CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    try:
        if 'esterno' in d: c.esterno = bool(d['esterno'])
        if 'attivo' in d: c.attivo = bool(d['attivo'])
        if 'escluso_da_monitor_produzione' in d: c.escluso_da_monitor_produzione = bool(d['escluso_da_monitor_produzione'])
        if 'reparto_gruppo' in d: c.reparto_gruppo = (d.get('reparto_gruppo') or '').strip()
        if 'note' in d: c.note = (d.get('note') or '').strip()
        if 'fornitore_esterno' in d: c.fornitore_esterno = (d.get('fornitore_esterno') or '').strip()
        if 'tariffa_esterna' in d:
            v = d.get('tariffa_esterna')
            c.tariffa_esterna = float(v) if v not in (None, '') else None
            if c.tariffa_esterna is not None and c.tariffa_esterna < 0:
                return jsonify({'errore': True, 'messaggio': 'La tariffa esterna non può essere negativa'}), 400
        if 'lead_time_esterno_giorni' in d:
            v = d.get('lead_time_esterno_giorni')
            c.lead_time_esterno_giorni = float(v) if v not in (None, '') else None
            if c.lead_time_esterno_giorni is not None and c.lead_time_esterno_giorni < 0:
                return jsonify({'errore': True, 'messaggio': 'Il lead time non può essere negativo'}), 400
        if 'lead_time_esterno_manuale' in d:
            c.lead_time_esterno_manuale = bool(d['lead_time_esterno_manuale'])
        if 'driver_attivita' in d:
            driver = d.get('driver_attivita')
            if driver not in DRIVER_ATTIVITA_WOOD:
                return jsonify({'errore': True, 'messaggio': 'Driver di attività non valido'}), 400
            c.driver_attivita = driver
        if 'n_risorse_equivalenti' in d:
            c.n_risorse_equivalenti = float(d.get('n_risorse_equivalenti') or 1)
            if c.n_risorse_equivalenti <= 0:
                return jsonify({'errore': True, 'messaggio': 'Il numero di risorse equivalenti deve essere maggiore di zero'}), 400
        if 'ore_teoriche_periodo' in d:
            c.ore_teoriche_periodo = float(d.get('ore_teoriche_periodo') or 0)
            if c.ore_teoriche_periodo < 0:
                return jsonify({'errore': True, 'messaggio': 'Le ore teoriche non possono essere negative'}), 400
        if 'pct_efficienza' in d:
            c.pct_efficienza = float(d.get('pct_efficienza') or 0)
            if not (0 <= c.pct_efficienza <= 100):
                return jsonify({'errore': True, 'messaggio': "L'efficienza deve essere tra 0 e 100%"}), 400
        if 'periodo_riferimento' in d:
            periodo = d.get('periodo_riferimento')
            if periodo not in ('settimanale', 'mensile', 'annuale'):
                return jsonify({'errore': True, 'messaggio': 'Periodo di riferimento non valido'}), 400
            c.periodo_riferimento = periodo
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Valore numerico non valido'}), 400
    db.session.commit()
    return jsonify({'ok': True})


@magazzino_bp.route('/api/centri_costo_wood/<int:cid>/costi_pianificati', methods=['POST'])
def api_centro_costo_costo_pianificato_upsert(cid):
    """
    Imposta l'importo corrente di una voce di costo pianificato. Se la voce
    esiste già con un importo diverso, chiude la riga corrente (valido_al =
    oggi) e ne apre una nuova — non sovrascrive mai un valore storico.
    Se l'importo è identico a quello già corrente, non fa nulla (evita righe
    duplicate inutili quando si preme salva senza cambiare niente).
    """
    CentroCostoWood.query.get_or_404(cid)
    d = request.get_json(force=True)
    voce = d.get('voce')
    if voce not in VOCI_COSTO_PIANIFICATO_WOOD:
        return jsonify({'errore': True, 'messaggio': 'Voce di costo non valida'}), 400
    try:
        importo = float(d.get('importo'))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Importo non valido'}), 400
    if importo < 0:
        return jsonify({'errore': True, 'messaggio': "L'importo non può essere negativo"}), 400
    note = (d.get('note') or '').strip()

    oggi = date.today()
    corrente = CostoPianificatoCentroWood.query.filter_by(centro_costo_id=cid, voce=voce, valido_al=None).first()
    if corrente and corrente.importo == importo and corrente.note == note:
        return jsonify({'ok': True, 'invariato': True})
    if corrente:
        corrente.valido_al = oggi
    nuova = CostoPianificatoCentroWood(centro_costo_id=cid, voce=voce, importo=importo,
                                        valido_dal=oggi, note=note)
    db.session.add(nuova)
    db.session.commit()
    return jsonify({'ok': True, 'id': nuova.id})


@magazzino_bp.route('/api/centri_costo_wood/<int:cid>/calcola_tariffa', methods=['POST'])
def api_centro_costo_calcola_tariffa(cid):
    """
    Calcola la tariffa oraria dai costi pianificati correnti / capacità
    pratica, e la SALVA in costo_orario (+ tariffa_manodopera_diretta_oraria) —
    da quel momento il motore di Costo Standard la usa automaticamente, senza
    bisogno di ricalcolare nient'altro a mano.
    """
    c = CentroCostoWood.query.get_or_404(cid)
    ore_pratiche = _ore_pratiche_pianificate(c)
    if not ore_pratiche:
        return jsonify({'errore': True,
                        'messaggio': 'Capacità pratica non calcolabile: imposta "Ore teoriche periodo" (> 0) nella scheda del centro.'}), 400
    correnti = _costi_pianificati_correnti(cid)
    if not correnti:
        return jsonify({'errore': True,
                        'messaggio': 'Nessun costo pianificato inserito per questo centro — aggiungine almeno uno prima di calcolare la tariffa.'}), 400
    totale_costi_centro = sum(v.importo for v in correnti if v.voce != 'manodopera_diretta')
    costo_manodopera = sum(v.importo for v in correnti if v.voce == 'manodopera_diretta')
    c.costo_orario = round(totale_costi_centro / ore_pratiche, 4)
    c.tariffa_manodopera_diretta_oraria = round(costo_manodopera / ore_pratiche, 4)
    db.session.commit()
    return jsonify({'ok': True, 'costo_orario': c.costo_orario,
                    'tariffa_manodopera_diretta_oraria': c.tariffa_manodopera_diretta_oraria,
                    'ore_pratiche_pianificate': round(ore_pratiche, 4)})


# ══════════════════════════════════════════════════════════════════════════════
#  CICLI DI LAVORO IRON WOOD — sequenza di reparti (con produttività oraria
#  specifica) per un codice padre O figlio della distinta Iron Wood.
# ══════════════════════════════════════════════════════════════════════════════
def _ciclo_riga(r):
    return {
        'id': r.id, 'codice': r.codice, 'sequenza': r.sequenza,
        'centro_costo_id': r.centro_costo_id,
        'centro_costo_nome': r.centro_costo.nome if r.centro_costo else '',
        'centro_costo_esterno': r.centro_costo.esterno if r.centro_costo else False,
        'produttivita_oraria': r.produttivita_oraria, 'scarto_max_pct': r.scarto_max_pct, 'note': r.note,
        'produttivita_oraria_manuale': bool(r.produttivita_oraria_manuale),
        'produttivita_pezzi_osservati': r.produttivita_pezzi_osservati or 0,
    }


@magazzino_bp.route('/api/ciclo_lavoro_wood')
def api_ciclo_lavoro_lista():
    codice_filtro = request.args.get('codice', '').strip()
    codici_filtro = request.args.get('codici', '').strip()
    q = CicloLavoroWood.query
    if codice_filtro:
        q = q.filter_by(codice=codice_filtro)
    elif codici_filtro:
        lista = [c.strip() for c in codici_filtro.split(',') if c.strip()]
        q = q.filter(CicloLavoroWood.codice.in_(lista))
    righe = q.order_by(CicloLavoroWood.codice, CicloLavoroWood.sequenza).all()
    return jsonify([_ciclo_riga(r) for r in righe])


@magazzino_bp.route('/api/ciclo_lavoro_wood', methods=['POST'])
def api_ciclo_lavoro_crea():
    d = request.get_json(force=True)
    codice = (d.get('codice') or '').strip()
    if not codice:
        return jsonify({'errore': True, 'messaggio': 'Il codice è obbligatorio'}), 400
    try:
        sequenza = int(d.get('sequenza'))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'La sequenza deve essere un numero intero'}), 400
    try:
        centro_costo_id = int(d.get('centro_costo_id'))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Seleziona un reparto/centro di costo'}), 400
    if not CentroCostoWood.query.get(centro_costo_id):
        return jsonify({'errore': True, 'messaggio': 'Reparto/centro di costo non trovato'}), 404
    try:
        produttivita = float(d.get('produttivita_oraria') or 0)
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'La produttività oraria deve essere un numero'}), 400
    try:
        scarto_max_pct = float(d['scarto_max_pct']) if d.get('scarto_max_pct') not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Lo scarto massimo deve essere un numero'}), 400

    esistente = CicloLavoroWood.query.filter_by(codice=codice, sequenza=sequenza).first()
    manuale = bool(d.get('produttivita_oraria_manuale'))
    if esistente:
        # aggiorna la fase esistente invece di duplicarla (stesso codice+sequenza)
        esistente.centro_costo_id = centro_costo_id
        esistente.produttivita_oraria = produttivita
        esistente.produttivita_oraria_manuale = manuale
        esistente.scarto_max_pct = scarto_max_pct
        esistente.note = (d.get('note') or '').strip()
        db.session.commit()
        return jsonify({'ok': True, 'id': esistente.id, 'aggiornata': True})

    r = CicloLavoroWood(codice=codice, sequenza=sequenza, centro_costo_id=centro_costo_id,
                         produttivita_oraria=produttivita, produttivita_oraria_manuale=manuale,
                         scarto_max_pct=scarto_max_pct, note=(d.get('note') or '').strip())
    db.session.add(r)
    db.session.commit()
    return jsonify({'ok': True, 'id': r.id, 'aggiornata': False})


@magazzino_bp.route('/api/ciclo_lavoro_wood/<int:rid>', methods=['DELETE'])
def api_ciclo_lavoro_elimina(rid):
    r = CicloLavoroWood.query.get_or_404(rid)
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  COSTO STANDARD IRON WOOD — logica SAP: Materiali (BOM ricorsiva) +
#  Lavorazione (Routing/Ciclo di Lavoro) + Overhead (% su materiali+lavorazione).
#  Un codice foglia (classificato come materia prima/componente
#  acquisto/laserato in ArticoloApprovvigionamento) non ha BOM/routing sotto:
#  il suo costo standard è semplicemente il prezzo d'acquisto registrato.
# ══════════════════════════════════════════════════════════════════════════════
def _overhead_pct():
    """LEGACY — mantenuta solo per non rompere eventuali chiamate esterne, non più usata dal motore di calcolo."""
    imp = ImpostazioneCostoWood.query.first()
    return imp.aliquota_overhead_pct if imp else 0


def _overhead_pct_materiali():
    imp = ImpostazioneCostoWood.query.first()
    return imp.aliquota_overhead_materiali_pct if imp else 0


def _overhead_pct_produzione():
    imp = ImpostazioneCostoWood.query.first()
    return imp.aliquota_overhead_produzione_pct if imp else 0


def _calcola_costo_standard(codice, _visitati=None, _cache=None, _profondita=0, _max_profondita=15):
    """
    Ricorsivo bottom-up, logica SAP: Materiali + Lavorazione (SOLO macchina/
    reparto) + Manodopera diretta (tariffa propria, separata) + Overhead a
    DUE basi distinte — Overhead Materiali (% su costo_materiali) e Overhead
    Produzione (% su costo_lavorazione + costo_manodopera). Le due aliquote
    si applicano ad OGNI livello della distinta: un semilavorato porta il
    proprio overhead già dentro il suo costo_totale, e quando diventa
    "materiale" del prodotto sopra, l'overhead materiali del padre si
    riapplica su di lui — è corretto, è così anche in SAP.
    Ritorna sempre le stesse chiavi anche in caso di ciclo nella distinta o
    profondità eccessiva. 'codici_senza_costo': i codici foglia nell'albero
    classificati ma senza prezzo — il contributo è 0 per necessità di
    calcolo, il totale va considerato NON AFFIDABILE finché non si registra
    il prezzo: non inventiamo mai un valore, lo segnaliamo sempre.
    """
    vuoto = {'costo_materiali': 0.0, 'costo_lavorazione': 0.0, 'costo_manodopera': 0.0,
             'costo_overhead': 0.0, 'costo_overhead_materiali': 0.0, 'costo_overhead_produzione': 0.0,
             'costo_totale': 0.0, 'ore_manodopera_standard': 0.0, 'codici_senza_costo': set()}
    if _visitati is None:
        _visitati = set()
    if _cache is None:
        _cache = {}
    if codice in _cache:
        return _cache[codice]
    if codice in _visitati or _profondita >= _max_profondita:
        return vuoto
    _visitati = _visitati | {codice}

    approvv = ArticoloApprovvigionamento.query.filter_by(codice=codice).first()
    if approvv and approvv.tipo_approvvigionamento != 'DA_CLASSIFICARE':
        if approvv.costo_acquisto_standard is None:
            risultato = dict(vuoto); risultato['codici_senza_costo'] = {codice}
        else:
            prezzo = round(approvv.costo_acquisto_standard, 4)
            risultato = dict(vuoto); risultato['costo_materiali'] = prezzo; risultato['costo_totale'] = prezzo
        _cache[codice] = risultato
        return risultato

    costo_materiali = 0.0
    ore_materiali = 0.0
    codici_senza_costo = set()
    for r in _righe_bom_attive_wood(codice):
        sub = _calcola_costo_standard(r.codice_figlio, _visitati, _cache, _profondita + 1, _max_profondita)
        costo_materiali += sub['costo_totale'] * (r.quantita or 1.0)
        ore_materiali += sub['ore_manodopera_standard'] * (r.quantita or 1.0)
        codici_senza_costo |= sub['codici_senza_costo']

    costo_lavorazione = 0.0    # SOLO macchina/reparto (costo_orario, esclusa manodopera)
    costo_manodopera = 0.0     # manodopera diretta, tariffa propria (tariffa_manodopera_diretta_oraria)
    ore_dirette = 0.0
    for f in CicloLavoroWood.query.filter_by(codice=codice).all():
        if f.produttivita_oraria and f.produttivita_oraria > 0 and f.centro_costo:
            ore_fase = 1.0 / f.produttivita_oraria
            ore_dirette += ore_fase
            costo_lavorazione += ore_fase * (f.centro_costo.costo_orario or 0)
            costo_manodopera += ore_fase * (f.centro_costo.tariffa_manodopera_diretta_oraria or 0)

    oh_materiali_pct = _overhead_pct_materiali()
    oh_produzione_pct = _overhead_pct_produzione()
    costo_overhead_materiali = costo_materiali * (oh_materiali_pct / 100.0)
    costo_overhead_produzione = (costo_lavorazione + costo_manodopera) * (oh_produzione_pct / 100.0)
    costo_overhead = costo_overhead_materiali + costo_overhead_produzione
    costo_totale = costo_materiali + costo_lavorazione + costo_manodopera + costo_overhead

    risultato = {
        'costo_materiali':           round(costo_materiali, 4),
        'costo_lavorazione':         round(costo_lavorazione, 4),
        'costo_manodopera':          round(costo_manodopera, 4),
        'costo_overhead':            round(costo_overhead, 4),
        'costo_overhead_materiali':  round(costo_overhead_materiali, 4),
        'costo_overhead_produzione': round(costo_overhead_produzione, 4),
        'costo_totale':              round(costo_totale, 4),
        'ore_manodopera_standard':   round(ore_dirette + ore_materiali, 4),
        'codici_senza_costo':        codici_senza_costo,
    }
    _cache[codice] = risultato
    return risultato


def _costo_standard_serializzabile(r):
    """Converte il dict di _calcola_costo_standard in una forma JSON-friendly (set -> list ordinata)."""
    out = dict(r)
    out['codici_senza_costo'] = sorted(r.get('codici_senza_costo', set()))
    out['completo'] = len(out['codici_senza_costo']) == 0
    return out


def _annota_costo_albero(componenti, _cache):
    """
    Cammina l'albero già costruito da _esplodi_bom_wood e annota, per ogni
    nodo, il costo standard UNITARIO di quel codice specifico (non
    ri-sommando i suoi figli qui: un semilavorato usa il proprio costo
    standard già calcolato ricorsivamente da _calcola_costo_standard — i
    figli mostrati sotto sono dettaglio informativo, non vanno sommati di
    nuovo al padre). Segnala sempre esplicitamente quando il costo non è
    disponibile, senza mai mostrare uno zero silenzioso.
    """
    for c in componenti:
        approvv = ArticoloApprovvigionamento.query.filter_by(codice=c['codice']).first()
        ha_figli_bom = bool(c.get('figli'))
        if approvv and approvv.tipo_approvvigionamento != 'DA_CLASSIFICARE':
            c['tipo'] = 'ACQUISTO'
            c['origine_costo'] = approvv.tipo_approvvigionamento
            if approvv.costo_acquisto_standard is None:
                c['costo_unitario'] = None
                c['costo_esteso'] = None
                c['disponibile'] = False
            else:
                c['costo_unitario'] = round(approvv.costo_acquisto_standard, 4)
                c['costo_esteso'] = round(approvv.costo_acquisto_standard * c['quantita_totale'], 4)
                c['disponibile'] = True
        else:
            sub = _calcola_costo_standard(c['codice'], _cache=_cache)
            c['tipo'] = 'SEMILAVORATO' if ha_figli_bom else ('DA_CLASSIFICARE' if not approvv else approvv.tipo_approvvigionamento)
            c['origine_costo'] = 'CALCOLATO_BOM_ROUTING' if ha_figli_bom else 'NON_CLASSIFICATO'
            if sub['codici_senza_costo']:
                c['costo_unitario'] = None
                c['costo_esteso'] = None
                c['disponibile'] = False
            else:
                c['costo_unitario'] = sub['costo_totale']
                c['costo_esteso'] = round(sub['costo_totale'] * c['quantita_totale'], 4)
                c['disponibile'] = True
        _annota_costo_albero(c.get('figli', []), _cache)


@magazzino_bp.route('/api/costo_standard/<codice>/dettaglio_materiali')
def api_costo_standard_dettaglio_materiali(codice):
    """
    Esplosione GERARCHICA (multilivello, non appiattita) della distinta con
    costo standard unitario/esteso per ogni componente e la sua origine
    (prezzo d'acquisto registrato, oppure calcolato da BOM+routing per un
    semilavorato interno) — dietro al pulsante "Materiali" della pagina
    Costo Standard. I componenti senza prezzo/costo configurato sono
    segnalati esplicitamente (disponibile=false), mai mostrati come zero.
    """
    cache = {}
    componenti = _esplodi_bom_wood(codice, qta=1.0)
    _annota_costo_albero(componenti, cache)
    radice = _calcola_costo_standard(codice, _cache=cache)
    return jsonify({
        'codice': codice,
        'costo_radice': _costo_standard_serializzabile(radice),
        'componenti': componenti,
    })


def _crea_versione_costo_standard(codice):
    """
    Calcola il costo standard di un codice e lo salva SIA nella tabella
    "ultimo calcolo" (CostoStandardWood, per la vista rapida) SIA come nuova
    riga immutabile in CostoStandardVersioneWood (versione incrementale) —
    ogni chiamata crea sempre una nuova versione, mai sovrascrive una
    versione precedente. Ritorna (dict_calcolo, oggetto_versione).
    """
    r = _costo_standard_serializzabile(_calcola_costo_standard(codice))

    cs = CostoStandardWood.query.get(codice)
    if not cs:
        cs = CostoStandardWood(codice=codice)
        db.session.add(cs)
    cs.costo_materiali          = r['costo_materiali']
    cs.costo_lavorazione        = r['costo_lavorazione']
    cs.costo_manodopera         = r['costo_manodopera']
    cs.costo_overhead           = r['costo_overhead']
    cs.costo_overhead_materiali = r['costo_overhead_materiali']
    cs.costo_overhead_produzione = r['costo_overhead_produzione']
    cs.costo_totale             = r['costo_totale']
    cs.ore_manodopera_standard  = r['ore_manodopera_standard']
    cs.completo                 = r['completo']
    cs.codici_senza_costo       = ','.join(r['codici_senza_costo'])
    cs.calcolato_il             = datetime.utcnow()

    ultima_versione = (db.session.query(db.func.max(CostoStandardVersioneWood.versione))
                       .filter_by(codice=codice).scalar() or 0)
    versione = CostoStandardVersioneWood(
        codice=codice, versione=ultima_versione + 1,
        costo_materiali=r['costo_materiali'], costo_lavorazione=r['costo_lavorazione'],
        costo_manodopera=r['costo_manodopera'],
        costo_overhead=r['costo_overhead'], costo_overhead_materiali=r['costo_overhead_materiali'],
        costo_overhead_produzione=r['costo_overhead_produzione'], costo_totale=r['costo_totale'],
        ore_manodopera_standard=r['ore_manodopera_standard'],
        overhead_materiali_pct_usata=_overhead_pct_materiali(), overhead_produzione_pct_usata=_overhead_pct_produzione(),
        completo=r['completo'], codici_senza_costo=','.join(r['codici_senza_costo']),
    )
    db.session.add(versione)
    db.session.flush()  # per avere versione.id disponibile subito, senza commit qui (lo fa il chiamante)

    # Dettaglio per componente, congelato in QUESTO momento — riusa la stessa
    # esplosione/annotazione del popup "Materiali", poi appiattisce sommando
    # le quantità se un codice compare più volte nell'albero (il prezzo resta
    # lo stesso ovunque compaia, quindi non serve mediarlo).
    albero = _esplodi_bom_wood(codice, qta=1.0)
    cache_costi = {}
    _annota_costo_albero(albero, cache_costi)
    dettaglio_per_codice = {}
    def _raccogli_dettaglio(nodi):
        for n in nodi:
            riga = dettaglio_per_codice.setdefault(n['codice'], {'quantita': 0.0, 'prezzo': n['costo_unitario'], 'tipo': n['tipo']})
            riga['quantita'] += n['quantita_totale']
            _raccogli_dettaglio(n.get('figli', []))
    _raccogli_dettaglio(albero)
    for cod, info in dettaglio_per_codice.items():
        db.session.add(CostoStandardVersioneDettaglioWood(
            versione_id=versione.id, codice_componente=cod, quantita_standard=round(info['quantita'], 4),
            prezzo_standard_unitario=info['prezzo'], tipo=info['tipo'],
        ))

    # Congela le fasi di routing DEL CODICE STESSO (non dei sottocomponenti:
    # solo il routing del codice calcolato viene mai confrontato a consuntivo
    # con gli eventi di produzione) — reparto, produttività e tariffe di
    # QUESTO momento, per poter isolare in futuro una vera varianza di tariffa.
    for f in CicloLavoroWood.query.filter_by(codice=codice).all():
        if f.centro_costo:
            db.session.add(CostoStandardVersioneFaseWood(
                versione_id=versione.id, sequenza=f.sequenza, nome_reparto=f.centro_costo.nome,
                produttivita_oraria_congelata=f.produttivita_oraria or 0,
                costo_orario_congelato=f.centro_costo.costo_orario or 0,
                tariffa_manodopera_congelata=f.centro_costo.tariffa_manodopera_diretta_oraria or 0,
            ))

    return r, versione


@magazzino_bp.route('/api/costo_standard/<codice>')
def api_costo_standard_calcola(codice):
    """Calcolo LIVE (non salvato, nessuna nuova versione) — usato per anteprima prima di 'Ricalcola e salva'."""
    r = _costo_standard_serializzabile(_calcola_costo_standard(codice))
    return jsonify({'codice': codice, **r})


@magazzino_bp.route('/api/costo_standard/<codice>/salva', methods=['POST'])
def api_costo_standard_salva(codice):
    """Calcola e PERSISTE il costo standard di un codice: aggiorna la vista
    rapida (CostoStandardWood) e crea una nuova versione immutabile."""
    r, versione = _crea_versione_costo_standard(codice)
    db.session.commit()
    return jsonify({'ok': True, 'codice': codice, 'versione': versione.versione, **r})


@magazzino_bp.route('/api/costo_standard/<codice>/versioni')
def api_costo_standard_versioni(codice):
    """Storico di tutte le versioni salvate del costo standard di un codice."""
    versioni = (CostoStandardVersioneWood.query.filter_by(codice=codice)
                .order_by(CostoStandardVersioneWood.versione.desc()).all())
    return jsonify([{
        'id': v.id, 'versione': v.versione, 'costo_materiali': v.costo_materiali,
        'costo_lavorazione': v.costo_lavorazione, 'costo_overhead': v.costo_overhead,
        'costo_totale': v.costo_totale, 'ore_manodopera_standard': v.ore_manodopera_standard,
        'overhead_pct_usata': v.overhead_pct_usata, 'completo': v.completo,
        'codici_senza_costo': v.codici_senza_costo.split(',') if v.codici_senza_costo else [],
        'calcolato_il': v.calcolato_il.strftime('%d/%m/%Y %H:%M') if v.calcolato_il else None,
    } for v in versioni])


@magazzino_bp.route('/api/costo_standard')
def api_costo_standard_lista():
    """Elenco dei costi standard già salvati (i codici padre della distinta Iron Wood, con o senza calcolo salvato)."""
    padri = sorted({row[0] for row in db.session.query(DistintaBaseWood.codice_padre).distinct().all()})
    salvati = {c.codice: c for c in CostoStandardWood.query.all()}
    risultato = []
    for codice in padri:
        cs = salvati.get(codice)
        risultato.append({
            'codice': codice,
            'costo_materiali':          cs.costo_materiali if cs else None,
            'costo_lavorazione':        cs.costo_lavorazione if cs else None,
            'costo_manodopera':         cs.costo_manodopera if cs else None,
            'costo_overhead':           cs.costo_overhead if cs else None,
            'costo_overhead_materiali': cs.costo_overhead_materiali if cs else None,
            'costo_overhead_produzione': cs.costo_overhead_produzione if cs else None,
            'costo_totale':             cs.costo_totale if cs else None,
            'ore_manodopera_standard':  cs.ore_manodopera_standard if cs else None,
            'completo':                 cs.completo if cs else None,
            'codici_senza_costo':       cs.codici_senza_costo.split(',') if cs and cs.codici_senza_costo else [],
            'calcolato_il':             cs.calcolato_il.strftime('%d/%m/%Y %H:%M') if cs and cs.calcolato_il else None,
        })
    return jsonify(risultato)


@magazzino_bp.route('/api/impostazioni_costo', methods=['GET', 'PUT'])
def api_impostazioni_costo():
    """
    Due aliquote separate, come in SAP (costing sheet): Overhead Materiali
    (% su costo_materiali) e Overhead Produzione (% su lavorazione+manodopera).
    Non una percentuale unica su tutto.
    """
    imp = ImpostazioneCostoWood.query.first()
    if request.method == 'GET':
        return jsonify({
            'aliquota_overhead_materiali_pct': imp.aliquota_overhead_materiali_pct if imp else 0,
            'aliquota_overhead_produzione_pct': imp.aliquota_overhead_produzione_pct if imp else 0,
        })
    d = request.get_json(force=True)
    try:
        materiali_pct = float(d.get('aliquota_overhead_materiali_pct') or 0)
        produzione_pct = float(d.get('aliquota_overhead_produzione_pct') or 0)
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Aliquota non valida'}), 400
    if materiali_pct < 0 or produzione_pct < 0:
        return jsonify({'errore': True, 'messaggio': 'Le aliquote non possono essere negative'}), 400
    if not imp:
        imp = ImpostazioneCostoWood()
        db.session.add(imp)
    imp.aliquota_overhead_materiali_pct = materiali_pct
    imp.aliquota_overhead_produzione_pct = produzione_pct
    imp.aggiornato_il = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'aliquota_overhead_materiali_pct': imp.aliquota_overhead_materiali_pct,
                    'aliquota_overhead_produzione_pct': imp.aliquota_overhead_produzione_pct})


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDE DI LAVORAZIONE IRON WOOD — Taglio / Piega / Satinatura, inserite a
#  mano da Angelo (le sue schede erano sparse in Excel diversi tra loro).
#  Matrici e Rulli sono anagrafiche di supporto per i menu a tendina della
#  Scheda Piega, identificate dal proprio codice (come qualsiasi altro codice).
# ══════════════════════════════════════════════════════════════════════════════
@magazzino_bp.route('/parametri-lavorazione-wood')
def pagina_parametri_lavorazione():
    return render_template('parametri_lavorazione_wood.html', active='parametri_lavorazione')


@magazzino_bp.route('/esploratore-prodotto')
def pagina_esploratore_prodotto():
    """
    Vista visiva alternativa dello stesso esploso BOM + Ciclo di Lavoro già
    disponibile in Parametri di Lavorazione — stesso endpoint dati
    (/api/distinta_base_wood/<codice>), nessun nuovo calcolo lato server:
    solo una presentazione più immediata a colpo d'occhio (card, colori,
    nodi apribili/richiudibili) per chi deve leggerla al volo, non editarla.
    """
    return render_template('esploratore_prodotto.html', active='esploratore_prodotto')


@magazzino_bp.route('/schede-lavorazione-wood')
def pagina_schede_lavorazione():
    """Vecchio indirizzo — redirect di cortesia verso la pagina rinominata."""
    from flask import redirect, url_for
    return redirect(url_for('magazzino.pagina_parametri_lavorazione'))


def _crud_anagrafica_semplice(Model, nome_singolare):
    """Fabbrica per le due anagrafiche identiche Matrici/Rulli (lista + crea + elimina)."""
    def lista():
        righe = Model.query.order_by(Model.codice).all()
        return jsonify([{'id': r.id, 'codice': r.codice, 'descrizione': r.descrizione} for r in righe])

    def crea():
        d = request.get_json(force=True)
        codice = (d.get('codice') or '').strip().upper()
        if not codice:
            return jsonify({'errore': True, 'messaggio': f'Il codice {nome_singolare} è obbligatorio'}), 400
        if Model.query.filter_by(codice=codice).first():
            return jsonify({'errore': True, 'messaggio': f'Esiste già un/a {nome_singolare} con codice "{codice}"'}), 409
        r = Model(codice=codice, descrizione=(d.get('descrizione') or '').strip())
        db.session.add(r); db.session.commit()
        return jsonify({'ok': True, 'id': r.id})

    def elimina(rid):
        r = Model.query.get_or_404(rid)
        db.session.delete(r); db.session.commit()
        return jsonify({'ok': True})

    return lista, crea, elimina


_lista_matrici, _crea_matrice, _elimina_matrice = _crud_anagrafica_semplice(MatriceWood, 'matrice')
magazzino_bp.add_url_rule('/api/matrici_wood', 'lista_matrici', _lista_matrici, methods=['GET'])
magazzino_bp.add_url_rule('/api/matrici_wood', 'crea_matrice', _crea_matrice, methods=['POST'])
magazzino_bp.add_url_rule('/api/matrici_wood/<int:rid>', 'elimina_matrice', _elimina_matrice, methods=['DELETE'])

_lista_rulli, _crea_rullo, _elimina_rullo = _crud_anagrafica_semplice(RulloWood, 'rullo')
magazzino_bp.add_url_rule('/api/rulli_wood', 'lista_rulli', _lista_rulli, methods=['GET'])
magazzino_bp.add_url_rule('/api/rulli_wood', 'crea_rullo', _crea_rullo, methods=['POST'])
magazzino_bp.add_url_rule('/api/rulli_wood/<int:rid>', 'elimina_rullo', _elimina_rullo, methods=['DELETE'])


@magazzino_bp.route('/api/lunghezze_barra_wood', methods=['GET'])
def api_lista_lunghezze_barra():
    righe = LunghezzaBarraWood.query.order_by(LunghezzaBarraWood.valore_mm).all()
    return jsonify([{'id': r.id, 'valore_mm': r.valore_mm,
                      'etichetta': r.etichetta or f'{r.valore_mm/1000:g} mt'} for r in righe])


@magazzino_bp.route('/api/lunghezze_barra_wood', methods=['POST'])
def api_crea_lunghezza_barra():
    d = request.get_json(force=True)
    try:
        valore_mm = float(d.get('valore_mm'))
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Il valore in mm è obbligatorio e numerico'}), 400
    if LunghezzaBarraWood.query.filter_by(valore_mm=valore_mm).first():
        return jsonify({'errore': True, 'messaggio': 'Esiste già una lunghezza barra con questo valore'}), 409
    r = LunghezzaBarraWood(valore_mm=valore_mm, etichetta=(d.get('etichetta') or '').strip())
    db.session.add(r); db.session.commit()
    return jsonify({'ok': True, 'id': r.id})


@magazzino_bp.route('/api/lunghezze_barra_wood/<int:rid>', methods=['DELETE'])
def api_elimina_lunghezza_barra(rid):
    r = LunghezzaBarraWood.query.get_or_404(rid)
    db.session.delete(r); db.session.commit()
    return jsonify({'ok': True})


def _flatten_albero_lavorazione(codice_radice, _visitati=None, _profondita=0, _max_profondita=15):
    """
    Percorre tutta la Distinta Base a partire da codice_radice e ritorna la
    lista di TUTTE le coppie (codice_padre, codice_figlio) raggiungibili, a
    qualunque livello — usa _righe_bom_attive_wood, quindi se un componente
    ha alternative (es. barra 7m/6m) considera solo quella attiva. Un guard
    su _visitati evita loop infiniti in caso di cicli accidentali nella BOM.
    """
    if _visitati is None:
        _visitati = set()
    if codice_radice in _visitati or _profondita > _max_profondita:
        return []
    _visitati.add(codice_radice)
    coppie = []
    figli = _righe_bom_attive_wood(codice_radice)
    for f in figli:
        coppie.append((codice_radice, f.codice_figlio))
    for f in figli:
        coppie.extend(_flatten_albero_lavorazione(f.codice_figlio, _visitati, _profondita + 1, _max_profondita))
    return coppie


@magazzino_bp.route('/api/schede_lavorazione_wood/radici')
def api_radici_schede_lavorazione():
    """
    Codici 'di vertice': compaiono come codice_padre in Distinta Base ma MAI
    come codice_figlio di nessun altro — usati per il prev/next della
    pagina (ogni 'Codice padre generale' è una di queste radici).
    """
    tutti_padri = {r[0] for r in db.session.query(DistintaBaseWood.codice_padre).distinct().all()}
    tutti_figli = {r[0] for r in db.session.query(DistintaBaseWood.codice_figlio).distinct().all()}
    return jsonify(sorted(tutti_padri - tutti_figli))


@magazzino_bp.route('/api/schede_lavorazione_wood/albero/<codice_radice>')
def api_albero_schede_lavorazione(codice_radice):
    """
    Tabella UNICA (non tre separate) per un 'Codice padre generale': ogni
    riga è una coppia padre/figlio della Distinta Base sotto quella radice,
    con tutte le specifiche macchina già compilate (o vuote se non ancora
    inserite) — esattamente come la tabella dello screenshot di riferimento.
    """
    codice_radice = codice_radice.strip().upper()
    coppie = _flatten_albero_lavorazione(codice_radice)
    viste, coppie_uniche = set(), []
    for p in coppie:
        if p not in viste:
            viste.add(p); coppie_uniche.append(p)
    if not coppie_uniche:
        return jsonify({'trovato': False, 'righe': []})

    schede = {(s.codice_padre, s.codice_figlio): s for s in
              SchedaLavorazioneWood.query.filter(
                  SchedaLavorazioneWood.codice_padre.in_({p for p, _ in coppie_uniche})).all()}

    # Quante fasi di Ciclo di Lavoro esistono per ogni codice_padre di questo albero — una sola query,
    # non una per riga: alimenta la nuova colonna "Ciclo di Lavoro" e il riepilogo sopra la tabella.
    # Padre e figlio sono entrambi articoli lavorabili/classificabili.
    codici_padre = {p for p, _ in coppie_uniche}
    codici_albero = codici_padre | {f for _, f in coppie_uniche}
    conteggio_fasi = dict(
        db.session.query(CicloLavoroWood.codice, db.func.count(CicloLavoroWood.id))
        .filter(CicloLavoroWood.codice.in_(codici_albero))
        .group_by(CicloLavoroWood.codice).all()
    )

    approvvigionamenti = {a.codice: a for a in ArticoloApprovvigionamento.query.filter(ArticoloApprovvigionamento.codice.in_(codici_albero)).all()}
    righe = []
    for padre, figlio in sorted(coppie_uniche, key=lambda x: x[0]):
        s = schede.get((padre, figlio))
        righe.append({
            'id': s.id if s else None, 'codice_padre': padre, 'codice_figlio': figlio,
            'n_fasi_ciclo_lavoro_padre': conteggio_fasi.get(padre, 0),
            'n_fasi_ciclo_lavoro_figlio': conteggio_fasi.get(figlio, 0),
            # I parametri articolo sono indipendenti: sia il padre sia il figlio
            # possono essere materia prima, laserato, acquisto ecc. e avere U.M./ciclo propri.
            'approvvigionamento_padre': {'tipo_approvvigionamento': approvvigionamenti.get(padre).tipo_approvvigionamento if approvvigionamenti.get(padre) else 'DA_CLASSIFICARE', 'unita_misura': approvvigionamenti.get(padre).unita_misura if approvvigionamenti.get(padre) else ''},
            'approvvigionamento_figlio': {'tipo_approvvigionamento': approvvigionamenti.get(figlio).tipo_approvvigionamento if approvvigionamenti.get(figlio) else 'DA_CLASSIFICARE', 'unita_misura': approvvigionamenti.get(figlio).unita_misura if approvvigionamenti.get(figlio) else ''},
            'lunghezza_barra_mm': s.lunghezza_barra_mm if s else None,
            'spessore_mm': s.spessore_mm if s else None,
            'pezzi_per_barra': s.pezzi_per_barra if s else None,
            'sviluppo': s.sviluppo if s else '',
            'matrice_id': s.matrice_id if s else None,
            'matrice_codice': (s.matrice.codice if s and s.matrice else None),
            'punto_zero': s.punto_zero if s else '',
            'indice_assorbimento': s.indice_assorbimento if s else '',
            'rullo_id': s.rullo_id if s else None,
            'rullo_codice': (s.rullo.codice if s and s.rullo else None),
            'impostazione_satinatrice': s.impostazione_satinatrice if s else '',
            'note': s.note if s else '',
        })
    return jsonify({'trovato': True, 'righe': righe})


@magazzino_bp.route('/api/schede_lavorazione_wood', methods=['POST'])
def api_upsert_scheda_lavorazione():
    d = request.get_json(force=True)
    padre = (d.get('codice_padre') or '').strip().upper()
    figlio = (d.get('codice_figlio') or '').strip().upper()
    if not padre or not figlio:
        return jsonify({'errore': True, 'messaggio': 'Codice padre e figlio sono obbligatori'}), 400
    try:
        lunghezza_barra_mm = float(d['lunghezza_barra_mm']) if d.get('lunghezza_barra_mm') not in (None, '') else None
        spessore_mm = float(d['spessore_mm']) if d.get('spessore_mm') not in (None, '') else None
        pezzi_per_barra = float(d['pezzi_per_barra']) if d.get('pezzi_per_barra') not in (None, '') else None
        matrice_id = int(d['matrice_id']) if d.get('matrice_id') not in (None, '') else None
        rullo_id = int(d['rullo_id']) if d.get('rullo_id') not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Valori numerici non validi'}), 400
    if matrice_id and not MatriceWood.query.get(matrice_id):
        return jsonify({'errore': True, 'messaggio': 'Matrice non trovata'}), 404
    if rullo_id and not RulloWood.query.get(rullo_id):
        return jsonify({'errore': True, 'messaggio': 'Rullo non trovato'}), 404

    valori = dict(
        lunghezza_barra_mm=lunghezza_barra_mm, spessore_mm=spessore_mm, pezzi_per_barra=pezzi_per_barra,
        sviluppo=(d.get('sviluppo') or '').strip(), matrice_id=matrice_id,
        punto_zero=(d.get('punto_zero') or '').strip(),
        indice_assorbimento=(d.get('indice_assorbimento') or '').strip(),
        rullo_id=rullo_id, impostazione_satinatrice=(d.get('impostazione_satinatrice') or '').strip(),
        note=(d.get('note') or '').strip(),
    )
    esistente = SchedaLavorazioneWood.query.filter_by(codice_padre=padre, codice_figlio=figlio).first()
    if esistente:
        for k, v in valori.items():
            setattr(esistente, k, v)
        rid = esistente.id
    else:
        r = SchedaLavorazioneWood(codice_padre=padre, codice_figlio=figlio, **valori)
        db.session.add(r); db.session.flush()
        rid = r.id
    db.session.commit()
    return jsonify({'ok': True, 'id': rid})


@magazzino_bp.route('/api/schede_lavorazione_wood/<int:rid>', methods=['DELETE'])
def api_elimina_scheda_lavorazione(rid):
    r = SchedaLavorazioneWood.query.get_or_404(rid)
    db.session.delete(r); db.session.commit()
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
#  INTEGRAZIONE MASTERLEDGER (Iron Segnaletica) — carico magazzino per i
#  materiali "da officina interna" (ferro per lavorazioni, filo di saldatura,
#  DPI/consumabili...) quando MasterLedger registra un'Entrata Merci per un
#  articolo classificato come tale (non destinato a MasterLogistic-WMS).
#  Endpoint SEPARATI da quelli usati dall'interfaccia di questa app (che
#  restano aperti, uso interno): qui serve sempre il token
#  MASTERLEDGER_API_TOKEN, stesso schema Bearer già usato per l'integrazione
#  PP (vedi blueprints/produzione_pp/routes.py::_api_auth) ma con una chiave
#  indipendente — così le due integrazioni si possono revocare separatamente.
#  Riusa _registra_movimento_giacenza già definita sopra in questo file:
#  stesso magazzino (GiacenzaWood), stesso storico movimenti
#  (MovimentoGiacenzaWood), nessuna tabella nuova.
# ══════════════════════════════════════════════════════════════════════════════
def _auth_masterledger():
    token = current_app.config.get('MASTERLEDGER_API_TOKEN', '')
    if not token:
        return jsonify(ok=False, error="API MasterLedger disabilitata: configurare MASTERLEDGER_API_TOKEN"), 503
    bearer = request.headers.get('Authorization', '')
    got = bearer[7:].strip() if bearer.lower().startswith('bearer ') else request.headers.get('X-MasterLedger-Token', '')
    if got != token:
        return jsonify(ok=False, error="non autorizzato"), 401


@magazzino_bp.route('/api/masterledger/carico_magazzino_produzione', methods=['POST'])
def api_masterledger_carico():
    """
    Notifica di arrivo merce da un'Entrata Merci di MasterLedger. Applica un
    carico (quantita positiva) alla giacenza del codice indicato — stesso
    motore di _registra_movimento_giacenza usato dalla rettifica manuale,
    ma con tipo movimento distinto ('carico_masterledger') per restare
    tracciabile nello storico come arrivato dall'integrazione, non da un
    click umano. Non crea l'articolo in ArticoloApprovvigionamento se manca:
    la classificazione (tipo/unità di misura) resta responsabilità di questa
    app, MasterLedger manda solo il movimento fisico.
    """
    auth = _auth_masterledger()
    if auth:
        return auth
    d = request.get_json(force=True) or {}
    sku = (d.get('sku') or '').strip()
    if not sku:
        return jsonify(ok=False, error='sku obbligatorio'), 400
    try:
        quantita = float(d.get('quantita'))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='quantita non valida'), 400
    if quantita <= 0:
        return jsonify(ok=False, error='quantita deve essere positiva (questo endpoint carica soltanto, non scarica)'), 400

    riferimento = (d.get('doc_riferimento') or '').strip()
    note = (d.get('note') or '').strip()
    _registra_movimento_giacenza(sku, quantita, 'carico_masterledger', riferimento=riferimento, note=note)
    db.session.commit()
    g = GiacenzaWood.query.get(sku)
    return jsonify(ok=True, sku=sku, quantita_totale=g.quantita if g else quantita)


@magazzino_bp.route('/api/masterledger/giacenza', methods=['GET'])
def api_masterledger_giacenza():
    """
    Lettura giacenza attuale, stesso formato semplice {sku: {stock,
    scorta_minima, unita_misura}} già usato da MasterLogistic-WMS
    (get_magazzino_wms) — così il client MasterLedger può riusare la stessa
    forma di parsing per entrambe le integrazioni.
    """
    auth = _auth_masterledger()
    if auth:
        return auth
    scorte_minime = {s.codice: s.scorta_minima for s in ScortaMinimaWood.query.all()}
    approvvigionamenti = {a.codice: a.unita_misura for a in ArticoloApprovvigionamento.query.all()}
    return jsonify({
        g.codice: {
            'stock': g.quantita or 0,
            'scorta_minima': scorte_minime.get(g.codice, 0),
            'unita_misura': approvvigionamenti.get(g.codice, ''),
        } for g in GiacenzaWood.query.all()
    })

