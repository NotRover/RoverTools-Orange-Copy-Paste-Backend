# Security Policy

Orange Copy Paste is an end-to-end encrypted clipboard app. This page says how
to report a vulnerability and what happens next.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Report it privately through GitHub's private vulnerability reporting: go to the
repository's **Security** tab and choose **Report a vulnerability**. This opens a
private advisory visible only to the maintainers and you.

Please include:

- A description of the issue and its impact.
- Steps to reproduce, or a proof of concept.
- The affected version or commit, and your environment (OS, app version).

## What to expect

- We aim to acknowledge a report within a few days.
- We will keep you updated as we investigate and work on a fix.
- We will credit you in the release notes when the fix ships, unless you prefer
  to stay anonymous.

## Scope

The app holds all key material and does all encryption and decryption; the
server stores encrypted items and holds no key that can open them. What the
server can see is listed in the
[security model](https://orange-copy-paste-app.pages.dev/docs/security/#what-the-server-can-see). Reports
that are especially valuable include anything that would let the server or a
third party read plaintext, recover keys, or act as another user or device.

Out of scope: issues that require a device already compromised by malware, and
findings against dependencies that are already tracked upstream.
