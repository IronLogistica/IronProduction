"""
Import massivo degli ARTICOLI DEL CLIENTE (conto lavoro) da Excel/CSV —
stesso comportamento dell'import anagrafica del resto di IronProduction:
lettura tollerante (xlsx, xls, "finto excel" HTML di Zucchetti, csv),
prima ANTEPRIMA senza scritture, poi CONFERMA ricaricando lo stesso file.

Copia locale del lettore tollerante: il modulo conto lavoro non importa
nulla da blueprints.magazzino (isolamento dalla giacenza Iron Wood).

Colonne riconosciute (maiuscole/minuscole, spazi e punti indifferenti):
  codice       → CODICE, COD, CODICE ARTICOLO, ARTICOLO, ARCODART
  descrizione  → DESCRIZIONE, DESC, DESCRIZIONE ARTICOLO, ARDESART
  udm          → UDM, UM, U.M., UNITA DI MISURA, ARUNMIS1        (facoltativa, default PZ)
  tracciamento → TRACCIAMENTO, TRACC                              (facoltativa, default LOTTO)
"""
import io
import re

import pandas as pd

from blueprints.conto_lavoro.models import TRACCIAMENTI, UDM

SINONIMI = {
    'codice': ('codice', 'cod', 'codicearticolo', 'articolo', 'arcodart'),
    'descrizione': ('descrizione', 'desc', 'descrizionearticolo', 'ardesart'),
    'udm': ('udm', 'um', 'unitadimisura', 'unitamisura', 'arunmis1'),
    'tracciamento': ('tracciamento', 'tracc'),
}
MAPPA_UDM = {
    'PZ': 'PZ', 'PZ.': 'PZ', 'N': 'PZ', 'N.': 'PZ', 'NR': 'PZ', 'NR.': 'PZ', 'NUM': 'PZ', 'PEZZI': 'PZ', 'PEZZO': 'PZ',
    'KG': 'KG', 'KG.': 'KG', 'CHILI': 'KG',
    'M': 'M', 'M.': 'M', 'MT': 'M', 'MT.': 'M', 'ML': 'M', 'METRI': 'M',
    'KIT': 'KIT',
}
MAX_RIGHE = 5000


def _normalizza_intestazione(nome):
    return re.sub(r'[^a-z0-9]', '', str(nome).strip().lower().replace('à', 'a'))


def leggi_file_tollerante(raw, filename):
    """(dataframe, formato) oppure (None, None) — prova xlsx/xls, HTML ('finto excel'), csv."""
    nome = (filename or '').lower()
    if nome.endswith('.csv'):
        tentativi = []
    elif nome.endswith('.xls'):
        tentativi = [('xls', lambda: pd.read_excel(io.BytesIO(raw), engine='xlrd')),
                     ('xlsx', lambda: pd.read_excel(io.BytesIO(raw), engine='openpyxl'))]
    else:
        tentativi = [('xlsx', lambda: pd.read_excel(io.BytesIO(raw), engine='openpyxl')),
                     ('xls', lambda: pd.read_excel(io.BytesIO(raw), engine='xlrd'))]
    for formato, leggi in tentativi:
        try:
            df = leggi()
            if df is not None and len(df.columns) >= 1:
                return df, formato
        except Exception:
            pass
    try:
        tabelle = pd.read_html(io.BytesIO(raw))
        if tabelle:
            return max(tabelle, key=lambda t: t.shape[0] * t.shape[1]), 'html (finto excel)'
    except Exception:
        pass
    for enc in ('utf-8-sig', 'latin-1', 'cp1252'):
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding=enc, sep=None, engine='python', dtype=str)
            if df is not None and len(df.columns) >= 1:
                return df, 'csv'
        except Exception:
            pass
    return None, None


def _cella(valore):
    if valore is None:
        return ''
    try:
        if pd.isna(valore):
            return ''
    except (TypeError, ValueError):
        pass
    testo = str(valore).strip()
    if re.fullmatch(r'\d+\.0', testo):   # codici numerici letti da Excel come 1234.0
        testo = testo[:-2]
    return testo


def analizza_file(raw, filename):
    """
    Legge il file e restituisce (righe_valide, scartate, formato) SENZA scrivere nulla.
    righe_valide: [{riga, codice, descrizione, udm, tracciamento}] (codice già maiuscolo, una per codice)
    scartate:     [{riga, codice, motivo}]
    Solleva ValueError se il file non è leggibile o manca la colonna del codice.
    """
    df, formato = leggi_file_tollerante(raw, filename)
    if df is None:
        raise ValueError('File non leggibile (provato: xlsx, xls, "finto excel"/HTML, csv).')
    colonne = {}
    for originale in df.columns:
        chiave = _normalizza_intestazione(originale)
        for campo, sinonimi in SINONIMI.items():
            if chiave in sinonimi and campo not in colonne:
                colonne[campo] = originale
    if 'codice' not in colonne:
        raise ValueError('Colonna del codice non trovata. Intestazioni ammesse: CODICE (o ARCODART). '
                         f'Trovate: {", ".join(str(c) for c in df.columns)}')
    if len(df) > MAX_RIGHE:
        raise ValueError(f'Troppe righe ({len(df)}): massimo {MAX_RIGHE} per file.')

    valide, scartate, visti = {}, [], {}
    for i, (_, row) in enumerate(df.iterrows(), start=2):   # riga 1 = intestazione
        codice = _cella(row.get(colonne['codice'])).upper()
        if not codice:
            continue
        if len(codice) > 100:
            scartate.append({'riga': i, 'codice': codice[:30] + '…', 'motivo': 'codice oltre 100 caratteri'})
            continue
        descrizione = _cella(row.get(colonne['descrizione'])) if 'descrizione' in colonne else ''
        udm_file = _cella(row.get(colonne['udm'])).upper() if 'udm' in colonne else ''
        udm = MAPPA_UDM.get(udm_file, udm_file) if udm_file else 'PZ'
        if udm not in UDM:
            scartate.append({'riga': i, 'codice': codice, 'motivo': f'unità di misura "{udm_file}" non riconosciuta (ammesse: {", ".join(UDM)})'})
            continue
        tracc_file = _cella(row.get(colonne['tracciamento'])).upper() if 'tracciamento' in colonne else ''
        tracciamento = 'MATRICOLA' if tracc_file.startswith('MAT') else 'LOTTO'
        if codice in visti:
            scartate.append({'riga': visti[codice], 'codice': codice, 'motivo': f'codice ripetuto: vale la riga {i}'})
        visti[codice] = i
        valide[codice] = {'riga': i, 'codice': codice, 'descrizione': descrizione[:300],
                          'udm': udm, 'tracciamento': tracciamento if tracciamento in TRACCIAMENTI else 'LOTTO'}
    return list(valide.values()), scartate, formato


def crea_modello_xlsx():
    """File modello da compilare, con due righe d'esempio."""
    df = pd.DataFrame([
        {'CODICE': 'FZ44', 'DESCRIZIONE': 'TELAIO DX LAT. INCLINATO RUOTA GIREVOLE', 'UDM': 'PZ', 'TRACCIAMENTO': 'LOTTO'},
        {'CODICE': 'TUBO-101', 'DESCRIZIONE': 'Tubo D.101 fornito dal cliente', 'UDM': 'PZ', 'TRACCIAMENTO': 'LOTTO'},
    ])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        df.to_excel(w, index=False, sheet_name='Articoli cliente')
    return buf.getvalue()
