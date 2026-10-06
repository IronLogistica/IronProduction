"""
BUG REALE segnalato da Mauri, 06/10/2026 — /conto-lavoro/riepilogo (e
potenzialmente /conto-lavoro/ e la lista Ordini) mostravano "Errore di
rete": _quantita_prodotta/_prodotta_riga interrogavano OrdineProduzione una
volta per OGNI riga d'ordine (N+1) invece che una sola volta per tutte —
con molti ordini/righe, centinaia di query sequenziali nella stessa
richiesta rischiavano di far scadere il timeout del server, tornando una
pagina di errore HTML invece di JSON (il fetch().json() del browser la
vede come "Errore di rete").

Verifica che _prodotta_per_codici + la mappa passata a _prodotta_riga/
_ordine_dict tengano il numero di query BASSO e INDIPENDENTE dal numero di
righe (non costante zero: ci sono comunque le query "vere" sui dati), e che
i numeri calcolati restino identici a prima.
"""
import os
import unittest

from flask import Flask
from sqlalchemy import event

from models import db, OrdineProduzione
from blueprints.conto_lavoro.routes import cl_bp
from blueprints.conto_lavoro.models import ClArticoloCliente, ClCliente, ClOrdine, ClOrdineRiga, ClSchemaVersion

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N_ARTICOLI = 25


class TestContoLavoroNPiu1Prodotta(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=os.path.join(RADICE, 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='x', CL_ENABLED=True,
                               SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_BINDS={'conto_lavoro': 'sqlite:///:memory:'},
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app.register_blueprint(cl_bp)

        @self.app.context_processor
        def _g():
            return {'now': '', 'kanban_gruppi': [], 'macchine_monitor': [], 'sidebar_gruppi': []}
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all(bind_key=None)
        db.create_all(bind_key='conto_lavoro')
        db.session.add(ClSchemaVersion(versione=4, descrizione='test'))

        cliente = ClCliente(ragione_sociale='FAZA Srl', piva='01234567890')
        db.session.add(cliente); db.session.flush()

        ordine = ClOrdine(cliente_id=cliente.id, numero_ordine='299/IS', stato='CONFERMATO')
        db.session.add(ordine); db.session.flush()

        # N articoli DIVERSI, ciascuno con un OP che ha già dichiarato
        # qualcosa — serve per dimostrare che la mappa aggregata restituisce
        # il valore GIUSTO per ciascun codice, non solo "meno query".
        for i in range(N_ARTICOLI):
            codice = f'ART{i:03d}'
            art = ClArticoloCliente(cliente_id=cliente.id, codice=codice, descrizione=f'Articolo {i}', udm='PZ')
            db.session.add(art); db.session.flush()
            db.session.add(ClOrdineRiga(ordine_id=ordine.id, n_riga=i + 1, articolo_id=art.id,
                                        descrizione=f'Articolo {i}', quantita=10))
            db.session.add(OrdineProduzione(codice=f'OP-{i}', codice_articolo=codice,
                                            qta_pianificata=10, qta_buona=i, stato='In esecuzione', priorita=1))
        db.session.commit()
        self.c = self.app.test_client()
        self.ordine_id = ordine.id

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def _conta_query(self, azione):
        conteggio = {'n': 0}
        def _conta(*a, **k): conteggio['n'] += 1
        event.listen(db.engine, 'before_cursor_execute', _conta)
        try:
            azione()
        finally:
            event.remove(db.engine, 'before_cursor_execute', _conta)
        return conteggio['n']

    def test_lista_ordini_poche_query_indipendenti_dalle_righe(self):
        n = self._conta_query(lambda: self.c.get('/conto-lavoro/api/ordini'))
        # Prima della fix: 1 query per riga (N_ARTICOLI) SOLO per 'prodotta',
        # più altrettante per r.articolo.codice se non eager-caricato — ben
        # oltre N_ARTICOLI. Dopo: una manciata di query fisse, mai legata al
        # numero di righe.
        self.assertLess(n, N_ARTICOLI, f'{n} query per {N_ARTICOLI} righe: ancora N+1')

    def test_dettaglio_ordine_poche_query_e_dati_corretti(self):
        n = self._conta_query(lambda: setattr(self, '_risposta',
                                               self.c.get(f'/conto-lavoro/api/ordini/{self.ordine_id}')))
        self.assertLess(n, N_ARTICOLI)
        d = self._risposta.get_json()
        righe = {r['codice']: r for r in d['righe']}
        self.assertEqual(righe['ART000']['quantita_prodotta'], 0.0)
        self.assertEqual(righe['ART010']['quantita_prodotta'], 10.0)
        self.assertEqual(righe['ART024']['quantita_prodotta'], 24.0)
        self.assertEqual(righe['ART024']['saldo_da_produrre'], -14.0)

    def test_live_poche_query_e_dati_corretti(self):
        n = self._conta_query(lambda: setattr(self, '_risposta', self.c.get('/conto-lavoro/api/live')))
        self.assertLess(n, N_ARTICOLI)
        gruppo = self._risposta.get_json()[0]
        righe = {r['codice']: r for r in gruppo['ordini'][0]['righe']}
        self.assertEqual(righe['ART005']['quantita_prodotta'], 5.0)

    def test_magazzino_poche_query_e_dati_corretti(self):
        n = self._conta_query(lambda: setattr(self, '_risposta', self.c.get('/conto-lavoro/api/magazzino')))
        self.assertLess(n, N_ARTICOLI)
        gruppo = self._risposta.get_json()[0]
        righe = {a['codice']: a for a in gruppo['articoli']}
        self.assertEqual(righe['ART012']['quantita_prodotta'], 12.0)


if __name__ == '__main__':
    unittest.main()
