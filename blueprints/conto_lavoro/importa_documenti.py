"""
TRIPLE WATCH — lettura dei PDF Zucchetti di DDT DI VENDITA e FATTURE del
conto lavoro (richiesta di Mauri, 28/11/2026).

Stesso algoritmo dell'import "DDT di carico" di IronProduction
(blueprints/acquisti_wood, _estrai_dati_ddt_carico, a sua volta clone di
MasterLogistic-WMS), letto dal lato opposto:
  - DDT di vendita: numero "NNN /DDT", data, destinatario, sezioni
    "Ns. doc.(ORDCL) n.: NNN" (l'ordine cliente da cui nasce la spedizione)
    con le righe articolo sotto;
  - fattura: numero "NNN /F..", data, totale documento e i DDT/ordini
    richiamati ("Ns. doc.(DDT...) n.: NNN", "DDT n. NNN", "NNN /DDT").

Un PDF puo' contenere PIU' documenti (la stampa di fine mese di tutte le
fatture): le pagine vengono raggruppate per numero di documento.
Nessuna funzione solleva eccezioni: cio' che non si legge resta vuoto e si
vede nell'anteprima prima di salvare.
"""
import io
import re

import PyPDF2

SKIP_CODICI = {'Ns.', 'Rif.', 'Pagina', 'Totale', 'TOTALE', 'Imponibile', 'IVA', 'Iva', 'Codice', 'Descrizione',
               'Vs.', 'Data', 'Numero', 'Banca', 'Scadenza', 'Trasporto', 'Porto', 'Causale', 'Aspetto', 'Colli',
               'Peso', 'Vettore', 'Destinatario:', 'Destinazione:', 'Spett.le', 'Partita', 'Cod.', 'Pag.'}
RE_ART = re.compile(r'([A-Za-z0-9][A-Za-z0-9./_-]+)\s+(.*?)\s+((?:m|mq|ml|pz|kg|nr|cad|lt|mt|n|conf|cf))\s*\.\s+'
                    r'([\d.]+(?:,\d+)?)')
RE_ORDCL = re.compile(r'Ns\.\s*doc\.\s*\(\s*ORDCL\s*\)\s*n\.?\s*:?\s*(\d+)', re.I)
RE_ORDINE_IS = re.compile(r'(\d+)\s*/\s*IS\b')
RE_RIF_DDT = [
    re.compile(r'Ns\.\s*doc\.\s*\(\s*DDT\w*\s*\)\s*n\.?\s*:?\s*(\d+)', re.I),
    re.compile(r'\bD\.?\s?D\.?\s?T\.?\s*(?:n\.?|nr\.?|num\.?|numero|n°)\s*:?\s*(\d+)', re.I),
    re.compile(r'\bbolla\s*(?:n\.?|nr\.?|n°)\s*:?\s*(\d+)', re.I),
    re.compile(r'(\d+)\s*/\s*DDT\b'),
]
RE_DATA = re.compile(r'(\d{2}/\d{2}/\d{4})')


def numero_it(s):
    """'6,000' / '1.234,50' -> float; None se non leggibile."""
    if not s:
        return None
    try:
        return float(str(s).replace('.', '').replace(',', '.'))
    except ValueError:
        return None


def pagine_pdf(raw):
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(raw))
        return [(p.extract_text() or '') for p in reader.pages]
    except Exception:
        return None


def riconosci_tipo(testo):
    """DDT di vendita o fattura, a punteggio sulle parole tipiche dei due documenti Zucchetti."""
    t = testo.upper()
    if 'CONFERMA ORDINE' in t:      # e' un ordine cliente: si carica dalla pagina Ordini
        return None
    punti_ddt = sum(1 for k in ('USCITA MERCI', 'DOCUMENTO DI TRASPORTO', 'D.D.T', 'ASPETTO DEI BENI', 'VETTORE',
                                'CAUSALE DEL TRASPORTO', 'ORDCL', 'PORTO') if k in t)
    punti_ddt += 2 if _numero_documento(testo, 'DDT') else 0
    punti_fat = sum(1 for k in ('FATTURA', 'IMPONIBILE', 'TOTALE DOCUMENTO', 'SCADENZ', 'NETTO A PAGARE',
                                'NOTA DI CREDITO', 'ALIQUOTA', 'MODALITA DI PAGAMENTO', 'MODALITÀ DI PAGAMENTO') if k in t)
    punti_fat += 2 if _numero_documento(testo, r'F[A-Z]{0,2}|NC') else 0
    if not punti_ddt and not punti_fat:
        return None
    return 'FATTURA' if punti_fat > punti_ddt else 'DDT'


