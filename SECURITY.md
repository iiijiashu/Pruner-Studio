# Security policy

Pruner Studio is a local single-user tool. It is not designed to be exposed to a LAN, the public internet, or a hosted multi-user environment.

## Supported version

Only the latest release is supported.

## Reporting

Please report vulnerabilities privately through GitHub's private vulnerability reporting feature when it is enabled for the repository. Do not include live cookies, QR codes, account identifiers, personal catalogs, or operation logs in an issue.

## Local-data boundary

Authentication state, imported catalogs, rules, and operation records are stored in the current operating-system user's application-data directory. They must never be committed to the repository or placed in the static `web/` directory.

If an authentication session is accidentally published, log out of NetEase Cloud Music, revoke the affected session where possible, and remove it from every Git ref before making the repository public.
