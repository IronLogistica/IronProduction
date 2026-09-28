"""
Conto lavoro — PR 0 (scheletro). Nessun database reale, nessun import di
app.py (che all'import esegue DDL): app minimale con il solo blueprint.
"""
import ast
import os
import unittest

import requests
from flask import Flask

from models import db
from blueprints.conto_lavoro.routes import cl_bp

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARTELLA_MODULO = os.path.join(RADICE, 'blueprints', 'conto_lavoro')

# Moduli/simboli che il Conto lavoro non deve MAI importare: muovono giacenza
# Iron Wood, OP, Kanban, contabilità o chiamano servizi esterni.
VIETATI = {
    'masterlogistic_client', 'masterledgerlight_client', 'requests',
    'blueprints.magazzino.routes', 'blueprints.produzione_pp.routes',
    'blueprints.acquisti_wood.routes', 'blueprints.terzisti.routes', 'blueprints.kanban.routes',
}
SIMBOLI_VIETATI = {
    'GiacenzaWood', 'MovimentoGiacenzaWood', 'OrdineProduzione', 'EventoConsuntivoPP',
    'MovimentoContabileWood', 'KanbanProdotto', '_registra_movimento_giacenza',
    '_registra_evento_consuntivo', 'DDTCaricoWood', 'LavorazioneTerzista',
}


def _leggi(percorso):
    with open(percorso, encoding='utf-8') as f:
        return f.read()


def _app(abilitato, secret='chiave-di-test-lunga-e-casuale', registra=None):
    """registra=None: come app.py (blueprint solo se abilitato); True: sempre (per
    provare anche il cancello interno del blueprint)."""
    app = Flask(__name__, template_folder=os.path.join(RADICE, 'templates'))
    app.config.update(TESTING=True, SECRET_KEY=secret, CL_ENABLED=abilitato,
                      SQLALCHEMY_DATABASE_URI='sqlite:///:memory:', SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    if registra or (registra is None and app.config.get('CL_ENABLED')):
        app.register_blueprint(cl_bp)

    @app.context_processor
    def _globali():  # base.html si aspetta questi valori (li dà app.py in produzione)
        return {'now': '', 'kanban_gruppi': [], 'macchine_monitor': [], 'sidebar_gruppi': []}
    return app


class TestCancelloModulo(unittest.TestCase):
    def test_spento_risponde_404_ovunque(self):
        c = _app(False).test_client()
        for url in ('/conto-lavoro/', '/conto-lavoro/api/stato', '/conto-lavoro/qualsiasi'):
            self.assertEqual(c.get(url).status_code, 404, url)
        self.assertEqual(c.post('/conto-lavoro/api/stato').status_code, 404)

    def test_cancello_interno_anche_se_registrato(self):
        c = _app(False, registra=True).test_client()
        self.assertEqual(c.get('/conto-lavoro/api/stato').status_code, 404)

    def test_acceso_senza_login_non_serve_secret_key(self):
        # Decisione 28/09: nessun login, quindi nessun vincolo sulla SECRET_KEY
        c = _app(True, secret='mes-carpenteria-dev-2024').test_client()
        self.assertEqual(c.get('/conto-lavoro/api/stato').status_code, 200)

    def test_acceso_risponde(self):
        c = _app(True).test_client()
        r = c.get('/conto-lavoro/api/stato')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['abilitato'])
        self.assertEqual(r.get_json()['schema'], 0)   # nessuna tabella nel DB di test
        self.assertEqual(c.get('/conto-lavoro/api/clienti').status_code, 503)
        self.assertEqual(c.get('/conto-lavoro/').status_code, 200)


class TestIsolamento(unittest.TestCase):
    def test_nessuna_chiamata_di_rete(self):
        originale = requests.Session.request

        def vietata(*a, **k):
            raise AssertionError('Il Conto lavoro non deve fare chiamate di rete')
        requests.Session.request = vietata
        try:
            c = _app(True).test_client()
            c.get('/conto-lavoro/'); c.get('/conto-lavoro/api/stato')
        finally:
            requests.Session.request = originale

    def test_nessun_import_o_simbolo_vietato(self):
        for nome in os.listdir(CARTELLA_MODULO):
            if not nome.endswith('.py'):
                continue
            albero = ast.parse(_leggi(os.path.join(CARTELLA_MODULO, nome)))
            for nodo in ast.walk(albero):
                if isinstance(nodo, ast.Import):
                    for a in nodo.names:
                        self.assertNotIn(a.name, VIETATI, f'{nome}: import {a.name}')
                elif isinstance(nodo, ast.ImportFrom):
                    self.assertNotIn(nodo.module, VIETATI, f'{nome}: from {nodo.module}')
                    for a in nodo.names:
                        self.assertNotIn(a.name, SIMBOLI_VIETATI, f'{nome}: {a.name}')
                elif isinstance(nodo, ast.Name):
                    self.assertNotIn(nodo.id, SIMBOLI_VIETATI, f'{nome}: usa {nodo.id}')

    def test_create_all_di_avvio_non_crea_tabelle_cl(self):
        app = _app(True)
        with app.app_context():
            db.create_all(bind_key=None)  # esattamente come app.py all'avvio
            tabelle = db.inspect(db.engine).get_table_names()
        self.assertFalse([t for t in tabelle if t.startswith('cl_')])

    def test_app_py_registra_il_blueprint_solo_se_acceso(self):
        sorgente = _leggi(os.path.join(RADICE, 'app.py'))
        self.assertIn("    if app.config.get('CL_ENABLED'):\n        app.register_blueprint(cl_bp)", sorgente)


class TestConfig(unittest.TestCase):
    def _config(self, **env):
        import importlib
        import config
        vecchi = {k: os.environ.get(k) for k in ('CL_ENABLED', 'CL_DATABASE_URL', 'DATABASE_URL')}
        try:
            for k in vecchi:
                os.environ.pop(k, None)
            os.environ.update(env)
            return importlib.reload(config).Config
        finally:
            for k, v in vecchi.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            importlib.reload(config)

    def test_spento_di_default_e_nessun_bind(self):
        cfg = self._config()
        self.assertFalse(cfg.CL_ENABLED)
        self.assertNotIn('conto_lavoro', cfg.SQLALCHEMY_BINDS)

    def test_acceso_usa_lo_stesso_postgres(self):
        cfg = self._config(CL_ENABLED='1', DATABASE_URL='postgres://u:p@host/ip')
        self.assertTrue(cfg.CL_ENABLED)
        bind = cfg.SQLALCHEMY_BINDS['conto_lavoro']
        self.assertEqual(bind['url'], 'postgresql://u:p@host/ip')
        self.assertEqual(bind['execution_options']['schema_translate_map'], {None: 'conto_lavoro'})


if __name__ == '__main__':
    unittest.main()
