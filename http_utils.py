"""Közös, újrapróbálkozós HTTP GET a MAVIR-gyűjtőknek.

A MAVIR rtdwweb szervere időnként 429-et (Too Many Requests) vagy
átmeneti 5xx hibát ad - ez nem feltétlenül jelenti, hogy a gyűjtés
véglegesen meghiúsult, ezért néhányszor újrapróbáljuk, növekvő
várakozással, mielőtt feladnánk.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)


def get_with_retry(
    url: str,
    headers: dict,
    timeout: int = 30,
    max_retries: int = 4,
    backoff_seconds: float = 5.0,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"{resp.status_code} - átmeneti hiba", response=resp
                )
            resp.raise_for_status()
            return resp
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = backoff_seconds * attempt
                log.warning("HTTP hiba (%s. próbálkozás/%s): %s - várakozás %.0f mp", attempt, max_retries, exc, wait)
                time.sleep(wait)
    raise last_exc
