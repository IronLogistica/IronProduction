#!/usr/bin/env python3
"""
seed_demo_costo_standard.py — Popola uno scenario dimostrativo completo del
flusso Costo Standard SAP-style: distinta base + ciclo di lavoro → calcolo e
congelamento dello standard → rilascio OP → consuntivo con scostamenti reali
voluti (per mostrare tutte le varianze) → generazione movimenti contabili.

Eseguire DOPO il primo avvio dell'app (schema già inizializzato):
    python3 seed_demo_costo_standard.py

Idempotente: rieseguirlo aggiorna i dati demo esistenti invece di duplicarli
(tranne il consuntivo, che non viene mai registrato due volte per lo stesso
event_id — comportamento standard di EventoConsuntivoPP).
"""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(__file__))

from app import app
from models import (db, CentroCostoWood, ArticoloApprovvigionamento, DistintaBaseWood,
                     CicloLavoroWood, GiacenzaWood, OrdineProduzione, LegameCostoStandardOrdineWood,
                     EventoConsuntivoPP, ASA_MASTERWORK, assicura_conti_contabili_wood)
from blueprints.magazzino.routes import _crea_versione_costo_standard
from blueprints.produzione_pp.routes import _registra_evento_consuntivo, _genera_movimenti_contabili_ordine

CODICE_MATERIALE = 'DEMO-MP01'
CODICE_FINITO = 'DEMO-FIN01'
CENTRO_NOME = 'Demo Saldatura'
CODICE_OP = 'OP-DEMO-000001'


