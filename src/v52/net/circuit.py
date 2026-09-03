from __future__ import annotations

import dataclasses
import time


@dataclasses.dataclass(slots=True)
class CircuitState:
    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, base_cooldown: float = 15.0, max_cooldown: float = 300.0):
        self.failure_threshold = max(1, failure_threshold)
        self.base_cooldown = max(1.0, base_cooldown)
        self.max_cooldown = max(self.base_cooldown, max_cooldown)
        self._states: dict[str, CircuitState] = {}

    def allow(self, key: str) -> tuple[bool, float]:
        st = self._states.get(key)
        if not st:
            return True, 0.0
        now = time.monotonic()
        if st.opened_until > now:
            return False, st.opened_until - now
        return True, 0.0

    def success(self, key: str) -> None:
        st = self._states.setdefault(key, CircuitState())
        st.failures = 0
        st.opened_until = 0.0
        st.last_error = ""

    def failure(self, key: str, error: str) -> float:
        st = self._states.setdefault(key, CircuitState())
        st.failures += 1
        st.last_error = error[:500]
        if st.failures >= self.failure_threshold:
            exponent = min(8, st.failures - self.failure_threshold)
            cooldown = min(self.max_cooldown, self.base_cooldown * (2**exponent))
            st.opened_until = time.monotonic() + cooldown
            return cooldown
        return 0.0
