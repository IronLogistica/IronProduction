"""
TOTEM UFFICIO ACQUISTI E LOGISTICA (richiesta Mauri, 13/10/2026).

Postazione di lavoro di Claudio e Grazia per la gestione ordinaria: una
DASHBOARD con le notifiche KPI di tutti i reparti, e le sezioni Kanban
(con Kanban HPI), Terzisti, Acquisti, Inventari & Fabbisogni — che aprono
dentro il totem le pagine GIA' esistenti del programma (nessuna logica
duplicata: i numeri sono quelli delle stesse funzioni usate altrove).

Ogni KPI e' calcolato in un blocco separato: se uno fallisce, la dashboard
mostra "n.d." per quello e continua con gli altri — mai una pagina bianca.
"""
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, render_template

from models import (db, OrdineProduzione, EventoConsuntivoPP, GiacenzaWood, LavorazioneTerzista,
                    OrdineAcquistoWood, SostituzioneMaterialeOP)

totem_acquisti_bp = Blueprint('totem_acquisti', __name__)

STATI_TERZISTI_APERTI = ('ATTESA_RIENTRO', 'IN_RITARDO', 'PARZIALE')


def _data_it(s):
    try:
        return datetime.strptime((s or '').strip(), '%d/%m/%Y').date()
    except ValueError:
        return None


def _pl(n, singolare, plurale):
    return f'{n} {singolare if n == 1 else plurale}'


def _kpi(chiave, titolo, valore, livello, testo, sezione, url, reparto, dettaglio=''):
    """livello: 'rosso' (da fare subito), 'arancio' (attenzione), 'blu' (info), 'verde' (tutto ok)."""
    return {'chiave': chiave, 'titolo': titolo, 'valore': valore, 'livello': livello, 'testo': testo,
            'sezione': sezione, 'url': url, 'reparto': reparto, 'dettaglio': dettaglio}


def _kpi_kanban():
    from blueprints.kanban.routes import api_kanban_hpi_dati
    dati = api_kanban_hpi_dati().get_json()
    gruppi = dati if isinstance(dati, list) else dati.get('gruppi', dati.get('categorie', []))
    prodotti = [p for g in gruppi for p in g.get('prodotti', [])]
    rossi = [p for p in prodotti if p.get('stato_colore') == 'red']
    scorta = [p for p in prodotti if p.get('stato_codice') == 'pianifica_scorta']
    in_vern = [p for p in prodotti if p.get('stato_colore') == 'yellow']
    nomi = lambda lst: ', '.join(p['nome'].split(' — ')[0] for p in lst[:6]) + (' …' if len(lst) > 6 else '')
    return [
        _kpi('kanban_fare_produzione', 'Ordini clienti scoperti', len(rossi),
             'rosso' if rossi else 'verde',
             _pl(len(rossi), 'prodotto', 'prodotti') + ' con ordini clienti NON coperti: serve produzione' if rossi
             else 'Tutti gli ordini clienti sono coperti', 'kanban', '/kanban-hpi', 'Kanban', nomi(rossi)),
        _kpi('kanban_sotto_scorta', 'Prodotti sotto scorta', len(scorta),
             'arancio' if scorta else 'verde',
             _pl(len(scorta), 'prodotto', 'prodotti') + ' sotto scorta minima: pianificare produzione' if scorta
             else 'Nessun prodotto sotto scorta', 'kanban', '/kanban-hpi', 'Kanban', nomi(scorta)),
        _kpi('kanban_dopo_verniciatura', 'In attesa di verniciatura', len(in_vern),
             'blu', _pl(len(in_vern), 'prodotto evadibile', 'prodotti evadibili') + ' dopo la verniciatura', 'kanban', '/kanban-hpi',
             'Kanban', nomi(in_vern)),
    ]


