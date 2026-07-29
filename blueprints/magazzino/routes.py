from flask import Blueprint, render_template, jsonify, request
from datetime import datetime
from models import db, ArticoloML, DistintaBaseML, DistintaBaseWood

magazzino_bp = Blueprint('magazzino', __name__)


@magazzino_bp.route('/magazzino')
def index():
    return render_template('magazzino/index.html', active='magazzino')


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
    if _visitati is None:
        _visitati = set()
    if codice in _visitati or _profondita >= _max_profondita:
        return []
    _visitati = _visitati | {codice}

    righe = DistintaBaseWood.query.filter_by(codice_padre=codice).order_by(DistintaBaseWood.livello).all()
    componenti = []
    for r in righe:
        art = ArticoloML.query.filter_by(sku=r.codice_figlio).first()
        qta_totale = (r.quantita or 1.0) * qta
        componenti.append({
            'id':                r.id,
            'codice':            r.codice_figlio,
            'descrizione':       art.descrizione if art else '',
            'quantita_unitaria': r.quantita,
            'quantita_totale':   round(qta_totale, 3),
            'stock':             art.stock if art else None,
            'fornitore':         art.fornitore if art else None,
            'note':              r.note or '',
            'figli':             _esplodi_bom_wood(r.codice_figlio, qta_totale, _visitati, _profondita + 1, _max_profondita),
        })
    return componenti


@magazzino_bp.route('/api/distinta_base_wood/<codice>')
def api_distinta_base_wood(codice):
    art = ArticoloML.query.filter_by(sku=codice).first()
    componenti = _esplodi_bom_wood(codice)
    return jsonify({
        'codice':      codice,
        'descrizione': art.descrizione if art else '',
        'trovato':     art is not None,
        'ha_bom':      len(componenti) > 0,
        'componenti':  componenti,
    })


@magazzino_bp.route('/api/distinta_base_wood', methods=['GET'])
def api_lista_distinta_wood():
    """Restituisce tutte le righe della distinta base Iron Wood (per la tabella di gestione)."""
    righe = DistintaBaseWood.query.order_by(DistintaBaseWood.codice_padre, DistintaBaseWood.livello).all()
    return jsonify([{
        'id':            r.id,
        'codice_padre':  r.codice_padre,
        'codice_figlio': r.codice_figlio,
        'quantita':      r.quantita,
        'livello':       r.livello,
        'note':          r.note or '',
    } for r in righe])


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
