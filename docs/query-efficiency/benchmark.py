"""Offline SQLite benchmark of the chat history/quota index. Uses disposable DBs."""
import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import subprocess
import tempfile
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCE = "049d721f900f394c1415ebd7db69a552194c9736"
MIGRATION = "20260909000000_chat_history_quota_index"
CUTOFF = 1788739200000
SAMPLES = 31
WARMUPS = 3
INSERT = """INSERT INTO workspace_chats
    (id, workspaceId, prompt, response, include, user_id, createdAt,
     lastUpdatedAt, thread_id, api_session_id) VALUES (?,?,?,?,?,?,?,?,?,?)"""
QUOTA = 'SELECT COUNT(*) FROM workspace_chats WHERE user_id = ? AND createdAt >= ?'
HISTORY = """SELECT * FROM workspace_chats WHERE workspaceId=? AND user_id=?
    AND thread_id IS NULL AND api_session_id IS NULL AND include=1
    ORDER BY id DESC LIMIT 20"""
QUERIES = {"quota_busy": (QUOTA, (1, CUTOFF)), "quota_sparse": (QUOTA, (777, CUTOFF)),
           "history_busy": (HISTORY, (1, 1)), "history_sparse": (HISTORY, (17, 777))}

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def generated_row(row_id):
    user = None if row_id % 29 == 0 else (1 if row_id % 10 == 0 else row_id % 1000 + 1)
    created = CUTOFF + (row_id % 2880 - 1440) * 60000
    return (row_id, row_id % 20 + 1, "synthetic prompt", "x" * 256,
            row_id % 3 != 0, user, created, created,
            None if row_id % 2 == 0 else row_id % 50,
            "synthetic-api" if row_id % 7 == 0 else None)

def fixture_rows(size):
    for row_id in range(1, size + 1):
        yield generated_row(row_id)
    # Before/exactly/after cutoff, NULL, hidden, thread, workspace, and API boundaries.
    for offset, delta in enumerate((-1, 0, 1, 0, 0, 0, 0), start=1):
        yield (size + offset, offset, "boundary", "{}", offset % 2 == 0,
               None if offset == 4 else 1002, CUTOFF + delta, CUTOFF,
               offset if offset % 2 else None, "api" if offset == 7 else None)

def configure(connection):
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA cache_size=-8192")

def seed(connection, migrations, size):
    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))
    configure(connection)
    connection.executemany("INSERT INTO users(id,password) VALUES (?,?)",
                           ((user, "synthetic-unusable-password") for user in range(1, 1003)))
    connection.executemany(INSERT, fixture_rows(size))
    connection.commit()

def summary(samples):
    ordered = sorted(samples)
    return {"samplesMs": samples, "p50Ms": statistics.median(samples),
            "p95Ms": ordered[math.ceil(len(ordered) * .95) - 1], "maxMs": max(samples)}

def write_operations(connection, operation, first_id):
    if operation == "insert100":
        connection.executemany(INSERT, (generated_row(i) for i in range(first_id, first_id + 100)))
    elif operation == "update1":
        connection.execute("UPDATE workspace_chats SET createdAt=? WHERE id=?", (CUTOFF, first_id))
    else:
        connection.execute("DELETE FROM workspace_chats WHERE id=?", (first_id,))
    connection.commit()

def row_digest(connection):
    accumulator = hashlib.sha256()
    for row in connection.execute("SELECT * FROM workspace_chats ORDER BY id"):
        accumulator.update((json.dumps(row, separators=(",", ":")) + "\n").encode())
    return accumulator.hexdigest()

def verify_database(connection):
    require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "Integrity failed")
    require(connection.execute("PRAGMA foreign_key_check").fetchall() == [], "Foreign keys failed")

def storage_bytes(connection):
    return connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]

def write_new(path, report):
    with path.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")

def full_row(row):
    return (*row[:4], int(row[4]), *row[5:9], None, row[9], None)

def fixture_oracles(size):
    counts = {1: 0, 777: 0}
    histories = {1: deque(maxlen=20), 777: deque(maxlen=20)}
    for row in fixture_rows(size):
        user = row[5]
        if user not in counts:
            continue
        counts[user] += int(row[6] >= CUTOFF)
        workspace = 1 if user == 1 else 17
        if row[1] == workspace and row[4] and row[8] is None and row[9] is None:
            histories[user].append(full_row(row))
    require(all(histories.values()), "History oracle must exercise nonempty results")
    return {"quota_busy": [(counts[1],)], "quota_sparse": [(counts[777],)],
            "history_busy": list(reversed(histories[1])), "history_sparse": list(reversed(histories[777]))}

