import io
import re
from datetime import datetime, date

import PyPDF2
from flask import Blueprint, render_template, jsonify, request, Response

from models import (db, OrdineAcquistoWood, RigaOrdineAcquistoWood,
                    DDTCaricoWood, RigaDDTCaricoWood, MappaCodiceFornitoreWood,
                    OrdineProduzione, GiacenzaWood, ArticoloApprovvigionamento, ScortaMinimaWood)
from blueprints.magazzino.routes import (_registra_movimento_giacenza, api_fabbisogno_produzione,
                    _netta_e_esplodi_wood, _carica_mappa_distinta_base_wood, STATI_CHE_IMPEGNANO, _saldo_materiale_op,
                    _calcola_campi_giacenza)

acquisti_wood_bp = Blueprint('acquisti_wood', __name__)

# ══════════════════════════════════════════════════════════════════════════════
#  ORDINI DI ACQUISTO IRON WOOD — clone (adattato) del sistema di
#  MasterLogistic-WMS: carichi il PDF dell'ordine fornitore, viene parsato
#  automaticamente (fornitore, numero ordine, articoli/quantità/date), e
#  tracciato con card fino al ricevimento. A differenza dell'originale, PDF
#  e dati vivono nel database Postgres locale (non su file/JSON): su Railway
#  il filesystem è effimero e verrebbe azzerato ad ogni redeploy.
# ══════════════════════════════════════════════════════════════════════════════

SKIP_CODICI_PDF = {
    'IRON', 'Cod.', 'Vs.', 'Pag.', 'N.', 'Del.',
    'Trasporti', 'AU', 'VEN', 'ACQ', 'IT',
    'RIMBORSO', 'CONTRIBUTO', 'IMBALLO', 'IMBALLAGGI', 'COSTI',
    'TRASPORTO', 'SPESE', 'SCONTO', 'OMAGGIO',
    'NOLEGGIO', 'ACCONTO', 'CAPARRA', 'PENALE',
    'Intesa', 'San', 'Paolo', 'Banca', 'Ns.', 'banca',
    'Rif.', 'rif.', 'Destinazione', 'Destinatario', 'Valuta',
    'Sconti', 'Pagamento', 'Sconto', 'Cod.fornitore',
}


def _parse_prezzo_it(s):
    """Converte un numero in formato italiano ('1.234,56' o '423,50000') in float. None se mancante/non parsabile."""
    if not s:
        return None
    try:
        return float(s.replace('.', '').replace(',', '.'))
    except ValueError:
        return None


def _risolvi_codice_fornitore(fornitore, codice_grezzo):
    """
    Cerca in MappaCodiceFornitoreWood una riga per (fornitore, codice_grezzo)
    e ritorna il codice interno corrispondente se trovata, altrimenti ritorna
    codice_grezzo invariato (nessuna mappa = comportamento di prima, il
    codice del PDF viene usato così com'è).
    """
    if not codice_grezzo:
        return codice_grezzo
    mappa = MappaCodiceFornitoreWood.query.filter_by(
        fornitore=fornitore, codice_fornitore=codice_grezzo).first()
    return mappa.codice_interno if mappa else codice_grezzo


