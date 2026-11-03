"""Impostazioni AMPS: valori di partenza nel codice, modificabili dalla pagina «Impostazioni AMPS» senza riscrivere il programma."""
from blueprints.masterplan.modelli import MpImpostazione
from models import db

# chiave: (valore di partenza, minimo, massimo, etichetta, unità, usata dal rilascio)
PARAMETRI = {
    'orizzonte_settimane':      (26, 4, 52, 'Orizzonte del piano', 'settimane', 'R1'),
    'zona_congelata_settimane': (2, 0, 12, 'Zona congelata: contano solo gli ordini certi', 'settimane', 'R1'),
    'finestra_consumo_settimane': (2, 0, 8, "Finestra di consumo: un ordine mangia la previsione di ±", 'settimane', 'R1'),
    'pct_certa':                (100, 0, 100, 'Affidabilità «Certa»', '%', 'R1'),
    'pct_probabile':            (70, 0, 100, 'Affidabilità «Probabile»', '%', 'R1'),
    'pct_possibile':            (30, 0, 100, 'Affidabilità «Possibile» (solo scenario ottimista)', '%', 'R1'),
    'affidabilita_pesata':      (1, 0, 1, 'Pesa le quantità con la % di affidabilità (1 = sì, 0 = quantità intere)', '', 'R1'),
    'storico_mesi':             (12, 3, 36, 'Mesi di storico ordini per proporre mix e previsioni', 'mesi', 'R1'),
    'ore_giorno':               (8, 1, 24, 'Ore lavorative al giorno (default reparti)', 'ore', 'R2'),
    'giorni_settimana':         (5, 1, 7, 'Giorni lavorativi a settimana (default reparti)', 'giorni', 'R2'),
    'lead_time_default_giorni': (15, 1, 365, 'Lead time prudente se il fornitore non è compilato', 'giorni lavorativi', 'R2'),
}


def valori():
    """Tutti i parametri: salvati se presenti, altrimenti valore di partenza."""
    salvati = {r.chiave: r.valore for r in MpImpostazione.query.all()}
    out = {}
    for k, (default, *_r) in PARAMETRI.items():
        try:
            out[k] = int(float(salvati[k])) if k in salvati else default
        except (TypeError, ValueError):
            out[k] = default
    return out


def salva(nuovi):
    """Salva i parametri validati (intero tra minimo e massimo). Ritorna l'elenco degli errori (vuoto = ok)."""
    errori = []
    for k, v in (nuovi or {}).items():
        if k not in PARAMETRI:
            errori.append(f'parametro sconosciuto: {k}')
            continue
        _d, lo, hi, etichetta, *_r = PARAMETRI[k]
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            errori.append(f'{etichetta}: numero non valido')
            continue
        if not lo <= n <= hi:
            errori.append(f'{etichetta}: deve essere tra {lo} e {hi}')
            continue
        riga = db.session.get(MpImpostazione, k)
        if riga:
            riga.valore = str(n)
        else:
            db.session.add(MpImpostazione(chiave=k, valore=str(n)))
    if errori:
        db.session.rollback()
    else:
        db.session.commit()
    return errori


def peso_affidabilita(imp, affidabilita):
    """Frazione (0..1) con cui una previsione di quella affidabilità entra nel calcolo."""
    if not imp['affidabilita_pesata']:
        return 1.0
    return imp['pct_' + affidabilita.lower()] / 100.0
