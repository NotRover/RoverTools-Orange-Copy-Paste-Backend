# Security Policy

RoverTools is an end-to-end encrypted clipboard product. We take security
reports seriously and appreciate responsible disclosure.

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

The client holds all key material and performs all encryption and decryption;
the backend stores only ciphertext and never sees plaintext or keys. Reports
that are especially valuable include anything that would let the server or a
third party read plaintext, recover keys, or act as another user or device.

Out of scope: issues that require a device already compromised by malware, and
findings against dependencies that are already tracked upstream.
