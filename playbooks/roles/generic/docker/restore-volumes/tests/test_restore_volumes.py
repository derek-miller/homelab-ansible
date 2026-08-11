#!/usr/bin/env python3
"""Tests for restoring from age-encrypted archives.

The failure guarded here is the quiet one: encryption goes on, backups keep
succeeding, and the break only surfaces the day someone needs a restore.
Stdlib only, since CI's venv holds just yamllint.
"""

import fnmatch
import os
import re
import sys

ROLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULTS = os.path.join(ROLE, "defaults", "main.yml")
MAIN = os.path.join(ROLE, "tasks", "main.yml")
RESTORE_VOLUME = os.path.join(ROLE, "tasks", "restore_volume.yml")

results = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("      got:  %r" % (got,))
        print("      want: %r" % (want,))
    results.append(ok)
    return ok


def read(path):
    with open(path) as fh:
        return fh.read()


def archive_patterns():
    text = read(DEFAULTS)
    block = re.search(r"^docker_restore_volumes_archive_patterns:\n((?:\s*-\s*.+\n)+)", text, re.M)
    if not block:
        return []
    return re.findall(r'-\s*"([^"]+)"', block.group(1))


def volume_regex():
    m = re.search(r"map\('regex_search',\s*'([^']+)'", read(MAIN))
    return m.group(1) if m else None


def tasks(text):
    blocks = re.split(r"^(?=\s*- name: )", text, flags=re.M)
    found = {}
    for block in blocks:
        m = re.match(r"\s*- name: (.+)", block)
        if m:
            found[m.group(1).strip()] = block
    return found


# Verified against offen v2.48.2 on a real archive.
PLAINTEXT = "backup-2026-08-09T01-00-00.tar.gz"
ENCRYPTED = "backup-2026-08-10T01-00-00.tar.gz.age"


def test_globs_match_encrypted_archives():
    pats = archive_patterns()
    check("defaults declare more than one archive pattern", len(pats) >= 2, True)

    def matched(name):
        return any(fnmatch.fnmatch(name, p) for p in pats)

    check("plaintext archive is discoverable", matched(PLAINTEXT), True)
    check("age archive is discoverable", matched(ENCRYPTED), True)
    check("unrelated files are not discoverable", matched("backup-notes.txt"), False)


def test_newest_archive_wins_across_the_cutover():
    pats = archive_patterns()
    listing = [
        "backup-2026-08-08T01-00-00.tar.gz",
        PLAINTEXT,
        ENCRYPTED,
        "backup-2026-08-07T01-00-00.tar.gz",
    ]

    def latest(names):
        found = sorted(f for f in names if any(fnmatch.fnmatch(f, p) for p in pats))
        return found[-1] if found else None

    check("latest archive is the encrypted one", latest(listing), ENCRYPTED)

    check(
        "a newer plaintext archive still wins",
        latest([ENCRYPTED, "backup-2026-08-11T01-00-00.tar.gz"]),
        "backup-2026-08-11T01-00-00.tar.gz",
    )


