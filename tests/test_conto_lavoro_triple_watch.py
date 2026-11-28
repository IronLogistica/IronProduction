"""Triple Watch (Mauri, 28/11/2026): DDT di vendita e fatture letti dai PDF Zucchetti e agganciati
all'ordine cliente; ordine inserito a mano agganciato poi dal PDF. Serve un Postgres locale."""
import io
import os
import unittest
from urllib.parse import urlsplit

from flask import Flask
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine

from models import db
from blueprints.conto_lavoro import migra
from blueprints.conto_lavoro import models as clm
from blueprints.conto_lavoro.routes import cl_bp
from blueprints.conto_lavoro.importa_documenti import estrai_documenti

PG_URL = os.environ.get('CL_TEST_PG_URL', '')
PG_LOCALE = urlsplit(PG_URL).hostname in ('localhost', '127.0.0.1') if PG_URL else False
RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pdf(*pagine):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for righe in pagine:
        y = 800
        for r in righe.strip().split('\n'):
            c.drawString(40, y, r.strip())
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


DDT_617 = """IRON WOOD SRLS
Documento di trasporto
617 /DDT
Data documento
20/11/2026
Destinatario:
1023
FAZA SRL
Partita IVA o codice fiscale 01766260547
Rif. n. cliente 611 /DDT Del: 10/11/2026
Ns. doc.(ORDCL) n.: 293 del 12/11/2026
AS_001693 TELAIO FRANGIZOLLE FCH n. 3,000
AS_001695 ASSALE RUOTE FCH n. 2,000
Vettore Aspetto dei beni Causale del trasporto"""
FATT_45 = """IRON WOOD SRLS
FATTURA
45 /FT
Data documento 30/11/2026
Spett.le FAZA SRL
Partita IVA 01766260547
Rif. Ns. DDT n. 617 del 20/11/2026
AS_001693 TELAIO FRANGIZOLLE FCH n. 3,000 25,00 75,00
Imponibile 75,00
Totale documento 91,50
Scadenze"""
FATT_46 = FATT_45.replace('45 /FT', '46 /FT').replace('DDT n. 617', 'DDT n. 999').replace('91,50', '10,00')


class TestLettura(unittest.TestCase):
    def test_pdf_con_piu_fatture(self):
        docs, err = estrai_documenti(pdf(FATT_45, FATT_46))
        self.assertIsNone(err)
        self.assertEqual([(d['tipo'], d['numero'], d['importo']) for d in docs],
                         [('FATTURA', '45', 91.5), ('FATTURA', '46', 10.0)])
        self.assertIn(('DDT', '617'), docs[0]['riferimenti'])
        self.assertNotIn(('DDT', '611'), docs[0]['riferimenti'])

    def test_ddt_vendita(self):
        d = estrai_documenti(pdf(DDT_617))[0][0]
        self.assertEqual((d['tipo'], d['numero'], d['data'], d['cliente_piva']), ('DDT', '617', '20/11/2026', '01766260547'))
        self.assertEqual(d['riferimenti'], [('ORDINE', '293')])
        self.assertEqual([(r['codice'], r['quantita'], r['ordine_rif']) for r in d['righe']],
                         [('AS_001693', 3.0, '293'), ('AS_001695', 2.0, '293')])


