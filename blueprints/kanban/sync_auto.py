"""
Riallineamento AUTOMATICO del Kanban da MasterLogistic-WMS (richiesta Mauri,
14/10/2026 — T250DT: impegno inserito in Logistics, ma il Kanban HPI non lo
mostrava finché qualcuno non premeva 🔄 sulla riga della scheda Kanban).

Ogni KANBAN_SYNC_ORE ore (default 4) un thread di sottofondo esegue, prodotto
per prodotto e in sequenza, ESATTAMENTE la logica del pulsante 🔄 della
singola scheda (risincronizza_prodotto_da_wms): riservato clienti, Finiti IS,
grezzi, in trattamento, in produzione, Finiti IW.

Sicurezza:
- un solo giro alla volta fra tutti i worker gunicorn: il lock è un UPDATE
  condizionato su EsecuzioneJobWood (chi lo vince esegue, gli altri no);
- se WMS non risponde per un prodotto, quel prodotto resta com'era (nessun
  azzeramento) e si passa al successivo; commit prodotto per prodotto;
- KANBAN_SYNC_ORE=0 spegne il riallineamento automatico.
"""
import os
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import or_, update

from models import db, log, KanbanProdotto, EsecuzioneJobWood

NOME_JOB = 'kanban_wms_riallineamento'
CONTROLLO_OGNI_SECONDI = 600          # ogni 10 minuti si guarda se è ora
DURATA_MASSIMA_GIRO = timedelta(minutes=45)   # oltre: giro considerato morto, si può ripartire

_avviato_in_pid = None
_lock_thread = threading.Lock()


def ore_intervallo():
    try:
        return float(os.environ.get('KANBAN_SYNC_ORE', '4'))
    except ValueError:
        return 4.0


def _assicura_riga():
    if db.session.get(EsecuzioneJobWood, NOME_JOB) is None:
        try:
            db.session.add(EsecuzioneJobWood(nome=NOME_JOB))
            db.session.commit()
        except Exception:
            db.session.rollback()


def prenota_giro(forza=False):
    """True se QUESTO processo si è aggiudicato il giro (lock atomico in DB)."""
    _assicura_riga()
    ora = datetime.utcnow()
    in_corso_da_poco = EsecuzioneJobWood.ultimo_avvio > ora - DURATA_MASSIMA_GIRO
    non_in_corso = or_(EsecuzioneJobWood.ultimo_avvio.is_(None),
                       EsecuzioneJobWood.ultima_fine >= EsecuzioneJobWood.ultimo_avvio,
                       ~in_corso_da_poco)
    if forza:
        condizione = non_in_corso
    else:
        scaduto = or_(EsecuzioneJobWood.ultimo_avvio.is_(None),
                      EsecuzioneJobWood.ultimo_avvio < ora - timedelta(hours=ore_intervallo()))
        condizione = scaduto & non_in_corso
    res = db.session.execute(update(EsecuzioneJobWood)
                             .where(EsecuzioneJobWood.nome == NOME_JOB, condizione)
                             .values(ultimo_avvio=ora))
    db.session.commit()
    return res.rowcount == 1


def esegui_giro():
    """Un giro completo su tutti i prodotti Kanban. Da chiamare dopo prenota_giro()."""
    from blueprints.kanban.routes import risincronizza_prodotto_da_wms
    from masterlogistic_client import MasterLogisticError
    prodotti = [p for p in KanbanProdotto.query.order_by(KanbanProdotto.id).all()
                if p.prodotto not in ('Totali',) and not (p.prodotto or '').isdigit()]
    ok = errori = 0
    for p in prodotti:
        try:
            if risincronizza_prodotto_da_wms(p, timeout=8):
                db.session.commit()
                ok += 1
        except MasterLogisticError:
            db.session.rollback()
            errori += 1
        except Exception as e:  # un prodotto problematico non ferma il giro
            db.session.rollback()
            errori += 1
            log(f'Kanban sync automatico: errore su {p.prodotto}: {e}')
    job = db.session.get(EsecuzioneJobWood, NOME_JOB)
    job.ultima_fine = datetime.utcnow()
    job.esito = (f"{ok} {'prodotto aggiornato' if ok == 1 else 'prodotti aggiornati'}"
                 + (f", {errori} non {'raggiungibile' if errori == 1 else 'raggiungibili'} su WMS (lasciati invariati)"
                    if errori else ''))
    db.session.commit()
    log(f'Kanban sync automatico da WMS: {job.esito}')
    return ok, errori


def _ciclo(app):
    while True:
        try:
            with app.app_context():
                if prenota_giro():
                    esegui_giro()
        except Exception as e:
            try:
                with app.app_context():
                    db.session.rollback()
                    log(f'Kanban sync automatico: giro non riuscito: {e}')
            except Exception:
                pass
        time.sleep(CONTROLLO_OGNI_SECONDI)


def avvia_se_serve(app):
    """Avvia il thread UNA volta per processo (gunicorn --preload forka i worker:
    un thread nato nel master non sopravvive, quindi si parte alla prima richiesta)."""
    global _avviato_in_pid
    if app.config.get('TESTING') or ore_intervallo() <= 0:
        return
    with _lock_thread:
        if _avviato_in_pid == os.getpid():
            return
        _avviato_in_pid = os.getpid()
    threading.Thread(target=_ciclo, args=(app,), daemon=True, name='kanban-sync-wms').start()


def stato():
    _assicura_riga()
    job = db.session.get(EsecuzioneJobWood, NOME_JOB)
    fmt = lambda d: d.strftime('%d/%m/%Y %H:%M') + ' UTC' if d else None
    in_corso = bool(job.ultimo_avvio and (not job.ultima_fine or job.ultima_fine < job.ultimo_avvio)
                    and job.ultimo_avvio > datetime.utcnow() - DURATA_MASSIMA_GIRO)
    prossimo = (job.ultimo_avvio + timedelta(hours=ore_intervallo())) if job.ultimo_avvio else None
    return {'ogni_ore': ore_intervallo(), 'ultimo_avvio': fmt(job.ultimo_avvio), 'ultima_fine': fmt(job.ultima_fine),
            'ultimo_avvio_iso': job.ultimo_avvio.isoformat() + 'Z' if job.ultimo_avvio else None,
            'ultima_fine_iso': job.ultima_fine.isoformat() + 'Z' if job.ultima_fine else None,
            'prossimo_iso': prossimo.isoformat() + 'Z' if prossimo else None,
            'esito': job.esito or '', 'in_corso': in_corso}