def _kpi_acquisti(oggi):
    aperti = OrdineAcquistoWood.query.filter(OrdineAcquistoWood.stato_label != 'ORDINE_RICEVUTO').all()
    ritardo = [o for o in aperti if o.data_consegna and o.data_consegna < oggi]
    settimana = [o for o in aperti if o.data_consegna and oggi <= o.data_consegna <= oggi + timedelta(days=7)]
    da_confermare = [o for o in aperti if (o.stato_label or '') == 'DA_CONFERMARE']
    forn = lambda lst: ', '.join(f'{o.fornitore} n.{o.ordine_n}' for o in lst[:5]) + (' …' if len(lst) > 5 else '')
    return [
        _kpi('oa_ritardo', 'Ordini fornitore in ritardo', len(ritardo), 'rosso' if ritardo else 'verde',
             _pl(len(ritardo), 'ordine fornitore', 'ordini fornitore') + ' oltre la data di consegna: sollecitare' if ritardo
             else 'Nessun ordine fornitore in ritardo', 'acquisti', '/ordini-acquisto-wood', 'Acquisti', forn(ritardo)),
        _kpi('oa_da_confermare', 'Ordini da confermare', len(da_confermare), 'arancio' if da_confermare else 'verde',
             _pl(len(da_confermare), 'ordine fornitore', 'ordini fornitore') + ' senza conferma' if da_confermare
             else 'Tutti gli ordini fornitore sono confermati', 'acquisti', '/ordini-acquisto-wood', 'Acquisti',
             forn(da_confermare)),
        _kpi('oa_settimana', 'Consegne attese in 7 giorni', len(settimana), 'blu',
             _pl(len(settimana), 'consegna fornitore attesa', 'consegne fornitori attese') + f' entro una settimana (su {len(aperti)} ordini aperti)',
             'acquisti', '/materiale-in-arrivo', 'Acquisti', forn(settimana)),
    ]


def _kpi_fabbisogni():
    from blueprints.acquisti_wood.routes import calcola_lista_fabbisogni
    righe = calcola_lista_fabbisogni()
    da_ordinare = [r for r in righe if r.get('stato') == 'DA_ORDINARE']
    return [_kpi('fabbisogni_da_ordinare', 'Materiali da ordinare', len(da_ordinare),
                 'rosso' if da_ordinare else 'verde',
                 _pl(len(da_ordinare), 'codice mancante', 'codici mancanti') + ' per produzione o scorta: da ordinare' if da_ordinare
                 else 'Nessun materiale da ordinare', 'inventari', '/lista-fabbisogni', 'Acquisti',
                 ', '.join(r.get('codice', '') for r in da_ordinare[:8]) + (' …' if len(da_ordinare) > 8 else ''))]


def _kpi_alert_scorte_padre():
    from blueprints.magazzino.routes import calcola_alert_fabbisogno_codici_padre
    n = len(calcola_alert_fabbisogno_codici_padre())
    return [_kpi('alert_scorte_padre', 'Codici padre in allarme', n, 'arancio' if n else 'verde',
                 _pl(n, 'codice padre', 'codici padre') + ' sotto il lead time di rifornimento' if n else 'Lead time rifornimento ok',
                 'kanban', '/alert-scorte', 'Pianificazione')]


def _kpi_terzisti(oggi):
    aperte = LavorazioneTerzista.query.filter(LavorazioneTerzista.stato.in_(STATI_TERZISTI_APERTI)).all()
    ritardo = [l for l in aperte if _data_it(l.data_rientro_prev) and _data_it(l.data_rientro_prev) < oggi]
    in_arrivo = [l for l in aperte if _data_it(l.data_rientro_prev)
                 and oggi <= _data_it(l.data_rientro_prev) <= oggi + timedelta(days=3)]
    ddt = lambda lst: ', '.join(f'DDT {l.ddt_uscita}' for l in lst[:6]) + (' …' if len(lst) > 6 else '')
    return [
        _kpi('terzisti_ritardo', 'Terzisti in ritardo', len(ritardo), 'rosso' if ritardo else 'verde',
             _pl(len(ritardo), 'spedizione al terzista', 'spedizioni ai terzisti') + ' oltre la data di rientro prevista' if ritardo
             else 'Nessun rientro terzisti in ritardo', 'terzisti', '/terzisti/spedizioni', 'Terzisti', ddt(ritardo)),
        _kpi('terzisti_aperti', 'Lavorazioni presso terzisti', len(aperte), 'blu',
             _pl(len(aperte), 'spedizione ancora da rientrare', 'spedizioni ancora da rientrare') + f', {len(in_arrivo)} attese entro 3 giorni',
             'terzisti', '/terzisti', 'Terzisti', ddt(in_arrivo)),
    ]


