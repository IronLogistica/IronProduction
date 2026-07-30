import io
import re
from datetime import datetime, date

import PyPDF2
from flask import Blueprint, render_template, jsonify, request, Response

from models import db, OrdineAcquistoWood, RigaOrdineAcquistoWood

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

    # 3. Articoli — cattura codice, descrizione, quantità e data evasione,
    # parsando solo le righe DOPO l'intestazione colonne (banca/pagamento/
    # fornitore vengono ignorati).
    HEADER_COLONNE = ('codice merce', 'descrizione della merce', 'codice articolo')
    idx_header = None
    for i, linea in enumerate(linee):
        if any(h in linea.lower() for h in HEADER_COLONNE):
            idx_header = i
            break
    linee_articoli = linee[idx_header + 1:] if idx_header is not None else linee
    _pending_codice = None
    _pending_desc = None
    for linea in linee_articoli:
        if _pending_codice:
            m_qty = re.search(r'^([a-z]{1,3})\.\s+([\d\.]+)(?:,\d+)?(?:\s+(\d{2}/\d{2}/\d{4}))?', linea)
            if m_qty:
                dati['articoli'].append({
                    "codice": _pending_codice, "descrizione": _pending_desc,
                    "unita_misura": m_qty.group(1), "qta": m_qty.group(2).replace('.', ''),
                    "data_evasione": m_qty.group(3) or '',
                })
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
            r'([A-Za-z0-9][A-Za-z0-9./_-]+)\s+(.+?)\s+([a-z]{1,3})\.\s+([\d\.]+)(?:,\d+)?(?:\s+(\d{2}/\d{2}/\d{4}))?',
            linea)
        if not match_art:
            match_art = re.search(
                r'^(\d{2,})\s+(.+?)\s+([a-z]{1,3})\.\s+([\d\.]+)(?:,\d+)?(?:\s+(\d{2}/\d{2}/\d{4}))?', linea)
        if not match_art:
            m_split = re.match(r'^([A-Za-z0-9]{2,})\s+([A-Z].{3,})$', linea)
            if m_split and m_split.group(1) not in SKIP_CODICI_PDF:
                _pending_codice = m_split.group(1)
                _pending_desc = m_split.group(2).strip()
                continue
        if match_art and match_art.group(1) not in SKIP_CODICI_PDF:
            dati['articoli'].append({
                "codice": match_art.group(1), "descrizione": match_art.group(2).strip(),
                "unita_misura": match_art.group(3), "qta": match_art.group(4).replace('.', ''),
                "data_evasione": match_art.group(5) or '',
            })

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
        } for r in o.righe],
    }


@acquisti_wood_bp.route('/ordini-acquisto-wood')
def pagina_ordini_acquisto():
    return render_template('acquisti_wood/index.html', active='acquisti_wood')


@acquisti_wood_bp.route('/api/ordini_acquisto_wood', methods=['GET'])
def api_lista_ordini_acquisto():
    ordini = OrdineAcquistoWood.query.order_by(OrdineAcquistoWood.caricato_il.desc()).all()
    return jsonify([_ordine_dict(o) for o in ordini])


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
        db.session.add(RigaOrdineAcquistoWood(
            ordine_id=o.id, codice=art['codice'], descrizione=art['descrizione'],
            unita_misura=art['unita_misura'], qta_originale=qta, data_evasione=art['data_evasione'],
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
    except (TypeError, ValueError):
        return jsonify({'errore': True, 'messaggio': 'Quantità non valida'}), 400
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
