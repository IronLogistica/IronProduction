from flask import Blueprint, render_template, jsonify, request, current_app
from models import (
    db, Terzista, LavorazioneTerzista, RigaCommessa, FaseRiga, log,
    SchedaTrattamento, TIPI_TRATTAMENTO_SCHEDA, FORNITORI_SCHEDA_DEFAULT,
    KanbanProdotto, storico_aggiungi_auto, CentroCostoWood, OrdineProduzione,
    RettificaGrezzoIW,
)
from masterlogistic_client import carica_produzione, sku_da_nome_prodotto, MasterLogisticError
from blueprints.magazzino.routes import _registra_movimento_giacenza, _grezzo_iw_per_codici
from datetime import datetime, date
import os, re, json, shutil
import PyPDF2

terzisti_bp = Blueprint('terzisti', __name__)


def _aggiorna_kanban_da_rientro_ddt(codice, qta_rientrata_ora):
    """
    Aggancio automatico: un rientro DDT confermato per un codice che ha una
    scheda Kanban aumenta 'Finiti IW' (verniciati) della stessa quantità —
    SOLO in locale (Kanban + Magazzino/GiacenzaWood). Il rientro da
    terzista NON deve mai toccare MasterLogistic-WMS: il pezzo è appena
    tornato verniciato, ma resta di proprietà di Iron Wood — non è stato
    venduto a Iron Segnaletica. 'Finiti IS' legge lo stock WMS in diretta,
    quindi deve muoversi SOLO quando Iron Segnaletica registra il proprio
    DDT di acquisto (vedi /api/wms/scarica_finiti_iw, chiamato da
    MasterLogistic-WMS in quel momento, non da qui).
    BUG REALE CORRETTO: prima questa funzione notificava ANCHE WMS
    (carica_produzione), gonfiando Finiti IS per un rientro che non è
    affatto una vendita — un pezzo risultava così sia "Finiti IW" locale
    sia "Finiti IS" su WMS, doppiamente presente.
    Non bloccante: se il codice non ha una scheda Kanban il DDT resta
    comunque confermato — solo loggato.
    """
    if not codice or qta_rientrata_ora <= 0:
        return
    p = (KanbanProdotto.query
         .filter(db.func.upper(KanbanProdotto.prodotto).like(f'{codice.strip().upper()}%'))
         .first())
    if not p:
        return
    p.verniciati = (p.verniciati or 0) + qta_rientrata_ora
    ora_now = datetime.utcnow()
    if ora_now >= datetime(2026, 7, 1):
        storico_aggiungi_auto(p.id, qta_rientrata_ora)
    sku = sku_da_nome_prodotto(p.prodotto)
    # Magazzino LOCALE di IronProduction (GiacenzaWood/Materiali) — senza
    # questo, il rientro si vedeva su Kanban ("Finiti IW" saliva) ma NON
    # sulla pagina Materiali, perché quella legge solo GiacenzaWood, mai
    # il campo Kanban p.verniciati. Le due cose vanno tenute allineate.
    _registra_movimento_giacenza(sku, qta_rientrata_ora, 'carico_produzione',
                                  riferimento=codice, note=f'Rientro DDT terzista — {qta_rientrata_ora} pz')
    log(f'Kanban: +{qta_rientrata_ora} "Finiti IW" su {p.prodotto} da rientro DDT terzista (locale, MAI notificato a WMS)')


def _aggiorna_lead_time_esterno_da_rientro(trattamento, data_uscita, data_rientro):
    """
    Auto-apprendimento del lead time fornitore esterno: quando un DDT di
    rientro chiude COMPLETAMENTE una lavorazione (vedi conferma_ddt_rientro),
    calcola i giorni reali trascorsi tra spedizione e rientro e aggiorna
    CentroCostoWood.lead_time_esterno_giorni — con una media cumulativa
    (si stabilizza mano a mano, non scatta su un solo DDT) — per ogni centro
    di costo ESTERNO il cui nome corrisponde al tipo di trattamento
    (zincatura/verniciatura), individuato per parola chiave nel nome (stesso
    principio già usato altrove nell'app, es. 'sega' in nome_l).
    Salta i centri bloccati a mano (lead_time_esterno_manuale=True).
    Non bloccante: qualunque problema di parsing/match viene solo loggato,
    non deve mai far fallire la conferma del DDT rientro.
    """
    try:
        if not data_uscita or not data_rientro:
            return
        d_uscita = datetime.strptime(data_uscita, '%d/%m/%Y')
        d_rientro = datetime.strptime(data_rientro, '%d/%m/%Y')
        giorni = (d_rientro - d_uscita).days
        if giorni < 0:
            return  # data incoerente (es. OCR sbagliato) — meglio ignorare che sporcare la media

        t = (trattamento or '').lower()
        parole_chiave = []
        if 'vernic' in t:
            parole_chiave.append('vernic')
        if 'zinc' in t:
            parole_chiave.append('zinc')
        if not parole_chiave:
            return

        for c in CentroCostoWood.query.filter_by(esterno=True).all():
            if c.lead_time_esterno_manuale:
                continue
            if any(k in (c.nome or '').lower() for k in parole_chiave):
                n = c.lead_time_esterno_n_osservazioni or 0
                attuale = c.lead_time_esterno_giorni or 0
                c.lead_time_esterno_giorni = round(((attuale * n) + giorni) / (n + 1), 2)
                c.lead_time_esterno_n_osservazioni = n + 1
                log(f'Lead time esterno "{c.nome}" aggiornato automaticamente: '
                    f'{giorni}gg osservati (media ora {c.lead_time_esterno_giorni}gg su {n + 1} osservazioni)')
    except Exception as e:
        log(f'WARN: auto-apprendimento lead time esterno fallito: {e}')


