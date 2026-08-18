"""Server-authored messages to users.

Everything else the backend stores is ciphertext it cannot read. These are the
exception, and deliberately so: they are the *server's own words* - maintenance
windows, a quota change, a note to one account - not user content, so there is
nothing to encrypt and nobody to encrypt it to. Nothing here ever quotes an
entry, a note, or a space name the server would have had to decrypt to know.

Delivery is belt and braces, because the interesting cases are exactly the ones
where the user is not looking: a live socket gets it now, and
``GET /announcements`` hands the same rows to a device that was closed. The
client keys them as ``announcement:<id>``, so both paths landing is a no-op.
"""
