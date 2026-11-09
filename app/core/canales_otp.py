"""
Verification channels (RF-062), managed from Odoo.

Odoo publishes to holi-cache which channels are switched on and which one the
app suggests. Without that key, or with Redis unreadable, the Edge falls back
to OTP_CHANNELS_ENABLED / OTP_CHANNEL_DEFAULT: a Redis hiccup must not stop
anyone from signing up.

Whatever Odoo says, sign-up only offers phone channels (ADR-017): that rule
lives here, not in the editable configuration.
"""
import json
import logging
from dataclasses import dataclass
from typing import List

from ..clients.redis_client import get_cache_redis
from .config import settings

_logger = logging.getLogger(__name__)

CLAVE_CANALES_OTP = "content:otp_channels"
CANALES_VALIDOS = ("sms", "whatsapp", "correo")
CANALES_TELEFONO = ("sms", "whatsapp")


@dataclass(frozen=True)
class CanalesOtp:
    habilitados: List[str]
    sugerido: str

    def para(self, solo_telefono: bool) -> List[str]:
        """Channels on offer: sign-up passes solo_telefono=True."""
        return [c for c in self.habilitados if c in CANALES_TELEFONO] if solo_telefono else list(self.habilitados)

    def sugerido_para(self, solo_telefono: bool) -> str:
        disponibles = self.para(solo_telefono)
        return self.sugerido if self.sugerido in disponibles else (disponibles or [""])[0]


def _normalizar(canales, sugerido) -> CanalesOtp:
    habilitados = []
    for canal in canales or []:
        canal = str(canal).strip().lower()
        if canal in CANALES_VALIDOS and canal not in habilitados:
            habilitados.append(canal)
    return CanalesOtp(habilitados=habilitados, sugerido=str(sugerido or "").strip().lower())


def canales_de_entorno() -> CanalesOtp:
    return _normalizar(settings.otp_channels, settings.OTP_CHANNEL_DEFAULT)


async def leer_canales_otp() -> CanalesOtp:
    """What Odoo published, else the environment variables."""
    try:
        crudo = await get_cache_redis().get(CLAVE_CANALES_OTP)
    except Exception as e:
        _logger.warning("Could not read OTP channels from holi-cache, using env: %s", e)
        return canales_de_entorno()
    if not crudo:
        return canales_de_entorno()
    try:
        dato = json.loads(crudo)
    except ValueError:
        _logger.error("The OTP channels key holds invalid JSON, using env")
        return canales_de_entorno()
    if not isinstance(dato, dict):
        return canales_de_entorno()
    canales = _normalizar(dato.get("canales"), dato.get("sugerido"))
    # A list with no phone channel would make sign-up impossible: Odoo blocks
    # saving that, and the Edge refuses to honour it if it ever arrives.
    if not canales.para(solo_telefono=True):
        _logger.error("Odoo published OTP channels without a phone channel, using env")
        return canales_de_entorno()
    return canales
