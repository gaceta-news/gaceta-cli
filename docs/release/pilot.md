# Local beta release checklist

Use disposable data throughout. Record the exact commit, OS, Python, client versions, elapsed times, and pass/fail for each step. A local wheel smoke is not a cold GitHub install.

- [ ] `make check` on the integrated release candidate.
- [ ] `bash scripts/smoke-release.sh`: build wheel from sdist, install locked requirements, render an offline paper.
- [ ] Match package metadata to the intended release tag. From outside a checkout, install the exact public revision following the README in a new user environment with empty uv cache. Time downloads separately from edition generation.
- [ ] Build an edition on the prior release with synthetic reader data. Stop writers; copy the entire disposable data directory. Upgrade the test installation and rebuild. Check story IDs, seen state, saved stories and source links, not just the row count. Restore the untouched backup to a second directory with the prior binary and verify those checks again.
- [ ] Verify both supported clients using only isolated public exports. Check supported change and cited source, no material change, unknown prior reading, skipped edition, missing evidence, private data exclusion and no writes during reads. Review a real public-source change with a real editor separately from the scripted demo.

## Reader pilot (opt-in, no telemetry)

Invite three readers first; fix common blockers before inviting seven more. Use self-install on the reader's own machine, not a shared instance. Explain model costs before setup. Ask them to use a few public feeds they already follow. Observe setup with permission; record where intervention was needed without collecting feeds, raw transcripts, or personal reading history.

After one week ask once: Did you get a cited answer on your own without help? On how many separate days did you come back? What change did the paper help you understand, and did its source support it? What did it get wrong or send you looking up yourself? Feedback is optional; record nonresponses as unknown.

Expand only if at least six of ten get a cited answer unaided and four report returning on at least three separate days the next week. Require a concrete useful example and independent success in each advertised client. Hold for privacy leakage, unsupported changes in acceptance cases, or broken advertised installs. No synthetic run counts as a reader.
