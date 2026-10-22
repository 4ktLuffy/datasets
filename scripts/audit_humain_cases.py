"""Recompute reviews/humain-derived-cases.json from a release's cases.jsonl.

Download the evaluated release and check it against releases/current.json:

    curl -LO https://huggingface.co/datasets/najdresearch/najd-benchmark/resolve/3fa471f6c8ed2ebc37a8b60d40c88170d682b569/datasets/najd-benchmark/2026.09.27.1/cases.jsonl
    uv run python scripts/audit_humain_cases.py cases.jsonl

Rule families are recomputed from each case's own options and key. Explicit families are checked
against the recorded key and a SHA-256 of the keyed option text, so no upstream text is stored.
Only the three HUMAIN multiple-choice sources below are supported.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "reviews/humain-derived-cases.json"
LETTERS = "ABCDEFGH"
# humain-arapro stores a 1-based answerIndex; the other two store answerKey letters.
SOURCES = {
    "humain-aratruthfulqa": "answerKey",
    "humain-arapro": "answerIndex",
    "humain-aramath": "answerKey",
}
WHOLE = re.compile(r"-?\d+")
FLOAT_TAIL = re.compile(r"\d\.\d*(0{10}|9{10})\d")


def options(case):
    """Option texts without their own position label ("A. x" or "(A) x")."""
    out = []
    for i, option in enumerate(case["expected"].get("options") or []):
        letter = re.escape(LETTERS[i])
        out.append(re.sub(rf"^\s*(\({letter}\)|{letter}\.)\s*", "", str(option), count=1).strip())
    return out


def key(case):
    source, expected = case["provenance"]["sourceId"], case["expected"]
    count = len(expected.get("options") or [])
    if SOURCES.get(source) == "answerIndex":
        index = int(expected["answerIndex"]) - 1
    elif SOURCES.get(source) == "answerKey" and str(expected.get("answerKey")) in LETTERS[:count]:
        index = LETTERS.index(expected["answerKey"])
    else:
        raise ValueError(f"{case['id']}: unsupported source or answer key")
    if not 0 <= index < count:
        raise ValueError(f"{case['id']}: answer outside the options")
    return index


def last_option_key(case):
    return len(options(case)) >= 5 and key(case) == len(options(case)) - 1


def duplicate_options(case):
    return len(set(options(case))) < len(options(case))


def keyed_text_repeated(case):
    texts = options(case)
    return texts.count(texts[key(case)]) > 1


def float_artefact(case):
    return any(FLOAT_TAIL.search(text) for text in options(case))


def unique_shortest_key(case):
    lengths = [len(text) for text in options(case)]
    return lengths[key(case)] == min(lengths) and lengths.count(min(lengths)) == 1


def numerically_equal_options(case):
    values = []
    for text in options(case):
        try:
            values.append(float(text))
        except ValueError:
            return False
    return len(set(values)) < len(values)


RULES = {
    "last_option_key": last_option_key,
    "duplicate_options": duplicate_options,
    "keyed_text_repeated": keyed_text_repeated,
    "float_artefact": float_artefact,
    "unique_shortest_key": unique_shortest_key,
    "numerically_equal_options": numerically_equal_options,
}


def keyed_sha256(case):
    return hashlib.sha256(options(case)[key(case)].encode("utf-8")).hexdigest()


def audit(cases, review):
    by_id = {case["id"]: case for case in cases}
    pool = {}
    for case in cases:
        if case["provenance"].get("sourceId") in SOURCES and case["expected"].get("options"):
            pool.setdefault(case["provenance"]["sourceId"], []).append(case)
    problems = []
    for family in review["families"]:
        listed = set(family["case_ids"])
        if family["method"] == "explicit":
            found = {
                cid
                for cid, check in family["checks"].items()
                if cid in by_id
                and LETTERS[key(by_id[cid])] == check["key"]
                and keyed_sha256(by_id[cid]) == check["keyed_option_sha256"]
            }
        else:
            rule = RULES[family["method"]]
            found = {case["id"] for case in pool.get(family["source_id"], []) if rule(case)}
        if "eligible" in family:
            eligible = sum(len(options(c)) >= 5 for c in pool.get(family["source_id"], []))
            if eligible != family["eligible"]:
                problems.append((family["id"], [f"eligible {eligible}"], []))
        if found != listed or len(listed) != family["count"]:
            problems.append((family["id"], sorted(found - listed), sorted(listed - found)))
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", type=Path)
    args = parser.parse_args()
    data = args.cases.read_bytes()
    review = json.loads(REVIEW.read_text(encoding="utf-8"))
    digest = hashlib.sha256(data).hexdigest()
    print(
        f"cases.jsonl sha256 {digest}",
        "(evaluated release)"
        if digest == review["cases_sha256"]
        else "(differs from the evaluated release)",
    )
    cases = [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
    problems = audit(cases, review)
    for family_id, extra, missing in problems:
        print(f"{family_id}: not listed {extra[:5]}, not found {missing[:5]}")
    flagged = {i for f in review["families"] for i in f["case_ids"]}
    if len(flagged) != review["distinct_case_count"]:
        problems.append(("distinct_case_count", [], []))
    families = len(review["families"])
    print(f"{families} families, {len(flagged)} distinct cases, {len(problems)} mismatches")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
