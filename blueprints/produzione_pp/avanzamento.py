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


# 5 zone di reparto, ognuna raggruppa una o più macchine (centri di costo) —
# stesso schema usato sul cartaceo dall'ufficio ("1 Taglio, 2 Fori,
# 3 Curvatura, 4 Sgola-Sati, 5 Saldatura"). L'ordine qui è l'ordine delle
# colonne nella tabella Avanzamento Commesse.
ZONE_STANDARD = [
    ('taglio', 'Taglio', ['sega']),
    ('fori', 'Fori', ['pressopieg', 'punzon', 'trapan']),
    ('curvatura', 'Curvatura', ['calandr', 'curvatub']),
    ('sgola_sati', 'Sgola/Sati', ['satin', 'sgol']),
    ('saldatura', 'Saldatura', ['sald']),
]


def _zona_di_centro(centro):
    """Ritorna la chiave-zona (taglio/fori/curvatura/sgola_sati/saldatura) a
    cui appartiene questo centro di costo, individuata per parola chiave nel
    nome (stesso riconoscimento già usato altrove nell'app) — None se il
    centro non rientra in nessuna delle 5 zone standard."""
    nome_l = (centro.nome or '').lower()
    for zona, _, parole in ZONE_STANDARD:
        if any(p in nome_l for p in parole):
            return zona
    return None


