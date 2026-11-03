"""
Richiesta Pierantonio (03/11/2026): sulla scheda magazzino il fornitore dei carichi da acquisto,
rettifiche di inventario correggibili senza aggiungere righe, e gli ultimi 50 movimenti in un pop-up.
"""
import unittest
from datetime import datetime, timedelta

from flask import Flask

from models import db, GiacenzaWood, MovimentoGiacenzaWood, DDTCaricoWood
from blueprints.magazzino.routes import magazzino_bp, _registra_movimento_giacenza


class TestMovimentiMagazzinoInfo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        cls.app.register_blueprint(magazzino_bp)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (MovimentoGiacenzaWood, GiacenzaWood, DDTCaricoWood):
                db.session.query(m).delete()
            db.session.add(DDTCaricoWood(filename='ddt_123.pdf', fornitore='ACCIAI ROSSI SRL', numero_ddt='123', confermato=True))
            _registra_movimento_giacenza('PINXFL162', 1000, 'carico_acquisto', riferimento='ddt_123.pdf', note='DDT 123')
            _registra_movimento_giacenza('PINXFL162', -142, 'scarico_produzione', riferimento='OP-1')
            db.session.commit()
        self.c = self.app.test_client()

    def _giacenza(self):
        with self.app.app_context():
            return GiacenzaWood.query.get('PINXFL162').quantita

    def _n_mov(self):
        with self.app.app_context():
            return MovimentoGiacenzaWood.query.filter_by(codice='PINXFL162').count()

    def test_popup_ultimi_movimenti_con_fornitore_e_saldo(self):
        righe = self.c.get('/api/giacenza_wood/PINXFL162/movimenti').get_json()
        self.assertEqual([r['tipo'] for r in righe], ['scarico_produzione', 'carico_acquisto'])
        self.assertEqual(righe[0]['saldo_dopo'], 858)
        self.assertEqual(righe[1]['saldo_dopo'], 1000)
        self.assertEqual(righe[1]['fornitore'], 'ACCIAI ROSSI SRL')
        self.assertFalse(righe[1]['correggibile'])
        self.assertEqual(len(self.c.get('/api/giacenza_wood/PINXFL162/movimenti?limite=1').get_json()), 1)

    def test_scheda_mostra_fornitore(self):
        html = self.c.get('/scheda-magazzino?codice=PINXFL162').get_data(as_text=True)
        self.assertIn('ACCIAI ROSSI SRL', html)

    def test_inventario_rifatto_subito_corregge_la_stessa_riga(self):
        self.c.post('/api/inventario-codice-padre/salva', json={'conteggi': {'PINXFL162': 658}})
        self.assertEqual(self._giacenza(), 658)
        n = self._n_mov()
        # errore di battitura: rifatto dopo pochi minuti con il numero giusto
        self.c.post('/api/inventario-codice-padre/salva', json={'conteggi': {'PINXFL162': 668}})
        self.assertEqual(self._giacenza(), 668)
        self.assertEqual(self._n_mov(), n, 'nessuna riga in più')
        with self.app.app_context():
            ultima = MovimentoGiacenzaWood.query.order_by(MovimentoGiacenzaWood.id.desc()).first()
            self.assertEqual(ultima.quantita, -190)
            self.assertIn('da 858 a 668', ultima.note)
            # dopo mezz'ora un nuovo inventario è una riga nuova
            ultima.creato_il = datetime.utcnow() - timedelta(minutes=45)
            db.session.commit()
        self.c.post('/api/inventario-codice-padre/salva', json={'conteggi': {'PINXFL162': 600}})
        self.assertEqual(self._n_mov(), n + 1)

    def test_modifica_da_magazzino_ricorretta(self):
        self.c.post('/api/giacenza_wood/rettifica', json={'codice': 'PINXFL162', 'quantita': 142, 'nuovo_stock': 1000, 'note': 'Modifica da Magazzino'})
        n = self._n_mov()
        self.c.post('/api/giacenza_wood/rettifica', json={'codice': 'PINXFL162', 'quantita': -100, 'nuovo_stock': 900, 'note': 'Modifica da Magazzino'})
        self.assertEqual(self._giacenza(), 900)
        self.assertEqual(self._n_mov(), n)
        # riportato al valore iniziale: la rettifica sparisce
        self.c.post('/api/giacenza_wood/rettifica', json={'codice': 'PINXFL162', 'quantita': -42, 'nuovo_stock': 858, 'note': 'Modifica da Magazzino'})
        self.assertEqual(self._giacenza(), 858)
        self.assertEqual(self._n_mov(), n - 1)

    def test_correggi_rettifica_vecchia(self):
        self.c.post('/api/inventario/salva', json={'conteggi': {'PINXFL162': 1058}})
        with self.app.app_context():
            mid = MovimentoGiacenzaWood.query.filter_by(tipo='rettifica_inventario').one().id
        self.assertEqual(self._giacenza(), 1058)
        r = self.c.post(f'/api/movimento-giacenza/{mid}/correggi', json={'quantita': 10}).get_json()
        self.assertTrue(r['ok'])
        self.assertEqual(self._giacenza(), 868)
        with self.app.app_context():
            self.assertIn('era +200', MovimentoGiacenzaWood.query.get(mid).note)
            acquisto = MovimentoGiacenzaWood.query.filter_by(tipo='carico_acquisto').one().id
        self.assertEqual(self.c.post(f'/api/movimento-giacenza/{acquisto}/correggi', json={'quantita': 5}).status_code, 400,
                         'i carichi da documento non si correggono da qui')


if __name__ == '__main__':
    unittest.main()