# ══════════════════════════════════════════════════════════════════════════════
#  CARTELLE — aggiungere in app.config:
#    TERZISTI_USCITA_FOLDER  = 'terzisti_uscita'
#    TERZISTI_RIENTRO_FOLDER = 'terzisti_rientro'
#    TERZISTI_DONE_FOLDER    = 'terzisti_done'
# ══════════════════════════════════════════════════════════════════════════════

def _folder(key, fallback):
    try:    return current_app.config.get(key, fallback)
    except: return fallback

USCITA_FOLDER  = lambda: _folder('TERZISTI_USCITA_FOLDER',  'terzisti_uscita')
RIENTRO_FOLDER = lambda: _folder('TERZISTI_RIENTRO_FOLDER', 'terzisti_rientro')
DONE_FOLDER    = lambda: _folder('TERZISTI_DONE_FOLDER',    'terzisti_done')

# Codici da ignorare nel parsing (intestazioni, magazzini, causali)
SKIP_TZ = {
    'AU','TRA','IT','DDT','Mag','TGT','Pag','n','nr','N','Nr',
    'Vs','doc','DDTCL','ZINCATA','ZINCATURA','VERNICIATURA',
    'SABBIATURA','CROMATURA','FOSFATAZIONE','NICHELATURA',
}

TRATTAMENTI = [
    'ZINCATURA','VERNICIATURA','SABBIATURA','CROMATURA',
    'FOSFATAZIONE','NICHELATURA','TRATTAMENTO TERMICO',
]


# ══════════════════════════════════════════════════════════════════════════════
#  PARSER PDF — calibrato sui DDT reali Ironwood / T.G.T. Srl
# ══════════════════════════════════════════════════════════════════════════════

