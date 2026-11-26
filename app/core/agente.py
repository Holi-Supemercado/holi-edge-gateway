"""
agent-api, the shopping agent, calling on a customer's behalf.

It proves who it is with a Google ID token minted for its Cloud Run service
account and sent in X-Holi-Agent-Token; Authorization keeps carrying the
customer's own token, so the Edge applies the same permissions as in the app.
The token is checked here against Google's published keys, with no call to
Google per request: the keys are cached for an hour.
"""
import jwt

from .config import settings
from .security import SecurityError

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
EMISORES_GOOGLE = ["https://accounts.google.com", "accounts.google.com"]

_claves_google = jwt.PyJWKClient(GOOGLE_CERTS_URL, cache_keys=True, lifespan=3600)


def verificar_token_agente(token: str) -> str:
    """
    The service account behind a valid agent token. Blocking on a key cache
    miss (it downloads Google's keys), so call it off the event loop.
    """
    permitidas = settings.agent_service_accounts
    if not permitidas or not settings.AGENT_TOKEN_AUDIENCE:
        raise SecurityError("No agent is configured")
    try:
        clave = _claves_google.get_signing_key_from_jwt(token)
        datos = jwt.decode(
            token, clave.key, algorithms=["RS256"],
            audience=settings.AGENT_TOKEN_AUDIENCE, issuer=EMISORES_GOOGLE,
        )
    except jwt.PyJWKClientConnectionError:
        raise
    except jwt.PyJWTError as e:
        raise SecurityError(f"Invalid agent token: {e}")

    correo = str(datos.get("email", "")).lower()
    if not datos.get("email_verified") or correo not in permitidas:
        raise SecurityError("The token belongs to a service account that is not an agent")
    return correo
