"""Concurrency helpers: probe and command many devices at once.

Discovery can return several accessories; issuing HTTP requests to all
of them sequentially makes commands feel sluggish (each device state
GET is polled at ~1 s by design). A small thread fan-out keeps the CLI
snappy while staying dependency-free. Threads are plain worker threads
joined before exit -- no daemon threads are left behind.
"""

import threading
from typing import Any, Callable, List, Optional, Tuple

from . import discovery
from .device import ElgatoDevice


def _run_capture(fn: Callable[[], Any]) -> Tuple[bool, Any]:
    """Run fn(), capturing the result or exception as (ok, payload)."""
    try:
        return True, fn()
    except Exception as err:  # noqa: BLE001 - collected and re-reported
        return False, err


def query_devices(
    found: List[Any], timeout: float = 4.0
) -> List[Any]:
    """Filter mDNS results down to devices whose HTTP API actually answers.

    Each candidate is probed concurrently with GET /elgato/lights so a
    stale cache entry (device powered off) never blocks the command.
    Devices whose endpoint is missing (firmware < 1.0.3 quirk, 404) are
    still considered present, since /elgato/lights is available even then.
    """
    if len(found) <= 1:
        return found

    results: List[Tuple[int, Any]] = [None] * len(found)  # type: ignore[list-item]
    threads: List[threading.Thread] = []

    def probe(index: int, candidate: Any) -> None:
        client = ElgatoDevice(
            host=candidate.ip,
            port=candidate.port,
            name=candidate.name,
            model=candidate.model,
            mac=candidate.mac,
            timeout=timeout,
        )
        try:
            client.get_state()
            results[index] = candidate
        except Exception:
            results[index] = None

    for index, candidate in enumerate(found):
        thread = threading.Thread(target=probe, args=(index, candidate))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()

    return [r for r in results if r is not None]


def act_on_devices(
    targets: List[ElgatoDevice], fn: Callable[[ElgatoDevice], Any], parallel: bool
) -> Tuple[List[Tuple[ElgatoDevice, Any]], List[Tuple[ElgatoDevice, Exception]]]:
    """Apply `fn` to every target device.

    Returns ``(successes, failures)`` where each entry pairs the device
    with its result or exception. When `parallel` is true and more than
    one device is targeted, each device gets its own thread; ordering of
    `successes` still follows the input order (results are slotted).
    """
    successes: List[Tuple[ElgatoDevice, Any]] = []
    failures: List[Tuple[ElgatoDevice, Exception]] = []

    if not parallel or len(targets) == 1:
        for target in targets:
            try:
                successes.append((target, fn(target)))
            except Exception as err:  # noqa: BLE001
                failures.append((target, err))
        return successes, failures

    slots: List[Tuple[bool, Any]] = [None] * len(targets)  # type: ignore[list-item]
    threads: List[threading.Thread] = []

    def worker(index: int, target: ElgatoDevice) -> None:
        try:
            slots[index] = (True, fn(target))
        except Exception as err:  # noqa: BLE001
            slots[index] = (False, err)

    for index, target in enumerate(targets):
        thread = threading.Thread(target=worker, args=(index, target))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()

    for target, slot in zip(targets, slots):
        if slot is None:
            continue
        ok, payload = slot
        if ok:
            successes.append((target, payload))
        else:
            failures.append((target, payload))
    return successes, failures
