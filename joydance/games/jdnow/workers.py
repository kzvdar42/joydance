"""Process workers shared by the JDNow controllers in one JoyDance instance."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import logging
from multiprocessing import get_context
import os

from .scoring import score_adaptive_move


logger = logging.getLogger(__name__)


class JDNowScoringWorkers:
    def __init__(self):
        self._capacity = min(6, os.cpu_count() or 1)
        self._executor = None
        self._warmup = ()
        self._unavailable = False

    def prepare(self, connected_count):
        if connected_count < 2 or self._capacity < 2 or self._executor is not None or self._unavailable:
            return
        try:
            self._executor = ProcessPoolExecutor(max_workers=self._capacity, mp_context=get_context("spawn"))
            self._warmup = tuple(self._executor.submit(os.getpid) for _ in range(self._capacity))
            logger.debug("JDN warming %d scoring processes", self._capacity)
        except (OSError, RuntimeError) as error:
            self._disable(error)

    def _disable(self, error):
        if not self._unavailable:
            self._unavailable = True
            logger.warning("JDN scoring processes unavailable (%s); using local scoring", type(error).__name__)

    async def score(self, *args):
        if self._executor is not None and not self._unavailable and all(future.done() for future in self._warmup):
            try:
                return await asyncio.get_running_loop().run_in_executor(self._executor, score_adaptive_move, *args)
            except (BrokenProcessPool, OSError) as error:
                self._disable(error)
        return await asyncio.to_thread(score_adaptive_move, *args)

    def close(self):
        self._unavailable = True
        if self._executor is not None:
            self._executor.shutdown(cancel_futures=True)
            self._executor = None
