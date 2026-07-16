"""Einfacher Hintergrund-Job-Manager mit Fortschrittsanzeige fürs Dashboard."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


class JobAbgebrochen(Exception):
    """Vom Nutzer abgebrochen - wird beim nächsten Fortschritts-Checkpoint
    aus dem progress-Callback geworfen und darf nirgends verschluckt
    werden (auch nicht von der Optional-Schritt-Toleranz der Pipeline)."""

    def __str__(self) -> str:  # noqa: D105
        return "vom Nutzer abgebrochen"


@dataclass
class Job:
    id: str
    name: str
    status: str = "laeuft"  # laeuft | fertig | fehler | abgebrochen
    fortschritt: float = 0.0
    meldung: str = ""
    fehler: str | None = None
    ergebnis: Any = None
    gestartet: float = field(default_factory=time.time)
    beendet: float | None = None
    abbruch: threading.Event = field(default_factory=threading.Event)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "fortschritt": round(self.fortschritt, 3),
            "meldung": self.meldung,
            "fehler": self.fehler,
            "ergebnis": self.ergebnis,
            "laufzeit_sekunden": round(
                (self.beendet or time.time()) - self.gestartet, 1),
        }


@dataclass
class JobManager:
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self, name: str, fn: Callable[..., Any]) -> Job:
        """Startet fn(progress) in einem Thread. progress(f, meldung) meldet Stand."""
        job = Job(id=uuid.uuid4().hex[:12], name=name)
        with self._lock:
            self._jobs[job.id] = job

        def progress(fraction: float, meldung: str = "") -> None:
            if job.abbruch.is_set():
                raise JobAbgebrochen()
            job.fortschritt = max(0.0, min(1.0, float(fraction)))
            if meldung:
                job.meldung = meldung

        def runner() -> None:
            try:
                job.ergebnis = fn(progress)
                job.status = "fertig"
                job.fortschritt = 1.0
            except JobAbgebrochen:
                job.status = "abgebrochen"
                job.meldung = "abgebrochen"
            except Exception as exc:  # noqa: BLE001 - Fehler ans Dashboard melden
                job.status = "fehler"
                job.fehler = f"{type(exc).__name__}: {exc}"
                job.meldung = str(exc)
                traceback.print_exc()
            finally:
                job.beendet = time.time()

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running_for(self, name_prefix: str) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.status == "laeuft" and job.name.startswith(name_prefix):
                    return job
        return None

    def running_any(self) -> Job | None:
        with self._lock:
            for job in self._jobs.values():
                if job.status == "laeuft":
                    return job
        return None

    def cancel(self, job_id: str) -> Job | None:
        """Abbruch anfordern; greift beim nächsten Fortschritts-Checkpoint
        (laufende ffmpeg-Prozesse werden dabei beendet)."""
        job = self.get(job_id)
        if job is not None and job.status == "laeuft":
            job.abbruch.set()
            job.meldung = "Abbruch angefordert …"
        return job


MANAGER = JobManager()
