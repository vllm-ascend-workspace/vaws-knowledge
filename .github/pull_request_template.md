<!-- Keep this short. A long form gets filled in mechanically, which is worse
     than no form: it produces the appearance of review. -->

## What this proposes

<!-- One or two sentences. For a corpus change, say which entries and why. -->

## Evidence

<!-- References, not prose. run_manifest id, pull request, commit, ci_run or
     issue. "It worked when I tried it" is not evidence, and an entry whose
     evidence cannot be followed belongs in corpus/unverified/. -->

- [ ] Every entry I am proposing carries at least one followable evidence reference
- [ ] For anything proposed as `verified`: somebody other than me has confirmed it, and is named in `verification.verified_by`

## Screening

- [ ] I ran the source-side gate in my own fork (`python -m vaws_knowledge redact --check <paths>`) and it passed
- [ ] I understand this repository is public and its history cannot be recalled

<!-- If a fact cannot be stated without an address, hostname, path or identifier
     from your environment, it belongs in your own repository's project layer.
     That is a supported outcome, not a failure. -->

## Coordinate

- [ ] All twelve `scope` dimensions are declared
- [ ] Every `any` claim states its basis

<!-- An `any` claim with no stated basis is the usual root cause of two forks'
     knowledge silently contradicting each other. -->
