"""Compose the single sign-on service from the environment."""

from app.config import AuthConfig
from app.google import GoogleSignIn
from app.oauth import OAuthStore
from app.store import SessionStore
from app.web import create_app

config = AuthConfig.from_env()
store = SessionStore(config.database_url)
store.initialize()
store.sync_users(config.owner_email, config.allowed_emails)
oauth = OAuthStore(config.database_url)
oauth.initialize()
app = create_app(config, store, GoogleSignIn(config.client_id, config.client_secret, config.redirect_uri), oauth)