def _estrai_dati_ordine_acquisto(testo_completo):
    """
    Parsing del testo estratto da un PDF ordine fornitore — stessa logica
    (regex e fallback) del sistema già in uso su MasterLogistic-WMS per gli
    ordini Iron Segnaletica, qui adattata per i fornitori Iron Wood.

    NOTA IMPORTANTE: questa logica è tarata sul formato PDF Zucchetti usato
    dai fornitori già visti su MasterLogistic — se i fornitori di materiali
    Iron Wood (filo di saldatura, ferro, laserati...) usano un formato PDF
    diverso, il parsing automatico potrebbe non riconoscere tutte le righe:
    in quel caso il testo grezzo resta comunque salvato e consultabile per
    tarare i pattern, e l'ordine può essere completato a mano.
    """
    dati = {"fornitore": "Sconosciuto", "ordine_n": "N/D", "rif_fornitore": "", "articoli": []}
    linee = [l.strip() for l in testo_completo.split('\n') if l.strip()]

    # 1. Numero ordine (es. "87 /OF" oppure "Numero documento ... 87")
    for i, linea in enumerate(linee):
        if "Numero documento" in linea:
            for offset in range(1, 4):
                if i + offset < len(linee):
                    match_n = re.search(r'^(\d+)(\s*/OF)?$', linee[i + offset])
                    if match_n:
                        dati['ordine_n'] = match_n.group(1)
                        break
            if dati['ordine_n'] != "N/D":
                break
    if dati['ordine_n'] == 'N/D':
        match_of = re.search(r'(\d+)\s*/OF', testo_completo)
        if match_of:
            dati['ordine_n'] = match_of.group(1)

    # 2. Fornitore (da "Destinatario:")
    for i, linea in enumerate(linee):
        if "Destinatario:" in linea and i + 2 < len(linee):
            dati['fornitore'] = linee[i + 2] if linee[i + 1].isdigit() else linee[i + 1]
            break

    # 3. Articoli — cattura codice, descrizione, quantità, data evasione e
    # prezzo unitario, parsando solo le righe DOPO l'intestazione colonne
    # (banca/pagamento/fornitore vengono ignorati).
    #
    # Il prezzo unitario (es. "423,50000  847,00" = prezzo unitario + importo,
    # formato italiano con virgola decimale) a volte compare in coda alla
    # stessa riga articolo, a volte su una riga di continuazione a sé stante
    # subito dopo (quando la descrizione va a capo) — vedi PREZZO_RIGA_RE più
    # sotto per il secondo caso.
    HEADER_COLONNE = ('codice merce', 'descrizione della merce', 'codice articolo')
    idx_header = None
    for i, linea in enumerate(linee):
        if any(h in linea.lower() for h in HEADER_COLONNE):
            idx_header = i
            break
    linee_articoli = linee[idx_header + 1:] if idx_header is not None else linee
    PREZZO_INLINE = r'(?:\s+([\d.]{1,12},\d{1,5})\s+([\d.]{1,12},\d{1,5}))?'
    _pending_codice = None
    _pending_desc = None
    _indici_articoli = []  # indice in linee_articoli dove è stato trovato ogni articolo, per il fallback prezzo
    for idx, linea in enumerate(linee_articoli):
        if _pending_codice:
            m_qty = re.search(r'^([a-z]{1,3})\.\s+([\d\.]+)(?:,\d+)?(?:\s+(\d{2}/\d{2}/\d{4}))?' + PREZZO_INLINE, linea)
            if m_qty:
                dati['articoli'].append({
                    "codice": _pending_codice, "descrizione": _pending_desc,
                    "unita_misura": m_qty.group(1), "qta": m_qty.group(2).replace('.', ''),
                    "data_evasione": m_qty.group(3) or '', "prezzo_unitario": _parse_prezzo_it(m_qty.group(4)),
                })
                _indici_articoli.append(idx)
                _pending_codice = None
                _pending_desc = None
                continue
            else:
                _tok = linea.split()[0] if linea.split() else ''
                _solo_desc = (_tok.isupper() and len(_tok) > 4 and
                              not any(c.isdigit() for c in _tok) and _tok not in SKIP_CODICI_PDF)
                if _solo_desc:
                    _pending_desc = (_pending_desc + ' ' + linea).strip()
                    continue
                else:
                    _pending_codice = None
                    _pending_desc = None

        match_art = re.search(
            r'([A-Za-z0-9][A-Za-z0-9./_-]+)\s+(.+?)\s+([a-z]{1,3})\.\s+([\d\.]+)(?:,\d+)?(?:\s+(\d{2}/\d{2}/\d{4}))?' + PREZZO_INLINE,
            linea)
        if not match_art:
            match_art = re.search(
                r'^(\d{2,})\s+(.+?)\s+([a-z]{1,3})\.\s+([\d\.]+)(?:,\d+)?(?:\s+(\d{2}/\d{2}/\d{4}))?' + PREZZO_INLINE, linea)
        if not match_art:
            # BUG REALE TROVATO E CORRETTO: questo pattern di recupero (per
            # quando codice e descrizione stanno su una riga e la quantità
            # sulla riga successiva — comune quando PyPDF2 spezza le righe
            # di un PDF multi-colonna) escludeva "/", "_", "-", "." dal
            # codice — mentre il pattern principale sopra li include. Un
            # codice come "T30/34" finiva silenziosamente scartato: non
            # corrispondeva a NESSUNO dei pattern, quindi l'intera riga
            # spariva senza generare nessun articolo, né un errore.
            m_split = re.match(r'^([A-Za-z0-9][A-Za-z0-9./_-]+)\s+([A-Z].{3,})$', linea)
            if m_split and m_split.group(1) not in SKIP_CODICI_PDF:
                _pending_codice = m_split.group(1)
                _pending_desc = m_split.group(2).strip()
                continue
        if match_art and match_art.group(1) not in SKIP_CODICI_PDF:
            dati['articoli'].append({
                "codice": match_art.group(1), "descrizione": match_art.group(2).strip(),
                "unita_misura": match_art.group(3), "qta": match_art.group(4).replace('.', ''),
                "data_evasione": match_art.group(5) or '', "prezzo_unitario": _parse_prezzo_it(match_art.group(6)),
            })
            _indici_articoli.append(idx)

    # 3bis. Fallback prezzo: per gli articoli senza prezzo trovato inline,
    # cerca nelle 2 righe successive il pattern "prezzo_unitario importo"
    # OVUNQUE nella riga (non l'intera riga: capita che la descrizione a capo
    # resti incollata al numero, es. "...DIREZIONALE423,50000 847,00 22" —
    # PyPDF2 unisce colonne senza spazio). Si prende SOLO il primo numero
    # (prezzo unitario); l'eventuale testo prima/dopo viene ignorato.
    PREZZO_RIGA_RE = re.compile(r'(\d{1,3}(?:\.\d{3})*,\d{1,5})\s+(\d{1,3}(?:\.\d{3})*,\d{2})')
    for art, idx_trovato in zip(dati['articoli'], _indici_articoli):
        if art.get('prezzo_unitario') is not None:
            continue
        for offset in (1, 2):
            j = idx_trovato + offset
            if j < len(linee_articoli):
                m_prezzo = PREZZO_RIGA_RE.search(linee_articoli[j])
                if m_prezzo:
                    art['prezzo_unitario'] = _parse_prezzo_it(m_prezzo.group(1))
                    break

    # 4. Riferimento conferma d'ordine fornitore
    match_rif = re.search(r'Rif\.?\s*Des\.?\s*Fornitore[:\s]*([A-Za-z0-9./_-]+)', testo_completo, re.IGNORECASE)
    if match_rif:
        dati['rif_fornitore'] = match_rif.group(1)

    return dati


def _data_da_gg_mm_aaaa(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, '%d/%m/%Y').date()
    except ValueError:
        return None


