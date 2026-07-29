from flask import Blueprint, render_template, jsonify, request
from datetime import datetime
from models import (db, ArticoloML, DistintaBaseML, DistintaBaseWood, Commessa, RigaCommessa,
                    CentroCostoWood, CicloLavoroWood, ArticoloApprovvigionamento,
                    TIPI_APPROVVIGIONAMENTO)

magazzino_bp = Blueprint('magazzino', __name__)

STATI_CHIUSI_COMMESSA = {"COMPLETATA", "SPEDITA", "ANNULLATA"}


@magazzino_bp.route('/magazzino')
def index():
    return render_template('magazzino/index.html', active='magazzino')


@magazzino_bp.route('/centri-costo-wood')
def pagina_centri_costo():
    return render_template('centri_costo_wood.html', active='centri_costo')


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
        record.aggiornato_il = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': True, 'codice': codice,
                        'tipo_approvvigionamento': record.tipo_approvvigionamento,
                        'lead_time_fornitura_giorni': record.lead_time_fornitura_giorni})
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


@magazzino_bp.route('/api/distinta_base/<codice>')
def api_distinta_base(codice):
    try:
        art = ArticoloML.query.filter_by(sku=codice).first()
        componenti = _esplodi_bom(codice)
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


def _arricchisci_bom_wood(componenti, descr_map, fasi_map):
    for c in componenti:
        info = descr_map.get(c['codice'])
        if info:
            c['descrizione'] = info['descrizione']
            c['stock']       = info['stock']
            c['fornitore']   = info['fornitore']
        c['cicli_lavoro'] = fasi_map.get(c['codice'], [])
        _arricchisci_bom_wood(c.get('figli', []), descr_map, fasi_map)


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

    _arricchisci_bom_wood(componenti, descr_map, fasi_map)

    info_root = descr_map.get(codice)
    return jsonify({
        'codice':       codice,
        'descrizione':  info_root['descrizione'] if info_root else '',
        'trovato':      info_root is not None,
        'ha_bom':       len(componenti) > 0,
        'cicli_lavoro': fasi_map.get(codice, []),
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
    Fabbisogno di materie prime/semilavorati per TUTTE le commesse Iron Wood
    ancora aperte (non COMPLETATA/SPEDITA/ANNULLATA): per ogni riga con saldo
    da produrre, esplode la distinta base locale (distinta_base_wood) e
    aggrega i componenti necessari a ogni livello.
    Nessuna scrittura qui: solo lettura, per essere interrogato da MasterLogistic.
    """
    aggregato = {}
    origine = {}  # codice -> lista di {commessa, riga_codice, quantita}

    commesse = Commessa.query.filter(~Commessa.stato.in_(STATI_CHIUSI_COMMESSA)).all()
    for c in commesse:
        for riga in c.righe:
            saldo = riga.saldo
            if saldo <= 0:
                continue
            componenti = _esplodi_bom_wood(riga.codice, qta=saldo)
            if not componenti:
                continue
            locale = {}
            _flatten_componenti(componenti, locale)
            for codice, qta in locale.items():
                aggregato[codice] = aggregato.get(codice, 0) + qta
                origine.setdefault(codice, []).append({
                    'commessa': c.numero, 'riga_codice': riga.codice, 'quantita': round(qta, 3)
                })

    risultato = []
    for codice, qta in aggregato.items():
        art = ArticoloML.query.filter_by(sku=codice).first()
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
        'id': c.id, 'nome': c.nome, 'esterno': c.esterno, 'note': c.note,
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
    c = CentroCostoWood(nome=nome, esterno=bool(d.get('esterno')), note=(d.get('note') or '').strip())
    db.session.add(c)
    db.session.commit()
    return jsonify({'ok': True, 'id': c.id})


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
