"""Check that the conformance kit is internally consistent.

The kit is a standard. A wrong vector in a standard is worse than no standard:
four implementations would be corrected until they all reproduced the same
wrong number, and the anchor would have quietly moved. So the vectors are
themselves checked, and this file is wired into the test suite.

What it proves:

  * every expected hash is the sha256 of the expected payload recorded beside
    it - the two are not allowed to drift apart;
  * every expected payload is exactly what step 4 produces (sorted keys, the
    (",", ":") separators, no \\uXXXX escaping) - checked by re-serializing it;
  * every vector's input entry canonicalizes to its stated payload, per
    conformance/reference.py;
  * every invariance group agrees on one hash;
  * the anchor vector still matches examples/valid-entry.yaml and the hash
    recorded there, when that file is present;
  * the kit still refuses a vector whose recorded payload is not what
    the reference produces, so an NFC-composed expected payload cannot
    silently replace an NFD input;
  * a vector survives the runner's own wire format unchanged;
  * every hash-vector entry is schema-valid against
    schemas/knowledge-v2.schema.json, so a public-boundary fixture cannot
    carry an invalid UUID or other structural defect;
  * every gate vector declares what is wrong with it, the declared bad value is
    really present, and it is drawn from a reserved documentation range rather
    than from anything real;
  * the kit imports nothing from tools/, bot/, server/ or sync/.

Exit status: 0 if consistent, 1 if not, 2 if it could not run at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
VECTORS = HERE / "vectors"
GATE_VECTORS = HERE / "gate_vectors"
EXAMPLE = REPO / "examples" / "valid-entry.yaml"
SCHEMA = REPO / "schemas" / "knowledge-v2.schema.json"

sys.path.insert(0, str(HERE))

import reference  # noqa: E402  (same directory, by design: the spec aid)

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PLACEHOLDER_HASH = "sha256:" + "0" * 64
BANNER = "# SYNTHETIC BAD VALUES - THIS FILE EXISTS TO BE REJECTED."

GATE_VERDICTS = {
    "redaction": {"accept", "reject"},
    "schema": {"accept", "reject"},
    "export": {"byte-identical"},
}

# Shapes a conforming redaction gate must recognise. Deliberately loose: the
# kit checks that the vector really contains something of this shape, not that
# an implementation uses these exact expressions.
OFFENDING_SHAPES = {
    "ipv4": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    "hostname": re.compile(r"\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}\b", re.I),
    "user_path": re.compile(r"(?:/home/|/Users/)[^/\s]+/"),
    "email": re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", re.I),
    "credential": re.compile(r"(?i)\b(?:bearer|token|password|secret|api[_-]?key)\b"),
}

# Values a fixture is allowed to use. Anything outside this is treated as
# possibly real and refused, so that "it is only a fixture" can never be the
# reason a real address entered git history.
RESERVED_IPV4 = (
    re.compile(r"^192\.0\.2\.\d{1,3}$"),  # RFC 5737 TEST-NET-1
    re.compile(r"^198\.51\.100\.\d{1,3}$"),  # RFC 5737 TEST-NET-2
    re.compile(r"^203\.0\.113\.\d{1,3}$"),  # RFC 5737 TEST-NET-3
    # RFC 2544 benchmarking, IANA reserved. Included because redaction gates
    # reasonably exempt the documentation ranges above as harmless, and a
    # vector needs at least one address that every reading must refuse.
    re.compile(r"^198\.1[89]\.\d{1,3}\.\d{1,3}$"),
)
RESERVED_DOMAIN_SUFFIXES = (
    ".invalid",
    ".example",
    ".test",
    ".localhost",
    ".example.com",
    ".example.net",
    ".example.org",
)


class Problems:
    """Collected inconsistencies, printed as they are found."""

    def __init__(self, quiet: bool = False):
        self.items: list = []
        self.notes: list = []
        self.checks = 0
        self.quiet = quiet

    def check(self, ok: bool, where: str, message: str):
        self.checks += 1
        if ok:
            return True
        self.items.append(f"{where}: {message}")
        sys.stdout.write(f"BAD   {where}: {message}\n")
        return False

    def note(self, message: str):
        self.notes.append(message)
        if not self.quiet:
            sys.stdout.write(f"NOTE  {message}\n")


def _load_yaml_module():
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        sys.stderr.write(
            "PyYAML is required to read the conformance vectors.\n"
            "Install it with: python3 -m pip install PyYAML\n"
        )
        raise SystemExit(2) from None
    return yaml


# --- canonicalization vectors ---------------------------------------------


def check_hash_vectors(problems: Problems) -> list:
    yaml = _load_yaml_module()
    vectors = []
    seen_ids = {}
    groups = {}

    paths = sorted(VECTORS.glob("*.yaml"))
    problems.check(bool(paths), "conformance/vectors", "directory has no vectors")

    for path in paths:
        where = f"vectors/{path.name}"
        try:
            vector = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.check(False, where, f"cannot parse: {exc}")
            continue

        required = ("id", "title", "spec", "entry", "expected_payload", "expected_content_hash")
        missing = [key for key in required if key not in (vector or {})]
        if not problems.check(not missing, where, f"missing keys {missing}"):
            continue

        vectors.append(vector)
        vector_id = vector["id"]
        problems.check(
            vector_id == path.stem,
            where,
            f"id {vector_id!r} does not match the file name",
        )
        problems.check(
            vector_id not in seen_ids,
            where,
            f"duplicate id, also used by {seen_ids.get(vector_id)}",
        )
        seen_ids[vector_id] = path.name

        expected_hash = str(vector["expected_content_hash"]).strip()
        expected_payload = str(vector["expected_payload"])

        # The core check: the recorded hash is the hash of the recorded payload.
        problems.check(
            bool(HASH_RE.match(expected_hash)),
            where,
            f"expected_content_hash is malformed: {expected_hash!r}",
        )
        digest = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
        problems.check(
            expected_hash == f"sha256:{digest}",
            where,
            f"expected_content_hash is not the sha256 of expected_payload "
            f"(payload hashes to sha256:{digest})",
        )

        # The payload is exactly what step 4 produces.
        try:
            reparsed = json.loads(expected_payload)
        except json.JSONDecodeError as exc:
            problems.check(False, where, f"expected_payload is not valid JSON: {exc}")
            reparsed = None
        if reparsed is not None:
            problems.check(
                sorted(reparsed) == ["rule", "scope"],
                where,
                f"payload top-level keys must be rule and scope, got {sorted(reparsed)}",
            )
            reserialized = json.dumps(
                reparsed, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            problems.check(
                reserialized == expected_payload,
                where,
                "expected_payload is not in step 4 form (sort_keys=True, "
                "ensure_ascii=False, separators=(\",\", \":\"))",
            )
            problems.check(
                "\\u" not in expected_payload,
                where,
                "expected_payload contains \\u escapes; step 4 says ensure_ascii=False",
            )

        # The vector's input really canonicalizes to the stated payload.
        try:
            computed_payload = reference.canonical_json(vector["entry"])
            computed_hash = reference.content_hash(vector["entry"])
        except Exception as exc:
            problems.check(False, where, f"reference cannot canonicalize the entry: {exc}")
            continue
        problems.check(
            computed_payload == expected_payload,
            where,
            "the entry does not canonicalize to expected_payload",
        )
        problems.check(
            computed_hash == expected_hash,
            where,
            f"the entry canonicalizes to {computed_hash}, not {expected_hash}",
        )

        # The entry's own content_hash field is either the vector's answer or
        # the all-zero placeholder. A stale real-looking hash there would be
        # read as an expected value by someone skimming.
        stored = str(vector["entry"].get("content_hash", ""))
        problems.check(
            stored in (expected_hash, PLACEHOLDER_HASH),
            where,
            "entry.content_hash should be either the expected hash or the "
            f"all-zero placeholder, got {stored!r}",
        )

        # A vector must survive the runner's wire format unchanged.
        for fmt, dumped in (
            ("entry-yaml", yaml.safe_load(yaml.safe_dump(vector["entry"], allow_unicode=True))),
            ("entry-json", json.loads(json.dumps(vector["entry"], ensure_ascii=False))),
        ):
            problems.check(
                dumped == vector["entry"],
                where,
                f"entry does not survive a {fmt} round trip; the runner would "
                "feed the implementation something other than the vector",
            )

        group = vector.get("invariance_group")
        if group:
            groups.setdefault(group, []).append((vector_id, expected_hash, expected_payload))

    for group, members in sorted(groups.items()):
        hashes = {member[1] for member in members}
        problems.check(
            len(hashes) == 1,
            f"invariance group {group}",
            f"members disagree: {[(m[0], m[1][:14]) for m in members]}",
        )
        payloads = {member[2] for member in members}
        problems.check(
            len(payloads) == 1,
            f"invariance group {group}",
            "members produce the same hash but different payloads, which would "
            "mean a collision rather than an invariance",
        )
        problems.check(
            len(members) > 1,
            f"invariance group {group}",
            "an invariance group with one member proves nothing",
        )

    return vectors


def check_hash_vectors_against_schema(problems: Problems, vectors: list):
    """Hash-vector entries must be schema-valid as public-boundary fixtures."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        problems.note(
            "jsonschema is not installed, so hash vectors were not validated "
            "against schemas/knowledge-v2.schema.json. Install it with: "
            "python3 -m pip install jsonschema"
        )
        return
    if not SCHEMA.is_file():
        problems.note(
            "schemas/knowledge-v2.schema.json is not present in this checkout, "
            "so hash vectors were not validated against it"
        )
        return
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(
        {
            "$schema": schema.get("$schema"),
            "$defs": schema.get("$defs", {}),
            "$ref": "#/$defs/entry",
        }
    )
    for vector in vectors:
        where = f"vectors/{vector['id']}.yaml"
        errors = list(validator.iter_errors(vector["entry"]))
        detail = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
        problems.check(
            not errors,
            where,
            f"the hash-vector entry must be schema-valid: {detail}",
        )


