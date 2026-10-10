from concurrent.futures import ThreadPoolExecutor


HID_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="joydance-hid",
)
