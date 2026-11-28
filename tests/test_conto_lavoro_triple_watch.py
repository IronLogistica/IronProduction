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


# Formati REALI Zucchetti (PDF di Mauri, 28/11/2026), con dati di fantasia
DDT_REALE = """1Destinazione:
Destinatario:
000567
CLIENTE PROVA Srl
Codice cliente
000567Partita IVA o codice fiscale
03042380547Data documento 28/11/2026
Numero documento 721
D.D.T. - Uscita Merci
Ns. doc.(ORDCL) n.:367 27/11/2026
Vs.doc. n.: 299/DDT/713/T 25/11/2026
R.(ORDCL) vs ord 299 - DDT/713/T
T9US90-00 CARCASSA T9US/EU 90 n. 15,000
PUNTATURA + SALDATURA
Trasporto a mezzo Porto
Vettore Data e ora del trasporto"""
FATT_REALE_P1 = """1Partita IVA
01314940543
Data Fattura
31/10/2026Numero Fattura
119Pag.
Descrizione U.M. Quantita Prezzo Sconti Importo
FATTURA DI VENDITA
Destinatario:
000001
CLIENTE STELLE SpA
Ns. doc.(DDTLA) n.:604 01/10/2026
230279 STELLE KL DX n. 100,000 3,30000 330,00 22
Totale documento  EU
>>> SEGUE >>>"""
FATT_REALE_P2 = """2Partita IVA
01314940543
Data Fattura
31/10/2026Numero Fattura
119Pag.
FATTURA DI VENDITA
Ns. doc.(DDTLA) n.:665 30/10/2026
230340 STELLE K SX n. 100,000 3,30000 330,00 22
Spese di incasso Spese di imballo5.775,00 1.270,50Totale documento  EU
7.045,50"""
ORDINE_FEMAC = """Destinatario:
000567
CLIENTE PROVA Srl
Partita IVA o codice fiscale
03042380547
Data documento
27/11/2026Numero documento
            367
Codice Descrizione U.M. Quantita Prezzo Sconti Importo C.I.Valuta
CONFERMA ORDINE CLIENTE
Rif. des. cliente Rif. n. cliente
299 /DDT/713/T Del: 25/11/2026Cod.cliente
367 /IS
T9US90-00 CARCASSA T9US/EU 90 n. 15,000
PUNTATURA + SALDATURA45,00000 675,00 22
T9US110-00 CARCASSA T9US/EU 110 n. 5,000
PUNTATURA + SALDATURA90,00000 450,00 22"""


class TestFormatiReali(unittest.TestCase):
    def test_ddt_vendita_numero_documento(self):
        d = estrai_documenti(pdf(DDT_REALE))[0][0]
        self.assertEqual((d['tipo'], d['numero'], d['data'], d['riferimenti']),
                         ('DDT', '721', '28/11/2026', [('ORDINE', '367')]))
        self.assertEqual([(r['codice'], r['quantita']) for r in d['righe']], [('T9US90-00', 15.0)])

    def test_fattura_su_due_pagine(self):
        docs = estrai_documenti(pdf(FATT_REALE_P1, FATT_REALE_P2))[0]
        self.assertEqual(len(docs), 1)
        d = docs[0]
        self.assertEqual((d['tipo'], d['numero'], d['data'], d['importo'], d['pagine']),
                         ('FATTURA', '119', '31/10/2026', 7045.5, 2))
        self.assertEqual(d['riferimenti'], [('DDT', '604'), ('DDT', '665')])

    def test_ordine_prezzo_sulla_riga_sotto(self):
        from blueprints.conto_lavoro.importa_ordine import estrai_dati_ordine
        o = estrai_dati_ordine(ORDINE_FEMAC)
        self.assertEqual((o['numero_ordine'], o['rif_cliente']), ('367/IS', '299/DDT/713/T'))
        self.assertEqual([(r['codice'], r['quantita'], r['prezzo_unitario']) for r in o['righe']],
                         [('T9US90-00', 15.0, 45.0), ('T9US110-00', 5.0, 90.0)])
        riga_faza = 'AS_001693 TELAIO FRANGIZOLLE FCH n. 3,000 25,00000 75,00 22'
        o2 = estrai_dati_ordine('Codice Descrizione\n' + riga_faza)
        self.assertEqual((o2['righe'][0]['quantita'], o2['righe'][0]['prezzo_unitario']), (3.0, 25.0))

    def test_pulsante_sbagliato(self):
        d = estrai_documenti(pdf(FATT_REALE_P1, FATT_REALE_P2), 'DDT')[0][0]
        self.assertEqual(d.get('tipo_diverso'), 'FATTURA')

    def test_ordine_non_scambiato_per_fattura(self):
        self.assertIsNone(estrai_documenti(pdf(ORDINE_FEMAC))[0][0]['tipo'])


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

    def _evase(self):
        with self.app.app_context():
            o = clm.ClOrdine.query.filter_by(numero_ordine='293/IS').first()
            return [float(r.quantita_evasa) for r in o.righe]

    def test_evasione_automatica_dal_ddt(self):
        a = self._carica(pdf(DDT_617), 'ddt.pdf', conferma=False)
        righe = a['documenti'][0]['righe']
        self.assertEqual([(r['codice'], r['quantita'], r['ordine'], r['in_ordine']) for r in righe],
                         [('AS_001693', 3.0, '293/IS', True), ('AS_001695', 2.0, '293/IS', True)])
        self.assertEqual(self._evase(), [0.0, 0.0])                    # l'anteprima non evade
        r = self._carica(pdf(DDT_617), 'ddt.pdf')
        self.assertEqual(r['ordini_evasi'], 1)
        self.assertEqual(self._evase(), [3.0, 2.0])
        self._carica(pdf(DDT_617), 'ddt.pdf')                          # ricaricato: non raddoppia
        self.assertEqual(self._evase(), [3.0, 2.0])
        ddt2 = DDT_617.replace('617 /DDT', '640 /DDT').replace('AS_001693 TELAIO FRANGIZOLLE FCH n. 3,000\n', '')
        self._carica(pdf(ddt2), 'ddt2.pdf')                            # secondo DDT: si somma
        self.assertEqual(self._evase(), [3.0, 4.0])
        did = next(d['id'] for d in self.c.get('/conto-lavoro/api/triple-watch/quadro').json['ddt_tutti'] if d['numero'] == '640')
        self.c.delete(f'/conto-lavoro/api/triple-watch/documenti/{did}')  # eliminato: torna indietro
        self.assertEqual(self._evase(), [3.0, 2.0])

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
