from flask import Blueprint, render_template, jsonify, request
from datetime import datetime, date
from models import (db, ArticoloML, DistintaBaseML, DistintaBaseWood, Commessa, RigaCommessa,
                    CentroCostoWood, CicloLavoroWood, ArticoloApprovvigionamento,
                    TIPI_APPROVVIGIONAMENTO, GiacenzaWood, MovimentoGiacenzaWood, OrdineProduzione,
                    ImpostazioneCostoWood, CostoStandardWood, VarianzaProduzioneWood,
                    CostoPianificatoCentroWood, DRIVER_ATTIVITA_WOOD, VOCI_COSTO_PIANIFICATO_WOOD,
                    CostoStandardVersioneWood, LegameCostoStandardOrdineWood,
                    CostoStandardVersioneDettaglioWood, CostoStandardVersioneFaseWood)

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
        record.aggiornato_il = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'codice': codice,
                        'tipo_approvvigionamento': record.tipo_approvvigionamento,
                        'lead_time_fornitura_giorni': record.lead_time_fornitura_giorni,
                        'costo_acquisto_standard': record.costo_acquisto_standard})
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

    righe = DistintaBaseWood.query.filter_by(codice_padre=codice).order_by(DistintaBaseWood.livello).all()
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

        esistente = DistintaBaseWood.query.filter_by(codice_padre=padre, codice_figlio=figlio).first()
        if esistente:
            esistente.quantita = quantita
            esistente.livello  = livello
            esistente.note     = note
        else:
            db.session.add(DistintaBaseWood(
                codice_padre=padre, codice_figlio=figlio,
                quantita=quantita, livello=livello, note=note,
                creato_il=datetime.utcnow()
            ))
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'errore': True, 'messaggio': str(e)}), 500


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


@magazzino_bp.route('/api/distinta_base_wood/importa', methods=['POST'])
def api_importa_distinta_wood():
    """
    Caricamento massivo da file Excel/CSV per la distinta Iron Wood.
    Stesso principio del caricamento massivo articoli di MasterLogistic
    (route /importazione_massiva): colonne lette PER NOME (non per
    posizione fissa, quindi nessun rischio di sfasamento se il file
    cambia struttura), UPSERT riga per riga — mai un replace/DROP.
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
            esistenti = {(r.codice_padre, r.codice_figlio): r for r in DistintaBaseWood.query.all()}

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
                if numlev == 0:
                    continue  # riga di intestazione del prodotto, non un componente

                codcom_raw = str(row['codcom']).strip()
                child = codcom_raw.split()[-1].strip().upper() if codcom_raw else ''
                if not child or child.lower() == 'nan':
                    scartate += 1
                    righe_scartate.append(f"riga {i+2}: componente illeggibile in CODCOM ('{codcom_raw}')")
                    continue

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

            db.session.commit()
            return jsonify({
                'ok': True, 'nuovi': nuovi, 'aggiornati': aggiornati,
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


@magazzino_bp.route('/api/giacenza_wood', methods=['GET'])
def api_giacenza_lista():
    """Elenco giacenze, con limite di default (200) + ricerca — stesso principio
    già usato per distinta_base_wood, per non appesantire la pagina con import enormi."""
    LIMITE_DEFAULT = 200
    q = (request.args.get('q') or '').strip()
    query = GiacenzaWood.query
    totale = query.count()
    if q:
        query = query.filter(GiacenzaWood.codice.ilike(f'%{q}%'))
    righe = query.order_by(GiacenzaWood.aggiornato_il.desc()).limit(LIMITE_DEFAULT).all()
    return jsonify({
        'righe': [{'codice': g.codice, 'quantita': g.quantita,
                   'aggiornato_il': g.aggiornato_il.strftime('%d/%m/%Y %H:%M') if g.aggiornato_il else ''} for g in righe],
        'totale': totale, 'mostrate': len(righe), 'filtrato': bool(q), 'limite': LIMITE_DEFAULT,
    })


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


def _netta_e_esplodi_wood(codice, qta, giacenza_residua, out, _visitati=None, _profondita=0, _max_profondita=12):
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
    """
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita or qta <= 0:
        return
    _visitati = _visitati | {codice}

    disponibile_prima = giacenza_residua.get(codice, 0.0)
    usato = min(disponibile_prima, qta)
    giacenza_residua[codice] = disponibile_prima - usato
    mancante = qta - usato

    riga = out.setdefault(codice, {'fabbisogno': 0.0, 'usato': 0.0, 'mancante': 0.0})
    riga['fabbisogno'] += qta
    riga['usato']      += usato
    riga['mancante']   += mancante

    if mancante <= 0:
        return
    righe_bom = DistintaBaseWood.query.filter_by(codice_padre=codice).all()
    for r in righe_bom:
        qta_figlio = (r.quantita or 1.0) * mancante
        _netta_e_esplodi_wood(r.codice_figlio, qta_figlio, giacenza_residua, out, _visitati, _profondita + 1, _max_profondita)


