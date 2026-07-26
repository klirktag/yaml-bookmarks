# Encrypted bookmarks

> **Status: implemented.** This document is the single source of truth for how
> bookmark encryption works. It is implemented in `yaml_bookmarks/crypto.py`
> (primitives + `CryptoSession`) and `yaml_bookmarks/storage.py`
> (`BookmarkStore.unlock` / `lock` / `is_unlocked` and the encrypted-aware
> read/write paths), surfaced through the CLI and web UI as described below.

## Goal

Let a user optionally encrypt individual bookmarks so that someone who finds the
files on disk cannot read the URL, title, description or tags without a
password. Encrypted and plaintext bookmarks live side by side in the same store
directory and are used through the same CLI / web UI.

Two goals drive every decision below:

1. **Password-only secret.** The only thing the user must remember or carry
   between machines is a password. No key files to manage or move.
2. **Portability.** It must be easy to copy bookmarks — all of them or just a
   few — to another machine and keep using them. See
   [Portability](#portability) — this is the goal that shapes the salt design.

## Threat model

- **In scope:** an unauthorised person who obtains a copy of the store directory
  (a lost laptop, a synced folder, a backup, a shared disk) should not be able
  to read encrypted bookmarks without the password.
- **Explicitly out of scope:** a compromised running machine (keylogger, memory
  scraping while the vault is unlocked), and weak user passwords. The master key
  necessarily lives in RAM while the vault is unlocked.

The bar is "keep a non-high-tech person from reading your bookmarks," while using
standard, well-reviewed cryptography so that the actual strength is much higher
than that floor. It is deliberately *not* trying to be a hardened secrets
manager.

## Non-goals

- Not encrypting the *number* of bookmarks or their file timestamps on disk
  (filesystem metadata leaks that a vault exists and roughly how big it is).
- Not hiding that a given file is encrypted (the format is self-describing).
- Not multi-user access control, sharing, or key rotation policies.

## Overview

- Encryption is **per bookmark, opt-in.** A bookmark is either plaintext (the
  existing format) or encrypted; both can coexist in one directory.
- **Locked by default.** Before a password is entered, encrypted bookmarks are
  not shown at all — not listed, not searchable, not counted. They become visible
  and usable only after the vault is unlocked, and vanish again when it is locked.
- **A single flag, `crypt: true`, marks an encrypted file.** Plaintext bookmarks
  carry no encryption-related fields at all — no flag, no salt, no nonce — they
  remain byte-for-byte the current format. All the encryption fields are optional
  and appear only on encrypted files.
- Each encrypted bookmark is **one self-contained YAML file**: it carries
  everything needed to decrypt it *except the password*.
- The **entire payload** (url, title, description, tags, timestamps) is encrypted
  into a single blob. The only plaintext in an encrypted file is the `crypt: true`
  flag, the KDF parameters, the salt, and the nonce.
- Filenames for encrypted bookmarks are **random UUIDs** — the URL is secret and
  must not appear in the filename.
- Authenticated encryption (AEAD) is used throughout, so tampering or corruption
  is detected on decrypt.

## Cryptographic choices

### Cipher (the least critical choice)

Any modern AEAD is acceptable. Recommended:

- **XChaCha20-Poly1305** (via PyNaCl `SecretBox`) — 192-bit nonce removes any
  nonce-reuse concern, constant-time in software, misuse-resistant API. Preferred.
- **AES-256-GCM** (via the `cryptography` package) — the conventional standard,
  hardware-accelerated. Fine alternative; use a fresh random 96-bit nonce per
  file.

Never use unauthenticated modes (e.g. raw AES-CBC) without a separate MAC.

### Key derivation (the critical choice)

The security of password-based encryption lives in a slow, memory-hard KDF, not
in the cipher — an attacker with the files runs an *offline* guessing attack, and
the KDF is what makes each guess expensive. In order of preference:

1. **Argon2id** (via PyNaCl / `argon2-cffi`) — memory-hard, GPU/ASIC-resistant.
2. **scrypt** (`hashlib.scrypt`, standard library) — memory-hard, no extra
   dependency. Good default if minimising dependencies matters.
3. **PBKDF2-HMAC-SHA256** (`hashlib`) — stdlib but weakest against GPUs; only
   with a very high iteration count.

Tune parameters so one derivation takes **~100–300 ms** on a typical machine.
Store the chosen parameters in each file so they can evolve without breaking old
files.

## Salt and nonce — the key design decision

This is where portability and performance are reconciled.

- **One shared salt per vault, embedded in every file.** The salt is not secret,
  so copying the same salt into each encrypted file is harmless. Because all
  files in a vault share the salt, the app derives the master key **once**,
  caches it in memory for the session, and reuses it for every file. Listing 300
  bookmarks is **one** KDF run, not 300.
- **A unique random nonce per file.** This is what guarantees distinct
  ciphertexts. It is cheap (no KDF), so it costs nothing at scale.

Why **not** a unique salt per file: a per-file salt forces one (deliberately
slow) KDF run *per bookmark*, e.g. 300 × 200 ms ≈ 60 s just to list the library.
Unusable. Per-file salt only makes sense if each bookmark could be unlocked by a
*different* password, which is not the use case.

Why **not** a single salt kept in a separate metadata file: that would make a
bookmark file depend on external state, breaking "copy just a few files." Keeping
the (shared) salt inside each file keeps every file self-contained. See below.

## File format

**Detection rule:** a file is encrypted **if and only if it has `crypt: true`.**
No flag ⇒ it's a plaintext bookmark. That single flag is the only thing the
reader needs to check; all other encryption fields are optional and present only
on encrypted files.

A **plaintext** bookmark is unchanged from today — no encryption fields:

```yaml
url: https://example.com
title: Example
description: ...
tags: [a, b]
created: 1769380888   # optional unix timestamp (seconds)
```

An **encrypted** bookmark has filename `<uuid4>.yaml` (random, leaks nothing) and
looks like:

```yaml
crypt: true                      # the only signal that this file is encrypted
version: 1                       # optional; lets the encrypted format evolve
kdf: scrypt                      # scrypt | argon2id
kdf_params: { n: 16384, r: 8, p: 1 }
salt: <base64>                   # shared across the vault, duplicated per file
nonce: <base64>                  # unique per file
ciphertext: <base64>            # AEAD-encrypts the whole payload below
```

The plaintext that gets encrypted into `ciphertext` is exactly the normal record
shown above (url, title, description, tags, and optional `created`).

Notes:

- **Encrypt everything**, including the `created` timestamp — a plaintext date
  would leak activity patterns. The folder (per the existing model)
  is still expressed by the file's directory location, which is *not* encrypted;
  users who consider folder names sensitive should keep encrypted bookmarks at
  the root or in innocuously named folders.
- **Bind the filename as AEAD associated data (AAD).** This stops a ciphertext
  from being copied into a different file and silently accepted.
- **No central verifier needed for correctness.** The AEAD authentication tag is
  itself the password check: a wrong password fails to decrypt. An optional
  central "verifier token" could be added purely for a fast "wrong password"
  message, but nothing depends on it — keeping files self-sufficient.

## Visibility and unlocking

Encrypted bookmarks are **hidden until the vault is unlocked** — the app behaves,
before any password, as if only the plaintext bookmarks exist.

- **Locked (default) state.** Files with `crypt: true` are skipped entirely. The
  listing, search results, folder counts, and the JSON API all contain *only*
  plaintext bookmarks. Someone who opens the app without the password sees no
  encrypted titles, URLs, tags — not even a count of how many exist.
- **The padlock.** A padlock icon is always visible in the UI, shown **unlocked
  by default**. In that state no password is engaged: only plaintext bookmarks are
  visible and new bookmarks are saved as plaintext. The icon is present whether or
  not any encrypted files exist, so it reveals nothing on its own.
- **Clicking the padlock prompts for a password**, which is then used for two
  things at once:
  - **Unlocking** — every encrypted bookmark that password can decrypt becomes
    visible and fully usable (listed, searched, edited, moved) for the session.
  - **Encrypting new bookmarks** — while that password is engaged, newly added
    bookmarks are encrypted with it (see open questions re: allowing plaintext
    while engaged).

  The icon then reflects that a password is engaged.
- **Clicking the padlock again forgets the password.** Its key is dropped from
  memory, the bookmarks it revealed disappear from the UI, and new bookmarks
  revert to plaintext — locked again until a password is re-entered. (An idle
  timeout can auto-forget as well.)
- **One set at a time, switchable.** Only one password is engaged at any moment,
  so at most one set of encrypted bookmarks is visible (alongside the
  always-visible plaintext ones). Different passwords unlock different sets, but
  never simultaneously: to switch, lock the padlock and unlock again with the
  other password — the previous set is forgotten and hidden, the new one revealed.
  This still supports keeping, e.g., separate "work" and "personal" passwords; you
  switch between them. New bookmarks are encrypted with whichever password is
  currently engaged.
- **Encrypted bookmarks are marked.** Once visible, each encrypted bookmark is
  clearly flagged with a **padlock icon after its name/title**, so at a glance it
  is obvious which bookmarks are encrypted versus plaintext. (The CLI shows an
  equivalent marker, e.g. a `🔒` prefix.)
- **Wrong password.** Nothing unlocks — the AEAD tag fails to verify — and the app
  reports "wrong password" while staying locked.
- **Where the key lives.** Web UI: the engaged password's key lives only in server
  memory for the session, and an idle timeout drops it. CLI: the equivalent is a
  password supplied per command or via a short-lived unlocked session (prompted
  with `getpass`); encrypted bookmarks are omitted from `list` and lookups until a
  password is provided.

This is UI-level hiding, not steganography: on disk, a file with `crypt: true` is
plainly an encrypted bookmark to anyone who reads it. What stays protected is the
*contents*; the app simply declines to surface encrypted bookmarks until unlock.

## Portability

The whole design optimises for this.

- **Only the password travels between machines — in the user's head.** There is
  no key file to export or move.
- **Every encrypted file is self-contained** (salt + KDF params + nonce +
  ciphertext), so copying *any subset* of files is enough. On the destination:
  install `yaml-bookmarks`, point `YAML_BOOKMARKS_DIR` at the copied files, enter
  the password, and continue.
- **Mixed plaintext + encrypted is first-class.** The `crypt: true` flag
  distinguishes them, so non-sensitive bookmarks stay plaintext and copy with zero
  friction; only encrypted ones prompt for a password. This directly supports
  "copy some" — the user decides per bookmark.
- **Copying between machines with different passwords works.** Files carry their
  own salt, so files encrypted under different passwords (different salts) can
  coexist in one directory. Because only one password is engaged at a time, the app
  shows the set matching the currently engaged password and keeps the rest hidden
  until you switch passwords. Multiple independent "vaults" in one directory is
  therefore supported — you view them one at a time.

## Costs and trade-offs

- **Salt duplicated per file** — a few dozen harmless (non-secret) bytes each.
- **Changing the password rewrites every encrypted file** (re-derive key,
  re-encrypt). Unavoidable in any scheme; treat it as a one-off maintenance
  operation.
- **Master key in RAM while unlocked** — inherent to the threat model; acceptable
  given the goals.
- **A new dependency.** Real crypto should not be hand-rolled in pure Python
  (timing/correctness risks). This relaxes the project's current
  "pure-Python, no binary assets, two dependencies" principle in `CLAUDE.md`.
  Two acceptable shapes:
  - **PyNaCl (libsodium)** — one dependency providing *both* Argon2id and
    XChaCha20-Poly1305 with a hard-to-misuse API. Recommended baseline.
  - **`hashlib.scrypt` (stdlib) + `cryptography`'s AES-256-GCM** — one
    dependency, keeps the KDF in the standard library.

## As implemented

The shipped code uses the single-dependency (`cryptography`) shape:

- **KDF:** `hashlib.scrypt` (standard library), `n=2**15, r=8, p=1`
  (~150 ms, ~32 MiB per derivation). Parameters are stored per file
  (`kdf_params`) so they can evolve without breaking old files.
- **Cipher:** AES-256-GCM (`cryptography`), a fresh 12-byte nonce per file.
- **Salt:** one shared 16-byte salt per vault, embedded in every file; keys are
  derived once per salt and cached in `CryptoSession`.
- **Filenames:** `<uuid4>.yaml`. **Payload:** the whole record serialised to JSON
  and encrypted as one blob; the filename is bound as AEAD associated data.
- **Session:** the derived key(s) live only in memory (`BookmarkStore._session`);
  `lock()` drops them.

**Surfaces:**

- **CLI:** `-p/--password` on the read/write subcommands (prompts if given with
  no value); `add --encrypt/-e` writes a new bookmark encrypted; encrypted rows
  are marked `🔒`.
- **Web:** `GET /api/status`, `POST /api/unlock` `{password}`, `POST /api/lock`;
  the header padlock toggles engagement. `GET`/`POST /api/bookmarks` omit
  encrypted items while locked and encrypt new ones while engaged; encrypted
  bookmarks render a 🔒 after the title. The engaged key stays in server memory.

The originally-considered Argon2id + XChaCha20-Poly1305 (via PyNaCl) remains a
valid alternative; scrypt + AES-256-GCM was chosen to add only one dependency and
keep the KDF in the standard library.

## Interaction with the existing architecture

- Lives behind `BookmarkStore` (see `storage.py`) so the CLI and web UI stay
  thin. Reading a file checks for the `crypt: true` flag: files without it parse
  as plaintext exactly as today; files with it are decrypted with the cached
  master key.
- Encrypted bookmarks are filtered out of every read path (`list`, `get`,
  `folders` counts, the JSON API) while locked, and included once unlocked — see
  [Visibility and unlocking](#visibility-and-unlocking). The held master key is
  what flips that behaviour; the web UI keeps it in server memory for the session,
  the CLI obtains it via `getpass` (optionally a short-lived agent).
- `folders()` still works — folder membership is the directory path, which is not
  encrypted.

## Open questions (for whenever this is built)

- While a password is engaged, whether *every* new bookmark is encrypted with it
  (the default described above) or the user can still opt a specific bookmark to
  plaintext via a per-add override. The CLI equivalent of "engaged" (e.g. an
  `--encrypt`/`--password` flag or a short-lived unlocked session).
- Locking policy for the web UI (idle timeout before the key is dropped).
- Whether to offer "encrypt existing" / "decrypt to plaintext" migrations.
- Optional key-file unlock as an alternative to a typed password (still portable,
  but adds a secret to move).
