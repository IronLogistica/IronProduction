import os

class Config:
    _db = os.environ.get('DATABASE_URL', 'sqlite:///masterproduction.db')
    if _db.startswith('postgres://'):
        _db = _db.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _db
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mes-carpenteria-dev-2024')

    # ══════════════════════════════════════════════════════════════════════
    #  BIND VERSO IL DB DI MASTERLOGISTIC (stesso progetto Railway, Postgres
    #  separato). Sola lettura — IronProduction non crea/altera nulla lì
    #  (vedi ArticoloML in models.py e db.create_all(bind_key=None) in app.py).
    #  Su Railway: variabile MASTERLOGISTIC_DATABASE_URL = connection string
    #  INTERNA (*.railway.internal) del plugin Postgres di MasterLogistic —
    #  si trova tra le variabili d'ambiente del servizio MasterLogistic.
    # ══════════════════════════════════════════════════════════════════════
    _ml_db = os.environ.get('MASTERLOGISTIC_DATABASE_URL', '')
    if _ml_db.startswith('postgres://'):
        _ml_db = _ml_db.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_BINDS = {'masterlogistic': _ml_db} if _ml_db else {}

    # Token Bearer obbligatorio per le API PP; lasciare vuoto disabilita le API.
    PP_API_TOKEN = os.environ.get('PP_API_TOKEN', '')

    # Token Bearer per l'integrazione con MasterLedger (Iron Segnaletica):
    # riceve qui il carico magazzino per i materiali "da officina interna"
    # (ferro, filo di saldatura, DPI...) quando arriva un'Entrata Merci.
    # Separato da PP_API_TOKEN apposta: integrazioni diverse, chiavi diverse,
    # revocabili indipendentemente. Vuoto = endpoint disabilitato.
    MASTERLEDGER_API_TOKEN = os.environ.get('MASTERLEDGER_API_TOKEN', '')

    # URL + token per notificare a MasterLogistic-WMS il carico di prodotto
    # finito (es. transenne appena verniciate, pronte alla vendita) — vedi
    # services/masterlogistic_client.py, chiamato da blueprints/kanban/
    # routes.py quando 'verniciati' aumenta. Diverso da
    # MASTERLOGISTIC_DATABASE_URL qui sopra: quello è un bind diretto al DB
    # (sola lettura, mai scrittura); questo è l'endpoint HTTP di WMS che fa
    # l'incremento atomico e ricalcola gli stati fascicoli — la scrittura
    # passa sempre da lì, mai dal bind diretto.
    MASTERLOGISTIC_URL = os.environ.get('MASTERLOGISTIC_URL', '')
    MASTERLOGISTIC_API_TOKEN = os.environ.get('MASTERLOGISTIC_API_TOKEN', '')

    # PIN del CAPO REPARTO per sbloccare "Storico Correzioni" in Dichiarazione
    # di Produzione — cambialo impostando la variabile Railway CAPO_PIN se
    # vuoi un valore diverso da quello di default.
    CAPO_PIN = os.environ.get('CAPO_PIN', '1234')
