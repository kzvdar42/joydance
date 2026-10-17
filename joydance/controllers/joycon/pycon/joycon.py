import asyncio
import functools
import time
import threading
from typing import Optional, Tuple
import math
import logging

import hid

from .constants import (JOYCON_L_PRODUCT_ID, JOYCON_PRODUCT_IDS,
                        JOYCON_R_PRODUCT_ID, JOYCON_PRO_PRODUCT_ID, JOYCON_VENDOR_ID)
from joydance.controllers.hid_executor import HID_EXECUTOR
from joydance.utils.errors import ControllerConnectionError


logger = logging.getLogger(__name__)


# TODO: disconnect, power off sequence
class JoyCon:
    _INPUT_REPORT_SIZE = 49
    _COMMAND_OUTPUT_REPORT_SIZE = 0x31
    _BASIC_OUTPUT_REPORT_SIZE = 0x0A
    _INPUT_REPORT_PERIOD = 0.015
    _INPUT_READ_TIMEOUT_MS = 100
    _RUMBLE_DATA = b'\x00\x01\x40\x40\x00\x01\x40\x40'

    vendor_id: int
    product_id: int
    serial: Optional[str]
    simple_mode: bool
    color_body: Tuple[int, int, int]
    color_btn: Tuple[int, int, int]
    stick_cal: dict[str, list[int]]

    def __init__(self, vendor_id: int, product_id: int, serial: str = None, simple_mode=False):
        if vendor_id != JOYCON_VENDOR_ID:
            raise ValueError(f'vendor_id is invalid: {vendor_id!r}')

        if product_id not in JOYCON_PRODUCT_IDS:
            raise ValueError(f'product_id is invalid: {product_id!r}')

        self.vendor_id = vendor_id
        self.product_id = product_id
        self.serial = serial
        self.simple_mode = simple_mode  # TODO: It's for reporting mode 0x3f

        self._rumble_enabled = True
        self._first_connection_timeout = 1
        self.reconnect_timeout = 1
        # Max attempts for SPI read + sensor setup after HID open (each failure sleeps reconnect_timeout)
        self._max_setup_attempts = 10
        # Seconds to wait for a subcommand reply. A JoyCon emits 0x30 reports at
        # about 60Hz, so report-count based timeouts expire too quickly.
        self._subcommand_response_timeout = 5.0
        self._subcommand_resend_interval = 0.25
        self._first_report_attempts = 30
        # Seconds to wait for first 0x30 input report after setup (thread must publish is_connected)
        self._first_input_report_timeout = 5.0

        # setup internal state
        self._rumble_data = self._RUMBLE_DATA
        self._input_hooks = []
        self._input_report = bytes(self._INPUT_REPORT_SIZE)
        self._accel_reports = None
        self._packet_number = 0
        self.color_body = (128, 128, 128)
        self.color_btn = (255, 255, 255)
        self.color_grip_left = None
        self.color_grip_right = None
        self.deadzone = {"left": 0, "right": 0}
        self._set_default_stick_calibration()
        self.set_accel_calibration((0, 0, 0), (1, 1, 1))

        # connection state
        self._joycon_device = None
        self._update_input_report_thread = None
        self.preconnect_event = threading.Event()
        self.is_connected = threading.Event()
        self._should_run = threading.Event()
        self._connect_lock = asyncio.Lock()
        self._connection_lifecycle_lock = threading.Lock()
        self._connection_generation = 0
        self._active_connection_cancel = None
        self._closed = False
        self._loop = None
        self._reconnect_future = None
        self._was_state_initialized = False

    @property
    def was_state_initialized(self):
        """Returns true if internal state was read from hid and populated."""
        return self._was_state_initialized

    def _get_connection_generation(self):
        with self._connection_lifecycle_lock:
            return self._connection_generation

    def _ensure_connection_request_is_current(self, request_generation):
        with self._connection_lifecycle_lock:
            is_current = request_generation == self._connection_generation
        if not is_current:
            raise ControllerConnectionError(
                f"{id(self)} {self.serial}: connection request was stopped"
            )

    def _begin_connection_attempt(self, request_generation, allow_reopen):
        with self._connection_lifecycle_lock:
            if request_generation != self._connection_generation:
                raise ControllerConnectionError(
                    f"{id(self)} {self.serial}: connection request was stopped"
                )
            if self._closed:
                if not allow_reopen:
                    raise ControllerConnectionError(
                        f"{id(self)} {self.serial}: controller is closed"
                    )
                reopening_after_close = True
                self._closed = False
            else:
                reopening_after_close = False

            connection_cancel = threading.Event()
            self._active_connection_cancel = connection_cancel
            return (
                self._connection_generation,
                connection_cancel,
                reopening_after_close,
            )

    def _is_connection_attempt_current(self, connection_generation, connection_cancel):
        with self._connection_lifecycle_lock:
            return (
                not self._closed
                and connection_generation == self._connection_generation
                and self._active_connection_cancel is connection_cancel
                and not connection_cancel.is_set()
            )

    def _ensure_connection_attempt_is_current(
        self,
        connection_generation,
        connection_cancel,
    ):
        if not self._is_connection_attempt_current(
            connection_generation,
            connection_cancel,
        ):
            raise ControllerConnectionError(
                f"{id(self)} {self.serial}: connection attempt was stopped"
            )

    def _ensure_active_connection_is_not_stopped(self):
        with self._connection_lifecycle_lock:
            connection_cancel = self._active_connection_cancel
            is_stopped = self._closed or (
                connection_cancel is not None and connection_cancel.is_set()
            )
        if is_stopped:
            raise ControllerConnectionError(
                f"{id(self)} {self.serial}: connection attempt was stopped"
            )

    def _wait_for_connection(
        self,
        connection_generation,
        connection_cancel,
        timeout,
    ):
        deadline = time.monotonic() + timeout
        while not connection_cancel.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.is_connected.wait(min(0.1, remaining)):
                return True
        self._ensure_connection_attempt_is_current(
            connection_generation,
            connection_cancel,
        )
        return False

    async def _run_in_thread(self, func, *args):
        """Run a blocking call in a thread and finish safely on cancellation."""
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            loop.run_in_executor(HID_EXECUTOR, functools.partial(func, *args))
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with self._connection_lifecycle_lock:
                if self._active_connection_cancel is not None:
                    self._active_connection_cancel.set()
            try:
                await asyncio.shield(task)
            except Exception:
                logger.debug(
                    "%s: blocking reconnect step failed after cancellation",
                    self.serial,
                    exc_info=True,
                )
            raise

    async def connect(self):
        """Connect to the JoyCon."""
        request_generation = self._get_connection_generation()
        return await self._connect(request_generation, allow_reopen=True)

    async def _connect(self, request_generation, allow_reopen):
        self._loop = asyncio.get_running_loop()
        was_queued = self._connect_lock.locked()
        async with self._connect_lock:
            self._ensure_connection_request_is_current(request_generation)
            if was_queued and self.is_connected.is_set():
                return True
            connection_generation = None
            connection_cancel = None
            reopening_after_close = False
            is_connected = False
            connection_cancelled = False
            try:
                (
                    connection_generation,
                    connection_cancel,
                    reopening_after_close,
                ) = self._begin_connection_attempt(request_generation, allow_reopen)
                with self._connection_lifecycle_lock:
                    if (
                        self._closed
                        or connection_generation != self._connection_generation
                        or self._active_connection_cancel is not connection_cancel
                        or connection_cancel.is_set()
                    ):
                        raise ControllerConnectionError(
                            f"{id(self)} {self.serial}: connection attempt was stopped"
                        )
                    self.preconnect_event.clear()
                    self.is_connected.clear()

                if reopening_after_close:
                    previous_reader = self._update_input_report_thread
                    if previous_reader is not None and previous_reader.is_alive():
                        await self._run_in_thread(
                            previous_reader.join,
                            self._first_input_report_timeout,
                        )
                        self._ensure_connection_attempt_is_current(
                            connection_generation,
                            connection_cancel,
                        )
                        if previous_reader.is_alive():
                            raise ControllerConnectionError(
                                f"{id(self)} {self.serial}: previous input reader did not stop"
                            )

                if self._joycon_device:
                    try:
                        await self._run_in_thread(self._close)
                    except Exception:
                        logger.debug("%s: error closing device before reconnect", self.serial, exc_info=True)
                    self._ensure_connection_attempt_is_current(
                        connection_generation,
                        connection_cancel,
                    )

                for attempt in range(self._max_setup_attempts):
                    self._ensure_connection_attempt_is_current(
                        connection_generation,
                        connection_cancel,
                    )
                    try:
                        await self._run_in_thread(
                            self._connect_hid,
                            connection_generation,
                            connection_cancel,
                        )
                        self._ensure_connection_attempt_is_current(
                            connection_generation,
                            connection_cancel,
                        )
                        break
                    except Exception as exc:
                        # A close while HID setup was running must not be treated as
                        # an ordinary setup failure followed by another open attempt.
                        self._ensure_connection_attempt_is_current(
                            connection_generation,
                            connection_cancel,
                        )
                        logger.debug("%s %s: error in _connect_hid", id(self), self.serial, exc_info=True)
                        if attempt >= self._max_setup_attempts - 1:
                            await self._run_in_thread(self._close)
                            self._ensure_connection_attempt_is_current(
                                connection_generation,
                                connection_cancel,
                            )
                            raise ControllerConnectionError(
                                f"{id(self)} {self.serial}: connect setup failed after "
                                f"{self._max_setup_attempts} attempts"
                            ) from exc
                        await asyncio.sleep(self.reconnect_timeout)
                        self._ensure_connection_attempt_is_current(
                            connection_generation,
                            connection_cancel,
                        )

                with self._connection_lifecycle_lock:
                    if (
                        self._closed
                        or connection_generation != self._connection_generation
                        or self._active_connection_cancel is not connection_cancel
                        or connection_cancel.is_set()
                    ):
                        raise ControllerConnectionError(
                            f"{id(self)} {self.serial}: connection attempt was stopped"
                        )
                    self.preconnect_event.set()
                    self._should_run.set()

                started_new_input_thread = False
                if (
                    self._update_input_report_thread is None
                    or not self._update_input_report_thread.is_alive()
                ):
                    self._update_input_report_thread = threading.Thread(
                        target=self._update_input_report, daemon=True,
                    )
                    self._update_input_report_thread.start()
                    started_new_input_thread = True
                self._ensure_connection_attempt_is_current(
                    connection_generation,
                    connection_cancel,
                )
                is_connected = await self._run_in_thread(
                    self._wait_for_connection,
                    connection_generation,
                    connection_cancel,
                    self._first_input_report_timeout,
                )
                self._ensure_connection_attempt_is_current(
                    connection_generation,
                    connection_cancel,
                )
                if not is_connected:
                    # Only tear down the reader thread if this connect attempt started it.
                    # On reconnect, keep _should_run so the existing thread can recover via OSError/reconnect.
                    if started_new_input_thread:
                        self._should_run.clear()
                    try:
                        await self._run_in_thread(self._close)
                    except Exception:
                        logger.debug("%s: error closing device after input report timeout", self.serial, exc_info=True)
                    raise ControllerConnectionError(
                        f"{id(self)} {self.serial}: timeout waiting for first input report (0x30)"
                    )
            except asyncio.CancelledError:
                connection_cancelled = True
                raise
            finally:
                connection_stopped = False
                if connection_cancel is not None:
                    with self._connection_lifecycle_lock:
                        connection_stopped = (
                            connection_cancelled
                            or connection_generation != self._connection_generation
                            or self._active_connection_cancel is not connection_cancel
                            or connection_cancel.is_set()
                            or self._closed
                        )
                        if connection_stopped:
                            self._should_run.clear()
                            self.is_connected.clear()
                        if not is_connected:
                            self.preconnect_event.set()
                        if self._active_connection_cancel is connection_cancel:
                            self._active_connection_cancel = None

                if connection_stopped:
                    try:
                        self._close()
                    except Exception:
                        logger.debug(
                            "%s: error closing device after stopped connection attempt",
                            self.serial,
                            exc_info=True,
                        )
            return is_connected

    def _connect_hid(self, connection_generation, connection_cancel):
        self._ensure_connection_attempt_is_current(
            connection_generation,
            connection_cancel,
        )
        try:
            joycon_device = self._open(
                self.vendor_id,
                self.product_id,
                self.serial,
            )

            with self._connection_lifecycle_lock:
                if (
                    not self._closed
                    and connection_generation == self._connection_generation
                    and self._active_connection_cancel is connection_cancel
                    and not connection_cancel.is_set()
                ):
                    self._joycon_device = joycon_device
                    joycon_device = None

            if joycon_device is not None:
                try:
                    joycon_device.close()
                finally:
                    self._ensure_connection_attempt_is_current(
                        connection_generation,
                        connection_cancel,
                    )

            if connection_cancel.wait(self._first_connection_timeout):
                self._ensure_connection_attempt_is_current(
                    connection_generation,
                    connection_cancel,
                )
            self._ensure_connection_attempt_is_current(
                connection_generation,
                connection_cancel,
            )
            self._prepare_input_reports()
            self._ensure_connection_attempt_is_current(
                connection_generation,
                connection_cancel,
            )
            self._wait_for_any_input_report()
            self._ensure_connection_attempt_is_current(
                connection_generation,
                connection_cancel,
            )
            self._read_joycon_data()
            self._ensure_connection_attempt_is_current(
                connection_generation,
                connection_cancel,
            )
            self._setup_sensors()
            self._ensure_connection_attempt_is_current(
                connection_generation,
                connection_cancel,
            )
            self._was_state_initialized = True
        except Exception:
            self._close()
            raise

    def _open(self, vendor_id, product_id, serial):
        try:
            if hasattr(hid, "device"):  # hidapi
                _joycon_device = hid.device()
                _joycon_device.open(vendor_id, product_id, serial)
            elif hasattr(hid, "Device"):  # hid
                _joycon_device = hid.Device(vendor_id, product_id, serial)
            else:
                raise ValueError("Unrecognized HID library implementation. Supported libraries: hidapi or hid")
        except IOError as e:
            raise IOError('JoyCon connection failed') from e
        return _joycon_device

    def _close(self):
        with self._connection_lifecycle_lock:
            joycon_device = self._joycon_device
            self._joycon_device = None
            self._accel_reports = None
        if joycon_device:
            joycon_device.close()

    def close(self):
        """Safely closes the connection without fully deleting the object"""
        with self._connection_lifecycle_lock:
            self._closed = True
            self._accel_reports = None
            self._connection_generation += 1
            connection_cancel = self._active_connection_cancel
            if connection_cancel is not None:
                connection_cancel.set()
            self._should_run.clear()
            self.is_connected.clear()
            self.preconnect_event.set()
            reconnect_future = self._reconnect_future
            joycon_device = self._joycon_device
            self._joycon_device = None

        if reconnect_future and not reconnect_future.done():
            reconnect_future.cancel()
        try:
            if joycon_device:
                joycon_device.close()
        except Exception:
            pass

    def disconnect_device(self):
        """Safely disconnect the device"""
        try:
            self._write_output_report(b'\x01', b'\x06', b'\x00')
        finally:
            self.close()

    async def reconnect(self):
        """Attempts to reconnect to the JoyCon"""
        try:
            logger.debug("%s %s: reconnecting", id(self), self.serial)
            request_generation = self._get_connection_generation()
            await self._connect(request_generation, allow_reopen=False)
            logger.debug("%s %s: reconnecting done", id(self), self.serial)
            return True
        except ControllerConnectionError as exc:
            logger.debug("%s %s: reconnect failed: %s", id(self), self.serial, exc)
            return False
        except Exception:
            logger.exception("%s %s: reconnect failed", id(self), self.serial)
            return False

    def _schedule_reconnect_from_reader(self):
        with self._connection_lifecycle_lock:
            if self._closed or not self._should_run.is_set():
                return
            if self._reconnect_future and not self._reconnect_future.done():
                return
            if not self._loop or self._loop.is_closed():
                logger.debug("%s: cannot schedule reconnect; event loop is unavailable", self.serial)
                return
            self._reconnect_future = asyncio.run_coroutine_threadsafe(
                self.reconnect(),
                self._loop,
            )

    def _update_input_report(self):
        while self._should_run.is_set():
            try:
                self.preconnect_event.wait()
                if not self._joycon_device:
                    # Check again if the thread should run
                    if not self._should_run.is_set():
                        return
                    self._schedule_reconnect_from_reader()
                    time.sleep(self.reconnect_timeout)
                    continue

                report = [0]
                # TODO, handle input reports of type 0x21 and 0x3f
                while (len(report) == 0 or report[0] != 0x30) and self._should_run.is_set():
                    report = self._read_input_report()
                    if report is None:
                        raise OSError("joycon_device is None!")

                self._publish_input_report(report)
            except OSError:
                logger.exception("%s %s: connection lost to hid device", id(self), self.serial)
                self.is_connected.clear()
                try:
                    self._close()
                except Exception:
                    logger.debug("%s: error closing device after read failure", self.serial, exc_info=True)
                time.sleep(self.reconnect_timeout)
            except Exception:
                logger.exception("%s %s: Unexpected error in input report:", id(self), self.serial)
                time.sleep(self.reconnect_timeout)
        self._update_input_report_thread = None

    def _read_input_report(self) -> bytes:
        if self._joycon_device:
            return bytes(self._joycon_device.read(
                max_length=self._INPUT_REPORT_SIZE, timeout_ms=self._INPUT_READ_TIMEOUT_MS
            ))

    def _publish_input_report(self, report):
        if len(report) == 0 or report[0] != 0x30:
            return

        with self._connection_lifecycle_lock:
            if self._closed or not self._should_run.is_set():
                return
            self._input_report = report
            if self._accel_reports is not None:
                self._accel_reports.append(report)
            self.is_connected.set()
            input_hooks = tuple(self._input_hooks)

        for callback in input_hooks:
            callback(self)

    def _write_output_report(self, command, subcommand, argument):
        if not self._joycon_device:
            logger.debug(
                "%s: Wanted to write output report, but JoyCon is not connected",
                self.serial,
            )
            return

        packet_number = self._packet_number.to_bytes(1, byteorder='little')
        if command == b'\x10':
            report = b''.join([command, packet_number, self._rumble_data])
            expected_report_size = self._BASIC_OUTPUT_REPORT_SIZE
        else:
            report = b''.join([
                command,
                packet_number,
                self._rumble_data,
                subcommand,
                argument,
            ])
            expected_report_size = (
                self._COMMAND_OUTPUT_REPORT_SIZE
                if command == b'\x01'
                else len(report)
            )

        if len(report) > expected_report_size:
            raise ValueError(
                f"Output report {command.hex()} is {len(report)} bytes, "
                f"expected at most {expected_report_size}"
            )

        report = report.ljust(expected_report_size, b'\x00')
        logger.debug(
            "%s: JoyCon writing output report report_id=%s len=%s is_with_rumble=%s",
            self.serial,
            command.hex(),
            len(report),
            self._rumble_data != JoyCon._RUMBLE_DATA,
        )
        bytes_written = self._joycon_device.write(report)
        if bytes_written is not None and bytes_written != len(report):
            error = None
            if hasattr(self._joycon_device, "error"):
                try:
                    error = self._joycon_device.error()
                except Exception:
                    error = None
            logger.warning(
                "%s: JoyCon output report write returned %s, expected %s; hid_error=%s",
                self.serial,
                bytes_written,
                len(report),
                error,
            )
        self._packet_number = (self._packet_number + 1) & 0xF

    def _send_subcmd_get_response(
        self,
        subcommand,
        argument,
        timeout=None,
        response_validator=None,
    ) -> Tuple[bool, bytes]:
        # TODO: handle subcmd when daemon is running
        if timeout is None:
            timeout = self._subcommand_response_timeout

        self._write_output_report(b'\x01', subcommand, argument)

        deadline = time.monotonic() + timeout
        next_resend = time.monotonic() + self._subcommand_resend_interval
        num_attempts = 0
        empty_reports = 0
        seen_report_ids = {}
        seen_subcmd_replies = {}
        while True:  # TODO, avoid this, await daemon instead
            self._ensure_active_connection_is_not_stopped()
            report = self._read_input_report()
            num_attempts += 1
            now = time.monotonic()
            if now >= deadline:
                raise Exception(
                    f"Timeout reading input report after {timeout:.2f}s/{num_attempts} attempts; "
                    f"empty_reports={empty_reports}; "
                    f"seen_report_ids={seen_report_ids}; "
                    f"seen_subcmd_replies={seen_subcmd_replies}"
                )

            if not report:
                empty_reports += 1
            elif report[0] != 0x21:
                seen_report_ids[report[0]] = seen_report_ids.get(report[0], 0) + 1
            else:
                response = report[13:]
                if len(response) < 2 or response[1:2] != subcommand:
                    if len(response) >= 2:
                        seen_subcmd_replies[response[1]] = seen_subcmd_replies.get(response[1], 0) + 1
                # Check if response is valid for our request or some "stale" response for other request
                elif response_validator and not response_validator(response):
                    logger.debug(
                        "%s: ignoring stale subcommand response for %s: %s",
                        self.serial,
                        subcommand.hex(),
                        response.hex(),
                    )
                else:
                    # TODO: determine if the cut bytes are worth anything
                    return response[0] & 0x80, response  # (ack, data)

            if now >= next_resend:
                self._write_output_report(b'\x01', subcommand, argument)
                next_resend = now + self._subcommand_resend_interval

    def _wait_for_any_input_report(self):
        empty_reports = 0
        for _ in range(self._first_report_attempts):
            self._ensure_active_connection_is_not_stopped()
            report = self._read_input_report()
            if not report:
                empty_reports += 1
                continue
            return report

        raise ControllerConnectionError(
            f"{id(self)} {self.serial}: no input reports after full-mode setup; "
            f"empty_reports={empty_reports}"
        )

    def _prepare_input_reports(self):
        """Switch sends this after every connection, then asks for full report mode.

        Full mode is required before SPI reads reliably return input report 0x21.
        """
        self._write_output_report(b'\x01', b'\x08', b'\x00')
        time.sleep(0.02)
        self._write_output_report(b'\x01', b'\x03', b'\x30')
        time.sleep(0.02)

    def _spi_flash_read(self, address, size) -> bytes:
        assert size <= 0x1d
        argument = address.to_bytes(4, "little") + size.to_bytes(1, "little")

        def is_matching_spi_flash_read_response(response):
            return len(response) >= 7 and response[2:7] == argument

        ack, report = self._send_subcmd_get_response(
            b'\x10',
            argument,
            response_validator=is_matching_spi_flash_read_response,
        )
        if not ack:
            raise IOError(f"After SPI read @ {address:#06x}: got NACK")

        if report[:2] != b'\x90\x10':
            raise IOError(f"Something else than the expected ACK was received! {report}")
        assert report[2:7] == argument, (report[2:5], argument)

        return report[7:size + 7]

    def _read_joycon_data(self):
        try:
            color_data = self._spi_flash_read(0x6050, 12 if self.is_pro() else 6)
            if len(color_data) < 6:
                raise ValueError("Incomplete controller color data")
            self.color_body = tuple(color_data[:3])
            self.color_btn = tuple(color_data[3:6])
            if self.is_pro():
                left = tuple(color_data[6:9])
                right = tuple(color_data[9:12])
                self.color_grip_left = left if len(left) == 3 and left != (255, 255, 255) else None
                self.color_grip_right = right if len(right) == 3 and right != (255, 255, 255) else None
        except Exception:
            logger.warning("%s: Error reading JoyCon colors; using defaults", self.serial)
            logger.debug("%s: Error reading JoyCon colors", self.serial, exc_info=True)

        self._set_default_stick_calibration()
        stick_sides = ("left", "right") if self.is_pro() else (
            "left" if self.is_left() else "right",
        )
        for side in stick_sides:
            try:
                self.stick_cal[side] = self._read_stick_calibration_data(side)
            except Exception:
                logger.warning("%s: Error reading %s stick calibration; using defaults", self.serial, side)
                logger.debug("%s: Error reading stick calibration data", self.serial, exc_info=True)

            try:
                buf = self._spi_flash_read(0x6086 if side == "left" else 0x6098, 16)
                self.deadzone[side] = (buf[4] << 8) & 0xF00 | buf[3]
            except Exception:
                logger.warning("%s: Error reading stick device parameters; using defaults", self.serial)
                logger.debug("%s: Error reading stick device parameters", self.serial, exc_info=True)

        try:
            # user IMU data
            if self._spi_flash_read(0x8026, 2) == b"\xB2\xA1":
                imu_cal = self._spi_flash_read(0x8028, 24)
            # factory IMU data
            else:
                imu_cal = self._spi_flash_read(0x6020, 24)

            self.set_accel_calibration((
                self._to_int16le_from_2bytes(imu_cal[0], imu_cal[1]),
                self._to_int16le_from_2bytes(imu_cal[2], imu_cal[3]),
                self._to_int16le_from_2bytes(imu_cal[4], imu_cal[5]),
            ), (
                self._to_int16le_from_2bytes(imu_cal[6], imu_cal[7]),
                self._to_int16le_from_2bytes(imu_cal[8], imu_cal[9]),
                self._to_int16le_from_2bytes(imu_cal[10], imu_cal[11]),
            ))
        except Exception:
            logger.warning("%s: Error reading IMU calibration data; using defaults", self.serial)
            logger.debug("%s: Error reading IMU calibration data", self.serial, exc_info=True)

    def _read_stick_calibration_data(self, side):
        is_left = side == "left"
        user_stick_cal_addr = 0x8012 if is_left else 0x801D
        use_user_data = False
        buf = None
        try:
            if self._spi_flash_read(user_stick_cal_addr - 2, 2) == b"\xB2\xA1":
                buf = self._spi_flash_read(user_stick_cal_addr, 9)
                use_user_data = True
        except Exception:
            logger.warning("%s: Error reading user stick calibration data", self.serial)
            logger.debug("%s: Error reading user stick calibration data", self.serial, exc_info=True)

        try:
            if not use_user_data:
                factory_stick_cal_addr = 0x603D if is_left else 0x6046
                buf = self._spi_flash_read(factory_stick_cal_addr, 9)
        except Exception:
            logger.warning("%s: Error reading factory stick calibration data", self.serial)
            logger.debug("%s: Error reading factory stick calibration data", self.serial, exc_info=True)

        if buf is None:
            return self.stick_cal[side]

        if len(buf) != 9 or buf == b"\xFF" * 9:
            raise ValueError("Invalid stick calibration data")
        stick_cal = [0] * 6

        # X Axis Max above center
        stick_cal[0 if is_left else 2] = (buf[1] << 8) & 0xF00 | buf[0]
        # Y Axis Max above center
        stick_cal[1 if is_left else 3] = (buf[2] << 4) | (buf[1] >> 4)
        # X Axis Center
        stick_cal[2 if is_left else 4] = (buf[4] << 8) & 0xF00 | buf[3]
        # Y Axis Center
        stick_cal[3 if is_left else 5] = (buf[5] << 4) | (buf[4] >> 4)
        # X Axis Min below center
        stick_cal[4 if is_left else 0] = (buf[7] << 8) & 0xF00 | buf[6]
        # Y Axis Min below center
        stick_cal[5 if is_left else 1] = (buf[8] << 4) | (buf[7] >> 4)
        if any(value <= 0 for value in stick_cal):
            raise ValueError("Invalid stick calibration range")
        return stick_cal

    def _set_default_stick_calibration(self):
        # Some third-party or half-awake JoyCons do not return calibration data.
        # Use measured fallback values so the controller can still initialize.
        self.stick_cal = {
            "left": [1066, 807, 1943, 2377, 956, 1014],
            "right": [1029, 1022, 2176, 1618, 936, 821],
        }
        if self.is_pro():
            # Match hid-nintendo's fallback center (2000) and range (500–3500).
            self.stick_cal = {
                side: [1500, 1500, 2000, 2000, 1500, 1500]
                for side in ("left", "right")
            }

    def _setup_sensors(self):
        logger.debug("%s: _setup_sensors", self.serial)
        # Enable 6 axis sensors
        self._write_output_report(b'\x01', b'\x40', b'\x01')
        # It needs delta time to update the setting
        time.sleep(0.02)
        # Change format of input report
        self._write_output_report(b'\x01', b'\x03', b'\x30')
        logger.debug("%s: _setup_sensors done", self.serial)

    @staticmethod
    def _to_int16le_from_2bytes(hbytebe, lbytebe):
        uint16le = (lbytebe << 8) | hbytebe
        int16le = uint16le if uint16le < 32768 else uint16le - 65536
        return int16le

    def _get_nbit_from_input_report(self, offset_byte, offset_bit, nbit):
        byte = self._input_report[offset_byte]
        return (byte >> offset_bit) & ((1 << nbit) - 1)

    def __del__(self):
        self._should_run.clear()
        self._close()

    def set_accel_calibration(self, offset_xyz=None, coeff_xyz=None):
        if offset_xyz and coeff_xyz:
            self._ACCEL_OFFSET_X, self._ACCEL_OFFSET_Y, self._ACCEL_OFFSET_Z = offset_xyz

            cx, cy, cz = coeff_xyz
            self._ACCEL_COEFF_X = (1.0 / (cx - self._ACCEL_OFFSET_X)) * 4.0
            self._ACCEL_COEFF_Y = (1.0 / (cy - self._ACCEL_OFFSET_Y)) * 4.0
            self._ACCEL_COEFF_Z = (1.0 / (cz - self._ACCEL_OFFSET_Z)) * 4.0

    def get_actual_stick_value(self, pre_cal, orientation, side):  # X/Horizontal = 0, Y/Vertical = 1
        stick_cal = self.stick_cal[side]
        diff = pre_cal - stick_cal[2 + orientation]
        if (abs(diff) < self.deadzone[side]):
            return 0
        elif diff > 0:  # Axis is above center
            return diff / stick_cal[orientation]
        else:
            return diff / stick_cal[4 + orientation]

    def register_update_hook(self, callback):
        self._input_hooks.append(callback)
        return callback  # this makes it so you could use it as a decorator

    def is_left(self):
        return self.product_id == JOYCON_L_PRODUCT_ID

    def is_right(self):
        return self.product_id == JOYCON_R_PRODUCT_ID

    def is_pro(self):
        return self.product_id == JOYCON_PRO_PRODUCT_ID

    def get_battery_charging(self):
        return self._get_nbit_from_input_report(2, 4, 1)

    def get_battery_level(self):
        return self._get_nbit_from_input_report(2, 5, 3)

    def get_button_y(self):
        return self._get_nbit_from_input_report(3, 0, 1)

    def get_button_x(self):
        return self._get_nbit_from_input_report(3, 1, 1)

    def get_button_b(self):
        return self._get_nbit_from_input_report(3, 2, 1)

    def get_button_a(self):
        return self._get_nbit_from_input_report(3, 3, 1)

    def get_button_right_sr(self):
        return self._get_nbit_from_input_report(3, 4, 1)

    def get_button_right_sl(self):
        return self._get_nbit_from_input_report(3, 5, 1)

    def get_button_r(self):
        return self._get_nbit_from_input_report(3, 6, 1)

    def get_button_zr(self):
        return self._get_nbit_from_input_report(3, 7, 1)

    def get_button_minus(self):
        return self._get_nbit_from_input_report(4, 0, 1)

    def get_button_plus(self):
        return self._get_nbit_from_input_report(4, 1, 1)

    def get_button_r_stick(self):
        return self._get_nbit_from_input_report(4, 2, 1)

    def get_button_l_stick(self):
        return self._get_nbit_from_input_report(4, 3, 1)

    def get_button_home(self):
        return self._get_nbit_from_input_report(4, 4, 1)

    def get_button_capture(self):
        return self._get_nbit_from_input_report(4, 5, 1)

    def get_button_charging_grip(self):
        return self._get_nbit_from_input_report(4, 7, 1)

    def get_button_down(self):
        return self._get_nbit_from_input_report(5, 0, 1)

    def get_button_up(self):
        return self._get_nbit_from_input_report(5, 1, 1)

    def get_button_right(self):
        return self._get_nbit_from_input_report(5, 2, 1)

    def get_button_left(self):
        return self._get_nbit_from_input_report(5, 3, 1)

    def get_button_left_sr(self):
        return self._get_nbit_from_input_report(5, 4, 1)

    def get_button_left_sl(self):
        return self._get_nbit_from_input_report(5, 5, 1)

    def get_button_l(self):
        return self._get_nbit_from_input_report(5, 6, 1)

    def get_button_zl(self):
        return self._get_nbit_from_input_report(5, 7, 1)

    def get_stick_left_horizontal(self):
        if not (self.is_left() or self.is_pro()):
            return 0

        pre_cal = self._get_nbit_from_input_report(6, 0, 8) \
            | (self._get_nbit_from_input_report(7, 0, 4) << 8)
        return self.get_actual_stick_value(pre_cal, 0, "left")

    def get_stick_left_vertical(self):
        if not (self.is_left() or self.is_pro()):
            return 0

        pre_cal = self._get_nbit_from_input_report(7, 4, 4) \
            | (self._get_nbit_from_input_report(8, 0, 8) << 4)
        return self.get_actual_stick_value(pre_cal, 1, "left")

    def get_stick_right_horizontal(self):
        if self.is_left():
            return 0

        pre_cal = self._get_nbit_from_input_report(9, 0, 8) \
            | (self._get_nbit_from_input_report(10, 0, 4) << 8)
        return self.get_actual_stick_value(pre_cal, 0, "right")

    def get_stick_right_vertical(self):
        if self.is_left():
            return 0

        pre_cal = self._get_nbit_from_input_report(10, 4, 4) \
            | (self._get_nbit_from_input_report(11, 0, 8) << 4)
        return self.get_actual_stick_value(pre_cal, 1, "right")

    def set_accel_collection(self, enabled):
        with self._connection_lifecycle_lock:
            self._accel_reports = [] if enabled else None

    def get_accel_events(self):
        """Drain received motion reports without consuming the status snapshot."""
        with self._connection_lifecycle_lock:
            if self._accel_reports is None:
                return []
            reports, self._accel_reports = self._accel_reports, []
        return [sample for report in reports for sample in self.get_accels(report)]

    def get_accels(self, input_report=None):
        if input_report is None:
            input_report = bytes(self._input_report)
        accels = []

        for idx in range(3):
            x = self.get_accel_x(input_report, sample_idx=idx)
            y = self.get_accel_y(input_report, sample_idx=idx)
            z = self.get_accel_z(input_report, sample_idx=idx)
            accels.append((x, y, z))

        return accels

    def get_accel_x(self, input_report=None, sample_idx=0):
        if not input_report:
            input_report = self._input_report

        if sample_idx not in (0, 1, 2):
            raise IndexError('sample_idx should be between 0 and 2')
        data = self._to_int16le_from_2bytes(
            input_report[13 + sample_idx * 12],
            input_report[14 + sample_idx * 12])
        return data * self._ACCEL_COEFF_X

    def get_accel_y(self, input_report=None, sample_idx=0):
        if not input_report:
            input_report = self._input_report

        if sample_idx not in (0, 1, 2):
            raise IndexError('sample_idx should be between 0 and 2')
        data = self._to_int16le_from_2bytes(
            input_report[15 + sample_idx * 12],
            input_report[16 + sample_idx * 12])
        return data * self._ACCEL_COEFF_Y * (1 if self.is_left() else -1)

    def get_accel_z(self, input_report=None, sample_idx=0):
        if not input_report:
            input_report = self._input_report

        if sample_idx not in (0, 1, 2):
            raise IndexError('sample_idx should be between 0 and 2')
        data = self._to_int16le_from_2bytes(
            input_report[17 + sample_idx * 12],
            input_report[18 + sample_idx * 12])
        return data * self._ACCEL_COEFF_Z * (1 if self.is_left() else -1)

    def get_status(self) -> dict:
        return {
            "battery": {
                "charging": self.get_battery_charging(),
                "level": self.get_battery_level(),
            },
            "buttons": {
                "right": {
                    "y": self.get_button_y(),
                    "x": self.get_button_x(),
                    "b": self.get_button_b(),
                    "a": self.get_button_a(),
                    "sr": self.get_button_right_sr(),
                    "sl": self.get_button_right_sl(),
                    "r": self.get_button_r(),
                    "zr": self.get_button_zr(),
                },
                "shared": {
                    "minus": self.get_button_minus(),
                    "plus": self.get_button_plus(),
                    "r-stick": self.get_button_r_stick(),
                    "l-stick": self.get_button_l_stick(),
                    "home": self.get_button_home(),
                    "capture": self.get_button_capture(),
                    "charging-grip": self.get_button_charging_grip(),
                },
                "left": {
                    "down": self.get_button_down(),
                    "up": self.get_button_up(),
                    "right": self.get_button_right(),
                    "left": self.get_button_left(),
                    "sr": self.get_button_left_sr(),
                    "sl": self.get_button_left_sl(),
                    "l": self.get_button_l(),
                    "zl": self.get_button_zl(),
                }
            },
            "analog-sticks": {
                "left": {
                    "horizontal": self.get_stick_left_horizontal(),
                    "vertical": self.get_stick_left_vertical(),
                },
                "right": {
                    "horizontal": self.get_stick_right_horizontal(),
                    "vertical": self.get_stick_right_vertical(),
                },
            },
            "accel": self.get_accels(),
        }

    def encode_rumble_data(self, frequency=320.0, amplitude=0.0):
        """Encode rumble data for the JoyCon.
        
        Args:
            frequency (float): Frequency in Hz (valid range: 40.875885-1252.572266)
            amplitude (float): Amplitude from 0.0 to 1.0
        """
        # Clamp values to valid ranges
        frequency = max(40.875885, min(1252.572266, frequency))
        amplitude = max(0.0, min(1.0, amplitude))

        # Encode frequency
        freq_encoded = int(round(math.log2(frequency/10.0)*32.0))
        freq_data = bytes([
            ((freq_encoded >> 8) & 0xFF) + 0x40, 
            freq_encoded & 0xFF
        ])

        # Encode amplitude
        amp_encoded = int(round(amplitude * 100))
        amp_data = bytes([
            (amp_encoded << 1) & 0xFF,
            0x40
        ])

        # Combine data for both motors (HD and LRA)
        return amp_data + freq_data + amp_data + freq_data

    @property
    def rumble_enabled(self):
        return self._rumble_enabled

    @rumble_enabled.setter
    def rumble_enabled(self, enabled):
        if not enabled:
            self.stop_rumble()
        self._rumble_enabled = enabled

    def rumble(self, frequency=320.0, amplitude=0.0):
        """Send a rumble effect to the JoyCon.
        
        Args:
            frequency (float): Frequency in Hz (40.875885-1252.572266)
            amplitude (float): Amplitude from 0.0 to 1.0
        """
        logger.debug("JoyCon rumble: %s %s %s", self.rumble_enabled, frequency, amplitude)
        if not self.rumble_enabled:
            return

        self._rumble_data = self.encode_rumble_data(frequency, amplitude)
        # Send a dummy subcommand to trigger the rumble
        self._write_output_report(b'\x10', b'\x00', b'\x00')

    def stop_rumble(self):
        """Stop any active rumble effect"""
        self._rumble_data = self._RUMBLE_DATA
        self._write_output_report(b'\x10', b'\x00', b'\x00')

    def set_player_lamp_on(self, on_pattern: int):
        self._write_output_report(
            b'\x01', b'\x30',
            (on_pattern & 0xF).to_bytes(1, byteorder='little'))

    def set_player_lamp_flashing(self, flashing_pattern: int):
        self._write_output_report(
            b'\x01', b'\x30',
            ((flashing_pattern & 0xF) << 4).to_bytes(1, byteorder='little'))

    def set_player_lamp(self, pattern: int):
        logger.debug("%s %s: set_player_lamp %s", id(self), self.serial, pattern)
        self._write_output_report(
            b'\x01', b'\x30',
            pattern.to_bytes(1, byteorder='little'))
