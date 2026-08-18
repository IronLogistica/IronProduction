"""masterledgerlight_client.py — Lettura anagrafica articolo da MasterLedgerLight
per l'interrogazione automatica del Kanban Gruppi.

Stesso schema di masterlogistic_client.py (integrazione HTTP verso un altro
servizio Railway), ma qui in direzione opposta e con autenticazione Bearer
fin dall'inizio — simmetrico a come MasterLedgerLight stesso chiama IronProduction
per il magazzino di officina interna (vedi blueprints/magazzino/routes.py::
_auth_masterledger nel repository IronLogistica/IronProduction).

Endpoint usato:
    GET /materials/api/kanban/articolo/<codice>
        → {ok, trovato, codice, descrizione, tipo, tipo_label, uom,
           costo_standard, prezzo_vendita, aliquota_iva, scorta_minima,
           carpenteria_propria, destinazione_acquisto, attivo}
    Auth: Bearer MASTERLEDGERLIGHT_API_TOKEN (deve combaciare con
    KANBAN_API_TOKEN impostato lato MasterLedgerLight).

Sola lettura: questo client non scrive mai nulla su MasterLedgerLight.
Qualunque problema (URL/token non configurato, rete, risposta inattesa)
solleva MasterLedgerLightError — il chiamante (Kanban) deve trattarlo come
non bloccante: l'interrogazione degli altri sistemi resta best-effort.
"""
import requests
from flask import current_app


class MasterLedgerLightError(Exception):
    """Sollevata per qualunque problema nel parlare con MasterLedgerLight."""
    pass


def _base_url():
    url = current_app.config.get("MASTERLEDGERLIGHT_URL", "")
    if not url:
        raise MasterLedgerLightError(
            "MASTERLEDGERLIGHT_URL non configurato. Aggiungilo nelle variabili d'ambiente "
            "(su Railway: Variables) per interrogare MasterLedgerLight dal Kanban."
        )
    return url.rstrip("/")


def _headers():
    token = current_app.config.get("MASTERLEDGERLIGHT_API_TOKEN", "")
    if not token:
        raise MasterLedgerLightError(
            "MASTERLEDGERLIGHT_API_TOKEN non configurato. Deve combaciare con "
            "KANBAN_API_TOKEN impostato lato MasterLedgerLight."
        )
    return {"Authorization": f"Bearer {token}"}


def cerca_articolo(codice, timeout=6):
    """
    Cerca un codice nell'anagrafica articoli di MasterLedgerLight. Ritorna
    il dizionario JSON della risposta (con 'trovato': True/False) — non
    solleva errore se il codice semplicemente non esiste lì, quello è un
    esito normale. Solleva MasterLedgerLightError solo per problemi veri
    (config mancante, rete, auth, risposta malformata).
    """
    if not codice:
        raise MasterLedgerLightError("Codice mancante per l'interrogazione MasterLedgerLight.")

    url = f"{_base_url()}/materials/api/kanban/articolo/{codice}"
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise MasterLedgerLightError(f"MasterLedgerLight non raggiungibile ({url}): {e}")

    if resp.status_code == 401:
        raise MasterLedgerLightError("MasterLedgerLight ha rifiutato l'autenticazione: verifica MASTERLEDGERLIGHT_API_TOKEN.")
    if resp.status_code == 503:
        raise MasterLedgerLightError("Integrazione disabilitata lato MasterLedgerLight (KANBAN_API_TOKEN non configurato).")

    try:
        dati = resp.json()
    except ValueError:
        raise MasterLedgerLightError(f"MasterLedgerLight ha risposto in un formato inatteso (status {resp.status_code}).")

    if not dati.get("ok"):
        raise MasterLedgerLightError(f"MasterLedgerLight ha rifiutato la richiesta: {dati.get('error', 'errore sconosciuto')}")
    return dati