def check_anchor(problems: Problems, vectors: list):
    """The anchor vector must still agree with examples/valid-entry.yaml."""
    yaml = _load_yaml_module()
    anchor = next((v for v in vectors if v["id"] == "anchor-valid-entry"), None)
    if not problems.check(anchor is not None, "vectors", "the anchor vector is missing"):
        return
    if not EXAMPLE.is_file():
        problems.note(
            f"{EXAMPLE.relative_to(REPO)} is not present in this checkout, so the "
            "anchor was checked only against the hash recorded inside the vector"
        )
        return
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    entry = document["entries"][0]
    where = "vectors/anchor-valid-entry.yaml"
    problems.check(
        entry.get("scope") == anchor["entry"].get("scope")
        and entry.get("rule") == anchor["entry"].get("rule"),
        where,
        "the anchor vector's scope+rule no longer match examples/valid-entry.yaml",
    )
    problems.check(
        str(entry.get("content_hash")) == str(anchor["expected_content_hash"]).strip(),
        where,
        f"examples/valid-entry.yaml records {entry.get('content_hash')}, the "
        f"anchor vector expects {anchor['expected_content_hash']}",
    )


# --- gate vectors ----------------------------------------------------------


def _is_reserved(offending_class: str, value: str) -> bool:
    if offending_class == "ipv4":
        return any(pattern.match(value) for pattern in RESERVED_IPV4)
    if offending_class in ("hostname", "email"):
        host = value.rsplit("@", 1)[-1].rstrip(".").lower()
        return any(host.endswith(suffix) for suffix in RESERVED_DOMAIN_SUFFIXES)
    if offending_class == "user_path":
        return "example" in value.lower()
    if offending_class == "credential":
        return "EXAMPLE" in value or "example" in value
    return False


