"""Compose the single sign-on service from the environment."""

from app.config import AuthConfig
from app.google import GoogleSignIn
from app.store import SessionStore
from app.web import create_app

config = AuthConfig.from_env()
store = SessionStore(config.database_url)
store.initialize()
app = create_app(config, store, GoogleSignIn(config.client_id, config.client_secret, config.redirect_uri))
