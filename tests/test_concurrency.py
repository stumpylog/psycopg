"""
Tests dealing with concurrency issues.
"""

import os
import sys
import time
import queue
import pytest
import shutil
import tempfile
import threading
import subprocess as sp

import psycopg3


@pytest.mark.slow
def test_concurrent_execution(dsn):
    def worker():
        cnn = psycopg3.connect(dsn)
        cur = cnn.cursor()
        cur.execute("select pg_sleep(0.5)")
        cur.close()
        cnn.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t0 = time.time()
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert time.time() - t0 < 0.8, "something broken in concurrency"


@pytest.mark.slow
def test_commit_concurrency(conn):
    # Check the condition reported in psycopg2#103
    # Because of bad status check, we commit even when a commit is already on
    # its way. We can detect this condition by the warnings.
    notices = queue.Queue()
    conn.add_notice_handler(lambda diag: notices.put(diag.message_primary))
    stop = False

    def committer():
        nonlocal stop
        while not stop:
            conn.commit()

    cur = conn.cursor()
    t1 = threading.Thread(target=committer)
    t1.start()
    for i in range(1000):
        cur.execute("select %s;", (i,))
        conn.commit()

    # Stop the committer thread
    stop = True

    assert notices.empty(), "%d notices raised" % notices.qsize()


@pytest.mark.slow
def test_multiprocess_close(dsn):
    # Check the problem reported in psycopg2#829
    # Subprocess gcs the copy of the fd after fork so it closes connection.
    module = f"""\
import time
import psycopg3

def thread():
    conn = psycopg3.connect({repr(dsn)})
    curs = conn.cursor()
    for i in range(10):
        curs.execute("select 1")
        time.sleep(0.1)

def process():
    time.sleep(0.2)
"""

    script = """\
import time
import threading
import multiprocessing
import mptest

t = threading.Thread(target=mptest.thread, name='mythread')
t.start()
time.sleep(0.2)
multiprocessing.Process(target=mptest.process, name='myprocess').start()
t.join()
"""

    dir = tempfile.mkdtemp()
    try:
        with open(os.path.join(dir, "mptest.py"), "w") as f:
            f.write(module)
        env = dict(os.environ)
        env["PYTHONPATH"] = dir + os.pathsep + env.get("PYTHONPATH", "")
        out = sp.check_output(
            [sys.executable, "-c", script], stderr=sp.STDOUT, env=env
        ).decode("utf8", "replace")
        assert out == "", out.strip().splitlines()[-1]
    finally:
        shutil.rmtree(dir, ignore_errors=True)


@pytest.mark.slow
def test_notifies(conn, dsn):
    nconn = psycopg3.connect(dsn)
    npid = nconn.pgconn.backend_pid

    def notifier():
        time.sleep(0.25)
        nconn.pgconn.exec_(b"notify foo, '1'")
        time.sleep(0.25)
        nconn.pgconn.exec_(b"notify foo, '2'")
        nconn.close()

    conn.pgconn.exec_(b"listen foo")
    t0 = time.time()
    t = threading.Thread(target=notifier)
    t.start()
    ns = []
    gen = conn.notifies()
    for n in gen:
        ns.append((n, time.time()))
        if len(ns) >= 2:
            gen.send(True)
    assert len(ns) == 2

    n, t1 = ns[0]
    assert n.pid == npid
    assert n.channel == "foo"
    assert n.payload == "1"
    assert t1 - t0 == pytest.approx(0.25, abs=0.05)

    n, t1 = ns[1]
    assert n.pid == npid
    assert n.channel == "foo"
    assert n.payload == "2"
    assert t1 - t0 == pytest.approx(0.5, abs=0.05)