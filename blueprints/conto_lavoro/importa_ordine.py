"""
Import dell'ORDINE CLIENTE da PDF (conto lavoro) — stesso formato Zucchetti
già riconosciuto per gli ordini fornitore in blueprints/acquisti_wood
("CONFERMA ORDINE CLIENTE"), qui letto dal lato opposto: il documento che
il CLIENTE manda a noi, non quello che noi mandiamo a un fornitore.

L'ordine cliente è l'origine del "codice di magazzino" (ClArticoloCliente,
vedi blueprints/conto_lavoro/models.py): un codice dell'ordine che non
esiste ancora per quel cliente viene creato automaticamente al momento
della conferma import (vedi routes.py, api_importa_ordine) — non gestiamo
distinta base in ingresso, solo il codice/descrizione/UdM così come scritti
sull'ordine.

NOTA: la logica è tarata sul PDF d'esempio fornito da Mauri (IronWood Srls
-> Faza Srl, "Ordini_clienti.PDF"). Se un altro cliente usa un formato PDF
diverso, il parsing automatico potrebbe non riconoscere tutte le righe: il
testo grezzo resta comunque salvato in cl_ordine.testo_grezzo_pdf, e
l'ordine letto male si vede subito in anteprima (righe vuote/sospette)
prima di essere confermato.
"""
import re

UDM_RICONOSCIUTE = ('n', 'pz', 'kg', 'm', 'mq', 'ml', 'lt', 'mt', 'nr', 'cad')
MAPPA_UDM_ORDINE = {'N': 'PZ', 'PZ': 'PZ', 'NR': 'PZ', 'CAD': 'PZ', 'KG': 'KG',
                    'M': 'M', 'MQ': 'M', 'ML': 'M', 'MT': 'M', 'LT': 'KG'}

RIGA_ARTICOLO_RE = re.compile(
    r'^([A-Za-z0-9][A-Za-z0-9._/-]*)\s+(.+?)\s+(' + '|'.join(UDM_RICONOSCIUTE) + r')\.\s+'
    r'([\d.,]+)\s+(?:([\d.,]+)\s+([\d.,]+)\s+)?(\d+)$')


def _numero_it(s):
    """'6,000' / '25,00000' / '1.234,50' -> float. None se non parsabile."""
    if not s:
        return None
    try:
        return float(s.replace('.', '').replace(',', '.'))
    except ValueError:
        return None


def _data_gg_mm_aaaa(s):
    m = re.search(r'(\d{2}/\d{2}/\d{4})', s or '')
    return m.group(1) if m else None


def estrai_dati_ordine(testo_completo):
    """
    Ritorna: {cliente_nome, cliente_piva, cod_cliente, numero_ordine,
    rif_cliente, data_documento, righe: [{codice, descrizione, udm, quantita,
    prezzo_unitario}]}. Non solleva mai eccezioni: i campi non trovati
    restano vuoti/None, così l'anteprima mostra cosa manca invece di un
    errore secco.
    """
    dati = {'cliente_nome': '', 'cliente_piva': '', 'cod_cliente': '',
           'numero_ordine': '', 'rif_cliente': '', 'data_documento': '', 'righe': []}
    linee = [l.strip() for l in testo_completo.split('\n') if l.strip()]

    # Cliente (da "Destinatario:") — stesso pattern usato per il fornitore
    # negli ordini d'acquisto: la riga col codice numerico, poi la ragione sociale.
    for i, linea in enumerate(linee):
        if 'Destinatario:' in linea and i + 2 < len(linee):
            dati['cliente_nome'] = linee[i + 2] if linee[i + 1].isdigit() else linee[i + 1]
            if linee[i + 1].isdigit():
                dati['cod_cliente'] = linee[i + 1]
            break

    match_piva = re.search(r'Partita IVA o codice fiscale\s*\n?\s*(\d{5,20})', testo_completo)
    if match_piva:
        dati['cliente_piva'] = match_piva.group(1)

    # Numero ordine: preferisce "NNN /IS" (come stampato in alto a destra),
    # altrimenti la riga sotto "Numero documento".
    match_is = re.search(r'(\d+)\s*/\s*IS\b', testo_completo)
    if match_is:
        dati['numero_ordine'] = f"{match_is.group(1)}/IS"
    else:
        for i, linea in enumerate(linee):
            if 'Numero documento' in linea:
                for offset in range(1, 4):
                    if i + offset < len(linee):
                        m = re.match(r'^(\d+)$', linee[i + offset])
                        if m:
                            dati['numero_ordine'] = m.group(1)
                            break
                break

    # Data documento
    match_data = re.search(r'Data documento\s*\n?\s*(\d{2}/\d{2}/\d{4})', testo_completo)
    if match_data:
        dati['data_documento'] = match_data.group(1)

    # Rif. n. cliente (es. "622 /DDT") — riferimento richiesto esplicitamente
    # da Mauri, sta a sinistra della data documento sull'ordine.
    for i, linea in enumerate(linee):
        if re.search(r'Rif\.?\s*n\.?\s*[Cc]liente', linea):
            if i + 1 < len(linee):
                m = re.match(r'^(\S+\s*/\s*\S+)\s+Del:', linee[i + 1])
                if m:
                    dati['rif_cliente'] = re.sub(r'\s*/\s*', '/', m.group(1))
            break

    # Righe articolo
    idx_header = None
    for i, linea in enumerate(linee):
        if 'codice' in linea.lower() and 'descrizione' in linea.lower():
            idx_header = i
            break
    linee_articoli = linee[idx_header + 1:] if idx_header is not None else linee
    for linea in linee_articoli:
        m = RIGA_ARTICOLO_RE.match(linea)
        if not m:
            continue
        udm_grezza = m.group(3).upper()
        dati['righe'].append({
            'codice': m.group(1).strip().upper(),
            'descrizione': m.group(2).strip(),
            'udm': MAPPA_UDM_ORDINE.get(udm_grezza, 'PZ'),
            'quantita': _numero_it(m.group(4)),
            'prezzo_unitario': _numero_it(m.group(5)),
        })
    return dati