def _linee(testo):
    return [l.strip() for l in testo.split('\n') if l.strip()]


def _numero_documento(testo, suffissi):
    """Numero del documento. Formati Zucchetti reali (PDF di Mauri, 28/11/2026):
    DDT di vendita "Numero documento 721", fattura "Numero Fattura" + "119" sulla riga sotto;
    in mancanza, 'NNN /DDT' o 'NNN /FT' su una riga che NON richiama altri documenti."""
    m = re.search(r'Numero\s+(?:documento|fattura)\s*:?\s*\n?\s*(\d+)', testo, re.I)
    if m:
        return m.group(1)
    re_num = re.compile(r'(\d+)\s*/\s*(' + suffissi + r')\b')
    for linea in _linee(testo):
        if re.search(r'Rif\.|Ns\.\s*doc|Vs\.|riferimento', linea, re.I):
            continue
        m = re_num.search(linea)
        if m:
            return m.group(1)
    # "Numero documento" con il numero sulle righe sotto
    linee = _linee(testo)
    for i, linea in enumerate(linee):
        if re.search(r'Numero\s+documento|N\.\s*documento|Fattura\s+n', linea, re.I):
            m = re.search(r'(?:n\.?|n°|numero)\s*:?\s*(\d+)\s*$', linea, re.I)
            if m:
                return m.group(1)
            for off in range(1, 4):
                if i + off < len(linee):
                    m = re.match(r'^(\d+)\b', linee[i + off])
                    if m:
                        return m.group(1)
            break
    return ''


def _data_documento(testo):
    m = re.search(r'Data\s+(?:documento|fattura)\s*:?\s*\n?\s*(\d{2}/\d{2}/\d{4})', testo, re.I)
    if m:
        return m.group(1)
    m = RE_DATA.search(testo)
    return m.group(1) if m else ''


def _cliente(testo):
    nome, piva = '', ''
    linee = _linee(testo)
    for i, linea in enumerate(linee):
        if re.search(r'Destinatario|Spett\.?le|Intestatario', linea, re.I):
            resto = re.sub(r'.*?(Destinatario|Spett\.?le|Intestatario)\s*:?', '', linea, flags=re.I).strip()
            if resto and not resto.isdigit():
                nome = resto
            elif i + 1 < len(linee):
                nome = linee[i + 2] if linee[i + 1].isdigit() and i + 2 < len(linee) else linee[i + 1]
            break
    for m in re.finditer(r'(?:Partita\s+IVA|P\.?\s?IVA)[^\d]{0,30}(\d{11})', testo, re.I):
        piva = m.group(1)   # l'ultima trovata: di solito la prima e' la nostra in intestazione
    return nome[:200], piva


def _righe_con_sezioni(testo):
    """Righe articolo, ognuna con l'ordine ORDCL della sezione in cui si trova."""
    righe, ordine_corrente = [], ''
    for linea in _linee(testo):
        m_o = RE_ORDCL.search(linea)
        if m_o:
            ordine_corrente = m_o.group(1)
            continue
        m = RE_ART.search(linea)
        if m and m.group(1) not in SKIP_CODICI and not re.match(r'^\d{1,2}/\d{1,2}', m.group(1)):
            righe.append({'ordine_rif': ordine_corrente, 'codice': m.group(1).upper(),
                          'descrizione': m.group(2).strip()[:300], 'quantita': numero_it(m.group(4))})
    return righe