def _giacenza_residua_dopo_impegni(escludi_op_id=None):
    """
    Parte dalla giacenza fisica attuale e simula il consumo di TUTTI gli OP
    aperti (stato in STATI_CHE_IMPEGNANO), in ordine di creazione (i più
    vecchi vengono serviti prima) — ritorna il dict {codice: qta rimasta}
    DOPO aver tolto quanto già impegnato da quegli OP.
    """
    giacenza_residua = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    op_aperti = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
                 .order_by(OrdineProduzione.id.asc()).all())
    for op in op_aperti:
        if escludi_op_id and op.id == escludi_op_id:
            continue
        saldo = (op.qta_pianificata or 0) - (op.qta_buona or 0)
        if saldo > 0:
            _netta_e_esplodi_wood(op.codice_articolo, saldo, giacenza_residua, {})
    return giacenza_residua


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
        'fornitore_esterno': c.fornitore_esterno, 'tariffa_esterna': c.tariffa_esterna,
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
        if 'reparto_gruppo' in d: c.reparto_gruppo = (d.get('reparto_gruppo') or '').strip()
        if 'note' in d: c.note = (d.get('note') or '').strip()
        if 'fornitore_esterno' in d: c.fornitore_esterno = (d.get('fornitore_esterno') or '').strip()
        if 'tariffa_esterna' in d:
            v = d.get('tariffa_esterna')
            c.tariffa_esterna = float(v) if v not in (None, '') else None
            if c.tariffa_esterna is not None and c.tariffa_esterna < 0:
                return jsonify({'errore': True, 'messaggio': 'La tariffa esterna non può essere negativa'}), 400
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
            if periodo not in ('mensile', 'annuale'):
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
        'produttivita_oraria': r.produttivita_oraria, 'note': r.note,
    }


@magazzino_bp.route('/api/ciclo_lavoro_wood')
def api_ciclo_lavoro_lista():
    codice_filtro = request.args.get('codice', '').strip()
    q = CicloLavoroWood.query
    if codice_filtro:
        q = q.filter_by(codice=codice_filtro)
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

    esistente = CicloLavoroWood.query.filter_by(codice=codice, sequenza=sequenza).first()
    if esistente:
        # aggiorna la fase esistente invece di duplicarla (stesso codice+sequenza)
        esistente.centro_costo_id = centro_costo_id
        esistente.produttivita_oraria = produttivita
        esistente.note = (d.get('note') or '').strip()
        db.session.commit()
        return jsonify({'ok': True, 'id': esistente.id, 'aggiornata': True})

    r = CicloLavoroWood(codice=codice, sequenza=sequenza, centro_costo_id=centro_costo_id,
                         produttivita_oraria=produttivita, note=(d.get('note') or '').strip())
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
    for r in DistintaBaseWood.query.filter_by(codice_padre=codice).all():
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
