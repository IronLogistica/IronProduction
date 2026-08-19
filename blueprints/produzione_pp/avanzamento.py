"""
Avanzamento Commesse — motore di stima per il Cruscotto KPI.

Incrocia: Ordini di Produzione aperti e la priorità assegnata (colonna PRIO
già usata ovunque nell'app), il Ciclo di Lavoro (routing + tempo standard per
fase, CicloLavoroWood.produttivita_oraria), la giacenza/netting materiali già
usata da Magazzino/Situazione OP, e la capacità oraria configurata sui Centri
di Costo (ore_teoriche_periodo/n_risorse_equivalenti/pct_efficienza) — per
restituire, per ogni OP ancora aperto:
  - una stima di quando sarà finita la produzione INTERNA (pronta per essere
    spedita a una eventuale lavorazione esterna, es. verniciatura)
  - una stima della DATA DI CONSEGNA finale, sommando il lead time del
    fornitore esterno configurato sul centro di costo esterno del ciclo

È un simulatore "a lista", non un vero solutore di scheduling: processa gli
OP in ordine di priorità (stessa regola usata in tutta l'app: priorita asc,
poi id asc) e per ciascuno simula il passaggio nei centri di costo del suo
routing, tenendo per ogni centro un cursore di "primo momento libero" —
condiviso e consumato in ordine, esattamente come fa già
_giacenza_residua_dopo_impegni per il materiale. Fa assunzioni esplicite
(vedi DEFAULT_* sotto) quando mancano dati di capacità/tempo standard/lead
time — e le segnala come avvisi nel risultato, non le nasconde.

NON tiene conto di: assenze non pianificate, urgenze last-minute inserite
dopo il calcolo, manutenzioni straordinarie, resa/scarto oltre lo standard.
È una stima gestionale, non un impegno contrattuale.

SEMPLIFICAZIONE NOTA: il tempo di produzione di un componente (semilavorato o
prodotto finito) viene simulato per l'INTERA quantità richiesta dall'OP, anche
se una parte fosse già disponibile a magazzino — la giacenza qui influenza
solo SE un materiale d'acquisto (senza un proprio Ciclo di Lavoro) manca e va
atteso dal fornitore, non se un semilavorato già pronto salta la produzione.
Tende quindi a sovrastimare leggermente i tempi quando c'è scorta di
semilavorati, mai a sottostimarli.
"""
from datetime import datetime, timedelta

DEFAULT_ORE_GIORNO = 8.0          # fallback quando il centro non ha capacità configurata
DEFAULT_LEAD_TIME_MATERIALE = 7   # giorni — fallback quando l'articolo mancante non ha lead time configurato
GIORNI_LAVORATIVI_SETTIMANA = 5
GIORNI_LAVORATIVI_MESE = 22
GIORNI_LAVORATIVI_ANNO = 260


def _aggiungi_giorni_lavorativi(inizio, giorni_decimali):
    """Somma un numero (anche frazionario) di giorni LAVORATIVI a una data,
    saltando sabato e domenica — calendario semplice, niente festività."""
    if giorni_decimali <= 0:
        return inizio
    interi = int(giorni_decimali)
    frazione = giorni_decimali - interi
    cursore = inizio
    aggiunti = 0
    while aggiunti < interi:
        cursore += timedelta(days=1)
        if cursore.weekday() < 5:
            aggiunti += 1
    if frazione > 0:
        cursore += timedelta(days=frazione)
    return cursore


def _capacita_giornaliera_ore(centro):
    """Ore/giorno lavorativo disponibili su questo centro, oppure None se non configurato.
    'ore_teoriche_periodo' è per SINGOLA risorsa sull'intero periodo scelto
    (settimanale/mensile/annuale) — qui si moltiplica per n_risorse_equivalenti
    (la frazione di persona: 0.25/0.5/1.25/2.75...) e per l'efficienza %, poi si
    divide per i giorni lavorativi di quello stesso periodo."""
    if not centro or not centro.ore_teoriche_periodo:
        return None
    if centro.periodo_riferimento == 'settimanale':
        giorni_periodo = GIORNI_LAVORATIVI_SETTIMANA
    elif centro.periodo_riferimento == 'annuale':
        giorni_periodo = GIORNI_LAVORATIVI_ANNO
    else:
        giorni_periodo = GIORNI_LAVORATIVI_MESE
    ore_totali = (centro.ore_teoriche_periodo * (centro.n_risorse_equivalenti or 1)
                  * ((centro.pct_efficienza or 100) / 100))
    return ore_totali / giorni_periodo if giorni_periodo else None


FASI_STANDARD = [
    ('taglio', ['sega', 'taglio', 'punzon', 'laser']),
    ('sgola_sati', ['sgol', 'satin']),
    ('piega', ['pieg', 'press']),
    ('saldatura', ['sald']),
]


