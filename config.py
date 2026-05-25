import os

class Config:
    _db = os.environ.get('DATABASE_URL', 'sqlite:///masterproduction.db')
    if _db.startswith('postgres://'):
        _db = _db.replace('postgres://', 'postgresql://', 1)
    SQLALCHEMY_DATABASE_URI = _db
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY', 'mes-carpenteria-dev-2024')
