# Security

PriorFolio 0.1.x is an initial release under development. Report vulnerabilities
privately using this repository's GitHub Security > Report a vulnerability
control once private vulnerability reporting is enabled. Do not post credentials
or real evidence publicly. If private reporting is unavailable, open a minimal
issue requesting a private reporting channel, without exploit details or data.

Use synthetic reproductions. Important boundaries include untrusted evidence
paths, archive contents, remote credential redaction, and disclosure-field leaks.

Private backups are not encrypted. They can contain secrets from the selected
projects, so store them with appropriate access controls. The shareable disclosure
omits automatic local paths, source contents, remote URLs, and file listings.
Review user-authored text before sharing it.

Verification checks consistency. Timestamp signatures are not verified by the
application. Only enable comparisons against local projects for runs you trust:
those comparisons intentionally read the project locations recorded in the run.

Remote-backup confirmation is a user attestation, not proof of remote delivery.
PriorFolio has no access to an email or cloud account.