def ordered_arms(connections, repetition):
    return list(connections) if repetition % 2 == 0 else list(reversed(connections))

def measure_reads(connections, oracles, trial):
    samples = {arm: {name: [] for name in QUERIES} for arm in connections}
    for repetition in range(-WARMUPS, SAMPLES):
        for name, (sql, parameters) in QUERIES.items():
            for arm in ordered_arms(connections, repetition + trial):
                start = time.perf_counter_ns()
                rows = connections[arm].execute(sql, parameters).fetchall()
                elapsed = (time.perf_counter_ns() - start) / 1e6
                require(rows == oracles[name], f"Read mismatch: {arm}/{name}")
                if repetition >= 0:
                    samples[arm][name].append(elapsed)
    return {arm: {name: summary(times) for name, times in cases.items()}
            for arm, cases in samples.items()}

def measure_cpu(connection, query, expected):
    sql, parameters = query
    attempts = 0
    wall_start, cpu_start = time.perf_counter_ns(), time.process_time_ns()
    while True:
        rows = connection.execute(sql, parameters).fetchall()
        require(rows == expected, "CPU batch query mismatch")
        attempts += 1
        wall_ms = (time.perf_counter_ns() - wall_start) / 1e6
        cpu_ms = (time.process_time_ns() - cpu_start) / 1e6
        if (wall_ms >= 250 and cpu_ms >= 125) or wall_ms >= 2000:
            break
    return {"attempts": attempts, "correctResults": attempts, "totalCpuMs": cpu_ms,
            "totalWallMs": wall_ms, "cpuMsPerAttempt": cpu_ms / attempts,
            "cpuMsPerCorrectResult": cpu_ms / attempts, "wallMsPerAttempt": wall_ms / attempts}

def measure_vm(connection, query, expected):
    callbacks = 0
    def progress():
        nonlocal callbacks
        callbacks += 1
        return 0
    connection.set_progress_handler(progress, 1000)
    try:
        rows = connection.execute(*query).fetchall()
    finally:
        connection.set_progress_handler(None, 0)
    require(rows == expected, "Instruction sample query mismatch")
    return {"lowerInstructions": callbacks * 1000, "upperInstructionsExclusive": (callbacks + 1) * 1000}

def read_resources(connections, oracles, trial):
    resources = {arm: {} for arm in connections}
    for name, query in QUERIES.items():
        for arm in ordered_arms(connections, trial):
            resources[arm][name] = {"cpu": measure_cpu(connections[arm], query, oracles[name]),
                                    "vm": measure_vm(connections[arm], query, oracles[name]),
                                    "plan": [row[3] for row in connections[arm].execute("EXPLAIN QUERY PLAN " + query[0], query[1])]}
    return resources

def mixed_row(row_id):
    return (row_id, 1, "synthetic mixed turn", "x" * 256, True, 1,
            CUTOFF + 1, CUTOFF + 1, None, None)

def execute_sequence(connection, sequence, row):
    count = connection.execute(QUOTA, (1, CUTOFF)).fetchone()[0] if sequence == "capped" else None
    history = connection.execute(HISTORY, (1, 1)).fetchall()
    connection.execute(INSERT, row)
    connection.commit()
    return count, history

def measure_sequences(connections, oracles, size, trial):
    samples = {arm: {} for arm in connections}
    expected_count = oracles["quota_busy"][0][0]
    expected_history = list(oracles["history_busy"])
    next_id = size + 8
    for sequence in ("capped", "uncapped"):
        times = {arm: [] for arm in connections}
        for repetition in range(-WARMUPS, SAMPLES):
            row = mixed_row(next_id)
            for arm in ordered_arms(connections, repetition + trial):
                start = time.perf_counter_ns()
                count, history = execute_sequence(connections[arm], sequence, row)
                elapsed = (time.perf_counter_ns() - start) / 1e6
                require(history == expected_history, f"{sequence}: mixed history mismatch")
                require(count == (expected_count if sequence == "capped" else None), "Mixed quota mismatch")
                if repetition >= 0:
                    times[arm].append(elapsed)
            expected_count += 1
            expected_history = ([full_row(row)] + expected_history)[:20]
            next_id += 1
        for arm in connections:
            samples[arm][sequence] = summary(times[arm])
    return samples, next_id

def measure_writes(connections, next_id, trial):
    samples = {arm: {} for arm in connections}
    for operation in ("insert100", "update1"):
        times = {arm: [] for arm in connections}
        for repetition in range(SAMPLES):
            for arm in ordered_arms(connections, repetition + trial):
                start = time.perf_counter_ns()
                write_operations(connections[arm], operation, next_id + repetition * 100)
                times[arm].append((time.perf_counter_ns() - start) / 1e6)
        for arm in connections:
            samples[arm][operation] = summary(times[arm])
    return samples