def _ordine_dict(o):
    return {
        'id': o.id, 'filename': o.filename, 'fornitore': o.fornitore, 'ordine_n': o.ordine_n,
        'rif_fornitore': o.rif_fornitore,
        'data_ordine': o.data_ordine.isoformat() if o.data_ordine else None,
        'data_consegna': o.data_consegna.isoformat() if o.data_consegna else None,
        'ritiro_proprio': o.ritiro_proprio, 'confermato': o.confermato, 'stato_label': o.stato_label,
        'nota_interna': o.nota_interna, 'caricato_il': o.caricato_il.isoformat() if o.caricato_il else None,
        'articoli': [{
            'id': r.id, 'codice': r.codice, 'descrizione': r.descrizione, 'unita_misura': r.unita_misura,
            'qta_originale': r.qta_originale, 'qta_ricevuta': r.qta_ricevuta, 'data_evasione': r.data_evasione,
            'prezzo_unitario': r.prezzo_unitario,
            'codice_fornitore_originale': r.codice_fornitore_originale or '',
        } for r in o.righe],
    }


@acquisti_wood_bp.route('/ordini-acquisto-wood')
def pagina_ordini_acquisto():
    return render_template('acquisti_wood/index.html', active='acquisti_wood')


@acquisti_wood_bp.route('/api/ordini_acquisto_wood', methods=['GET'])
def api_lista_ordini_acquisto():
    ordini = OrdineAcquistoWood.query.order_by(OrdineAcquistoWood.caricato_il.desc()).all()
    return jsonify([_ordine_dict(o) for o in ordini])


@acquisti_wood_bp.route('/api/ordini_acquisto_wood/<int:oid>/testo-grezzo-pdf')
def api_testo_grezzo_pdf(oid):
    """
    ENDPOINT DIAGNOSTICO TEMPORANEO — mostra il testo COSÌ COM'È estratto
    dal PDF già caricato (senza nessun parsing sopra), per capire perché
    l'estrazione automatica degli articoli manca alcune righe: la
    formattazione di un PDF può nascondere differenze sottili (spazi,
    ritorni a capo, unità di misura scritte diversamente) invisibili
    guardando solo il PDF a video, ma decisive per il regex che estrae
    codice/descrizione/quantità.
    """
    o = OrdineAcquistoWood.query.get_or_404(oid)
    if not o.pdf_bytes:
        return jsonify({'errore': True, 'messaggio': 'Nessun PDF salvato per questo ordine.'}), 404
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(o.pdf_bytes))
        testo_completo = "\n".join(page.extract_text() or '' for page in reader.pages)
    except Exception as e:
        return jsonify({'errore': True, 'messaggio': f'Impossibile rileggere il PDF: {e}'}), 400
    return jsonify({
        'filename': o.filename,
        'righe_estratte_da_parser': [a.codice for a in o.righe],
        'testo_grezzo': testo_completo,
        'righe_numerate': [f'{i}: {r}' for i, r in enumerate(testo_completo.split('\n'))],
    })


@acquisti_wood_bp.route('/api/ordini_acquisto_wood/upload', methods=['POST'])
def api_upload_ordine_acquisto():
    file = request.files.get('file_pdf')
    if not file or not file.filename.lower().endswith('.pdf'):
        return jsonify({'errore': True, 'messaggio': 'Carica un file PDF valido.'}), 400
    if OrdineAcquistoWood.query.filter_by(filename=file.filename).first():
        return jsonify({'errore': True, 'messaggio': f'Un ordine con il file "{file.filename}" è già stato caricato.'}), 409

    pdf_bytes = file.read()
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        testo_completo = "\n".join(page.extract_text() or '' for page in reader.pages)
    except Exception as e:
        return jsonify({'errore': True, 'messaggio': f'Impossibile leggere il PDF: {e}'}), 400

    dati = _estrai_dati_ordine_acquisto(testo_completo)

    o = OrdineAcquistoWood(
        filename=file.filename, pdf_bytes=pdf_bytes, fornitore=dati['fornitore'],
        ordine_n=dati['ordine_n'], rif_fornitore=dati['rif_fornitore'],
        testo_grezzo_pdf=testo_completo, stato_label='DA_CONFERMARE',
    )
    # data_consegna comune = la prima data di evasione trovata tra gli articoli, se c'è
    prima_data = next((a['data_evasione'] for a in dati['articoli'] if a['data_evasione']), None)
    o.data_consegna = _data_da_gg_mm_aaaa(prima_data)
    db.session.add(o)
    db.session.flush()

    for art in dati['articoli']:
        try:
            qta = float(art['qta'])
        except (TypeError, ValueError):
            qta = 0
        codice_grezzo = art['codice']
        codice_risolto = _risolvi_codice_fornitore(dati['fornitore'], codice_grezzo)
        db.session.add(RigaOrdineAcquistoWood(
            ordine_id=o.id, codice=codice_risolto, descrizione=art['descrizione'],
            codice_fornitore_originale=codice_grezzo if codice_risolto != codice_grezzo else '',
            unita_misura=art['unita_misura'], qta_originale=qta, data_evasione=art['data_evasione'],
            prezzo_unitario=art.get('prezzo_unitario'),
        ))
    db.session.commit()
    return jsonify({'ok': True, 'ordine': _ordine_dict(o), 'n_articoli_trovati': len(dati['articoli'])})