def calcola_avanzamento_commesse():
    """Ritorna (risultati, segmenti_per_centro): risultati è la lista per OP
    già in uso da Avanzamento Commesse; segmenti_per_centro è la STESSA
    simulazione riorganizzata per centro di costo invece che per OP — un
    segmento (op_codice, codice, inizio, fine, ore) per ogni fase
    schedulata su quel centro, nello stesso ordine di coda (priorità →
    SequenzaAvanzamentoKPI se Angelo ha trascinato manualmente) — usata dal
    Gantt per centro di costo. Un'unica simulazione, mai due calcoli
    separati che potrebbero disallinearsi (stesso principio già applicato
    più volte oggi ad altri bug di questo tipo).
    Ordinata come la coda di priorità già usata nel resto dell'app (stessa
    priorità = stessa data di consegna stimata sarebbe fuorviante: qui
    l'ordine riflette chi viene servito per primo sui centri condivisi)."""
    from models import (db, OrdineProduzione, CicloLavoroWood, CentroCostoWood,
                         EventoConsuntivoPP, ArticoloApprovvigionamento, FotoArticolo,
                         SequenzaAvanzamentoKPI)
    from blueprints.magazzino.routes import (_esplodi_componenti_op, _carica_mappa_distinta_base_wood,
                         _residuo_giacenza_progressivo, _netta_e_esplodi_wood, STATI_CHE_IMPEGNANO)

    oggi = datetime.now()

    ordini = OrdineProduzione.query.filter(OrdineProduzione.stato.in_(STATI_CHE_IMPEGNANO)).all()
    if not ordini:
        return [], {}

    # Ordine di visualizzazione — e di SIMULAZIONE (chi viene servito prima
    # sui centri condivisi, quindi anche le date stimate ne dipendono): se
    # Angelo ha trascinato una commessa in una posizione precisa, quella
    # vince; altrimenti si usa l'ordine di default (priorità OP, poi data
    # di consegna) — stesso identico principio già in uso per il riordino
    # manuale di una coda macchina nel Monitor (SequenzaMonitorMacchina).
    posizioni_manuali = {s.ordine_produzione_id: s.posizione for s in SequenzaAvanzamentoKPI.query.all()}
    ordini.sort(key=lambda o: (0, posizioni_manuali[o.id]) if o.id in posizioni_manuali
                else (1, o.priorita, o.data_prevista or oggi.date(), o.id))

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
    segmenti_per_centro = {}   # centro_id -> [segmento, ...], stesso ordine di coda

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
        inizio_produzione_op = None
        centri_con_saldo = set()
        tutti_cicli_op = []
        zona_totale = {z: 0 for z, _, _ in ZONE_STANDARD}
        zona_fatti = {z: 0 for z, _, _ in ZONE_STANDARD}
        for codice, moltiplicatore in moltiplicatore_per_codice.items():
            cicli = (CicloLavoroWood.query.filter_by(codice=codice)
                     .order_by(CicloLavoroWood.sequenza).all())
            if not cicli:
                continue
            tutti_cicli_op.append(cicli)
            qta_codice = round(saldo_op * moltiplicatore)
            # Denominatore SEPARATO per la percentuale di zona: deve essere
            # la quantità TOTALE pianificata per questo componente, non il
            # residuo attuale (qta_codice sopra, che si restringe mano a
            # mano che l'OP avanza). BUG REALE CORRETTO: usando qta_codice
            # (il residuo) anche per la percentuale di zona, una fase che
            # aveva già lavorato PIÙ del residuo ATTUALE (perché è una fase
            # precoce nel ciclo, già passata avanti rispetto al collo di
            # bottiglia più a valle) risultava SEMPRE bloccata al 100% —
            # anche quando in realtà non aveva ancora lavorato l'intera
            # quantità originale dell'ordine. Con questo fisso, la
            # percentuale di zona è coerente con 'Saldo da produrre': non
            # può mai segnare 100% verde se l'ordine ha ancora un residuo
            # complessivo diverso da zero PER QUELLA fase.
            qta_codice_pianificata = round((o.qta_pianificata or 0) * moltiplicatore)
            componente_param = None if codice == o.codice_articolo else codice
            cursore_componente = data_materiale_pronto
            # Separato da cursore_componente apposta: quest'ultimo può
            # ora "correre avanti" prima della fine reale (vedi Lotto di
            # Trasferimento sotto) — ma il completamento VERO di questo
            # componente (per dire quando l'intero OP è davvero finito,
            # fine_produzione_op) deve sempre aspettare la fine PIENA
            # dell'ultima fase, mai il lotto minimo di transito.
            ultima_fine_fase_vera = data_materiale_pronto

            for ciclo in cicli:
                centro = centri.get(ciclo.centro_costo_id)
                if not centro or centro.esterno:
                    continue  # l'ultima fase esterna (verniciatura) si gestisce a parte, dopo

                # Confronto TOLLERANTE, non un'uguaglianza esatta — stesso
                # bug reale già trovato e corretto altrove (_e_prima_fase_
                # del_ciclo/_e_ultima_fase_del_ciclo): MasterWork manda in
                # 'fase' un testo libero configurato per articolo (es.
                # "Saldatura Frontale c/Assemblaggio"), non necessariamente
                # identico al nome del Centro di Costo qui ("Saldatura").
                # Un confronto esatto qui faceva risultare pezzi_fatti=0
                # anche quando in realtà erano stati dichiarati, facendo
                # apparire una zona ferma allo 0% invece del suo vero
                # avanzamento.
                from blueprints.produzione_pp.routes import _fasi_corrispondono
                eventi_riga = EventoConsuntivoPP.query.filter(
                    EventoConsuntivoPP.op_code == o.codice,
                    EventoConsuntivoPP.componente == componente_param if componente_param
                    else EventoConsuntivoPP.componente.is_(None)
                ).all()
                pezzi_fatti = sum(e.pezzi_buoni or 0 for e in eventi_riga if _fasi_corrispondono(centro.nome, e.fase))

                # Avanzamento per ZONA (tabella Avanzamento Commesse): conta
                # SEMPRE, anche per una fase già completata al 100% (che qui
                # sotto uscirebbe subito col 'continue' perché non c'è più
                # nulla da schedulare) — altrimenti una fase finita farebbe
                # sparire il suo contributo dalla percentuale della zona.
                zona = _zona_di_centro(centro)
                if zona:
                    zona_totale[zona] += qta_codice_pianificata
                    zona_fatti[zona] += min(pezzi_fatti, qta_codice_pianificata)

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
                    # BUG REALE CORRETTO: senza questo, un centro senza
                    # 'ore_teoriche_periodo' configurato ignorava del
                    # tutto n_risorse_equivalenti nel fallback — mettere
                    # 6 persone invece di 1 non cambiava NULLA la stima,
                    # sempre bloccata a DEFAULT_ORE_GIORNO fisso. Ora la
                    # stima di default scala comunque con le risorse
                    # assegnate, anche prima di configurare la capacità
                    # precisa (ore_teoriche_periodo/periodo_riferimento).
                    cap = DEFAULT_ORE_GIORNO * (centro.n_risorse_equivalenti or 1)
                    avvisi.append(f"Capacità non configurata per {centro.nome} — stima "
                                  f"{DEFAULT_ORE_GIORNO:.0f}h/giorno × {centro.n_risorse_equivalenti or 1} risorse = {cap:.1f}h/giorno")

                libero_centro = cursore_centro.get(centro.id, oggi)
                inizio_fase = max(cursore_componente, libero_centro)
                if inizio_produzione_op is None or inizio_fase < inizio_produzione_op:
                    inizio_produzione_op = inizio_fase
                giorni_fase = ore_necessarie / cap if cap else 0
                fine_fase = _aggiungi_giorni_lavorativi(inizio_fase, giorni_fase)

                cursore_centro[centro.id] = fine_fase          # la MACCHINA si libera solo a fine lotto intero — non lavora due OP insieme
                ultima_fine_fase_vera = fine_fase               # il completamento VERO di questo componente, mai anticipato

                # LOTTO DI TRASFERIMENTO: se configurato ed è più piccolo
                # del residuo di questa fase, la fase SUCCESSIVA dello
                # stesso componente può iniziare non appena il primo
                # lotto è pronto — non deve aspettare che QUESTA fase
                # finisca l'intera quantità. Senza questo, il Gantt
                # sarebbe sempre rigidamente sequenziale (fase per fase,
                # mai in pipeline), irrealistico per qualunque lavorazione
                # dove i pezzi passano al reparto successivo mano a mano
                # che sono pronti, non tutti insieme alla fine.
                lotto_trasf = ciclo.lotto_trasferimento_minimo
                if lotto_trasf and produttivita > 0 and lotto_trasf < saldo_fase:
                    giorni_primo_lotto = (lotto_trasf / produttivita) / cap if cap else 0
                    cursore_componente = _aggiungi_giorni_lavorativi(inizio_fase, giorni_primo_lotto)
                else:
                    cursore_componente = fine_fase
                segmenti_per_centro.setdefault(centro.id, []).append({
                    'op_codice': o.codice, 'commessa': o.commessa or '', 'cliente': o.cliente or '',
                    'codice': codice, 'codice_articolo': o.codice_articolo,
                    'descrizione': o.descrizione or '',
                    'pezzi': saldo_fase, 'ore': round(ore_necessarie, 2),
                    'inizio_iso': inizio_fase.strftime('%Y-%m-%d'), 'fine_iso': fine_fase.strftime('%Y-%m-%d'),
                    'bandiera_stato': o.bandiera_stato, 'priorita': o.priorita,
                })

            if ultima_fine_fase_vera > fine_produzione_op:
                fine_produzione_op = ultima_fine_fase_vera

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
        foto = foto_per_codice.get(o.codice_articolo)
        inizio_produzione_op = inizio_produzione_op or oggi

        # Per ogni zona: None = nessun ordine di lavorazione in quella zona
        # per questo OP (colonna bianca con X), altrimenti percentuale
        # fatti/totale arrotondata (0-100).
        zone_pct = {}
        for zona, _, _ in ZONE_STANDARD:
            tot = zona_totale[zona]
            zone_pct[zona] = round(100 * zona_fatti[zona] / tot) if tot > 0 else None

        risultati.append({
            'op_codice': o.codice, 'commessa': o.commessa or '', 'codice_articolo': o.codice_articolo,
            'descrizione': o.descrizione or '', 'qta_pianificata': o.qta_pianificata, 'qta_buona': o.qta_buona or 0,
            'saldo': saldo_op, 'pct': pct, 'priorita': o.priorita, 'bandiera_stato': o.bandiera_stato,
            'materiale_completo': not materiali_mancanti,
            'centri_in_coda': sorted(centri_con_saldo),
            'zone': zone_pct, 'foto_id': foto,
            'centro_esterno': centro_esterno_nome, 'lead_time_esterno_giorni': lead_time_esterno,
            'data_inizio_produzione_stimata': inizio_produzione_op.strftime('%d/%m/%Y'),
            'data_inizio_iso': inizio_produzione_op.strftime('%Y-%m-%d'),
            'data_fine_produzione_stimata': fine_produzione_op.strftime('%d/%m/%Y'),
            'data_fine_produzione_iso': fine_produzione_op.strftime('%Y-%m-%d'),
            'data_consegna_stimata': data_consegna.strftime('%d/%m/%Y'),
            'data_consegna_iso': data_consegna.strftime('%Y-%m-%d'),
            'data_prevista': o.data_prevista.strftime('%d/%m/%Y') if o.data_prevista else None,
            'avvisi': avvisi,
        })

    # Arricchisce ogni segmento (organizzato per centro) con le stesse date
    # di consegna già calcolate per l'OP nel riepilogo sopra — per il
    # Gantt Centri di Costo/Pianificazione Generale, che altrimenti
    # avrebbe solo le date di lavorazione della singola fase, senza sapere
    # se quella commessa arriverà puntuale o in ritardo rispetto a quanto
    # promesso al cliente (o.data_prevista).
    date_per_op = {r['op_codice']: (r['data_prevista'], r['data_consegna_iso'], r['data_consegna_stimata'])
                   for r in risultati}
    for segmenti in segmenti_per_centro.values():
        for seg in segmenti:
            data_prevista, data_consegna_iso, data_consegna_stimata = date_per_op.get(seg['op_codice'], (None, None, None))
            seg['data_prevista'] = data_prevista
            seg['data_consegna_iso'] = data_consegna_iso
            seg['data_consegna_stimata'] = data_consegna_stimata
            seg['in_ritardo'] = bool(data_prevista and data_consegna_iso
                                      and datetime.strptime(data_consegna_iso, '%Y-%m-%d').date()
                                      > datetime.strptime(data_prevista, '%d/%m/%Y').date())

    return risultati, segmenti_per_centro
