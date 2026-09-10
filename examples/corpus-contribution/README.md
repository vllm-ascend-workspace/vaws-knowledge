# Corpus contribution fixtures

These Markdown files are examples for the contribution package. They are
not a live corpus and are not sent anywhere.

| File | Role |
|---|---|
| `ordinary.md` | Reporter observation, no coordinates |
| `duplicate.md` | Same claim as `related/graph-mode.md` |
| `supplement.md` | Adds a step to the related note |
| `condition-difference.md` | Same topic, different CANN |
| `conflict.md` | Opposite claim under the same conditions |
| `insufficient-evidence.md` | Strong metric without evidence |
| `related/graph-mode.md` | Already-published comparison target |
| `trusted-review.yml` | CI template, not enabled |

`python -m vaws_knowledge.contribution` is the package entry. Do not copy
`trusted-review.yml` into `.github/workflows` from this tree.