def _fasi_routing_op(cicli_per_codice, centri):
    """
    Per le 4 colonne standard della tabella (Taglio / Sgola-Sati / Piega /
    Saldatura, come nel vecchio Gantt di Angelo): True se QUALCHE codice
    del routing di questo OP passa da un centro il cui nome corrisponde a
    quella fase (stesso riconoscimento per parola chiave già usato altrove
    nell'app, es. get_macchine_monitor/totem_tabella), False se non gli
    serve — non è "già fatto/da fare", è "gli serve o no questo reparto".
    """
    presenti = set()
    for cicli in cicli_per_codice:
        for ciclo in cicli:
            centro = centri.get(ciclo.centro_costo_id)
            if not centro or centro.esterno:
                continue
            nome_l = (centro.nome or '').lower()
            for fase, parole in FASI_STANDARD:
                if any(p in nome_l for p in parole):
                    presenti.add(fase)
    return {fase: (fase in presenti) for fase, _ in FASI_STANDARD}


def calcola_avanzamento_commesse():
    """Ritorna una lista di dict, una per OP aperto, ordinata come la coda di
    priorità già usata nel resto dell'app (stessa priorità = stessa data di
    consegna stimata sarebbe fuorviante: qui l'ordine riflette chi viene
    servito per primo sui centri condivisi)."""
    from models import (db, OrdineProduzione, CicloLavoroWood, CentroCostoWood,
                         EventoConsuntivoPP, ArticoloApprovvigionamento, FotoArticolo)
    from blueprints.magazzino.routes import (_esplodi_componenti_op, _carica_mappa_distinta_base_wood,
                         _residuo_giacenza_progressivo, _netta_e_esplodi_wood, STATI_CHE_IMPEGNANO)

    oggi = datetime.now()

    ordini = (OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO))
              .order_by(OrdineProduzione.priorita.asc(), OrdineProduzione.id.asc()).all())
    if not ordini:
        return []

    mappa_distinta = _carica_mappa_distinta_base_wood()
    residuo_per_op, _ = _residuo_giacenza_progressivo(op_aperti=ordini, mappa=mappa_distinta)
    centri = {c.id: c for c in CentroCostoWood.query.all()}

    # Una sola query per TUTTE le miniature prodotto (la più recente per
    # ogni codice_articolo) invece di una per OP — stesso principio delle
    # ottimizzazioni fatte oggi sul Kanban, qui fin da subito.
    foto_per_codice = {}
    codici_op = {o.codice_articolo for o in ordini}
    if codici_op:
        for f in (FotoArticolo.query.filter(FotoArticolo.codice_articolo.in_(codici_op))
                  .order_by(FotoArticolo.caricato_il.desc()).all()):
            foto_per_codice.setdefault(f.codice_articolo, f.id)

    cursore_centro = {}   # centro_id -> datetime primo momento libero
    risultati = []

    for o in ordini:
        saldo_op = (o.qta_pianificata or 0) - (o.qta_buona or 0)
        if saldo_op <= 0:
            continue

        avvisi = []
        nodi = _esplodi_componenti_op(o, mappa_distinta=mappa_distinta)
        moltiplicatore_per_codice = {n['codice']: n['moltiplicatore'] for n in nodi}

        # ── Materiale mancante per QUESTO OP, rispetto a chi viene prima in coda ──
        # Solo i codici SENZA un proprio Ciclo di Lavoro contano come "materiale
        # da comprare" (leaf acquistati): un codice che ha un routing (es. il
        # prodotto finito stesso, o un semilavorato lavorato internamente) non
        # si "acquista" — la sua "mancanza" viene già risolta dalla sua stessa
        # simulazione di produzione qui sotto, non da un lead time fornitore.
        giacenza_snapshot = dict(residuo_per_op.get(o.id, {}))
        righe_netting = {}
        _netta_e_esplodi_wood(o.codice_articolo, saldo_op, giacenza_snapshot, righe_netting, mappa=mappa_distinta)
        codici_con_ciclo = ({c.codice for c in CicloLavoroWood.query
                             .filter(CicloLavoroWood.codice.in_(list(righe_netting.keys()))).all()}
                             if righe_netting else set())
        materiali_mancanti = {cod: r['mancante'] for cod, r in righe_netting.items()
                              if r.get('mancante', 0) > 0 and cod not in codici_con_ciclo}

        data_materiale_pronto = oggi
        if materiali_mancanti:
            lead_times = []
            for cod in materiali_mancanti:
                art = ArticoloApprovvigionamento.query.filter_by(codice=cod).first()
                lt = art.lead_time_fornitura_giorni if (art and art.lead_time_fornitura_giorni) else None
                if lt is None:
                    avvisi.append(f"Lead time acquisto non configurato per {cod} — stima {DEFAULT_LEAD_TIME_MATERIALE}gg")
                    lt = DEFAULT_LEAD_TIME_MATERIALE
                lead_times.append(lt)
            data_materiale_pronto = oggi + timedelta(days=max(lead_times))

        # ── Simulazione routing interno, componente per componente ──
        fine_produzione_op = oggi
        centri_con_saldo = set()
        tutti_cicli_op = []
        for codice, moltiplicatore in moltiplicatore_per_codice.items():
            cicli = (CicloLavoroWood.query.filter_by(codice=codice)
                     .order_by(CicloLavoroWood.sequenza).all())
            if not cicli:
                continue
            tutti_cicli_op.append(cicli)
            qta_codice = round(saldo_op * moltiplicatore)
            componente_param = None if codice == o.codice_articolo else codice
            cursore_componente = data_materiale_pronto

            for ciclo in cicli:
                centro = centri.get(ciclo.centro_costo_id)
                if not centro or centro.esterno:
                    continue  # l'ultima fase esterna (verniciatura) si gestisce a parte, dopo

                pezzi_fatti = int((db.session.query(db.func.sum(EventoConsuntivoPP.pezzi_buoni))
                                  .filter(EventoConsuntivoPP.op_code == o.codice,
                                          db.func.lower(EventoConsuntivoPP.fase) == centro.nome.strip().lower(),
                                          EventoConsuntivoPP.componente == componente_param if componente_param
                                          else EventoConsuntivoPP.componente.is_(None))
                                  .scalar()) or 0)
                saldo_fase = max(qta_codice - pezzi_fatti, 0)
                if saldo_fase <= 0:
                    continue
                centri_con_saldo.add(centro.nome)

                produttivita = ciclo.produttivita_oraria or 0
                if produttivita <= 0:
                    avvisi.append(f"Tempo standard non configurato per {codice} su {centro.nome}")
                    ore_necessarie = 0.0
                else:
                    ore_necessarie = saldo_fase / produttivita

                cap = _capacita_giornaliera_ore(centro)
                if not cap:
                    avvisi.append(f"Capacità non configurata per {centro.nome} — stima {DEFAULT_ORE_GIORNO:.0f}h/giorno")
                    cap = DEFAULT_ORE_GIORNO

                libero_centro = cursore_centro.get(centro.id, oggi)
                inizio_fase = max(cursore_componente, libero_centro)
                giorni_fase = ore_necessarie / cap if cap else 0
                fine_fase = _aggiungi_giorni_lavorativi(inizio_fase, giorni_fase)

                cursore_centro[centro.id] = fine_fase
                cursore_componente = fine_fase

            if cursore_componente > fine_produzione_op:
                fine_produzione_op = cursore_componente

        # ── Lavorazione esterna (es. verniciatura): primo centro esterno nel ciclo del prodotto padre ──
        centro_esterno_nome = None
        lead_time_esterno = 0
        for ciclo in (CicloLavoroWood.query.filter_by(codice=o.codice_articolo)
                      .order_by(CicloLavoroWood.sequenza).all()):
            centro = centri.get(ciclo.centro_costo_id)
            if centro and centro.esterno:
                centro_esterno_nome = centro.nome
                if centro.lead_time_esterno_giorni:
                    lead_time_esterno = centro.lead_time_esterno_giorni
                else:
                    avvisi.append(f"Lead time esterno non configurato per {centro.nome}")
                break

        data_consegna = fine_produzione_op + timedelta(days=lead_time_esterno) if lead_time_esterno else fine_produzione_op
        pct = round(100 * (o.qta_buona or 0) / o.qta_pianificata) if o.qta_pianificata else 0
        fasi = _fasi_routing_op(tutti_cicli_op, centri)
        foto = foto_per_codice.get(o.codice_articolo)

        risultati.append({
            'op_codice': o.codice, 'commessa': o.commessa or '', 'codice_articolo': o.codice_articolo,
            'descrizione': o.descrizione or '', 'qta_pianificata': o.qta_pianificata, 'qta_buona': o.qta_buona or 0,
            'saldo': saldo_op, 'pct': pct, 'priorita': o.priorita,
            'materiale_completo': not materiali_mancanti,
            'centri_in_coda': sorted(centri_con_saldo),
            'fasi': fasi, 'foto_id': foto,
            'centro_esterno': centro_esterno_nome, 'lead_time_esterno_giorni': lead_time_esterno,
            'data_fine_produzione_stimata': fine_produzione_op.strftime('%d/%m/%Y'),
            'data_consegna_stimata': data_consegna.strftime('%d/%m/%Y'),
            'data_prevista': o.data_prevista.strftime('%d/%m/%Y') if o.data_prevista else None,
            'avvisi': avvisi,
        })

    return risultati