def estrai_ddt_vendita(testo):
    nome, piva = _cliente(testo)
    ordini = list(dict.fromkeys(RE_ORDCL.findall(testo)))
    return {'tipo': 'DDT', 'numero': _numero_documento(testo, 'DDT'), 'data': _data_documento(testo),
            'cliente_nome': nome, 'cliente_piva': piva, 'importo': None,
            'riferimenti': [('ORDINE', n) for n in ordini], 'righe': _righe_con_sezioni(testo)}


def estrai_fattura(testo):
    nome, piva = _cliente(testo)
    numero = _numero_documento(testo, r'F[A-Z]{0,2}|NC')
    ddt = []
    for linea in _linee(testo):
        if re.search(r'cliente|Vs\.', linea, re.I):   # "Rif. n. cliente 611/DDT" e' il documento del CLIENTE, non il nostro
            continue
        for r in RE_RIF_DDT:
            for n in r.findall(linea):
                if n != numero:
                    ddt.append(n)
    ordini = list(dict.fromkeys(RE_ORDCL.findall(testo) + RE_ORDINE_IS.findall(testo)))
    importo = None
    for pat in (r'Totale\s+documento[^\d\n]*\n?\s*(?:€|EUR)?\s*([\d.]+,\d{2})',
                r'Totale\s+fattura[^\d\n]*\n?\s*(?:€|EUR)?\s*([\d.]+,\d{2})',
                r'Netto\s+a\s+pagare[^\d\n]*\n?\s*(?:€|EUR)?\s*([\d.]+,\d{2})',
                r'TOTALE[^\d\n]*\n?\s*(?:€|EUR)?\s*([\d.]+,\d{2})'):
        trovati = re.findall(pat, testo, re.I)
        if trovati:
            importo = numero_it(trovati[-1])
            break
    return {'tipo': 'FATTURA', 'numero': numero, 'data': _data_documento(testo), 'cliente_nome': nome,
            'cliente_piva': piva, 'importo': importo,
            'riferimenti': [('DDT', n) for n in dict.fromkeys(ddt)] + [('ORDINE', n) for n in ordini],
            'righe': _righe_con_sezioni(testo)}


def estrai_documenti(raw, tipo_richiesto='AUTO'):
    """Tutti i documenti di un PDF: le pagine con lo stesso numero (o senza numero) stanno insieme.
    Ritorna (documenti, errore)."""
    pagine = pagine_pdf(raw)
    if pagine is None:
        return [], 'Impossibile leggere il PDF.'
    if not any(p.strip() for p in pagine):
        return [], 'Il PDF non contiene testo leggibile (scansione?).'
    gruppi = []
    for testo in pagine:
        tipo = tipo_richiesto if tipo_richiesto in ('DDT', 'FATTURA') else (riconosci_tipo(testo) or
                                                                           (gruppi[-1]['tipo'] if gruppi else None))
        numero = _numero_documento(testo, 'DDT' if tipo == 'DDT' else r'F[A-Z]{0,2}|NC') if tipo else ''
        if gruppi and (not numero or (numero == gruppi[-1]['numero'] and tipo == gruppi[-1]['tipo'])):
            gruppi[-1]['testo'] += '\n' + testo
            gruppi[-1]['pagine'] += 1
        else:
            gruppi.append({'tipo': tipo, 'numero': numero, 'testo': testo, 'pagine': 1})
    documenti = []
    for g in gruppi:
        if g['tipo'] == 'DDT':
            d = estrai_ddt_vendita(g['testo'])
        elif g['tipo'] == 'FATTURA':
            d = estrai_fattura(g['testo'])
        else:
            d = {'tipo': None, 'numero': g['numero'], 'data': _data_documento(g['testo']), 'cliente_nome': '',
                 'cliente_piva': '', 'importo': None, 'riferimenti': [], 'righe': []}
        d['pagine'] = g['pagine']
        d['testo'] = g['testo']
        documenti.append(d)
    return documenti, None