def _kpi_produzione(oggi):
    aperti = OrdineProduzione.query.filter(OrdineProduzione.stato != 'Chiuso CO').all()
    ritardo = [o for o in aperti if o.data_prevista and o.data_prevista < oggi
               and o.stato != 'Tecnicamente completato']
    da_approvare = EventoConsuntivoPP.query.filter_by(approvato_direzione=False, visionato=False).count()
    return [
        _kpi('op_ritardo', 'Commesse in ritardo', len(ritardo), 'rosso' if ritardo else 'verde',
             _pl(len(ritardo), 'commessa di produzione', 'commesse di produzione') + ' oltre la data di consegna' if ritardo
             else 'Nessuna commessa in ritardo', 'dashboard', '/ordini-produzione', 'Produzione',
             ', '.join(f'{o.commessa or o.codice} ({o.codice_articolo})' for o in ritardo[:5])),
        _kpi('dichiarazioni_da_approvare', 'Dichiarazioni da approvare', da_approvare,
             'arancio' if da_approvare else 'verde',
             _pl(da_approvare, 'dichiarazione di produzione', 'dichiarazioni di produzione') + ' in attesa della Direzione' if da_approvare
             else 'Nessuna dichiarazione in attesa', 'dashboard', '/dichiarazione-produzione', 'Produzione'),
        _kpi('op_aperti', 'Commesse aperte', len(aperti), 'blu', _pl(len(aperti), 'commessa di produzione aperta', 'commesse di produzione aperte'),
             'dashboard', '/ordini-produzione', 'Produzione'),
    ]


def _kpi_magazzino():
    negativi = GiacenzaWood.query.filter(GiacenzaWood.quantita < 0).all()
    sost = SostituzioneMaterialeOP.query.filter_by(revocata_il=None).count()
    return [
        _kpi('giacenze_negative', 'Giacenze negative', len(negativi), 'rosso' if negativi else 'verde',
             _pl(len(negativi), 'codice', 'codici') + ' con giacenza negativa: verificare carichi/inventario' if negativi
             else 'Nessuna giacenza negativa', 'inventari', '/magazzino', 'Magazzino',
             ', '.join(f'{g.codice} ({g.quantita:g})' for g in negativi[:8]) + (' …' if len(negativi) > 8 else '')),
        _kpi('sostituzioni_materiale', 'Materiali cambiati su commessa', sost, 'blu' if sost else 'verde',
             _pl(sost, 'sostituzione di materiale attiva', 'sostituzioni di materiale attive') + ' su commesse aperte' if sost
             else 'Nessuna sostituzione materiale attiva', 'dashboard', '/ordini-produzione', 'Produzione'),
    ]


@totem_acquisti_bp.route('/totem/acquisti-logistica')
def totem_acquisti_logistica():
    try:
        from models import get_kanban_gruppi
        gruppi = get_kanban_gruppi()
    except Exception:
        db.session.rollback()
        gruppi = []
    return render_template('totem_acquisti/totem.html', kanban_gruppi=gruppi)


@totem_acquisti_bp.route('/api/totem-acquisti/kpi')
def api_totem_acquisti_kpi():
    oggi = date.today()
    kpi, errori = [], []
    blocchi = [('Kanban', _kpi_kanban), ('Acquisti', lambda: _kpi_acquisti(oggi)), ('Fabbisogni', _kpi_fabbisogni),
               ('Alert scorte', _kpi_alert_scorte_padre), ('Terzisti', lambda: _kpi_terzisti(oggi)),
               ('Produzione', lambda: _kpi_produzione(oggi)), ('Magazzino', _kpi_magazzino)]
    for nome, fn in blocchi:
        try:
            kpi.extend(fn())
        except Exception as e:  # un KPI rotto non deve mai bloccare la dashboard
            db.session.rollback()
            errori.append(f'{nome}: {e}')
    ordine = {'rosso': 0, 'arancio': 1, 'blu': 2, 'verde': 3}
    notifiche = sorted([k for k in kpi if k['livello'] in ('rosso', 'arancio') and k['valore']],
                       key=lambda k: ordine[k['livello']])
    return jsonify(kpi=kpi, notifiche=notifiche, errori=errori,
                   aggiornato_il=datetime.now().strftime('%d/%m/%Y %H:%M:%S'))
