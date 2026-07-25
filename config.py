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