def check_gate_vectors(problems: Problems) -> list:
    yaml = _load_yaml_module()
    vectors = []
    paths = sorted(GATE_VECTORS.glob("*.yaml"))
    problems.check(bool(paths), "conformance/gate_vectors", "directory has no vectors")

    for path in paths:
        where = f"gate_vectors/{path.name}"
        text = path.read_text(encoding="utf-8")
        problems.check(
            text.startswith(BANNER),
            where,
            "must begin with the synthetic-values banner so a bulk redaction "
            "scan can tell fixtures from leaks",
        )
        try:
            vector = yaml.safe_load(text)
        except Exception as exc:
            problems.check(False, where, f"cannot parse: {exc}")
            continue
        required = ("id", "gate", "expected_verdict", "title", "spec", "document")
        missing = [key for key in required if key not in (vector or {})]
        if not problems.check(not missing, where, f"missing keys {missing}"):
            continue
        vectors.append(vector)

        problems.check(
            vector["id"] == path.stem, where, "id does not match the file name"
        )
        gate = vector["gate"]
        if not problems.check(
            gate in GATE_VERDICTS, where, f"unknown gate {gate!r}"
        ):
            continue
        problems.check(
            vector["expected_verdict"] in GATE_VERDICTS[gate],
            where,
            f"verdict {vector['expected_verdict']!r} is not one of "
            f"{sorted(GATE_VERDICTS[gate])} for gate {gate}",
        )
        problems.check(
            path.stem.startswith(gate.replace("export", "export-")),
            where,
            f"file name should start with the gate name ({gate})",
        )

        document_text = yaml.safe_dump(vector["document"], allow_unicode=True)
        offending = vector.get("offending")

        if gate == "redaction" and vector["expected_verdict"] == "reject":
            if problems.check(
                isinstance(offending, dict),
                where,
                "a redaction rejection vector must declare what is wrong with it",
            ):
                for key in ("class", "value", "location", "synthetic_source"):
                    problems.check(key in offending, where, f"offending.{key} is missing")
                offending_class = offending.get("class")
                value = str(offending.get("value", ""))
                problems.check(
                    offending_class in OFFENDING_SHAPES,
                    where,
                    f"unknown offending class {offending_class!r}",
                )
                problems.check(
                    value in document_text,
                    where,
                    f"declared offending value {value!r} does not appear in the "
                    "document, so the vector tests nothing",
                )
                shape = OFFENDING_SHAPES.get(offending_class)
                if shape:
                    problems.check(
                        bool(shape.search(value)),
                        where,
                        f"{value!r} does not have the shape of a {offending_class}",
                    )
                problems.check(
                    _is_reserved(offending_class, value),
                    where,
                    f"{value!r} is not drawn from a reserved documentation range; "
                    "fixtures must be obviously synthetic",
                )
        elif vector["expected_verdict"] == "accept":
            problems.check(
                offending is None,
                where,
                "an accept-control vector must not declare an offending value",
            )

        if gate == "schema" and vector["expected_verdict"] == "reject":
            problems.check(
                isinstance(vector.get("violation"), dict)
                and "rule" in vector["violation"],
                where,
                "a schema rejection vector must name the rule it violates",
            )

        if gate == "export":
            problems.check(
                int(vector.get("runs", 0)) >= 2,
                where,
                "an export idempotence vector must declare at least 2 runs",
            )

        entry = (vector["document"].get("entries") or [{}])[0]
        if "rule" in entry and "scope" in entry:
            computed = reference.content_hash(entry)
            problems.check(
                str(entry.get("content_hash")) == computed,
                where,
                f"the document's content_hash is stale: canonicalization gives "
                f"{computed}",
            )

    return vectors


