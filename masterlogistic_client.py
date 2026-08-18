"""masterlogistic_client.py — Notifica a MasterLogistic-WMS del carico di
prodotto finito (transenne appena verniciate, pronte alla vendita).

Diverso dal bind diretto MASTERLOGISTIC_DATABASE_URL/ArticoloML (models.py):
quello è sola lettura per convenzione — IronProduction non scrive mai lì
direttamente, altrimenti salterebbe la logica di WMS che ricalcola gli
stati dei fascicoli ordine dopo un cambio di giacenza. Ogni SCRITTURA passa
da qui, che chiama l'endpoint HTTP dedicato di WMS.

Endpoint usato:
    POST /api/ironproduction/carico_produzione
        {sku, quantita (>0)} → {ok, sku, stock}
    Auth: Bearer MASTERLOGISTIC_API_TOKEN (deve combaciare con
    IRONPRODUCTION_API_TOKEN impostato lato MasterLogistic-WMS).
"""
import requests
from flask import current_app


class MasterLogisticError(Exception):
    """Sollevata per qualunque problema nel parlare con MasterLogistic-WMS."""
    pass


def _base_url():
    url = current_app.config.get("MASTERLOGISTIC_URL", "")
    if not url:
        raise MasterLogisticError(
            "MASTERLOGISTIC_URL non configurato. Aggiungilo nelle variabili d'ambiente "
            "(su Railway: Variables) per notificare i carichi di produzione a WMS."
        )
    return url.rstrip("/")


def _headers():
    token = current_app.config.get("MASTERLOGISTIC_API_TOKEN", "")
    if not token:
        raise MasterLogisticError(
            "MASTERLOGISTIC_API_TOKEN non configurato. Deve combaciare con "
            "IRONPRODUCTION_API_TOKEN impostato lato MasterLogistic-WMS."
        )
    return {"Authorization": f"Bearer {token}"}


def sku_da_nome_prodotto(nome_prodotto):
    """Estrae lo SKU da KanbanProdotto.prodotto: il selettore WMS nel form
    di creazione lo salva come 'SKU — Descrizione' (vedi templates/base.html,
    selezionaArticolo()); se il prodotto è stato inserito a mano senza
    passare da lì, il valore è già lo SKU puro. In entrambi i casi basta
    prendere la parte prima del trattino lungo."""
    if not nome_prodotto:
        return ""
    return nome_prodotto.split(" — ")[0].strip()


def carica_produzione(sku, quantita, timeout=8):
    """
    Notifica un carico di prodotto finito (quantita positiva) per lo SKU
    indicato. Ritorna {ok, sku, stock} in caso di successo. Solleva
    MasterLogisticError per qualunque problema — il chiamante (Kanban) deve
    trattarlo come non bloccante: la produzione resta registrata anche se
    WMS non risponde, ma l'utente va avvisato per verificare a mano.
    """
    if quantita is None or quantita <= 0:
        raise MasterLogisticError(f"Quantità non valida per il carico a MasterLogistic-WMS: {quantita}")
    if not sku:
        raise MasterLogisticError("SKU mancante per il carico a MasterLogistic-WMS.")

    url = f"{_base_url()}/api/ironproduction/carico_produzione"
    payload = {"sku": sku, "quantita": float(quantita)}
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise MasterLogisticError(f"MasterLogistic-WMS non raggiungibile ({url}): {e}")

    if resp.status_code == 401:
        raise MasterLogisticError("MasterLogistic-WMS ha rifiutato l'autenticazione: verifica MASTERLOGISTIC_API_TOKEN.")
    if resp.status_code == 503:
        raise MasterLogisticError("Integrazione disabilitata lato MasterLogistic-WMS (IRONPRODUCTION_API_TOKEN non configurato).")
    if resp.status_code == 404:
        raise MasterLogisticError(f"SKU {sku} non trovato in anagrafica MasterLogistic-WMS — crealo prima lì.")

    try:
        dati = resp.json()
    except ValueError:
        raise MasterLogisticError(f"MasterLogistic-WMS ha risposto in un formato inatteso (status {resp.status_code}).")

    if not dati.get("ok"):
        raise MasterLogisticError(f"MasterLogistic-WMS ha rifiutato il carico per {sku}: {dati.get('error', 'errore sconosciuto')}")
    return dati