def test_volume_regex_reads_real_tar_output():
    """Member names are identical either side of encryption."""
    rx = volume_regex()
    check("volume regex was found in main.yml", rx is not None, True)
    if rx is None:
        return
    lines = [
        "backup/",
        "backup/letsencrypt/",
        "backup/letsencrypt/acme.json",
        "backup/portainer-ee-data/",
        "backup/portainer-ee-data/portainer.db",
        "/backup/keycloak-pg-data/PG_VERSION",
    ]
    names = []
    for line in lines:
        m = re.search(rx, line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    check(
        "volume names extracted",
        names,
        ["letsencrypt", "portainer-ee-data", "keycloak-pg-data"],
    )


def test_no_task_reads_the_archive_directly():
    """A bare `tar -xzf <archive>` works right up until the archive is
    encrypted, so every read has to go through docker_restore_archive_cat."""
    for path in (MAIN, RESTORE_VOLUME):
        text = read(path)
        label = os.path.basename(path)
        named = re.findall(r"tar\s+-[a-z]*f\s+(?!-)(\S+)", text)
        check("%s: tar never opens a named archive" % label, named, [])
        for stmt in re.findall(r"^.*docker_restore_latest_backup.*$", text, re.M):
            check(
                "%s: latest_backup not mounted or untarred: %s" % (label, stmt.strip()[:60]),
                ("tar " in stmt) or (":/archive" in stmt),
                False,
            )


def test_pipelines_fail_loudly():
    for path in (MAIN, RESTORE_VOLUME):
        text = read(path)
        label = os.path.basename(path)
        for block in re.findall(
            r"shell:\s*(?:>-\s*\n)?((?:.*\n)*?)\s*(?:args|when|changed_when|register):", text
        ):
            if "|" not in block:
                continue
            check("%s: piped shell sets pipefail" % label, "pipefail" in block, True)
        # pipefail is a bashism; under /bin/sh it is silently not set.
        check(
            "%s: shell tasks declare bash" % label,
            len(re.findall(r"^\s*shell:", text, re.M)),
            len(re.findall(r"^\s*executable: /bin/bash\s*$", text, re.M)),
        )


def test_identity_is_staged_and_removed():
    text = read(MAIN)
    staging = tasks(text).get("stage the age identity on the restore target", "")
    check(
        "identity is staged from the vault-encrypted file",
        "docker_restore_volumes_age_identity_file" in staging,
        True,
    )
    check("staging suppresses --diff", "diff: no" in staging, True)
    always = text.split("\n  always:\n")
    check("main.yml has an always section", len(always) == 2, True)
    if len(always) == 2:
        check(
            "identity is removed in always",
            "docker_restore_volumes_age_identity_path" in always[1]
            and "state: absent" in always[1],
            True,
        )
    cleanup = tasks(text).get("remove the age identity from the restore target", "")
    check("cleanup is unconditional", "when:" in cleanup, False)


def test_check_mode_stages_what_the_listing_needs():
    """The listing carries check_mode: false, so its prerequisites and the
    cleanup that undoes them need it too, or --check dies on a missing age."""
    found = tasks(read(MAIN))
    for name in (
        "install age to decrypt the archive",
        "stage the age identity on the restore target",
        "list volume directories in archive",
        "remove the age identity from the restore target",
    ):
        check("%s: runs under check mode" % name, "check_mode: false" in found.get(name, ""), True)


def test_shell_interpolations_are_quoted():
    """Volume names come out of archive member names, which permit spaces and
    shell metacharacters."""
    for path in (MAIN, RESTORE_VOLUME):
        label = os.path.basename(path)
        for name, block in tasks(read(path)).items():
            if not re.search(r"^\s*shell:", block, re.M):
                continue
            body = block.split("\n", 1)[1]
            body = re.split(r"^\s*(?:args|when|register|changed_when):", body, flags=re.M)[0]
            for expr in re.findall(r"\{\{(.+?)\}\}", body, re.S):
                check(
                    "%s: %s interpolates %s quoted" % (label, name, expr.strip()[:40]),
                    "| quote" in expr or expr.strip() == "docker_restore_archive_cat",
                    True,
                )


def test_decrypt_only_when_encrypted():
    text = read(MAIN)
    m = re.search(r"docker_restore_archive_cat:\s*>-\s*\n((?:\s{10}.*\n)+)", text)
    check("archive read command is defined", m is not None, True)
    if m is None:
        return
    expr = m.group(1)
    check("archive read command is a single expression", expr.count("{{"), 1)
    check("uses age --decrypt for encrypted archives", "age --decrypt" in expr, True)
    check("falls back to cat for plaintext archives", "'cat'" in expr, True)
    check("keyed on the encrypted flag", "docker_restore_backup_encrypted" in expr, True)
    check(
        "encrypted flag is set from the .age suffix",
        "docker_restore_latest_backup.endswith('.age')" in text,
        True,
    )


for fn in (
    test_globs_match_encrypted_archives,
    test_newest_archive_wins_across_the_cutover,
    test_volume_regex_reads_real_tar_output,
    test_no_task_reads_the_archive_directly,
    test_pipelines_fail_loudly,
    test_identity_is_staged_and_removed,
    test_check_mode_stages_what_the_listing_needs,
    test_shell_interpolations_are_quoted,
    test_decrypt_only_when_encrypted,
):
    fn()

failed = results.count(False)
print("\n%d passed, %d failed" % (results.count(True), failed))
sys.exit(1 if failed else 0)
