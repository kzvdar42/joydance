import asyncio
import logging
import hid

from joydance.controllers import AbstractControllerWrapper, JoyConWrapper


logger = logging.getLogger(__name__)


CONTROLLER_CLASSES = { JoyConWrapper, }
VENDOR_IDS = set()
PRODUCT_IDS = set()
PRODUCT_ID_TO_CONTROLLER_CLASS = {}
_CONTROLLER_CONNECTION_LOCKS: dict[object, asyncio.Lock] = {}


def _get_controller_connection_lock(serial) -> asyncio.Lock:
    """Return the shared connection lock for a controller serial."""
    lock = _CONTROLLER_CONNECTION_LOCKS.get(serial)
    if lock is None:
        lock = asyncio.Lock()
        _CONTROLLER_CONNECTION_LOCKS[serial] = lock
    return lock


# Fill static variables
for controller_class in CONTROLLER_CLASSES:
    VENDOR_IDS.update(controller_class.VENDOR_IDS)
    PRODUCT_IDS.update(controller_class.PRODUCT_IDS)
    for product_id in controller_class.PRODUCT_IDS:
        if product_id in PRODUCT_ID_TO_CONTROLLER_CLASS:
            raise ValueError(f"Product ID {product_id} is already registered to {PRODUCT_ID_TO_CONTROLLER_CLASS[product_id]}")
        PRODUCT_ID_TO_CONTROLLER_CLASS[product_id] = controller_class


async def get_device_ids():
    """Get the device IDs for all controllers."""
    devices = []
    for vendor_id in VENDOR_IDS:
        devices.extend(hid.enumerate(vendor_id, 0))
    logger.debug("get_device_ids devices: %s", devices)
    out = []
    for device in devices:
        vendor_id = device["vendor_id"]
        product_id = device["product_id"]
        product_string = device["product_string"]
        serial = device.get("serial") or device.get("serial_number")

        if product_id not in PRODUCT_IDS:
            continue

        if not product_string:
            continue

        out.append(
            {
                "vendor_id": vendor_id,
                "product_id": product_id,
                "serial": serial,
                "product_string": product_string,
            }
        )
    return out


async def update_controllers_list(
    controllers: dict[str, AbstractControllerWrapper] = {},
) -> dict[str, AbstractControllerWrapper]:
    """Update the controllers list with the new devices."""
    devices = await get_device_ids()
    logger.debug("update_controllers_list devices: %s", devices)

    for dev in devices:
        try:
            serial = dev["serial"]
            if serial in controllers:
                continue

            async with _get_controller_connection_lock(serial):
                # Another discovery request may have connected this controller
                # while this request was waiting for the per-serial lock.
                if serial in controllers:
                    continue

                controller_class = PRODUCT_ID_TO_CONTROLLER_CLASS[dev["product_id"]]
                controller = controller_class(dev["vendor_id"], dev["product_id"], serial)
                await controller.connect()
                # set to "disconnected" pattern
                await controller.set_player_led(-1)
                controllers[serial] = controller
        except Exception as e:
            logger.error("Error updating controller list: %s", e, exc_info=True)
            continue
    return controllers
