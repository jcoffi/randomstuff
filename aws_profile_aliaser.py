#!/usr/bin/env python3
"""
aws_profile_aliaser.py

Terraform's `provider "aws" { profile = "<name>" }` hardcodes AWS profile names
that must exist in ~/.aws/config, or `terraform plan` (and terravision) fails with
  "failed to get shared config profile, <name>".

When you sign in via SSO, your generated profiles are typically named with the
account number as a suffix (e.g. `cmm-a-manage-111111111111`), so the bare name
the code wants (`cmm-a-manage`) doesn't exist.

This tool:
  1. Scans your Terraform repos for the AWS profile names the code references.
  2. Matches each to an existing account-suffixed SSO profile in ~/.aws/config.
  3. Appends the missing `[profile <name>]` SSO blocks (aliases) copying the
     matched profile's SSO settings.

DRY RUN by default -- prints the plan and the exact blocks it would add.
Use --apply to write (a timestamped backup of ~/.aws/config is made first).

Usage:
    python3 aws_profile_aliaser.py --terraform-root ~/terraform
    python3 aws_profile_aliaser.py --terraform-root ~/terraform --apply
    python3 aws_profile_aliaser.py --org            # suggest account ids for unmatched
"""
import argparse, configparser, glob, json, os, re, shutil, subprocess, sys
from datetime import datetime

# any `profile = "literal"` in .tf (covers provider/backend/assume_role blocks;
# variable-based `profile = var.x` is intentionally ignored -- nothing to alias)
PROFILE_REF = re.compile(r'profile\s*=\s*"([^"]+)"')
ACCT_SUFFIX = re.compile(r"[-_]?\d{12}$")
SSO_KEYS = ["sso_session", "sso_start_url", "sso_region",
            "sso_account_id", "sso_role_name", "region", "output"]


def referenced_profiles(terraform_root):
    """{profile_name: [relative .tf files that reference it]}."""
    refs = {}
    root = os.path.expanduser(terraform_root)
    for tf in glob.glob(os.path.join(root, "**", "*.tf"), recursive=True):
        try:
            text = open(tf).read()
        except Exception:
            continue
        for name in PROFILE_REF.findall(text):
            refs.setdefault(name, []).append(os.path.relpath(tf, root))
    return refs


def load_profiles(aws_config):
    """{profile_name: {settings}} from ~/.aws/config (default + [profile ...])."""
    cp = configparser.ConfigParser()
    try:
        cp.read(os.path.expanduser(aws_config))
    except configparser.Error as e:
        sys.exit(f"could not parse {aws_config}: {e}")
    profiles = {}
    for section in cp.sections():
        if section == "default":
            profiles["default"] = dict(cp[section])
        elif section.startswith("profile "):
            profiles[section[len("profile "):].strip()] = dict(cp[section])
    return profiles


def sso_fields(settings):
    return {k: settings[k] for k in SSO_KEYS if k in settings}


def match_existing(ref, profiles):
    """Find the existing profile to copy SSO settings from.
    Returns (matched_name, reason) or (None, reason)."""
    if ref in profiles:
        return ref, "exists"                       # already resolvable; nothing to do
    # existing profile == ref + account-number suffix (the common SSO naming)
    suffixed = [n for n in profiles
                if n.startswith(ref) and ACCT_SUFFIX.fullmatch(n[len(ref):])]
    if len(suffixed) == 1:
        return suffixed[0], "suffix"
    if len(suffixed) > 1:
        return None, "ambiguous"                   # multiple account-suffixed matches
    # loose: ref contained in exactly one profile that has an sso_account_id
    loose = [n for n in profiles
             if ref in n and sso_fields(profiles[n]).get("sso_account_id")]
    if len(loose) == 1:
        return loose[0], "contains"
    return None, "none"


def render_block(name, fields):
    lines = [f"[profile {name}]"]
    for k in SSO_KEYS:
        if k in fields:
            lines.append(f"{k} = {fields[k]}")
    return "\n".join(lines) + "\n"


def org_accounts():
    """{account_name_lower: account_id} via `aws organizations list-accounts`."""
    try:
        out = subprocess.run(["aws", "organizations", "list-accounts", "--output", "json"],
                             capture_output=True, text=True)
    except OSError as e:
        print(f"  [org] aws CLI not available: {e}", file=sys.stderr)
        return {}
    if out.returncode != 0:
        print(f"  [org] list-accounts failed: {out.stderr.strip()[:200]}", file=sys.stderr)
        return {}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}
    return {a["Name"].lower(): a["Id"] for a in data.get("Accounts", [])}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--terraform-root", default="~/terraform")
    ap.add_argument("--aws-config",
                    default=os.environ.get("AWS_CONFIG_FILE") or "~/.aws/config")
    ap.add_argument("--apply", action="store_true",
                    help="write the new [profile ...] blocks (default: dry run)")
    ap.add_argument("--org", action="store_true",
                    help="use `aws organizations list-accounts` to suggest account ids "
                         "for names with no matching profile")
    args = ap.parse_args()

    cfg_path = os.path.expanduser(args.aws_config)
    refs = referenced_profiles(args.terraform_root)
    if not refs:
        sys.exit(f"No `profile = \"...\"` references found under {args.terraform_root}")
    profiles = load_profiles(cfg_path)

    print(f"referenced profiles: {len(refs)}  |  existing profiles in "
          f"{args.aws_config}: {len(profiles)}\n", file=sys.stderr)

    to_add, resolved, unresolved = [], [], []
    for ref in sorted(refs):
        matched, reason = match_existing(ref, profiles)
        if reason == "exists":
            resolved.append((ref, "already in config"))
        elif matched:
            fields = sso_fields(profiles[matched])
            if not fields.get("sso_account_id") and not fields.get("sso_start_url"):
                unresolved.append((ref, f"matched {matched} but it has no SSO fields"))
            else:
                to_add.append((ref, matched, fields))
        else:
            unresolved.append((ref, reason))

    for ref, note in resolved:
        print(f"  OK    {ref}: {note}")
    for ref, matched, _ in to_add:
        print(f"  ADD   {ref}  <- copy SSO from '{matched}'")
    for ref, reason in unresolved:
        print(f"  MISS  {ref}: no unambiguous match ({reason})")

    # optional org hint for the misses
    if args.org and unresolved:
        accts = org_accounts()
        if accts:
            print("\n  [org] account-id hints for unmatched names "
                  "(you must still add sso_role_name/session manually):")
            for ref, _ in unresolved:
                hit = accts.get(ref.lower()) or next(
                    (aid for nm, aid in accts.items() if ref.lower() in nm), None)
                print(f"    {ref}: {hit or '(no matching org account name)'}")

    if not to_add:
        print("\nNothing to add.", file=sys.stderr)
        return

    blocks = ("\n# --- added by aws_profile_aliaser.py on "
              f"{datetime.now():%Y-%m-%d %H:%M} ---\n"
              + "\n".join(render_block(ref, f) for ref, _, f in to_add))
    print("\n" + "="*60 + "\nBlocks to append:\n" + "="*60)
    print(blocks)

    if not args.apply:
        print("(dry run -- re-run with --apply to write these to "
              f"{args.aws_config})", file=sys.stderr)
        return

    backup = f"{cfg_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(cfg_path, backup)
    with open(cfg_path, "a") as fh:
        fh.write(blocks if blocks.startswith("\n") else "\n" + blocks)
    print(f"appended {len(to_add)} profile(s) to {cfg_path}  (backup: {backup})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
