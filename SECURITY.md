# Security

PriorFolio 0.1.x is an initial release under development. Report vulnerabilities
privately using [GitHub's private reporting form](https://github.com/jesse-anderson/PriorFolio/security/advisories/new).
Do not post credentials
or real evidence publicly. If private reporting is unavailable, open a minimal
issue requesting a private reporting channel, without exploit details or data.

Use synthetic reproductions. Important boundaries include untrusted evidence
paths, archive contents, remote credential redaction, and disclosure-field leaks.

Backup ZIPs are not encrypted by PriorFolio; the instructions say to protect
each copy with a 7-Zip password before it leaves the computer. A cloud copy can
withhold files whose names suggest secrets, but detection is name-based, so
secrets inside ordinary files remain. Store every copy with appropriate access
controls. The shareable disclosure
omits automatic local paths, source contents, remote URLs, and file listings.
Review user-authored text before sharing it.

Verification checks consistency. Timestamp signatures are checked with OpenSSL 3
against three root certificates pinned in `priorfolio.py`; without OpenSSL 3
they are reported as not checked. Timestamp requests reveal the user's IP
address to the three services. Only enable comparisons against local projects
for runs you trust: those comparisons intentionally read the project locations
recorded in the run.

Scanning runs Git read-only in each selected project. PriorFolio skips
`git status` in repositories whose own config defines clean filters or includes
other config files, so scanning does not start programs named by the folder.

Remote-backup confirmation is a user attestation, not proof of remote delivery.
PriorFolio has no access to an email or cloud account.
