# Contributing

Status: current

Useful knowledge is ordinary Markdown with a title and non-empty body. Preserve
known conditions, observations, evidence references and uncertainty in the text.
No YAML schema, fixed sections, runtime coordinates or verification labels are
required. A note can record a useful observation without claiming a confirmed
root cause. Keep conclusions proportional to the evidence.

Knowledge remains reference material. The Agent decides whether to read or
retain it; lookup and capture are not required development or completion steps.
See [the package README](README.md) for the ordinary tools.

## Public notes

Contribute only content authorized for public sharing. The package prepares a
redacted public copy while retaining the private local source. Internal addresses,
hostnames, machine and container identifiers, private paths and credentials must
stay out of the public copy. Preserve technical conditions needed to understand
the observation when they can be shared safely.

Use the existing configured publishing path or
`python -m vaws_knowledge contribution prepare --help` for an explicit contribution
task. A redaction or transport failure affects that export only. Local notes
remain usable. Never upload existing private candidates merely because a sharing
configuration has been enabled.

The public corpus uses human review and merge. Review the text and its evidence,
combine compatible duplicates when useful, and preserve unresolved differences.
There is no trust promotion ladder or requirement to resolve every difference
before retaining an observation. See [public contribution](docs/contribution.md)
and [publishing setup](docs/publishing.md).

## Package changes

Keep runtime behavior in its owning package. Update affected callers, help,
skills and documentation with API changes. Run the checks relevant to the
change; existing fixtures and historical measurements are not fresh hardware
evidence. Native OpenViking tests require `VAWS_KNOWLEDGE_LIVE_OV=1` and their
documented local model cache; they are skipped by the default test command.

```sh
python -m pip install -e ".[test]"
python -m pytest tests
```

The full test command is available for package validation, not an extra step for
writing a note. Private test data and credentials remain outside tracked files.
