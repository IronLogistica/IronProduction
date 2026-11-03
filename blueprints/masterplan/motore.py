"""
MASTERPLAN · AMPS — regole di calcolo PURE (nessun accesso al database, nessuna scrittura).

Tutto in pezzi INTERI: dove una divisione non torna, l'arrotondamento va sul codice con la quota
più alta, così i totali restano esatti (famiglia 300 -> 150 + 90 + 60, mai 299 o 301).
"""
import math
from datetime import date, timedelta

TOLLERANZA_MIX = 0.01          # la somma delle % del mix deve fare 100 (±0,01)


class MixNonValido(ValueError):
    """Il mix di una famiglia non somma 100%: il calcolo per famiglia si blocca."""


def lunedi(d):
    """Lunedì della settimana di d (le settimane di AMPS partono di lunedì)."""
    return d - timedelta(days=d.weekday())


def primo_del_mese(d):
    return date(d.year, d.month, 1)


def aggiungi_mesi(d, n):
    m = d.year * 12 + (d.month - 1) + n
    return date(m // 12, m % 12 + 1, 1)


def elenco_settimane(inizio, n):
    """n lunedì consecutivi a partire dalla settimana di 'inizio'."""
    primo = lunedi(inizio)
    return [primo + timedelta(weeks=i) for i in range(n)]


def giorni_lavorativi_per_settimana(mese):
    """{lunedì: giorni lavorativi (lun-ven) del mese che cadono in quella settimana}."""
    mese = primo_del_mese(mese)
    out = {}
    g = mese
    while g.month == mese.month:
        if g.weekday() < 5:
            out[lunedi(g)] = out.get(lunedi(g), 0) + 1
        g += timedelta(days=1)
    return out


def spalma(quantita, pesi):
    """
    Divide un intero in parti intere proporzionali ai pesi (metodo del resto più grande):
    la somma delle parti è SEMPRE 'quantita'. A parità di resto prevale il peso più alto, poi la chiave.
    """
    quantita = int(quantita)
    chiavi = sorted(pesi)
    totale = sum(pesi[k] for k in chiavi)
    if quantita <= 0 or totale <= 0:
        return {k: 0 for k in chiavi}
    esatte = {k: quantita * pesi[k] / totale for k in chiavi}
    parti = {k: int(math.floor(esatte[k] + 1e-9)) for k in chiavi}
    resto = quantita - sum(parti.values())
    ordine = sorted(chiavi, key=lambda k: (-(esatte[k] - parti[k]), -pesi[k], k))
    for k in ordine[:resto]:
        parti[k] += 1
    return parti


def mensile_su_settimane(mese, quantita):
    """Previsione mensile -> {lunedì: pezzi}, divisa sulle settimane lavorative del mese."""
    return spalma(quantita, giorni_lavorativi_per_settimana(mese))


def controlla_mix(mix):
    """mix = {codice: %}. Solleva MixNonValido se non somma 100%."""
    if not mix:
        raise MixNonValido('il mix è vuoto')
    if any(v < 0 for v in mix.values()):
        raise MixNonValido('una quota è negativa')
    somma = sum(mix.values())
    if abs(somma - 100.0) > TOLLERANZA_MIX:
        raise MixNonValido(f'le quote sommano {somma:g}% invece di 100%')


def distribuisci_famiglia(totale, mix, articoli=None):
    """
    Divide la previsione di una famiglia sui suoi codici.
      totale   : pezzi previsti per la famiglia nel periodo
      mix      : {codice: % del mix} (deve fare 100%)
      articoli : {codice: pezzi} già previsti PER ARTICOLO nello stesso periodo
    Regole: la previsione per articolo prevale sulla quota che arriverebbe dalla famiglia; il resto
    della famiglia si ridistribuisce sugli altri codici in proporzione al mix.
    Ritorna ({codice: {'qta', 'origine': 'ARTICOLO'|'FAMIGLIA', 'pct'}}, [avvisi]).
    """
    controlla_mix(mix)
    articoli = {c: int(q) for c, q in (articoli or {}).items() if c in mix and q > 0}
    avvisi = []
    somma_art = sum(articoli.values())
    if somma_art > totale:
        avvisi.append(f'le previsioni per articolo ({somma_art}) superano quella di famiglia ({totale}): da correggere')
    resto = max(int(totale) - somma_art, 0)
    liberi = {c: p for c, p in mix.items() if c not in articoli and p > 0}
    quote = spalma(resto, liberi) if liberi else {}
    if resto > 0 and not liberi:
        avvisi.append(f'{resto} pezzi di famiglia senza codice a cui assegnarli (tutti i codici hanno già la previsione per articolo o quota 0%)')
    out = {}
    for c in mix:
        if c in articoli:
            out[c] = {'qta': articoli[c], 'origine': 'ARTICOLO', 'pct': mix[c]}
        else:
            out[c] = {'qta': quote.get(c, 0), 'origine': 'FAMIGLIA', 'pct': mix[c]}
    return out, avvisi


def _offset_finestra(finestra):
    """0, -1, +1, -2, +2 …: prima la stessa settimana, poi le vicine (prima quella precedente)."""
    out = [0]
    for i in range(1, finestra + 1):
        out += [-i, i]
    return out


def consuma_previsione(settimane, previsione, ordini, finestra=2, congelate=2):
    """
    Consumo della previsione per UN codice.
      settimane  : lista di lunedì (orizzonte)
      previsione : {lunedì: pezzi previsti}      ordini : {lunedì: pezzi di ordini certi}
    Ogni ordine "mangia" la previsione della sua settimana, poi quella delle vicine (±finestra).
    Domanda = ordini certi + previsione residua  (cioè il PIÙ ALTO tra i due, non la somma).
    Zona congelata: nelle prime 'congelate' settimane la previsione non consumata non conta (solo ordini).
    Ritorna ([{settimana, previsto, ordini, residuo, domanda, eccedenza}], …): 'eccedenza' = parte degli
    ordini che la previsione del codice non ha potuto assorbire (serve al consumo a livello di famiglia).
    """
    n = len(settimane)
    residuo = {w: int(previsione.get(w, 0)) for w in settimane}
    eccedenza = {w: 0 for w in settimane}
    for i, w in enumerate(settimane):
        da_coprire = int(ordini.get(w, 0))
        for off in _offset_finestra(finestra):
            j = i + off
            if da_coprire <= 0:
                break
            if 0 <= j < n:
                tolto = min(residuo[settimane[j]], da_coprire)
                residuo[settimane[j]] -= tolto
                da_coprire -= tolto
        eccedenza[w] = da_coprire
    return [{'settimana': w, 'previsto': int(previsione.get(w, 0)), 'ordini': int(ordini.get(w, 0)),
             'residuo': residuo[w], 'domanda': 0, 'eccedenza': eccedenza[w]} for w in settimane]


def consuma_famiglia(settimane, righe_per_codice, finestra=2):
    """
    Secondo passo per i codici di UNA famiglia: l'eccedenza degli ordini di un codice (oltre la sua quota)
    consuma la previsione residua degli altri codici della famiglia, in proporzione, così il totale
    famiglia non si gonfia. Modifica 'residuo' sul posto. Arrotondamento sul codice con la quota più alta.
    """
    n = len(settimane)
    for i, w in enumerate(settimane):
        eccesso = sum(righe[i]['eccedenza'] for righe in righe_per_codice.values())
        for off in _offset_finestra(finestra):
            j = i + off
            if eccesso <= 0:
                break
            if not 0 <= j < n:
                continue
            res = {c: righe[j]['residuo'] for c, righe in righe_per_codice.items() if righe[j]['residuo'] > 0}
            totale_res = sum(res.values())
            if totale_res <= 0:
                continue
            tolto = min(eccesso, totale_res)
            nuovi = spalma(totale_res - tolto, res)       # resto della previsione, in proporzione alle quote attuali
            for c, v in nuovi.items():
                righe_per_codice[c][j]['residuo'] = v
            eccesso -= tolto


def chiudi_domanda(righe, congelate=2):
    """Calcola 'domanda' per ogni settimana: ordini + residuo (il residuo nella zona congelata vale 0)."""
    for i, r in enumerate(righe):
        r['residuo_contato'] = 0 if i < congelate else r['residuo']
        r['domanda'] = r['ordini'] + r['residuo_contato']
    return righe
