"""
CONTO LAVORO — modulo AUTONOMO per ordini cliente in conto lavorazione
(saldatura attrezzature agricole, montaggio stelle ranghinatrici) con beni
e componenti di proprietà del CLIENTE.

PR 0 — SCHELETRO. Regole di isolamento che valgono per tutto il modulo:
  - NON importa e NON chiama mai: _registra_movimento_giacenza /
    GiacenzaWood / Kanban / OrdineProduzione / _registra_evento_consuntivo /
    MovimentoContabileWood / masterlogistic_client / masterledgerlight_client /
    requests. La merce del cliente non entra MAI nella giacenza Iron Wood.
  - Le tabelle del modulo (dal PR 1) vivono sul bind 'conto_lavoro' e NON
    vengono create da db.create_all(bind_key=None) all'avvio dell'app:
    il DDL si esegue solo con uno script esplicito.
  - Feature flag CL_ENABLED spento di default: con flag spento ogni rotta
    del modulo risponde 404, come se non esistesse.
  - Con flag acceso ma SECRET_KEY ancora quella predefinita nel codice
    (config.py) il modulo rifiuta di partire (503): le sessioni di login
    del PR 2 sarebbero falsificabili.
"""
from flask import Blueprint, abort, current_app, jsonify, render_template

VERSIONE_MODULO = 'pr0-scheletro'
SECRET_KEY_PREDEFINITA = 'mes-carpenteria-dev-2024'

cl_bp = Blueprint('conto_lavoro', __name__, url_prefix='/conto-lavoro')


def modulo_abilitato():
    return bool(current_app.config.get('CL_ENABLED'))


def secret_key_sicura():
    chiave = current_app.config.get('SECRET_KEY') or ''
    return bool(chiave) and chiave != SECRET_KEY_PREDEFINITA


@cl_bp.before_request
def _cancello_modulo():
    if not modulo_abilitato():
        abort(404)
    if not secret_key_sicura():
        return jsonify(ok=False, error='Conto lavoro non avviabile: impostare una SECRET_KEY propria '
                                       '(quella predefinita nel codice non è sicura).'), 503


@cl_bp.get('/')
def pagina_indice():
    return render_template('conto_lavoro/index.html', active='conto_lavoro', versione=VERSIONE_MODULO)


@cl_bp.get('/api/stato')
def api_stato():
    return jsonify(ok=True, modulo='conto_lavoro', versione=VERSIONE_MODULO, abilitato=True)