@acquisti_wood_bp.route('/api/ordini_acquisto_wood/<int:oid>', methods=['PUT'])
def api_modifica_ordine_acquisto(oid):
    o = OrdineAcquistoWood.query.get_or_404(oid)
    d = request.get_json(force=True)
    if 'fornitore' in d: o.fornitore = (d.get('fornitore') or '').strip()
    if 'ordine_n' in d: o.ordine_n = (d.get('ordine_n') or 'N/D').strip()
    if 'nota_interna' in d: o.nota_interna = (d.get('nota_interna') or '').strip()
    if 'ritiro_proprio' in d: o.ritiro_proprio = bool(d.get('ritiro_proprio'))
    if 'confermato' in d:
        o.confermato = bool(d.get('confermato'))
        if o.confermato and o.stato_label == 'DA_CONFERMARE':
            o.stato_label = 'ORDINE_CONFERMATO'
    if 'stato_label' in d:
        if d['stato_label'] not in ('DA_CONFERMARE', 'ORDINE_CONFERMATO', 'ORDINE_IN_ARRIVO', 'ORDINE_DA_RITIRARE', 'ORDINE_RICEVUTO'):
            return jsonify({'errore': True, 'messaggio': 'Stato non valido'}), 400
        o.stato_label = d['stato_label']
    if 'data_consegna' in d:
        try:
            o.data_consegna = date.fromisoformat(d['data_consegna']) if d.get('data_consegna') else None
        except ValueError:
            return jsonify({'errore': True, 'messaggio': 'Data non valida'}), 400
    db.session.commit()
    return jsonify({'ok': True, 'ordine': _ordine_dict(o)})


@acquisti_wood_bp.route('/api/ordini_acquisto_wood/<int:oid>', methods=['DELETE'])
def api_elimina_ordine_acquisto(oid):
    o = OrdineAcquistoWood.query.get_or_404(oid)
    db.session.delete(o)
    db.session.commit()
    return jsonify({'ok': True})


@acquisti_wood_bp.route('/api/ordini_acquisto_wood/righe/<int:rid>', methods=['PUT'])
def api_modifica_riga_ordine(rid):
    """Aggiorna la quantità ricevuta di una riga (ricevimento manuale, in attesa della Fase 2 con DDT)."""
    r = RigaOrdineAcquistoWood.query.get_or_404(rid)
    d = request.get_json(force=True)
    try:
        if 'qta_ricevuta' in d:
            r.qta_ricevuta = float(d.get('qta_ricevuta') or 0)
        if 'qta_originale' in d:
            r.qta_originale = float(d.get('qta_originale') or 0)
        if 'prezzo_unitario' in d:
            r.prezzo_unitario = float(d['prezzo_unitario']) if d.get('prezzo_unitario') not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Quantità o prezzo non validi'}), 400
    if 'codice' in d: r.codice = (d.get('codice') or '').strip()
    if 'descrizione' in d: r.descrizione = (d.get('descrizione') or '').strip()
    db.session.commit()
    return jsonify({'ok': True})


@acquisti_wood_bp.route('/ordini_acquisto_wood/<int:oid>/pdf')
def api_scarica_pdf(oid):
    o = OrdineAcquistoWood.query.get_or_404(oid)
    if not o.pdf_bytes:
        return jsonify({'errore': True, 'messaggio': 'PDF non disponibile per questo ordine.'}), 404
    return Response(o.pdf_bytes, mimetype='application/pdf',
                     headers={'Content-Disposition': f'inline; filename="{o.filename}"'})


@acquisti_wood_bp.route('/materiale-in-arrivo')
def pagina_materiale_in_arrivo():
    return render_template('acquisti_wood/materiale_in_arrivo.html', active='acquisti_wood')


