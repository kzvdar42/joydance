import logging

import hid

from joydance.controllers import AbstractControllerWrapper, JoyConWrapper


logger = logging.getLogger(__name__)


CONTROLLER_CLASSES = { JoyConWrapper, }
VENDOR_IDS = set()
PRODUCT_IDS = set()
PRODUCT_ID_TO_CONTROLLER_CLASS = {}


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
            if dev["serial"] in controllers:
                continue

            controller_class = PRODUCT_ID_TO_CONTROLLER_CLASS[dev["product_id"]]
            controller = controller_class(dev["vendor_id"], dev["product_id"], dev["serial"])
            # set to "disconnected" pattern
            await controller.set_player_led(-1)
            controllers[dev["serial"]] = controller
        except Exception as e:
            logger.error(f"Error updating controller list: {e}", exc_info=True)
            continue
    return controllers