def run_pair(size, cache_kib, trial):
    migrations = sorted(p for p in (REPO / "server/prisma/migrations").glob("*/migration.sql") if p.parent.name != MIGRATION)
    oracles = fixture_oracles(size)
    with tempfile.TemporaryDirectory(prefix="finops-read-study-") as folder:
        paths = {arm: Path(folder) / (arm + ".db") for arm in ("baseline", "indexed")}
        with closing(sqlite3.connect(paths["baseline"], cached_statements=0)) as baseline, closing(sqlite3.connect(paths["indexed"], cached_statements=0)) as indexed:
            seed(baseline, migrations, size)
            baseline.backup(indexed)
            connections = {"baseline": baseline, "indexed": indexed}
            for connection in connections.values():
                configure(connection)
                connection.execute(f"PRAGMA cache_size=-{cache_kib}")
            start = time.perf_counter_ns()
            indexed.executescript((REPO / "server/prisma/migrations" / MIGRATION / "migration.sql").read_text(encoding="utf-8"))
            migration_ms = (time.perf_counter_ns() - start) / 1e6
            storage = {arm: storage_bytes(connection) for arm, connection in connections.items()}
            reads = measure_reads(connections, oracles, trial)
            resources = read_resources(connections, oracles, trial)
            sequences, next_id = measure_sequences(connections, oracles, size, trial)
            writes = measure_writes(connections, next_id, trial)
        hashes = {}
        for arm, path in paths.items():
            with closing(sqlite3.connect(path)) as reopened:
                verify_database(reopened)
                require(reopened.execute("SELECT COUNT(*) FROM workspace_chats").fetchone()[0] == next_id - 1 + SAMPLES * 100, "Durable row count mismatch")
                hashes[arm] = row_digest(reopened)
        require(hashes["baseline"] == hashes["indexed"], "Durable full-row hashes differ")
    return {"rows": size + 7, "cacheKiB": cache_kib, "trial": trial,
            "readTimings": reads, "readResources": resources, "sqlSequenceTimings": sequences,
            "writeTimings": writes, "migrationMs": migration_ms, "databaseBytes": storage,
            "oracleQuotaCounts": {name: oracles[name][0][0] for name in ("quota_busy", "quota_sparse")},
            "oracleHistorySha256": {name: hashlib.sha256(json.dumps(oracles[name]).encode()).hexdigest() for name in ("history_busy", "history_sparse")},
            "finalChatSha256": hashes, "correctnessPassed": True,
            "readMeasuredAttemptsPerArm": SAMPLES * len(QUERIES), "sqlSequenceMeasuredAttemptsPerArm": SAMPLES * 2}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory for immutable run files")
    parser.add_argument("--quick", action="store_true", help="One 1,000-row smoke run; not performance evidence")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    migration = REPO / "server/prisma/migrations" / MIGRATION / "migration.sql"
    write_new(args.output / "metadata.json", {
        "baselineSourceCommit": SOURCE,
        "checkoutCommit": subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip(),
        "scriptSha256": digest(Path(__file__)), "migrationSha256": digest(migration),
        "recordedAt": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
        "quickSmokeOnly": args.quick, "queryDefinitions": QUERIES,
        "settings": "DELETE journal; FULL sync; no Python statement cache; single connection per arm; OS cache not flushed",
        "samplesPerQuery": SAMPLES, "warmupsPerQuery": WARMUPS,
        "paidCalls": 0, "providerSpendUsd": 0, "acceptedUserCostUsd": None})
    schedule = [(size, 8192, trial) for trial in (1, 2, 3) for size in (10000, 100000, 1000000)]
    schedule += [(1000000, 524288, trial) for trial in (1, 2, 3)]
    if args.quick:
        schedule = [(1000, 8192, 1)]
    for size, cache, trial in schedule:
        report = run_pair(size, cache, trial)
        for name in ("history_busy", "history_sparse"):
            plan = report["readResources"]["indexed"][name]["plan"]
            require(any("workspace_chats_user_history_quota_idx" in p for p in plan), "History index unused")
            require(not any("TEMP B-TREE" in p for p in plan), "History sort regression")
        write_new(args.output / f"pair-{size}-{cache}-{trial}.json", report)
        print(f"Passed correctness and history plans: rows={size}, cacheKiB={cache}, trial={trial}", flush=True)


if __name__ == "__main__":
    main()