def _leggi_pdf(path):
    try:
        with open(path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            return ''.join(p.extract_text() or '' for p in reader.pages)
    except Exception as e:
        print(f'[TERZISTI] Errore lettura PDF {path}: {e}')
        return ''


def _rileva_tipo(testo):
    """
    DDT USCITA  → 'Lavorazione Esterna'
                  oppure 'DOCUMENTO DI TRASPORTO' + 'Mag. di destinazione: TGT'
    DDT RIENTRO → 'Entrata Merci'
                  oppure 'Vs. doc.(DDTCL)'
    """
    e_rientro = (
        'Entrata Merci' in testo or
        bool(re.search(r'Vs\.?\s*doc\.?\s*\(DDTCL\)', testo))
    )
    # BUG FIX: parentesi esplicite — in Python 'and' ha precedenza su 'or'
    e_uscita = (
        'Lavorazione Esterna' in testo or
        (
            'DOCUMENTO DI TRASPORTO' in testo and
            bool(re.search(r'Mag\.?\s*di\s+destinazione:\s*TGT', testo))
        )
    )
    if e_rientro and not e_uscita:  return 'rientro'
    if e_uscita  and not e_rientro: return 'uscita'
    if e_rientro: return 'rientro'   # priorità rientro in caso di ambiguità
    return 'sconosciuto'


def _estrai_numero_ddt(testo):
    m = re.search(r'Numero documento\s+(\d+)', testo)
    return m.group(1) if m else ''


def _estrai_data(testo):
    m = re.search(r'Data documento\s+(\d{2}/\d{2}/\d{4})', testo)
    return m.group(1) if m else ''


def _estrai_terzista(testo):
    """Estrae nome terzista dal blocco Destinatario."""
    m = re.search(r'Destinatario:\s*\n\d+\n(.+?)\n', testo)
    if m: return m.group(1).strip()
    # Fallback: dopo codice fornitore 6 cifre
    m = re.search(r'\d{6}\n(.+?)\n', testo)
    return m.group(1).strip() if m else ''


def _estrai_trattamento(testo):
    testo_up = testo.upper()
    for t in TRATTAMENTI:
        if t in testo_up:
            return t
    return ''


def _parse_qta(s):
    """
    Converte quantità in notazione italiana in int.

    BUG FIX: il replace order sbagliato ('.','').(',','.') dava 80000 per '80,000'.
    Soluzione: se c'è la virgola (separatore decimale), prende solo la parte intera.
      '80,000'  → 80   (80 virgola 000 → int part = 80)
      '682,000' → 682
      '11,000'  → 11
      '1.500'   → 1500 (punto = migliaia, nessuna virgola)
      '130'     → 130  (intero semplice)
    """
    s = s.strip()
    if ',' in s:
        # La virgola è separatore decimale (notazione italiana)
        # Le quantità sono sempre interi → prendiamo solo la parte intera
        int_part = s.split(',')[0].replace('.', '')
        return int(int_part) if int_part.isdigit() else 0
    else:
        # Nessuna virgola: il punto è separatore migliaia (o numero intero puro)
        clean = s.replace('.', '')
        return int(clean) if clean.isdigit() else 0


def _preprocess_righe(testo):
    """
    Pre-processa il testo per correggere le righe spezzate da PyPDF2.

    Caso tipico (riga desc spezzata):
      'T200DT TRANSENNA L.2.000 D.32 DOPPIO\nTRAVERSOn. 11,000'
      → 'T200DT TRANSENNA L.2.000 D.32 DOPPIO TRAVERSO n. 11,000'

    BUG FIX #1 — Importi senza spazio (es. 'Base Curvata527,84'):
      PyPDF2 incolla l'importo (2 decimali = centesimi) alla riga descrizione.
      Viene rimosso PRIMA del join → 'Base Curvata527,84' → 'Base Curvata'.
      Le quantità (N,000 → 3 decimali) NON vengono toccate.

    BUG FIX #2 — Condizione di join troppo larga:
      Non unire una riga che ha GIÀ 'UM QTA' (è già completa) con la prossima.
    """
    # ── 1. Spazio prima di UM incollata a parola: 'TRAVERSOn.' → 'TRAVERSO n.'
    testo = re.sub(r'([A-Za-z])(?=(?:n|pz|Nr)\.)', r'\1 ', testo)

    # ── 2. Rimuove 'Mag. di origine/destinazione: ...' incollati sulla riga
    testo = re.sub(r'\s*Mag\.?\s*di\s+(?:origine|destinazione):[^\n]+', '', testo)

    # ── 3. BUG FIX #1: rimuove importi finali (esattamente 2 cifre decimali
    #       dopo virgola = centesimi euro).  Le quantità hanno 3 zeri → sicuro.
    #       Gestisce sia ' 495,60' che 'Curvata527,84' (zero spazi prima).
    testo = re.sub(r'(?<=\D)\d{1,7},\d{2}(?=\n|$)', '', testo, flags=re.MULTILINE)
    # Riga che inizia direttamente con un importo (riga isolata di solo importo)
    testo = re.sub(r'^\d{1,7},\d{2}\s*$', '', testo, flags=re.MULTILINE)

    righe = testo.split('\n')
    risultato = []
    i = 0
    while i < len(righe):
        r = righe[i].rstrip()
        if i + 1 < len(righe):
            prossima = righe[i + 1].strip()
            # BUG FIX #2: unisce SOLO se la riga corrente NON ha già 'UM QTA'
            # 'ZT ... n. 682,000' è già completa → non unire con 'Base Curvata'
            riga_ha_qta = bool(re.search(
                r'\s+(?:n|pz|Nr)\.\s*[\d\.,]+', r.strip()
            ))
            # La riga successiva è una continuazione: inizia con TOKEN TUTTO-CAPS + UM + QTA
            prossima_e_continuazione = bool(re.match(
                r'^[A-Z][A-Z0-9]*\s+(?:n|pz|Nr)\.\s*[\d\.,]+',
                prossima
            ))
            riga_e_articolo = bool(re.match(
                r'^[A-Z][A-Za-z0-9._-]+\s+.+', r.strip()
            ))
            if riga_e_articolo and not riga_ha_qta and prossima_e_continuazione:
                r = r.strip() + ' ' + prossima
                i += 2
                risultato.append(r)
                continue
        risultato.append(r)
        i += 1
    return '\n'.join(risultato)

def _estrai_articoli(testo):
    """
    Estrae lista articoli dal testo DDT.

    Pattern: CODICE  Descrizione…  UM  QTA[,000]  [Importo]
    Unità di misura supportate: n. / pz. / Nr. / PZ. (case-insensitive partial)

    BUG FIX: quantità 80,000 → 80 (non 80000). Vedi _parse_qta().
    """
    testo = _preprocess_righe(testo)
    articoli = []
    visti = set()

    # Pattern UM: n. / pz. / Nr. / PZ. / nr. — con o senza spazio prima del numero
    UM_PAT = r'(?:n|pz|Nr|PZ|nr)\.'

    for riga in testo.split('\n'):
        riga = riga.strip()
        if not riga:
            continue

        # Sicurezza: rimuove eventuali importi residui (2 decimali) a fine riga
        riga = re.sub(r'\s*\d{1,7},\d{2}\s*$', '', riga)

        m = re.match(
            r'^([A-Za-z][A-Za-z0-9._-]{1,})\s+(.+?)\s+' + UM_PAT + r'\s*([\d\.,]+)(?:\s+[\d\.,]+)?$',
            riga
        )
        if not m:
            continue

        codice = m.group(1)
        if codice in SKIP_TZ or codice.startswith('0') or len(codice) < 2:
            continue

        qta = _parse_qta(m.group(3))

        key = f'{codice}_{qta}'
        if key in visti:
            continue
        visti.add(key)

        articoli.append({
            'codice': codice,
            'desc':   m.group(2).strip(),
            'qta':    qta,
        })

    return articoli

def parse_ddt_terzista(path):
    """
    Parser principale. Restituisce:
    {
        tipo_ddt:    'uscita' | 'rientro' | 'sconosciuto',
        numero_ddt:  str,
        data_ddt:    str  (dd/mm/yyyy),
        terzista:    str,
        trattamento: str,
        articoli:    [ { codice, desc, qta } ]
    }
    """
    testo = _leggi_pdf(path)
    return {
        'tipo_ddt':    _rileva_tipo(testo),
        'numero_ddt':  _estrai_numero_ddt(testo),
        'data_ddt':    _estrai_data(testo),
        'terzista':    _estrai_terzista(testo),
        'trattamento': _estrai_trattamento(testo),
        'articoli':    _estrai_articoli(testo),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTE PAGINA
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/terzisti')
def index():
    return render_template('terzisti/index.html', active='terzisti')


@terzisti_bp.route('/terzisti/da-trattare')
def pagina_da_trattare():
    return render_template('terzisti/da_trattare.html', active='terzisti_da_trattare')


@terzisti_bp.route('/api/terzisti/materiali_da_trattare')
def api_materiali_da_trattare():
    """
    Materiali GREZZI pronti a magazzino da mandare in trattamento esterno
    (verniciatura/zincatura) — stessa fonte del 'Grezzo IW' già mostrato in
    Magazzino/Kanban (_grezzo_iw_per_codici: prodotto finito da Saldatura,
    ultima fase del ciclo, approvato dalla Direzione, più eventuali
    rettifiche manuali), qui filtrata alle sole quantità > 0 e presentata
    come pagina a parte da stampare. Per ogni codice: ultimo fornitore e
    ultimo trattamento eseguiti, presi dall'ultima Scheda Trattamento
    creata per quel codice (se mai spedito prima) — nessuno storico ancora
    se il codice non è mai stato trattato esternamente.
    """
    tutti_codici = [row[0] for row in db.session.query(OrdineProduzione.codice_articolo).distinct().all()]
    grezzo_per_codice = _grezzo_iw_per_codici(tutti_codici) if tutti_codici else {}
    codici_pronti = sorted(cod for cod, qta in grezzo_per_codice.items() if qta > 0)

    ultima_scheda_per_codice = {}
    if codici_pronti:
        for s in (SchedaTrattamento.query
                  .filter(SchedaTrattamento.codice_articolo.in_(codici_pronti))
                  .order_by(SchedaTrattamento.creato_il.desc()).all()):
            if s.codice_articolo not in ultima_scheda_per_codice:
                ultima_scheda_per_codice[s.codice_articolo] = s

    righe = []
    for cod in codici_pronti:
        s = ultima_scheda_per_codice.get(cod)
        righe.append({
            'codice': cod,
            'quantita': grezzo_per_codice[cod],
            'ultimo_fornitore': s.fornitore if s else None,
            'ultimo_trattamento': s.info_trattamento.get('label', s.tipo_trattamento) if s else None,
            'ultima_data': s.creato_il.strftime('%d/%m/%Y') if s and s.creato_il else None,
        })
    return jsonify(righe)


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDE TRATTAMENTI ESTERNI — cartellino da stampare con QR code
#  (conto vendita: arredo urbano / segnaletica stradale)
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/api/schede_trattamenti')
def api_schede_lista():
    schede = SchedaTrattamento.query.order_by(SchedaTrattamento.id.desc()).limit(80).all()
    return jsonify([{
        'id':                     s.id,
        'numero_scheda':          s.numero_scheda,
        'codice_articolo':        s.codice_articolo,
        'fornitore':              s.fornitore,
        'commessa':               s.commessa,
        'tipo_trattamento':       s.tipo_trattamento,
        'tipo_trattamento_label': s.info_trattamento.get('label', s.tipo_trattamento),
        'colore':                 s.colore,
        'creato_il':              s.creato_il.strftime('%d/%m/%Y %H:%M') if s.creato_il else '',
    } for s in schede])


@terzisti_bp.route('/api/schede_trattamenti', methods=['POST'])
def api_schede_crea():
    try:
        d          = request.get_json(force=True)
        codice     = (d.get('codice_articolo') or '').strip()
        fornitore  = (d.get('fornitore') or '').strip()
        zincatura  = bool(d.get('zincatura'))
        zinc_tipo  = (d.get('zincatura_tipo') or '').strip().upper()
        verniciatura = bool(d.get('verniciatura'))
        colore     = (d.get('colore') or '').strip()

        if not codice:
            return jsonify({'ok': False, 'error': 'Codice articolo obbligatorio'}), 400
        if not fornitore:
            return jsonify({'ok': False, 'error': 'Fornitore obbligatorio'}), 400
        if not zincatura and not verniciatura:
            return jsonify({'ok': False, 'error': 'Seleziona almeno un trattamento (Zincatura e/o Verniciatura)'}), 400
        if zincatura and zinc_tipo not in ('CALDO', 'FREDDO'):
            return jsonify({'ok': False, 'error': 'Specifica il tipo di zincatura (Caldo o Freddo)'}), 400

        # tipo_trattamento (colonna legacy) ricostruito per compatibilità/storico
        if zincatura and verniciatura:
            tipo_legacy = 'ZINCATURA_VERNICIATURA'
        elif zincatura:
            tipo_legacy = 'ZINCATURA_CALDO' if zinc_tipo == 'CALDO' else 'ZINCATURA_FREDDO'
        else:
            tipo_legacy = 'VERNICIATURA'

        s = SchedaTrattamento(
            codice_articolo = codice,
            fornitore       = fornitore,
            commessa        = (d.get('commessa') or '').strip(),
            tipo_trattamento= tipo_legacy,
            colore          = colore,
            zincatura       = zincatura,
            zincatura_tipo  = zinc_tipo if zincatura else '',
            verniciatura    = verniciatura,
        )
        db.session.add(s)
        db.session.commit()
        log(f'Scheda trattamento creata: {codice} — {fornitore} — {tipo_legacy}')
        return jsonify({'ok': True, 'id': s.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@terzisti_bp.route('/schede_trattamenti/<int:sid>/stampa')
def scheda_stampa(sid):
    s    = SchedaTrattamento.query.get_or_404(sid)
    info = s.info_trattamento

    qr_righe = [
        f"SCHEDA: {s.numero_scheda}",
        f"CODICE: {s.codice_articolo}",
        f"FORNITORE: {s.fornitore}",
        f"COMMESSA: {s.commessa or '-'}",
        f"TRATTAMENTO: {info.get('label', s.tipo_trattamento)}"
        + (f" {info.get('zinc_label')}" if info.get('zinc_label') else ""),
    ]
    if info.get('verniciatura') and s.colore:
        qr_righe.append(f"COLORE: {s.colore}")
    qr_text = "\n".join(qr_righe)

    return render_template(
        'terzisti/scheda_stampa.html',
        s=s, info=info, qr_text=qr_text,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  API ANAGRAFICA TERZISTI
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/api/terzisti')
def api_lista():
    return jsonify([{
        'id': t.id, 'nome': t.nome, 'email': t.email,
        'telefono': t.telefono, 'tipo': t.tipo,
    } for t in Terzista.query.order_by(Terzista.nome)])


@terzisti_bp.route('/api/terzisti', methods=['POST'])
def api_crea():
    try:
        d = request.get_json(force=True)
        t = Terzista(**{k: d.get(k, '') for k in ['nome', 'email', 'telefono', 'tipo', 'note']})
        db.session.add(t)
        db.session.commit()
        return jsonify({'ok': True, 'id': t.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@terzisti_bp.route('/api/terzisti/<int:tid>', methods=['DELETE'])
def api_elimina(tid):
    try:
        t = Terzista.query.get_or_404(tid)
        db.session.delete(t)
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  API LAVORAZIONI MANUALI (originali, invariate)
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/api/lavorazioni')
def api_lavorazioni():
    oggi = date.today()
    lav = LavorazioneTerzista.query.order_by(LavorazioneTerzista.data_rientro_prev).all()
    risultati = []
    for l in lav:
        stato = l.stato
        if stato == 'ATTESA_RIENTRO' and l.data_rientro_prev:
            try:
                if datetime.strptime(l.data_rientro_prev, '%d/%m/%Y').date() < oggi:
                    stato = 'IN_RITARDO'
            except Exception:
                pass
        riga     = RigaCommessa.query.get(l.riga_id)
        terzista = Terzista.query.get(l.terzista_id)
        risultati.append({
            'id': l.id,
            'commessa':          riga.commessa.numero if riga else '?',
            'codice':            riga.codice if riga else '?',
            'terzista':          terzista.nome if terzista else '?',
            'fase':              l.fase,
            'qta':               l.qta,
            'data_uscita':       l.data_uscita,
            'data_rientro_prev': l.data_rientro_prev,
            'data_rientro':      l.data_rientro,
            'stato':             stato,
            'costo':             l.costo,
            'ddt_uscita':        l.ddt_uscita,
            'ddt_rientro':       l.ddt_rientro,
            'note':              l.note,
        })
    return jsonify(risultati)


@terzisti_bp.route('/api/lavorazioni', methods=['POST'])
def api_crea_lavorazione():
    try:
        d = request.get_json(force=True)
        l = LavorazioneTerzista(
            riga_id=int(d['riga_id']), terzista_id=int(d['terzista_id']),
            fase=d.get('fase', ''), qta=int(d.get('qta', 0)),
            data_uscita=d.get('data_uscita', ''),
            data_rientro_prev=d.get('data_rientro_prev', ''),
            stato='ATTESA_RIENTRO', costo=float(d.get('costo', 0)),
            ddt_uscita=d.get('ddt_uscita', ''), note=d.get('note', ''),
        )
        db.session.add(l)
        fa = FaseRiga.query.filter_by(riga_id=l.riga_id, fase=l.fase).first()
        if fa:
            fa.stato = 'esternalizzata'
        log(f'Lavorazione terzista: riga {l.riga_id} → terzista {l.terzista_id}')
        db.session.commit()
        return jsonify({'ok': True, 'id': l.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


@terzisti_bp.route('/api/lavorazioni/<int:lid>/rientro', methods=['POST'])
def api_rientro(lid):
    try:
        l  = LavorazioneTerzista.query.get_or_404(lid)
        d  = request.get_json(force=True)
        l.data_rientro = d.get('data_rientro', datetime.utcnow().strftime('%d/%m/%Y'))
        l.ddt_rientro  = d.get('ddt_rientro', '')
        l.stato        = 'RIENTRATA'
        l.costo        = float(d.get('costo', l.costo))
        fa = FaseRiga.query.filter_by(riga_id=l.riga_id, fase=l.fase).first()
        if fa:
            fa.stato = d.get('stato_fase', 'completata')
        log(f'Rientro lavorazione terzista {lid}')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOAD DDT USCITA (spedizione al terzista)
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/api/upload_ddt_uscita', methods=['POST'])
def upload_ddt_uscita():
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'error': 'File PDF richiesto'}), 400

    folder = USCITA_FOLDER()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f.filename)
    f.save(path)

    dati = parse_ddt_terzista(path)

    # Guardia: blocca DDT rientro caricato per errore qui
    if dati['tipo_ddt'] == 'rientro':
        os.remove(path)
        return jsonify({
            'ok': False,
            'error': 'Questo è un DDT di RIENTRO (Entrata Merci). '
                     'Usare il pulsante "📥 Carica DDT Rientro".',
        }), 400

    return jsonify({'ok': True, 'anteprima': dati, 'filename': f.filename})


@terzisti_bp.route('/api/conferma_ddt_uscita', methods=['POST'])
def conferma_ddt_uscita():
    """
    Crea una LavorazioneTerzista per ogni articolo del DDT uscita.
    Tutti partono con stato ATTESA_RIENTRO e qta_rientrata=0.
    I rientri parziali incrementeranno qta_rientrata fino a qta (spedita).
    """
    try:
        d           = request.get_json(force=True)
        filename    = d.get('filename', '')
        terzista_n  = d.get('terzista', '')
        articoli    = d.get('articoli', [])
        numero_ddt  = d.get('numero_ddt', '')
        data_ddt    = d.get('data_ddt', '')
        trattamento = d.get('trattamento', '')
        rientro_prev= d.get('data_rientro_prev', '')

        # Cerca il terzista in anagrafica; se non esiste lo CREA AUTOMATICAMENTE
        # dalla regex (nome estratto dal PDF). Non serve inserirlo a priori.
        tz = Terzista.query.filter(Terzista.nome.ilike(f'%{terzista_n}%')).first()
        if not tz and terzista_n:
            tz = Terzista(
                nome=terzista_n,
                tipo=trattamento or '',
                email='', telefono='', note='Creato automaticamente da DDT'
            )
            db.session.add(tz)
            db.session.flush()   # ottiene l'id senza commit definitivo
            log(f'Terzista "{terzista_n}" creato automaticamente da DDT {numero_ddt}')
        tz_id = tz.id if tz else None   # BUG FIX: era 0 → FK crash su PostgreSQL

        for art in articoli:
            # note_json porta tutti i dati strutturati dell'articolo
            codice_art = art.get('codice', '')
            qta_art = int(art.get('qta', 0))
            note_j = json.dumps({
                'codice':      codice_art,
                'desc':        art.get('desc', ''),
                'trattamento': trattamento,
                'filename_pdf': filename,
                'qta_rientrata': 0,           # saldo rientri parziali
                'ddt_rientri':  [],           # lista DDT rientro collegati
            })
            lav = LavorazioneTerzista(
                riga_id           = None,   # BUG FIX: 0 violava FK su PostgreSQL
                terzista_id       = tz_id,  # None se terzista non estratto (nullable)
                fase              = trattamento or 'Trattamento Esterno',
                qta               = qta_art,
                data_uscita       = data_ddt,
                data_rientro_prev = rientro_prev,
                stato             = 'ATTESA_RIENTRO',
                costo             = 0.0,
                ddt_uscita        = numero_ddt,
                note              = note_j,
            )
            db.session.add(lav)
            db.session.flush()   # serve lav.id per tracciare la rettifica
            # Spedito = non più "in casa": scala il Grezzo IW pronto per lo
            # stesso importo, così Materiali da Trattare mostra solo quello
            # che è DAVVERO ancora a magazzino, non quello già partito per
            # il terzista. Stesso meccanismo (cumulativo, mai un valore che
            # sovrascrive) delle rettifiche manuali di Angelo — vedi
            # RettificaGrezzoIW e _grezzo_iw_per_codici.
            if codice_art and qta_art:
                db.session.add(RettificaGrezzoIW(
                    codice=codice_art, delta=-qta_art,
                    note=f'Spedito a terzista — DDT uscita {numero_ddt} (lavorazione #{lav.id})'
                ))

        # Archivia PDF
        done = DONE_FOLDER()
        os.makedirs(done, exist_ok=True)
        try:
            shutil.move(os.path.join(USCITA_FOLDER(), filename),
                        os.path.join(done, filename))
        except Exception:
            pass

        log(f'DDT USCITA terzista: {filename} — {len(articoli)} articoli — {terzista_n}')
        db.session.commit()
        return jsonify({'ok': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOAD DDT RIENTRO — gestisce rientri PARZIALI
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/api/upload_ddt_rientro', methods=['POST'])
def upload_ddt_rientro():
    f = request.files.get('file')
    if not f or not f.filename.lower().endswith('.pdf'):
        return jsonify({'ok': False, 'error': 'File PDF richiesto'}), 400

    folder = RIENTRO_FOLDER()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f.filename)
    f.save(path)

    dati = parse_ddt_terzista(path)

    # Guardia: blocca DDT uscita caricato per errore qui
    if dati['tipo_ddt'] == 'uscita':
        os.remove(path)
        return jsonify({
            'ok': False,
            'error': 'Questo è un DDT di USCITA (Documento di Trasporto / Lavorazione Esterna). '
                     'Usare il pulsante "📤 Carica DDT Uscita".',
        }), 400

    # Cerca lavorazioni aperte che matchano i codici articolo del DDT rientro.
    # Un DDT rientro può chiudere PARZIALMENTE più lavorazioni aperte.
    codici_rientro = {a['codice'] for a in dati['articoli']}
    terzista_nome  = dati.get('terzista', '')

    lav_aperte = LavorazioneTerzista.query.filter(
        LavorazioneTerzista.stato.in_(['ATTESA_RIENTRO', 'IN_RITARDO', 'PARZIALE'])
    ).all()

    match = []
    for lav in lav_aperte:
        try:
            note_j = json.loads(lav.note or '{}')
        except Exception:
            note_j = {}
        codice_lav = note_j.get('codice', '')
        tz = Terzista.query.get(lav.terzista_id)
        tz_nome = tz.nome if tz else ''

        if codice_lav not in codici_rientro:
            continue

        # Calcola saldo residuo
        qta_rientrata = int(note_j.get('qta_rientrata', 0))
        qta_residua   = lav.qta - qta_rientrata

        # Trova la qta di questo articolo nel DDT rientro
        qta_ddt = next((a['qta'] for a in dati['articoli'] if a['codice'] == codice_lav), 0)

        match.append({
            'lav_id':       lav.id,
            'codice':       codice_lav,
            'desc':         note_j.get('desc', ''),
            'terzista':     tz_nome,
            'ddt_uscita':   lav.ddt_uscita,
            'qta_spedita':  lav.qta,
            'qta_rientrata':qta_rientrata,
            'qta_residua':  qta_residua,
            'qta_ddt':      qta_ddt,       # quantità proposta dal DDT rientro
        })

    return jsonify({
        'ok': True,
        'anteprima': dati,
        'filename': f.filename,
        'match_lavorazioni': match,
    })


@terzisti_bp.route('/api/conferma_ddt_rientro', methods=['POST'])
def conferma_ddt_rientro():
    """
    Aggiorna il saldo rientri per ogni LavorazioneTerzista selezionata.

    Logica saldo parziale:
      note_json.qta_rientrata += qta_confermata_da_questo_ddt
      se qta_rientrata >= qta_spedita → stato = 'RIENTRATA'
      altrimenti                      → stato = 'PARZIALE'

    Il campo note_json.ddt_rientri accumula tutti i DDT collegati.
    """
    try:
        d          = request.get_json(force=True)
        filename   = d.get('filename', '')
        numero_ddt = d.get('numero_ddt', '')
        data_ddt   = d.get('data_ddt', '')
        conferme   = d.get('conferme', [])
        # conferme = [ { lav_id, qta_confermata } ]

        chiuse = 0
        parziali = 0

        for c in conferme:
            lav_id       = int(c.get('lav_id', 0))
            qta_conf     = int(c.get('qta_confermata', 0))
            if qta_conf <= 0:
                continue

            lav = LavorazioneTerzista.query.get(lav_id)
            if not lav:
                continue

            try:
                note_j = json.loads(lav.note or '{}')
            except Exception:
                note_j = {}

            # Aggiorna saldo
            qta_rientrata_prec = int(note_j.get('qta_rientrata', 0))
            nuova_rientrata    = qta_rientrata_prec + qta_conf
            ddt_list           = note_j.get('ddt_rientri', [])
            if numero_ddt and numero_ddt not in ddt_list:
                ddt_list.append(numero_ddt)

            note_j['qta_rientrata'] = nuova_rientrata
            note_j['ddt_rientri']   = ddt_list
            lav.note = json.dumps(note_j)

            # Aggancio automatico Kanban Gruppi: questo rientro alza "Finiti
            # IW" del codice corrispondente, se ha una scheda (vedi sopra).
            _aggiorna_kanban_da_rientro_ddt(note_j.get('codice', ''), qta_conf)

            # Aggiorna stato
            if nuova_rientrata >= lav.qta:
                lav.stato        = 'RIENTRATA'
                lav.data_rientro = data_ddt or datetime.utcnow().strftime('%d/%m/%Y')
                lav.ddt_rientro  = ', '.join(ddt_list)
                chiuse += 1
                fa = FaseRiga.query.filter_by(riga_id=lav.riga_id, fase=lav.fase).first()
                if fa:
                    fa.stato = 'completata'
                _aggiorna_lead_time_esterno_da_rientro(note_j.get('trattamento') or lav.fase,
                                                        lav.data_uscita, lav.data_rientro)
            else:
                lav.stato       = 'PARZIALE'
                lav.ddt_rientro = ', '.join(ddt_list)
                parziali += 1

        # Archivia PDF rientro
        done = DONE_FOLDER()
        os.makedirs(done, exist_ok=True)
        try:
            shutil.move(os.path.join(RIENTRO_FOLDER(), filename),
                        os.path.join(done, filename))
        except Exception:
            pass

        log(f'DDT RIENTRO terzista: {filename} — chiuse:{chiuse} parziali:{parziali}')
        db.session.commit()
        return jsonify({'ok': True, 'chiuse': chiuse, 'parziali': parziali})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  API DASHBOARD — spedizioni con saldo parziale
# ══════════════════════════════════════════════════════════════════════════════

@terzisti_bp.route('/api/spedizioni_terzisti')
def api_spedizioni_terzisti():
    """
    Restituisce tutte le lavorazioni con saldo aggiornato per la dashboard.
    Calcola dinamicamente stato e qta residua da note_json.
    """
    oggi = date.today()
    lavorazioni = LavorazioneTerzista.query.order_by(
        LavorazioneTerzista.data_uscita.desc()
    ).all()

    risultati = []
    for lav in lavorazioni:
        stato = lav.stato

        # Controlla ritardo su lavorazioni non ancora chiuse
        if stato in ('ATTESA_RIENTRO', 'PARZIALE') and lav.data_rientro_prev:
            try:
                if datetime.strptime(lav.data_rientro_prev, '%d/%m/%Y').date() < oggi:
                    stato = 'IN_RITARDO'
            except Exception:
                pass

        tz = Terzista.query.get(lav.terzista_id)

        try:
            note_j = json.loads(lav.note or '{}')
        except Exception:
            note_j = {}

        qta_rientrata = int(note_j.get('qta_rientrata', 0))
        qta_residua   = max(0, lav.qta - qta_rientrata)
        ddt_rientri   = note_j.get('ddt_rientri', [])

        risultati.append({
            'id':               lav.id,
            'terzista':         tz.nome if tz else '?',
            'terzista_tipo':    tz.tipo if tz else '',
            'codice':           note_j.get('codice', ''),
            'desc':             note_j.get('desc', ''),
            'trattamento':      note_j.get('trattamento', lav.fase),
            'qta_spedita':      lav.qta,
            'qta_rientrata':    qta_rientrata,
            'qta_residua':      qta_residua,
            'data_uscita':      lav.data_uscita,
            'data_rientro_prev':lav.data_rientro_prev,
            'data_rientro':     lav.data_rientro,
            'ddt_uscita':       lav.ddt_uscita,
            'ddt_rientri':      ddt_rientri,
            'stato':            stato,
        })
    return jsonify(risultati)


@terzisti_bp.route('/api/lavorazioni/<int:lid>/correggi_qta_spedita', methods=['POST'])
def api_correggi_qta_spedita(lid):
    """
    Corregge la quantità SPEDITA (uscita) di una lavorazione/DDT già
    confermato — es. si pensava di aver spedito 100 ma erano in realtà 95.
    Aggiusta insieme:
    - la riga stessa (LavorazioneTerzista.qta), da cui derivano
      dinamicamente qta_residua/stato/percentuale visti in tabella e nelle
      card KPI in alto — nessun altro campo da toccare a mano lì
    - il Grezzo IW pronto a magazzino (RettificaGrezzoIW), con SOLO la
      differenza (delta), stesso meccanismo cumulativo delle rettifiche
      manuali — se la correzione è verso il basso (spedito MENO di quanto
      registrato), la differenza torna disponibile come pronta a magazzino
      (non è mai davvero partita); se è verso l'alto, viene scalata la
      differenza in più
    """
    d = request.get_json(force=True)
    try:
        nuova_qta = int(d['nuova_qta'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'nuova_qta mancante o non numerica'}), 400
    if nuova_qta < 0:
        return jsonify({'ok': False, 'error': 'La quantità non può essere negativa'}), 400

    lav = LavorazioneTerzista.query.get_or_404(lid)
    vecchia_qta = lav.qta
    if nuova_qta == vecchia_qta:
        return jsonify({'ok': True, 'invariato': True})

    try:
        note_j = json.loads(lav.note or '{}')
    except Exception:
        note_j = {}
    codice = note_j.get('codice', '')
    qta_rientrata = int(note_j.get('qta_rientrata', 0))

    avviso = None
    if nuova_qta < qta_rientrata:
        # Caso anomalo: risultano rientrati più pezzi di quanti se ne
        # spediscano ora — non blocchiamo (l'errore originale potrebbe
        # essere proprio nella spedizione, e il rientro è già successo
        # davvero), ma segnaliamo per una verifica manuale.
        avviso = (f"Attenzione: risultano già {qta_rientrata} pezzi rientrati, "
                  f"più della nuova quantità spedita ({nuova_qta}) — verifica il DDT di rientro.")

    delta_correzione = nuova_qta - vecchia_qta   # negativo se si corregge al ribasso
    lav.qta = nuova_qta
    if codice and delta_correzione:
        # Delta di segno OPPOSTO alla correzione della spedita: se si
        # spedisce di MENO (delta_correzione negativo), quella differenza
        # non è mai partita → torna pronta a magazzino (rettifica positiva).
        db.session.add(RettificaGrezzoIW(
            codice=codice, delta=-delta_correzione,
            note=f'Correzione qtà spedita DDT {lav.ddt_uscita or "?"}: da {vecchia_qta} a {nuova_qta} (lavorazione #{lav.id})'
        ))
    db.session.commit()
    log(f'Corretta qtà spedita lavorazione #{lav.id} (DDT {lav.ddt_uscita}): {vecchia_qta} -> {nuova_qta}')
    return jsonify({'ok': True, 'qta_spedita': nuova_qta, 'avviso': avviso})


@terzisti_bp.route('/api/terzisti/residuo_per_fornitore')
def api_residuo_per_fornitore():
    """
    Riepilogo per FORNITORE + CODICE del totale ancora residuo presso ogni
    terzista — un codice può essere finito su più DDT diversi nel tempo
    (spedizioni successive), questa vista li somma per dare "quanto ho
    fisicamente in mano da lui in questo momento", con il dettaglio dei
    singoli DDT che compongono quel totale per il popup di approfondimento.
    """
    lavorazioni = LavorazioneTerzista.query.order_by(LavorazioneTerzista.data_uscita.desc()).all()
    gruppi = {}   # (terzista, codice) -> {'residua': int, 'dettaglio': [...]}
    for lav in lavorazioni:
        try:
            note_j = json.loads(lav.note or '{}')
        except Exception:
            note_j = {}
        codice = note_j.get('codice', '')
        if not codice:
            continue
        qta_rientrata = int(note_j.get('qta_rientrata', 0))
        qta_residua = max(0, lav.qta - qta_rientrata)
        if qta_residua <= 0:
            continue  # completamente rientrato: non pesa più sul residuo presso il fornitore
        tz = Terzista.query.get(lav.terzista_id)
        nome_terzista = tz.nome if tz else '(fornitore sconosciuto)'
        chiave = (nome_terzista, codice)
        if chiave not in gruppi:
            gruppi[chiave] = {'terzista': nome_terzista, 'codice': codice, 'desc': note_j.get('desc', ''),
                               'residua': 0, 'dettaglio': []}
        gruppi[chiave]['residua'] += qta_residua
        gruppi[chiave]['dettaglio'].append({
            'lavorazione_id': lav.id, 'ddt_uscita': lav.ddt_uscita, 'data_uscita': lav.data_uscita,
            'qta_spedita': lav.qta, 'qta_rientrata': qta_rientrata, 'qta_residua': qta_residua,
        })
    righe = sorted(gruppi.values(), key=lambda r: (-r['residua']))
    return jsonify(righe)
