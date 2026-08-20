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


def ottieni_stock_kanban(sku_vern, sku_grezzo="", timeout=8):
    """
    Interroga l'endpoint dedicato di WMS pensato apposta per popolare la
    scheda Kanban (GET /api/kanban-stock) — versione RIDOTTA per
    l'interrogazione automatica in fase di creazione (vedi blueprints/
    kanban/routes.py::api_interroga_codice): SOLO i campi di magazzino Iron
    Segnaletica, mai acquisti a fornitore. Per la Scheda WMS completa (con
    l'elenco degli ordini cliente aperti) usa invece _kanban_stock_grezzo(),
    che ritorna la risposta di WMS senza tagli.
    Non solleva errore se il codice semplicemente non esiste in WMS (torna
    tutto a zero, comportamento normale dell'endpoint) — solo per problemi
    veri di configurazione/rete.
    """
    dati = _kanban_stock_grezzo(sku_vern, sku_grezzo, timeout)
    return {
        'stock_iron_segnaletica': dati.get('stock_verniciati', 0),
        'stock_grezzi': dati.get('stock_grezzi', 0),
        'riservato_clienti': dati.get('riservato_clienti', 0),
        'ultimi_evasi': dati.get('ultimi_evasi', []),
    }


def ottieni_scheda_kanban(sku_vern, sku_grezzo="", timeout=8):
    """
    Versione COMPLETA per la Scheda WMS del singolo prodotto Kanban (modal
    '📊 Scheda WMS completa'): oltre a stock/riservato/ultimi evasi, include
    anche l'elenco degli ordini cliente ancora aperti su questo SKU (per la
    tabella 'Riservato a Clienti', riga per riga) — dato che l'interrogazione
    in fase di creazione (ottieni_stock_kanban) tiene volutamente fuori
    perché lì basta il totale.

    'riservato_clienti': quanto è impegnato/riservato per i clienti su
    questo SKU — già usato per il Kanban ('Riservato'), qui riletto anche
    come "Ordinato da Cliente (WMS)" per Iron Wood (Magazzino/Alert Scorte
    Codici Padre): il campo che conta davvero un impegno cliente reale
    verso quel prodotto.

    'scorta_minima': la scorta minima già configurata dentro MasterLogistic-
    WMS per questo SKU (colonna "SCORTA MIN." della sua scheda) — in
    IronProduction non è più un campo modificabile localmente, è solo una
    lettura di quel valore. AVVISO: nome campo NON confermato, assunto
    'scorta_minima' per coerenza — arriva a None finché non si verifica
    con chi ha costruito WMS.
    """
    dati = _kanban_stock_grezzo(sku_vern, sku_grezzo, timeout)
    return {
        'stock_verniciati': dati.get('stock_verniciati', 0),
        'stock_grezzi': dati.get('stock_grezzi', 0),
        'riservato_clienti': dati.get('riservato_clienti', 0),
        'ordini_clienti': dati.get('ordini_clienti', []),
        'ultimi_evasi': dati.get('ultimi_evasi', []),
        'scorta_minima': dati.get('scorta_minima'),
    }


def _kanban_stock_grezzo(sku_vern, sku_grezzo="", timeout=8):
    """Chiamata HTTP grezza a GET /api/kanban-stock — uso interno, condiviso
    da ottieni_stock_kanban() e ottieni_scheda_kanban()."""
    if not sku_vern:
        raise MasterLogisticError("SKU mancante per l'interrogazione stock Kanban.")
    url = f"{_base_url()}/api/kanban-stock"
    params = {"sku_vern": sku_vern}
    if sku_grezzo:
        params["sku_grezzo"] = sku_grezzo
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise MasterLogisticError(f"MasterLogistic-WMS non raggiungibile ({url}): {e}")
    if resp.status_code != 200:
        raise MasterLogisticError(f"MasterLogistic-WMS ha risposto {resp.status_code} su /api/kanban-stock")
    try:
        dati = resp.json()
    except ValueError:
        raise MasterLogisticError("MasterLogistic-WMS ha risposto in un formato inatteso (non JSON).")
    if isinstance(dati, dict) and dati.get('error'):
        raise MasterLogisticError(f"MasterLogistic-WMS: {dati['error']}")
    return dati


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
