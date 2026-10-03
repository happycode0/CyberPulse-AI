"""Publishing as the worker actually performs it: supply the connection and the output directory.

`worker/publish/build.py` deliberately takes a `Connection` and an `out_dir` as arguments so it can
be tested without a database or a real data directory. This is the thin layer that supplies both
from settings, mirroring how `worker/pipeline/run.py` wires the pure pipeline modules to the
database. Both the CLI (`--publish`) and the scheduler call it, so a scheduled publish and a
hand-run one cannot drift apart.

The scheduler then pushes what it published (`push_now`); `--publish` does not, so it stays safe
to run anywhere.
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from worker.db.session import get_engine
from worker.publish.build import build_all
from worker.publish.push import PushResult, publish_token_configured, push_data
from worker.settings import get_settings

logger = logging.getLogger(__name__)

# One publish at a time. `build_all` stages every file and then moves them into place with
# os.replace, so two publishes running at once would interleave their renames and could leave
# data/ holding some files from one build and some from another — a combination no single
# collection ever produced, and one the schema validation cannot catch because each file is
# individually valid. The cadences make this reachable rather than theoretical: the FAST lane runs
# at the top of every hour, and the NORMAL lane ("0 */4 * * *") at 00:00, 04:00, 08:00 and so on.
_PUBLISH_LOCK = asyncio.Lock()


async def publish_now(*, now: datetime | None = None) -> list[Path]:
    """Build and atomically write the public JSON files, returning the paths written.

    Parameters
    ----------
    now : datetime | None
        The moment to stamp the payloads with. Defaults to now, in UTC.

    Returns
    -------
    list[Path]
        Every file written.

    Raises
    ------
    ValidationFailure
        If any payload fails the secret scan or its schema. Nothing was written in that case —
        `build_all` is fail-closed, and the previously published files are left untouched.
    """
    moment = now if now is not None else datetime.now(UTC)
    async with _PUBLISH_LOCK:
        # build_all is synchronous and does both database and filesystem work. Off the event loop,
        # because the caller is usually the scheduler: a publish that blocked the loop would also
        # delay the next lane's trigger, and a slow publish would be indistinguishable from a
        # stalled collection.
        return await asyncio.to_thread(_build, moment)


def _build(now: datetime) -> list[Path]:
    with get_engine().connect() as conn:
        return build_all(conn, get_settings().data_dir, now=now)


async def push_now() -> PushResult | None:
    """Push `data/` to the `data` branch the site is built from, or return None when this host
    holds no publish token (building needs none, so such a host stops at `data/`).

    Under the publish lock: the push hashes the files while it runs, and a build moving new ones
    into place at the same time would push a mix of the two.

    Raises
    ------
    RuntimeError
        If a git command fails or hangs. Its message is already free of the token.
    """
    if not publish_token_configured():
        return None
    async with _PUBLISH_LOCK:
        return await asyncio.to_thread(push_data, get_settings().data_dir)
