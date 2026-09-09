# SQLite chat-query efficiency

This fork proposes a single index that accelerates per-user quota counts and sparse chat-history reads. **It is an experimental, workload-dependent tradeoff:** bulk inserts become slower, storage grows, and an uncapped busy-user sequence can regress. The upstream PR is submitted as a draft. No production deployment, whole-app speedup, or monetary saving has been established.

## What changes

The baseline is upstream commit `049d721f900f394c1415ebd7db69a552194c9736`. The only runtime change is this index, declared in the Prisma schema and added by migration `20260909000000_chat_history_quota_index`:

```sql
CREATE INDEX "workspace_chats_user_history_quota_idx"
ON "workspace_chats"("user_id", "workspaceId", "thread_id",
                     "api_session_id", "include", "id", "createdAt");
```

The quota count filters by `user_id` and a `createdAt` lower bound. SQLite can read that user's covering index entries without scanning every chat payload. Because time is the final column, this is a scan within one user's entries, not a timestamp range seek. Benefits depend on user distribution and retained history.

The tested history query filters user, workspace, thread, API session and inclusion, then requests the newest 20 IDs. Those equality columns precede `id`, allowing ordered index traversal without a temporary sort. A previous two-column `(user_id, createdAt)` candidate made busy history much slower by inducing a sort; that rejected candidate is not included here.

Query predicates, authorization, returned fields, and application behavior are unchanged. Existing retry/persistence concerns and the rest of the application are outside this change.

## Measured results

Twelve serial, matched synthetic SQLite pairs ran on Windows 11, Python 3.12.2 / SQLite 3.43.1, on September 9, 2026. Each group has three fresh pairs. Each fixture includes seven additional boundary rows. Times below are the median of the three per-trial medians; reduction ranges use paired ratios, so they need not equal the ratio of the displayed aggregate times.

| Rows / SQLite cache | Busy-user quota, before → after | Quota time reduction range | Capped SQL sequence, before → after | Sequence time reduction range |
| --- | --- | --- | --- | --- |
| 10k / 8 MiB | 0.434 → 0.100 ms | 76.6–77.7% | 6.906 → 6.153 ms | −0.8–10.9% |
| 100k / 8 MiB | 30.483 → 0.588 ms | 98.0–98.2% | 45.394 → 13.591 ms | 55.0–85.1% |
| 1M / 8 MiB | 303.241 → 4.976 ms | 98.3–98.5% | 354.524 → 15.678 ms | 93.7–96.7% |
| 1M / 512 MiB | 62.880 → 4.531 ms | 92.8–93.3% | 69.315 → 10.245 ms | 54.2–86.2% |

The capped sequence is **quota count + history read + one insert + commit**, without HTTP, Prisma overhead, retrieval, or an LLM. It is a constructed database sequence, not an accepted chat turn. At 100k–1M rows, busy-quota CPU per correct result fell 91.5–98.6%, and sparse-history time fell 87.6–95.1%. These are descriptive observed ranges, not confidence intervals or production guarantees.

The negative results matter:

- Busy history was already fast. At 100k–1M rows it became 6.8–33.4% slower, with the absolute median increase below 0.1 ms in every pair. Both history plans used the candidate index and required no temporary sort.
- Without the quota count, all six million-row busy-user sequences were slower: 1.1–27.3% more time. A small uncapped workload may see no net benefit.
- Index pages increased database size by 7.62% at 10k, 8.90% at 100k, and 9.17% at 1M rows for these payloads.
- Bulk insert batches have substantial extra cost. At 1M / 8 MiB, the median of trial medians rose from 6.023 to 22.989 ms per 100-row transaction; median trial p95 rose from 7.992 to 31.002 ms. At 1M / 512 MiB, these were 7.205 → 25.099 ms and 207.084 → 1,271.740 ms, respectively. The large, noisy tails are retained, not discarded. These results do not justify a universal performance fix or production rollout.

## Validation and limits