@acquisti_wood_bp.route('/api/materiale_in_arrivo')
def api_materiale_in_arrivo():
    """
    Vista consolidata degli Ordini di Acquisto NON ancora ricevuti
    completamente, ordinati per urgenza di consegna, con le righe ancora
    mancanti e un collegamento a cosa serve davvero alla produzione aperta
    (riusa /api/fabbisogno_produzione, già costruito) — per capire a colpo
    d'occhio se un ritardo sta bloccando un Ordine di Produzione o no.
    """
    from datetime import date
    oggi = date.today()

    try:
        risposta_fabbisogno = api_fabbisogno_produzione()
        fabbisogno_json = risposta_fabbisogno.get_json() if hasattr(risposta_fabbisogno, 'get_json') else risposta_fabbisogno
        codici_richiesti_produzione = {v['codice'] for v in fabbisogno_json.get('fabbisogno', [])}
    except Exception:
        codici_richiesti_produzione = set()

    ordini = (OrdineAcquistoWood.query.filter(OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO')
              .order_by(db.case((OrdineAcquistoWood.data_consegna.is_(None), 1), else_=0),
                        OrdineAcquistoWood.data_consegna.asc()).all())

    risultato = []
    for o in ordini:
        righe_mancanti = [r for r in o.righe if (r.qta_originale or 0) > (r.qta_ricevuta or 0)]
        if not righe_mancanti:
            continue  # tutte le righe già ricevute manualmente ma l'OA non è stato chiuso: non è "in arrivo"
        giorni_alla_consegna = (o.data_consegna - oggi).days if o.data_consegna else None
        risultato.append({
            'id': o.id, 'ordine_n': o.ordine_n, 'fornitore': o.fornitore, 'stato_label': o.stato_label,
            'ritiro_proprio': o.ritiro_proprio,
            'data_consegna': o.data_consegna.isoformat() if o.data_consegna else None,
            'giorni_alla_consegna': giorni_alla_consegna,
            'scaduto': giorni_alla_consegna is not None and giorni_alla_consegna < 0,
            'imminente': giorni_alla_consegna is not None and 0 <= giorni_alla_consegna <= 3,
            'righe_mancanti': [{
                'codice': r.codice, 'descrizione': r.descrizione,
                'mancante': round((r.qta_originale or 0) - (r.qta_ricevuta or 0), 3),
                'unita_misura': r.unita_misura,
                'richiesto_da_produzione': r.codice in codici_richiesti_produzione,
            } for r in righe_mancanti],
        })
    return jsonify(risultato)


# ══════════════════════════════════════════════════════════════════════════════
#  FASE 2 — DDT DI CARICO (arrivo merce): parsing PDF (clone adattato di
#  MasterLogistic, che qui gestisce DDT multi-ordine tramite "Ns. doc.(ORDFO)
#  n.: XXX"), abbinamento automatico alle righe dell'Ordine di Acquisto
#  corrispondente, e carico automatico della Giacenza Iron Wood.
#  NOTA: la risoluzione codice-fornitore → SKU interno (mappa_esterni in
#  MasterLogistic) non è ancora presente qui: si usa il codice così com'è
#  scritto sul DDT. Se i fornitori Iron Wood usano codici propri diversi dai
#  codici interni, andrà aggiunta una mappa dedicata in un prossimo passo.
# ══════════════════════════════════════════════════════════════════════════════

def _estrai_dati_ddt_carico(testo):
    """Parsing DDT di carico multi-ordine — stessa logica del sistema già in uso su MasterLogistic-WMS."""
    dati = {"fornitore": "", "numero_ddt": "", "data_ddt": "", "sezioni": []}
    linee = [l.strip() for l in testo.split('\n') if l.strip()]

    e_vendita = ('Uscita Merci' in testo or 'ORDCL' in testo or bool(re.search(r'Ns\.\s*doc\.\(ORDCL\)', testo)))
    e_acquisto = ('Entrata Merci' in testo or 'ORDFO' in testo or bool(re.search(r'Ns\.\s*doc\.\(ORDFO\)', testo)))
    if e_vendita and not e_acquisto:
        dati['tipo_ddt'] = 'vendita'
        return dati

    m_ddt = re.search(r'(\d+)\s*/\s*DDT', testo)
    if m_ddt:
        dati['numero_ddt'] = m_ddt.group(1)
    m_data = re.search(r'(\d{2}/\d{2}/\d{4})', testo)
    if m_data:
        dati['data_ddt'] = m_data.group(1)
    if "Destinatario:" in testo:
        parte = testo.split("Destinatario:")[1]
        ll = [l.strip() for l in parte.split('\n') if l.strip()]
        if len(ll) >= 2:
            dati['fornitore'] = ll[1] if ll[0].isdigit() else ll[0]

    RE_ORDFO = re.compile(r'Ns\.\s*doc\.\(ORDFO\)\s*n\.:\s*(\d+)')
    RE_ART = re.compile(r'([A-Za-z0-9][A-Za-z0-9./_-]+)\s+(.*?)\s+([a-z]{1,3})\s*\.\s+([\d\.]+)(?:,\d+)?')
    sezione_corrente = None
    for riga in linee:
        m_ordfo = RE_ORDFO.search(riga)
        if m_ordfo:
            if sezione_corrente is not None:
                dati['sezioni'].append(sezione_corrente)
            sezione_corrente = {'ordine_n': m_ordfo.group(1), 'articoli': []}
            continue
        m_art = RE_ART.search(riga)
        if m_art and m_art.group(1) not in SKIP_CODICI_PDF:
            art_entry = {
                "codice": m_art.group(1), "descrizione": m_art.group(2).strip(),
                "quantita": m_art.group(4).replace('.', '').split(',')[0],
            }
            if sezione_corrente is not None:
                sezione_corrente['articoli'].append(art_entry)
            else:
                dati.setdefault('articoli_senza_sezione', []).append(art_entry)
    if sezione_corrente is not None:
        dati['sezioni'].append(sezione_corrente)
    return dati


def _ddt_dict(d):
    return {
        'id': d.id, 'filename': d.filename, 'fornitore': d.fornitore, 'numero_ddt': d.numero_ddt,
        'data_ddt': d.data_ddt, 'caricato_il': d.caricato_il.isoformat() if d.caricato_il else None,
        'righe': [{
            'id': r.id, 'codice': r.codice, 'descrizione': r.descrizione, 'quantita': r.quantita,
            'ordine_n_riferimento': r.ordine_n_riferimento, 'abbinata': r.abbinata,
            'ordine_acquisto_id': r.ordine_acquisto_id,
        } for r in d.righe],
    }


@acquisti_wood_bp.route('/api/ddt_carico_wood', methods=['GET'])
def api_lista_ddt_carico():
    ddts = DDTCaricoWood.query.order_by(DDTCaricoWood.caricato_il.desc()).all()
    return jsonify([_ddt_dict(d) for d in ddts])


@acquisti_wood_bp.route('/api/ddt_carico_wood/upload', methods=['POST'])
def api_upload_ddt_carico():
    file = request.files.get('file_pdf')
    if not file or not file.filename.lower().endswith('.pdf'):
        return jsonify({'errore': True, 'messaggio': 'Carica un file PDF valido.'}), 400
    if DDTCaricoWood.query.filter_by(filename=file.filename).first():
        return jsonify({'errore': True, 'messaggio': f'Un DDT con il file "{file.filename}" è già stato caricato.'}), 409

    pdf_bytes = file.read()
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        testo = "\n".join(page.extract_text() or '' for page in reader.pages)
    except Exception as e:
        return jsonify({'errore': True, 'messaggio': f'Impossibile leggere il PDF: {e}'}), 400

    dati = _estrai_dati_ddt_carico(testo)
    if dati.get('tipo_ddt') == 'vendita':
        return jsonify({'errore': True, 'messaggio': 'Questo PDF sembra un DDT di VENDITA (uscita merci), non di carico/acquisto.'}), 400

    ddt = DDTCaricoWood(filename=file.filename, pdf_bytes=pdf_bytes, fornitore=dati['fornitore'],
                        numero_ddt=dati['numero_ddt'], data_ddt=dati['data_ddt'], testo_grezzo_pdf=testo)
    db.session.add(ddt)
    db.session.flush()

    n_abbinate = 0
    n_totali = 0
    n_ordini_completati = 0
    tutte_le_righe = [(sez['ordine_n'], art) for sez in dati['sezioni'] for art in sez['articoli']]
    tutte_le_righe += [('', art) for art in dati.get('articoli_senza_sezione', [])]

    for ordine_n_rif, art in tutte_le_righe:
        n_totali += 1
        try:
            qta = float(art['quantita'])
        except (TypeError, ValueError):
            qta = 0
        codice_risolto = _risolvi_codice_fornitore(dati['fornitore'], art['codice'])
        riga_ddt = RigaDDTCaricoWood(ddt_id=ddt.id, ordine_n_riferimento=ordine_n_rif,
                                     codice=codice_risolto, descrizione=art['descrizione'], quantita=qta)

        oa = OrdineAcquistoWood.query.filter_by(ordine_n=ordine_n_rif).first() if ordine_n_rif else None
        if oa:
            riga_oa = RigaOrdineAcquistoWood.query.filter_by(ordine_id=oa.id, codice=codice_risolto).first()
            if riga_oa:
                riga_oa.qta_ricevuta = (riga_oa.qta_ricevuta or 0) + qta
                riga_ddt.ordine_acquisto_id = oa.id
                riga_ddt.abbinata = True
                n_abbinate += 1

        db.session.add(riga_ddt)

        # Carica la Giacenza Iron Wood indipendentemente dall'abbinamento: la
        # merce È arrivata fisicamente, va tracciata comunque. costo_unitario
        # non disponibile qui (gli Ordini di Acquisto Fase 1 non estraggono
        # ancora il prezzo dal PDF) — movimento registrato senza valorizzazione.
        if qta > 0 and codice_risolto:
            _registra_movimento_giacenza(codice_risolto, qta, 'carico_acquisto',
                                          riferimento=ddt.filename,
                                          note=f'DDT {dati["numero_ddt"] or ddt.filename}' +
                                               (f' — rif. OA {ordine_n_rif}' if ordine_n_rif else ''))

    # Se tutte le righe di un OA abbinato sono ora coperte, segna l'ordine come ricevuto
    ordini_toccati = {r.ordine_acquisto_id for r in ddt.righe if r.ordine_acquisto_id}
    for oa_id in ordini_toccati:
        oa = OrdineAcquistoWood.query.get(oa_id)
        if oa and all((r.qta_ricevuta or 0) >= (r.qta_originale or 0) for r in oa.righe if r.qta_originale):
            oa.stato_label = 'ORDINE_RICEVUTO'
            n_ordini_completati += 1

    db.session.commit()
    return jsonify({'ok': True, 'ddt': _ddt_dict(ddt), 'n_righe_totali': n_totali,
                    'n_righe_abbinate': n_abbinate, 'n_ordini_completati': n_ordini_completati})


# ══════════════════════════════════════════════════════════════════════════════
#  MAPPA CODICE FORNITORE → CODICE INTERNO — risolve la codifica propria di
#  ogni fornitore verso lo SKU interno IronProduction, usata da entrambi i
#  parsing sopra (Ordine di Acquisto e DDT di carico). Senza una riga qui per
#  una data coppia (fornitore, codice), si continua a usare il codice grezzo
#  del PDF, come prima — nessuna mappa è mai obbligatoria.
# ══════════════════════════════════════════════════════════════════════════════

@acquisti_wood_bp.route('/api/mappa_codici_fornitore_wood', methods=['GET'])
def api_lista_mappa_codici_fornitore():
    righe = MappaCodiceFornitoreWood.query.order_by(MappaCodiceFornitoreWood.fornitore,
                                                     MappaCodiceFornitoreWood.codice_fornitore).all()
    return jsonify([{
        'id': r.id, 'fornitore': r.fornitore, 'codice_fornitore': r.codice_fornitore,
        'codice_interno': r.codice_interno, 'note': r.note or '',
    } for r in righe])


@acquisti_wood_bp.route('/api/mappa_codici_fornitore_wood', methods=['POST'])
def api_add_mappa_codice_fornitore():
    d = request.get_json(force=True)
    fornitore = (d.get('fornitore') or '').strip()
    codice_fornitore = (d.get('codice_fornitore') or '').strip()
    codice_interno = (d.get('codice_interno') or '').strip()
    if not fornitore or not codice_fornitore or not codice_interno:
        return jsonify({'errore': True, 'messaggio': 'Fornitore, codice fornitore e codice interno sono obbligatori.'}), 400

    esistente = MappaCodiceFornitoreWood.query.filter_by(fornitore=fornitore, codice_fornitore=codice_fornitore).first()
    if esistente:
        esistente.codice_interno = codice_interno
        esistente.note = (d.get('note') or '').strip()
    else:
        db.session.add(MappaCodiceFornitoreWood(
            fornitore=fornitore, codice_fornitore=codice_fornitore,
            codice_interno=codice_interno, note=(d.get('note') or '').strip(),
        ))
    db.session.commit()
    return jsonify({'ok': True})


@acquisti_wood_bp.route('/api/mappa_codici_fornitore_wood/<int:mid>', methods=['DELETE'])
def api_del_mappa_codice_fornitore(mid):
    riga = MappaCodiceFornitoreWood.query.get_or_404(mid)
    db.session.delete(riga)
    db.session.commit()
    return jsonify({'ok': True})


@acquisti_wood_bp.route('/ddt_carico_wood/<int:did>/pdf')
def api_scarica_pdf_ddt(did):
    d = DDTCaricoWood.query.get_or_404(did)
    if not d.pdf_bytes:
        return jsonify({'errore': True, 'messaggio': 'PDF non disponibile per questo DDT.'}), 404
    return Response(d.pdf_bytes, mimetype='application/pdf',
                     headers={'Content-Disposition': f'inline; filename="{d.filename}"'})


@acquisti_wood_bp.route('/acquisti-da-fabbisogno')
def pagina_acquisti_da_fabbisogno():
    return render_template('acquisti_wood/acquisti_da_fabbisogno.html', active='acquisti_wood')


@acquisti_wood_bp.route('/api/fabbisogno_acquisti_globale')
def api_fabbisogno_acquisti_globale():
    """
    Fabbisogno per Materia Prima e Componente d'Acquisto — riusa la STESSA
    identica formula già usata in Magazzino/Materiali
    (_calcola_campi_giacenza: Fabbisogno = MAX(Scorta Minima + Ordinato
    Cliente WMS − Disponibile Contabile Allargata, 0)), che incorpora GIA'
    sia il fabbisogno generato dagli Ordini di Produzione aperti (via
    Impegnato, dentro Disponibile Allargata) SIA la scorta minima di
    sicurezza — comprese le quantità già ordinate ai fornitori (dentro
    Disponibile Allargata anche quelle).

    BUG REALE CORRETTO: prima questa pagina calcolava il fabbisogno SOLO
    esplodendo la distinta base degli Ordini di Produzione aperti —
    ignorando completamente la scorta minima. Alzare la scorta minima di
    un codice (es. T1515, 2389 metri) senza nessun OP aperto che lo
    richiedesse non lo faceva MAI comparire qui, anche se in Magazzino/
    Materiali il Fabbisogno lo mostrava già correttamente — due calcoli
    diversi per lo stesso identico concetto, che potevano disallinearsi.
    """
    codici_rilevanti = {a.codice: a.tipo_approvvigionamento for a in ArticoloApprovvigionamento.query
                         .filter(ArticoloApprovvigionamento.tipo_approvvigionamento.in_(
                             ('MATERIA_PRIMA_FORNITORE', 'COMPONENTE_ACQUISTO'))).all()}
    if not codici_rilevanti:
        return jsonify([])
    presenti = {g.codice: g for g in GiacenzaWood.query.filter(GiacenzaWood.codice.in_(codici_rilevanti)).all()}
    righe_giacenza = [presenti.get(cod) or GiacenzaWood(codice=cod, quantita=0) for cod in codici_rilevanti]
    calcolate = _calcola_campi_giacenza(righe_giacenza)

    righe = []
    for r in calcolate:
        if r['fabbisogno'] <= 0:
            continue
        cod = r['codice']
        ultima_riga_oa = (RigaOrdineAcquistoWood.query.filter_by(codice=cod)
                          .join(OrdineAcquistoWood).order_by(OrdineAcquistoWood.caricato_il.desc()).first())
        righe_oa_aperte = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                          .filter(RigaOrdineAcquistoWood.codice == cod,
                                  OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
        gia_ordinato = sum(max((ro.qta_originale or 0) - (ro.qta_ricevuta or 0), 0) for ro in righe_oa_aperte)

        righe.append({
            'codice': cod, 'tipo': codici_rilevanti[cod],
            'descrizione': r['descrizione'] or (ultima_riga_oa.descrizione if ultima_riga_oa else ''),
            'unita_misura': r['unita_misura'] or (ultima_riga_oa.unita_misura if ultima_riga_oa else ''),
            'ultimo_fornitore': ultima_riga_oa.ordine.fornitore if ultima_riga_oa else '',
            'ultimo_prezzo': ultima_riga_oa.prezzo_unitario if ultima_riga_oa else None,
            # 'mancante' qui è già il fabbisogno UNIFICATO (produzione +
            # scorta minima, quanto già ordinato è già netto dentro
            # Disponibile Allargata) — 'gia_ordinato' resta mostrato SOLO
            # come informazione, NON va più sottratto una seconda volta
            # da 'da_ordinare_suggerito' (altrimenti si toglierebbe due
            # volte la stessa quantità).
            'mancante': round(r['fabbisogno'], 3),
            'gia_ordinato': round(gia_ordinato, 3),
            'da_ordinare_suggerito': round(r['fabbisogno'], 3),
        })
    righe.sort(key=lambda x: (-x['da_ordinare_suggerito'], x['codice']))
    return jsonify(righe)


def _fabbisogno_acquisti_semplice(tipo_approvvigionamento):
    """
    Fabbisogno per materiali NON legati alla Distinta Base di NESSUN
    prodotto — Materiale di Consumo (filo/gas di saldatura, ricambi
    torce e molette, reggetta per imballare) e Materiale di Sicurezza
    (scarpe antinfortunistiche, tappi, ecc.). A differenza di Materia
    Prima/Componente Acquisto/Laserato (dove il fabbisogno nasce
    dall'esplosione degli Ordini di Produzione aperti — vedi
    api_fabbisogno_acquisti_globale), questi materiali non compaiono MAI
    in nessuna distinta base: si consumano genericamente dal processo
    (es. il filo di saldatura non è "componente" di nessun Cavalletto),
    quindi il fabbisogno si calcola con la classica soglia di riordino,
    indipendente da quali OP sono aperti in questo momento:
      Fabbisogno = MAX(Scorta Minima locale − Giacenza attuale, 0)
    Mostra solo i codici con una scorta minima configurata (> 0): senza
    soglia non si può decidere se manca qualcosa.
    """
    articoli = ArticoloApprovvigionamento.query.filter_by(tipo_approvvigionamento=tipo_approvvigionamento).all()
    scorte_minime = {s.codice: s.scorta_minima for s in ScortaMinimaWood.query.all()}
    giacenze = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
    righe = []
    for a in articoli:
        scorta_min = scorte_minime.get(a.codice) or 0
        if scorta_min <= 0:
            continue
        giacenza = giacenze.get(a.codice) or 0
        mancante = max(scorta_min - giacenza, 0)
        if mancante <= 0:
            continue

        ultima_riga_oa = (RigaOrdineAcquistoWood.query.filter_by(codice=a.codice)
                          .join(OrdineAcquistoWood).order_by(OrdineAcquistoWood.caricato_il.desc()).first())
        righe_oa_aperte = (RigaOrdineAcquistoWood.query.join(OrdineAcquistoWood)
                          .filter(RigaOrdineAcquistoWood.codice == a.codice,
                                  OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all())
        gia_ordinato = sum(max((ro.qta_originale or 0) - (ro.qta_ricevuta or 0), 0) for ro in righe_oa_aperte)

        righe.append({
            'codice': a.codice,
            'descrizione': ultima_riga_oa.descrizione if ultima_riga_oa else '',
            'unita_misura': a.unita_misura or (ultima_riga_oa.unita_misura if ultima_riga_oa else ''),
            'ultimo_fornitore': ultima_riga_oa.ordine.fornitore if ultima_riga_oa else '',
            'ultimo_prezzo': ultima_riga_oa.prezzo_unitario if ultima_riga_oa else None,
            'giacenza': round(giacenza, 3),
            'scorta_minima': round(scorta_min, 3),
            'mancante': round(mancante, 3),
            'gia_ordinato': round(gia_ordinato, 3),
            'da_ordinare_suggerito': round(max(mancante - gia_ordinato, 0), 3),
        })
    righe.sort(key=lambda x: (-x['da_ordinare_suggerito'], x['codice']))
    return righe


@acquisti_wood_bp.route('/acquisti-consumabili')
def pagina_acquisti_consumabili():
    return render_template('acquisti_wood/acquisti_consumabili.html', active='acquisti_wood',
                            titolo='🧰 Acquisti — Materiale di Consumo',
                            sottotitolo='Filo/gas di saldatura, ricambi torce e molette, reggetta per imballare — soglia di riordino indipendente dagli Ordini di Produzione aperti.',
                            api_url='/api/fabbisogno_consumabili')


@acquisti_wood_bp.route('/api/fabbisogno_consumabili')
def api_fabbisogno_consumabili():
    return jsonify(_fabbisogno_acquisti_semplice('MATERIALE_CONSUMO'))


@acquisti_wood_bp.route('/acquisti-sicurezza')
def pagina_acquisti_sicurezza():
    return render_template('acquisti_wood/acquisti_consumabili.html', active='acquisti_wood',
                            titolo='🦺 Acquisti — Materiale di Sicurezza',
                            sottotitolo='Scarpe antinfortunistiche, tappi per orecchie, dispositivi di protezione — soglia di riordino indipendente dagli Ordini di Produzione aperti.',
                            api_url='/api/fabbisogno_sicurezza')


@acquisti_wood_bp.route('/api/fabbisogno_sicurezza')
def api_fabbisogno_sicurezza():
    return jsonify(_fabbisogno_acquisti_semplice('MATERIALE_SICUREZZA'))


@acquisti_wood_bp.route('/api/ordini_acquisto_wood/manuale', methods=['POST'])
def api_crea_ordine_acquisto_manuale():
    d = request.get_json(force=True)
    fornitore = (d.get('fornitore') or '').strip()
    righe = d.get('righe') or []
    if not fornitore:
        return jsonify({'errore': True, 'messaggio': 'Indica il fornitore.'}), 400
    if not righe:
        return jsonify({'errore': True, 'messaggio': 'Aggiungi almeno una riga.'}), 400

    filename = f"MANUALE-{fornitore}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    o = OrdineAcquistoWood(filename=filename, fornitore=fornitore, ordine_n='N/D', stato_label='DA_CONFERMARE')
    db.session.add(o)
    db.session.flush()
    for r in righe:
        codice = (r.get('codice') or '').strip()
        if not codice:
            continue
        try:
            qta = float(r.get('qta') or 0)
        except (TypeError, ValueError):
            qta = 0
        if qta <= 0:
            continue
        try:
            prezzo = float(r['prezzo_unitario']) if r.get('prezzo_unitario') not in (None, '') else None
        except (TypeError, ValueError):
            prezzo = None
        db.session.add(RigaOrdineAcquistoWood(
            ordine_id=o.id, codice=codice, descrizione=(r.get('descrizione') or '').strip(),
            unita_misura=(r.get('unita_misura') or '').strip(), qta_originale=qta, prezzo_unitario=prezzo,
        ))
    db.session.commit()
    return jsonify({'ok': True, 'id': o.id})