def check_gate_vectors_against_schema(problems: Problems, vectors: list):
    """Use the real schema, when jsonschema is installed, to prove each gate
    vector has exactly the defect it claims and no other."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        problems.note(
            "jsonschema is not installed, so gate vectors were not validated "
            "against schemas/knowledge-v2.schema.json. Install it with: "
            "python3 -m pip install jsonschema"
        )
        return
    if not SCHEMA.is_file():
        problems.note(
            "schemas/knowledge-v2.schema.json is not present in this checkout, "
            "so gate vectors were not validated against it"
        )
        return
    validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
    for vector in vectors:
        where = f"gate_vectors/{vector['id']}.yaml"
        errors = list(validator.iter_errors(vector["document"]))
        should_be_invalid = (
            vector["gate"] == "schema" and vector["expected_verdict"] == "reject"
        )
        if should_be_invalid:
            problems.check(
                bool(errors),
                where,
                "the document validates against the schema, so this rejection "
                "vector does not test what it says it tests",
            )
        else:
            detail = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
            problems.check(
                not errors,
                where,
                f"the document must be schema-valid so the declared defect is "
                f"the only one: {detail}",
            )


# --- kit hygiene -----------------------------------------------------------

FORBIDDEN_IMPORTS = ("tools", "bot", "server", "sync")


def check_kit_independence(problems: Problems):
    pattern = re.compile(
        r"^\s*(?:from|import)\s+(" + "|".join(FORBIDDEN_IMPORTS) + r")\b", re.M
    )
    for path in sorted(HERE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        found = pattern.findall(source)
        problems.check(
            not found,
            f"conformance/{path.name}",
            f"imports {found} from a package the kit is supposed to test; the "
            "kit must run in a checkout where those do not exist yet",
        )


def run(quiet: bool = False) -> Problems:
    problems = Problems(quiet=quiet)
    vectors = check_hash_vectors(problems)
    check_hash_vectors_against_schema(problems, vectors)
    check_anchor(problems, vectors)
    gate_vectors = check_gate_vectors(problems)
    check_gate_vectors_against_schema(problems, gate_vectors)
    check_kit_independence(problems)
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quiet", action="store_true", help="print only problems")
    args = parser.parse_args(argv)

    problems = run(quiet=args.quiet)
    sys.stdout.write(
        f"\n{problems.checks} consistency checks, {len(problems.items)} problems, "
        f"{len(problems.notes)} notes\n"
    )
    if problems.items:
        sys.stdout.write(
            "the kit is NOT self-consistent. Fix the vectors before asking any "
            "implementation to conform to them.\n"
        )
        return 1
    sys.stdout.write("the kit is self-consistent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