def run():
    with app.app_context():
        # 1) Centro di costo demo — tariffa macchina 25 €/h, manodopera 18 €/h
        centro = CentroCostoWood.query.filter_by(nome=CENTRO_NOME).first()
        if not centro:
            centro = CentroCostoWood(nome=CENTRO_NOME, costo_orario=25.0, tariffa_manodopera_diretta_oraria=18.0)
            db.session.add(centro); db.session.flush()
        else:
            centro.costo_orario = 25.0
            centro.tariffa_manodopera_diretta_oraria = 18.0

        # 2) Materiale foglia demo — costo standard 2,00 €/pz
        mat = ArticoloApprovvigionamento.query.filter_by(codice=CODICE_MATERIALE).first()
        if not mat:
            mat = ArticoloApprovvigionamento(codice=CODICE_MATERIALE, tipo_approvvigionamento='ACQUISTO', costo_acquisto_standard=2.00)
            db.session.add(mat)
        else:
            mat.tipo_approvvigionamento = 'ACQUISTO'
            mat.costo_acquisto_standard = 2.00

        # 3) Distinta base: 2 pz di materiale per 1 pz di finito
        bom = DistintaBaseWood.query.filter_by(codice_padre=CODICE_FINITO, codice_figlio=CODICE_MATERIALE).first()
        if not bom:
            bom = DistintaBaseWood(codice_padre=CODICE_FINITO, codice_figlio=CODICE_MATERIALE, quantita=2.0, livello=1)
            db.session.add(bom)
        else:
            bom.quantita = 2.0

        # 4) Ciclo di lavoro: 1 fase, 10 pz/ora → tempo standard 6 min/pz, scarto max 3%
        ciclo = CicloLavoroWood.query.filter_by(codice=CODICE_FINITO, sequenza=1).first()
        if not ciclo:
            ciclo = CicloLavoroWood(codice=CODICE_FINITO, sequenza=1, centro_costo_id=centro.id, produttivita_oraria=10.0, scarto_max_pct=3.0)
            db.session.add(ciclo)
        else:
            ciclo.centro_costo_id = centro.id
            ciclo.produttivita_oraria = 10.0
            ciclo.scarto_max_pct = 3.0

        # 5) Giacenza materiale sufficiente a coprire il fabbisogno
        giac = GiacenzaWood.query.get(CODICE_MATERIALE)
        if not giac:
            giac = GiacenzaWood(codice=CODICE_MATERIALE, quantita=1000)
            db.session.add(giac)
        else:
            giac.quantita = 1000
        db.session.commit()

        # 6) Calcola e salva il costo standard — versione 1, verrà congelata al rilascio
        _, versione = _crea_versione_costo_standard(CODICE_FINITO)
        db.session.commit()
        print(f"1) Costo standard {CODICE_FINITO} v{versione.versione}: "
              f"materiali € {versione.costo_materiali} + lavorazione € {versione.costo_lavorazione} + "
              f"manodopera € {versione.costo_manodopera} + overhead € {versione.costo_overhead} "
              f"= totale € {versione.costo_totale}/pz")

        # 7) Ordine di produzione demo — 100 pz pianificati
        o = OrdineProduzione.query.filter_by(codice=CODICE_OP).first()
        if not o:
            o = OrdineProduzione(codice=CODICE_OP, codice_articolo=CODICE_FINITO, descrizione='Demo costo standard',
                                  cliente='Cliente Demo', commessa='DEMO-01', qta_pianificata=100, qta_buona=0, qta_scarto=0,
                                  stato='Creato', asa=ASA_MASTERWORK, priorita=5, data_inizio=date.today())
            db.session.add(o); db.session.flush()
        db.session.commit()
        print(f"2) Ordine di produzione {o.codice} creato — {o.qta_pianificata} pz pianificati")

        # 8) Rilascio + aggancio allo standard appena calcolato (congelato da qui in poi)
        if o.stato == 'Creato':
            o.stato, o.data_rilascio = 'Rilasciato', datetime.utcnow()
        legame = LegameCostoStandardOrdineWood.query.get(o.codice)
        if not legame:
            legame = LegameCostoStandardOrdineWood(op_code=o.codice)
            db.session.add(legame)
        legame.costo_standard_versione_id = versione.id
        legame.agganciato_il = datetime.utcnow()
        db.session.commit()
        print(f"3) OP rilasciato e agganciato alla versione standard #{versione.versione}")

        # 9) Scenario REALE con scostamenti voluti, per mostrare tutte le varianze:
        #     - consumo materiale maggiore: BOM cambiata DOPO il rilascio a 2,12 pz
        #       (lo standard congelato resta 2,00 — la variazione simula un
        #       cambio ingegneristico o uno scarto di lavorazione non pianificato)
        #     - prezzo materiale aumentato DOPO il rilascio (2,10 invece di 2,00 congelato)
        #     - tempo di lavorazione superiore allo standard (870 min reali contro
        #       600 min standard = 100 pz / 10 pz/h × 60)
        #     - tariffa macchina aumentata DOPO il rilascio (28 invece di 25 congelati)
        bom.quantita = 2.12
        mat.costo_acquisto_standard = 2.10
        centro.costo_orario = 28.0
        db.session.commit()
        print("4) Scostamenti reali applicati: +6% consumo materiale, +5% prezzo materiale, "
              "+45% tempo lavorazione, +12% tariffa macchina")

        event_id = 'demo-evento-001'
        good, scrap, tempo_minuti = 97, 3, 870
        if not EventoConsuntivoPP.query.filter_by(event_id=event_id).first():
            o_lock = OrdineProduzione.query.filter_by(codice=CODICE_OP).with_for_update().first()
            _registra_evento_consuntivo(o_lock, CENTRO_NOME, datetime.utcnow(), good, scrap, tempo_minuti, event_id)
            db.session.commit()
            print(f"5) Consuntivo registrato: {good} pezzi buoni, {scrap} scarto, {tempo_minuti} minuti reali")
        else:
            print("5) Consuntivo demo già presente — non riregistrato (idempotente).")

        # 10) Predisposizione contabile — genera i movimenti in partita doppia
        assicura_conti_contabili_wood()
        n, avvisi = _genera_movimenti_contabili_ordine(o)
        db.session.commit()
        print(f"6) Movimenti contabili generati: {n} righe")
        if avvisi:
            for a in avvisi:
                print(f"   ⚠️ {a}")

        print(f"\nFatto — apri /ordini-produzione/{CODICE_OP}/analisi-costo per il report completo,\n"
              f"e /impostazioni-contabili per compilare i conti (oggi vuoti nella demo).")


if __name__ == '__main__':
    run()