All 2,976 timed reads and 1,488 timed constructed sequences matched independent fixture expectations. Full persisted rows matched between arms after reopen, and integrity and foreign-key checks passed. Every predeclared pair is retained. Read medians were allowed up to the greater of 5% or 0.1 ms regression; all medium/large capped sequence medians had to improve. Those bounded gates passed; write/storage results remain explicit tradeoffs, not passed production acceptance criteria.

The project's pinned Prisma 5.3.1 validated the schema and generated matching DDL. All 41 migrations applied to a disposable database. An actual Prisma client using SQLite 3.41.2 passed 168 count/history comparisons across user, workspace, thread, API-session and inclusion partitions, NULLs, cutoff boundaries, ordering/limit, migration removal/reapplication and insert/update/delete. Full-row and reopen checks passed. These are correctness checks; the timings above use Python's different SQLite runtime.

The benchmark alternates baseline/candidate order, uses 31 measured repetitions after three warm-ups for reads and sequences, disables Python statement caching, and uses DELETE journal / FULL synchronous mode. Writes have 31 measured repetitions. Both arms use identical seeded data and all baseline migrations; only the candidate receives the new index. OS caches are not flushed. This is one host, one deterministic user/payload distribution and one connection per arm. Production concurrency, other history query shapes, PostgreSQL, full application tests and installation on a live database were not evaluated.

## Cost interpretation

Measured: less CPU for the tested quota query. Unmeasured: hosting bills, LLM spend, attempted/accepted user-outcome cost, total application CPU, retries and user acceptance. Paid provider calls and provider spend were zero. Dollar savings and an overall cost-saving percentage are **unknown**. Extra writes, storage and fixed-price hosting can eliminate any bill saving.

## Reproduce

Run from the fork root with Python 3.12. The scripts use only the standard library and disposable synthetic databases; no API keys or user data are needed. Keep the published results unchanged and choose a new output directory each time.

```sh
python docs/query-efficiency/benchmark.py --quick --output /path/to/new-smoke-results
python docs/query-efficiency/benchmark.py --output /path/to/new-full-results
python docs/query-efficiency/summarize.py /path/to/new-full-results
```

The quick command is only a 1,000-row smoke check. The full command runs the fixed twelve-pair schedule. Published raw evidence predates the self-contained public script: the same fixture/measurement functions were extracted from the original frozen runner, and this public copy passed the quick smoke check. The public full command has not been independently rerun. Its metadata records the checkout and script hashes; use this fork revision for the documented workload.

To reproduce Prisma correctness, make a temporary copy of `server/prisma` outside any live installation. In that copy set the SQLite URL to `file:./benchmark.db` and the generator output to `./client`. Create an empty SQLite database file, install `prisma@5.3.1` and `@prisma/client@5.3.1` into that temporary workspace, then run:

```sh
prisma validate --schema /path/to/temp/prisma/schema.prisma
prisma generate --schema /path/to/temp/prisma/schema.prisma
prisma migrate deploy --schema /path/to/temp/prisma/schema.prisma
node docs/query-efficiency/prisma-regression.cjs /path/to/temp/prisma/client /path/to/temp/prisma/benchmark.db
```

Use the locally installed Prisma executable. The regression script refuses a database containing users or chats. It inserts synthetic records and temporarily drops/recreates the candidate index; use only the disposable copy. The fork includes an automatic Prisma migration, so starting it against an existing installation may build the index. Test on a backup copy first; index creation duration and write locking depend on database size. No live rollout or rollback was tested.

## Evidence

- [Summary, formulas and raw-file hashes](results/summary.json)
- [All twelve raw paired results](results/)
- [Original run metadata](results/run-metadata.json)
- [Prisma verification and setup failures](results/prisma-verification.json)
- [Standalone benchmark](benchmark.py) and [Prisma regression check](prisma-regression.cjs)

This is an independent MIT-licensed fork of [Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm). The upstream license and attribution are preserved. It is not an official Mintplex Labs release.