@unittest.skipUnless(PG_LOCALE, 'serve CL_TEST_PG_URL verso un Postgres LOCALE usa e getta')
class TestTripleWatch(unittest.TestCase):
    def setUp(self):
        eng = create_engine(PG_URL)
        with eng.begin() as c:
            c.exec_driver_sql('DROP SCHEMA IF EXISTS conto_lavoro CASCADE')
        eng.dispose()
        migra.applica_su_con_lock(PG_URL, migra.MIGRAZIONI[-1], out=lambda *_: None)
        self.app = Flask(__name__, template_folder=os.path.join(RADICE, 'templates'))
        self.app.config.update(TESTING=True, CL_ENABLED=True, SECRET_KEY='t', CL_PIN_ANGELO='1357',
                               SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SQLALCHEMY_TRACK_MODIFICATIONS=False,
                               SQLALCHEMY_BINDS={'conto_lavoro': {'url': PG_URL, 'execution_options':
                                                 {'schema_translate_map': {None: 'conto_lavoro'}}}})
        db.init_app(self.app)
        self.app.register_blueprint(cl_bp)
        with self.app.app_context():
            db.create_all(bind_key=None)
            c = clm.ClCliente(ragione_sociale='FAZA Srl', piva='01766260547')
            db.session.add(c); db.session.flush()
            arts = [clm.ClArticoloCliente(cliente_id=c.id, codice=k, descrizione=k, udm='PZ')
                    for k in ('AS_001693', 'AS_001695')]
            db.session.add_all(arts); db.session.flush()
            o = clm.ClOrdine(cliente_id=c.id, numero_ordine='293/IS', stato='CONFERMATO')
            db.session.add(o); db.session.flush()
            db.session.add_all([clm.ClOrdineRiga(ordine_id=o.id, n_riga=1, articolo_id=arts[0].id, quantita=3),
                                clm.ClOrdineRiga(ordine_id=o.id, n_riga=2, articolo_id=arts[1].id, quantita=5)])
            db.session.commit()
            self.cid = c.id
        self.c = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            for e in db.engines.values():
                e.dispose()
        eng = create_engine(PG_URL)
        with eng.begin() as c:
            c.exec_driver_sql('DROP SCHEMA IF EXISTS conto_lavoro CASCADE')
        eng.dispose()

    def _carica(self, contenuto, nome, conferma=True):
        return self.c.post('/conto-lavoro/api/triple-watch/importa', data={
            'files': (io.BytesIO(contenuto), nome), 'tipo': 'AUTO', 'conferma': 'true' if conferma else 'false'},
            content_type='multipart/form-data').json

    def _ordine(self):
        q = self.c.get('/conto-lavoro/api/triple-watch/quadro').json
        return q, next(o for o in q['ordini'] if o['numero_ordine'] == '293/IS')

    def test_ordine_ddt_fattura(self):
        r = self._carica(pdf(DDT_617), 'ddt.pdf', conferma=False)
        self.assertTrue(r['anteprima'])
        self.assertEqual(r['documenti'][0]['ordini_trovati'], ['293/IS'])
        _, o = self._ordine()
        self.assertEqual(o['esito'], 'NESSUN_DDT')                     # l'anteprima non salva
        self.assertEqual(self._carica(pdf(DDT_617), 'ddt.pdf')['nuovi'], 1)
        _, o = self._ordine()
        self.assertEqual((o['esito'], o['spedito'], o['ordinato']), ('DA_FATTURARE', 5.0, 8.0))
        self.assertEqual(self._carica(pdf(FATT_45, FATT_46), 'fatture_novembre.pdf')['nuovi'], 2)
        q, o = self._ordine()
        self.assertEqual((o['esito'], [f['numero'] for f in o['fatture']]), ('PARZIALE', ['45']))
        self.assertEqual([f['numero'] for f in q['fatture_senza_abbinamento']], ['46'])
        self.assertEqual(self._carica(pdf(FATT_45), 'f45.pdf')['aggiornati'], 1)   # ricaricare non duplica

    def test_ordine_a_mano_poi_pdf(self):
        corpo = {'cliente_id': self.cid, 'numero_ordine': '300/IS', 'righe': [{'codice': 'URG-9', 'quantita': 4}]}
        self.assertEqual(self.c.post('/conto-lavoro/api/ordini/manuale', json=corpo).status_code, 403)
        self.c.post('/conto-lavoro/api/angelo/accesso', json={'pin': '1357'})
        r = self.c.post('/conto-lavoro/api/ordini/manuale', json=corpo).json
        self.assertTrue(r['ok'])
        self.assertEqual((r['ordine']['stato'], r['codici_creati']), ('CONFERMATO', ['URG-9']))
        self.assertEqual(self.c.post('/conto-lavoro/api/ordini/manuale', json=corpo).status_code, 409)


if __name__ == '__main__':
    unittest.main()
