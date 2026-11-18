"""Many files at once, each with its own client and retries.

Files travel `parallel` at a time (default 4), each with `concurrency` multipart
threads of boto3's transfer manager (default 32), so the defaults allow up to
128 streams at once. Every attempt at a file gets a boto3 client of its own,
because a client is not safe to share between threads.

A Ctrl-C while files travel ends the process at once with status 130, the
POSIX convention for SIGINT: ThreadPoolExecutor.shutdown(wait=True) would block
for minutes waiting for the transfers in flight.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from boto3.s3.transfer import TransferConfig

from .clients import s3_client
from .log import fmt_size, log
from .retry import with_retry


def transfer_config(concurrency):
    return TransferConfig(max_concurrency=concurrency)


def log_settings(parallel, concurrency):
    log(f"  parallel={parallel}, concurrency={concurrency}")


def one_file(cfg, auth, name, attempt):
    """Call attempt(client) until it succeeds or the retries run out, each time with
    a new client; the seconds it took, retries included."""
    start = time.monotonic()
    with_retry(lambda: attempt(s3_client(cfg, auth)), name)
    return time.monotonic() - start


def run(transfer_one, items, parallel, then=None):
    """Call transfer_one(item) for every item, `parallel` at a time, wait for all of
    them, then call then(); the seconds from the first transfer to the end of then().

    The first failure is raised. then() runs under the same Ctrl-C rule as the
    transfers, so that an upload's manifest, written there, ends the same way.
    """
    start = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futures = [pool.submit(transfer_one, item) for item in items]
    try:
        for future in as_completed(futures):
            future.result()
        if then is not None:
            then()
    except KeyboardInterrupt:
        os._exit(130)
    pool.shutdown()
    return time.monotonic() - start


def log_done(verb, count, total_bytes, seconds):
    speed = total_bytes / seconds if seconds > 0 else 0
    log(f"{verb} {count} file(s), {fmt_size(total_bytes)} in {seconds:.1f}s ({fmt_size(speed)}/s)")
