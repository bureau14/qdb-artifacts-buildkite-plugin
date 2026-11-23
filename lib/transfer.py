"""Many files at once, each with its own client and retries.

Files travel ``parallel`` at a time, each with ``concurrency`` multipart threads of boto3's
transfer manager. Every attempt at a file gets a boto3 client of its own, because a client is
not safe to share between threads.
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


def one_file(cfg, name, attempt):
    """Call attempt(client) until it succeeds or the retries run out, each time with
    a new client; the seconds it took, retries included."""
    start = time.monotonic()
    with_retry(lambda: attempt(s3_client(cfg)), name)
    return time.monotonic() - start


def run(transfer_one, items, parallel):
    """Call transfer_one(item) for every item, `parallel` at a time, and wait for all of
    them; the seconds from the first transfer to the last. The first failure is raised."""
    start = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futures = [pool.submit(transfer_one, item) for item in items]
    try:
        for future in as_completed(futures):
            future.result()
    except KeyboardInterrupt:
        # shutdown(wait=True) would block for minutes on the transfers in flight; 130 is the
        # POSIX status of a process that SIGINT ended.
        os._exit(130)
    pool.shutdown()
    return time.monotonic() - start


def log_done(verb, count, total_bytes, seconds):
    speed = total_bytes / seconds if seconds > 0 else 0
    log(f"{verb} {count} file(s), {fmt_size(total_bytes)} in {seconds:.1f}s ({fmt_size(speed)}/s)")
