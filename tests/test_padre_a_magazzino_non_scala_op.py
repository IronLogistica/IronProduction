"""
Regola (Mauri, 12/10/2026 — OP-2026-000077, commessa 26100077, OL Saldatura
LIST/048): la giacenza del PRODOTTO FINITO di un OP non deve MAI scalare
quell'OP. Con 44 PINX110 gia' a magazzino l'OL Saldatura mostrava
'PINX110: Da produrre 193' (237 - 44) invece di 237. Un OP e' produzione
aggiuntiva: "ho 100 pronti, ne voglio fare altri 100 per arrivare a 200".
Valgono invece i semilavorati: i 25 PINX110-A a magazzino restano scalati.
"""
import unittest

from flask import Flask

from models import (db, CentroCostoWood, CicloLavoroWood, OrdineProduzione, GiacenzaWood,
                     DistintaBaseWood, EventoConsuntivoPP)
from blueprints.produzione_pp.routes import _lista_lavoro_op
from blueprints.magazzino.routes import _netta_e_esplodi_wood

OP = 'OP-2026-000077'


class TestPadreAMagazzinoNonScalaOP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__, template_folder='../templates')
        cls.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(cls.app)
        with cls.app.app_context():
            db.create_all(bind_key=None)

    def setUp(self):
        with self.app.app_context():
            db.session.remove()
            for m in (EventoConsuntivoPP, DistintaBaseWood, GiacenzaWood, CicloLavoroWood,
                      OrdineProduzione, CentroCostoWood):
                db.session.query(m).delete()
            db.session.commit()
            sald = CentroCostoWood(nome='Saldatura')
            db.session.add(sald)
            db.session.flush()
            self.sald_id = sald.id
            db.session.add_all([
                DistintaBaseWood(codice_padre='PINX110', codice_figlio='PINX110-B', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-B', codice_figlio='PINX110-A', quantita=1.0),
                DistintaBaseWood(codice_padre='PINX110-A', codice_figlio='PINXTT110', quantita=1.0),
                CicloLavoroWood(codice='PINX110-A', sequenza=1, centro_costo_id=self.sald_id),
                CicloLavoroWood(codice='PINX110-B', sequenza=1, centro_costo_id=self.sald_id),
                CicloLavoroWood(codice='PINX110', sequenza=1, centro_costo_id=self.sald_id),
                # Magazzino reale della foto: 44 finiti, 0 B, 25 A, 212 tubi
                GiacenzaWood(codice='PINX110', quantita=44),
                GiacenzaWood(codice='PINX110-B', quantita=0),
                GiacenzaWood(codice='PINX110-A', quantita=25),
                GiacenzaWood(codice='PINXTT110', quantita=212),
                OrdineProduzione(codice=OP, codice_articolo='PINX110', commessa='26100077',
                                  qta_pianificata=237, qta_buona=0, stato='Rilasciato', priorita=1),
            ])
            db.session.commit()

    def test_ol_saldatura_non_scala_i_44_finiti(self):
        with self.app.app_context():
            o = OrdineProduzione.query.filter_by(codice=OP).one()
            dati = _lista_lavoro_op(o, db.session.get(CentroCostoWood, self.sald_id), assegna_numero=False)
            saldi = {r['codice']: r['saldo'] for g in dati['gruppi'] for r in g['righe']}
            self.assertEqual(saldi['PINX110'], 237, 'prima della fix: 193 (237 - 44 finiti a magazzino)')
            self.assertEqual(saldi['PINX110-B'], 237)
            self.assertEqual(saldi['PINX110-A'], 212, 'i 25 semilavorati A valgono ancora')

    def test_fabbisogno_materiali_non_ridotto_dai_finiti(self):
        """La stessa regola vale per impegni/fabbisogni: i 44 finiti non riducono
        i materiali da preparare/comprare per la nuova commessa."""
        with self.app.app_context():
            giacenza = {g.codice: g.quantita for g in GiacenzaWood.query.all()}
            out = {}
            _netta_e_esplodi_wood('PINX110', 237, giacenza, out, escludi_fabbisogno_per='PINX110')
            self.assertEqual(out['PINX110-B']['fabbisogno'], 237)
            self.assertEqual(giacenza['PINX110'], 44, 'i finiti restano liberi, non consumati da questo OP')


if __name__ == '__main__':
    unittest.main()
