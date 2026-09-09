"""Recompute published result summaries and fixed gates; refuses to overwrite output."""
import argparse
import hashlib
import json
import statistics
from pathlib import Path

def stats(values):
    return {"min": min(values), "median": statistics.median(values), "max": max(values)}

def reduction(baseline, indexed):
    return 100 * (1 - indexed / baseline)

def comparison(pairs, section, name):
    baseline = [p[section]["baseline"][name]["p50Ms"] for p in pairs]
    indexed = [p[section]["indexed"][name]["p50Ms"] for p in pairs]
    return {"baselineP50Ms": stats(baseline), "indexedP50Ms": stats(indexed),
            "pairedMedianTimeReductionPercent": stats([reduction(b, c) for b, c in zip(baseline, indexed)]),
            "pairedSpeedupFactor": stats([b / c for b, c in zip(baseline, indexed)]),
            "baselineP95Ms": stats([p[section]["baseline"][name]["p95Ms"] for p in pairs]),
            "indexedP95Ms": stats([p[section]["indexed"][name]["p95Ms"] for p in pairs])}

def cpu_comparison(pairs, name):
    baseline = [p["readResources"]["baseline"][name]["cpu"]["cpuMsPerAttempt"] for p in pairs]
    indexed = [p["readResources"]["indexed"][name]["cpu"]["cpuMsPerAttempt"] for p in pairs]
    return {"baselineCpuMsPerRead": stats(baseline), "indexedCpuMsPerRead": stats(indexed),
            "pairedCpuReductionPercent": stats([reduction(b, c) for b, c in zip(baseline, indexed)])}

def summarize_group(pairs):
    comparisons = {}
    for section in ("readTimings", "sqlSequenceTimings", "writeTimings"):
        comparisons[section] = {name: comparison(pairs, section, name) for name in pairs[0][section]["baseline"]}
    comparisons["cpu"] = {name: cpu_comparison(pairs, name) for name in pairs[0]["readResources"]["baseline"]}
    comparisons["storageGrowthPercent"] = stats([100 * (p["databaseBytes"]["indexed"] / p["databaseBytes"]["baseline"] - 1) for p in pairs])
    comparisons["writeP95Diagnostics"] = [{"trial": p["trial"], "operation": name,
        "passed": p["writeTimings"]["indexed"][name]["p95Ms"] <= p["writeTimings"]["baseline"][name]["p95Ms"] + max(10, .05 * p["writeTimings"]["baseline"][name]["p95Ms"])}
        for p in pairs for name in p["writeTimings"]["baseline"]]
    return comparisons

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path, help="Run folder containing pair JSON files; summary.json must not exist")
HERE = parser.parse_args().directory
files = sorted(HERE.glob("pair-*.json"))
pairs = [json.loads(p.read_text()) for p in files]
expected = {(size + 7, 8192, trial) for size in (10000, 100000, 1000000) for trial in (1, 2, 3)}
expected |= {(1000007, 524288, trial) for trial in (1, 2, 3)}
assert len(pairs) == 12
assert {(p["rows"], p["cacheKiB"], p["trial"]) for p in pairs} == expected
for p in pairs:
    assert p["correctnessPassed"] and p["finalChatSha256"]["baseline"] == p["finalChatSha256"]["indexed"]
    for name, baseline in p["readTimings"]["baseline"].items():
        assert p["readTimings"]["indexed"][name]["p50Ms"] <= baseline["p50Ms"] + max(.1, .05 * baseline["p50Ms"]), (p["rows"], p["trial"], name)
    for name in ("history_busy", "history_sparse"):
        plans = p["readResources"]["indexed"][name]["plan"]
        assert any("workspace_chats_user_history_quota_idx" in x for x in plans)
        assert not any("TEMP B-TREE" in x for x in plans)
    if p["rows"] >= 100007:
        for name in p["sqlSequenceTimings"]["baseline"]:
            if "uncapped" not in name:
                assert p["sqlSequenceTimings"]["indexed"][name]["p50Ms"] < p["sqlSequenceTimings"]["baseline"][name]["p50Ms"]
groups = {f"{rows}-{cache}": summarize_group([p for p in pairs if (p["rows"], p["cacheKiB"]) == (rows, cache)])
          for rows, cache in sorted({(p["rows"], p["cacheKiB"]) for p in pairs})}
summary = {"sourceCommit": "049d721f900f394c1415ebd7db69a552194c9736", "pairCount": 12,
           "publicationGatesPassed": True, "correctnessFailures": 0,
           "paidCalls": 0, "providerSpendUsd": 0, "achievedBillSavingsPercent": None,
           "acceptedUserOutcomes": None, "attemptedUserOutcomeCostUsd": None, "acceptedUserOutcomeCostUsd": None,
           "measuredReadResults": sum(2*p["readMeasuredAttemptsPerArm"] for p in pairs),
           "measuredSqlSequences": sum(2*p["sqlSequenceMeasuredAttemptsPerArm"] for p in pairs),
           "groups": groups, "rawFilesSha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
           "formulas": {"timeReductionPercent": "100*(1-indexed_ms/baseline_ms)", "speedupFactor": "baseline_ms/indexed_ms",
                        "cpuReductionPercent": "100*(1-indexed_cpu_ms/baseline_cpu_ms)", "range": "min/max of paired trial ratios, not a confidence interval"}}
with (HERE / "summary.json").open("x") as out:
    json.dump(summary, out, indent=2)
for key, group in groups.items():
    print(key)
    for section in ("readTimings", "sqlSequenceTimings", "writeTimings"):
        for name, values in group[section].items():
            print(section, name, "median ms", values["baselineP50Ms"]["median"], values["indexedP50Ms"]["median"], "reduction", values["pairedMedianTimeReductionPercent"], "p95", values["baselineP95Ms"]["median"], values["indexedP95Ms"]["median"])
    print("cpu", group["cpu"]["quota_busy"]["pairedCpuReductionPercent"], "storage", group["storageGrowthPercent"])
