from joydance.constants import PairingMethod

from .jd23.game import JustDanceGame2023
from .jdnow.game import JustDanceGameNow
from .justdance_abstract import JustDanceGameAbstract
from .v1.game import JustDanceGameV1
from .v2.game import JustDanceGameV2


_GAME_CLASSES = {
    PairingMethod.OLD.value: JustDanceGameV1,
    PairingMethod.DEFAULT.value: JustDanceGameV2,
    PairingMethod.FAST.value: JustDanceGameV2,
    PairingMethod.STADIA.value: JustDanceGameV2,
    PairingMethod.JDMO.value: JustDanceGameV2,
    PairingMethod.JD23.value: JustDanceGame2023,
    PairingMethod.JDNOW.value: JustDanceGameNow,
}


def create_game(*, pairing_method: str, **options) -> JustDanceGameAbstract:
    return _GAME_CLASSES[pairing_method](pairing_method=pairing_method, **options)
