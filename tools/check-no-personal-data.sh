#!/usr/bin/env bash
# Guard against personal data — the release plan's QUA-2 requirement.
#
# Fails when operational data from the original installation reappears: an agent
# name, a machine name, a home path, a private IP, a personal address, or a
# forgotten backup file. It looks in file CONTENT and in file NAMES, and — with
# --git — in the committed tree as well.
#
# Why a guard rather than care: one `rsync` pulled in 27 `.bak-*` files carrying
# the whole agent nomenclature, and a manual cleanup had left 109 occurrences
# behind in the code. What is checked by hand comes back.
#
# WARNING: this is the SECOND version. The first was GREEN on a repository still
# full of residue, and an audit proved eleven ways around it in a single pass: no
# case-insensitivity (a capitalised name walked through), no prefix handling
# (`_<name>` walked through), an allowlist applied to the whole LINE and so
# usable as an escape hatch, incomplete IP ranges, and file names and the git
# tree never looked at. A green guard on a dirty repository is worse than no
# guard: it authorises publication. Every hardening below comes from a real
# bypass, not from an imagined precaution.
#
# Usage: tools/check-no-personal-data.sh [--git] [directory]
#   --git : ALSO check the content of HEAD, the committed tree, not just the
#           working directory — anonymising without committing leaves HEAD
#           intact, and HEAD is what gets published.
# Exit: 0 when clean, 1 otherwise.
set -uo pipefail

CHECK_GIT=0
GIT_REF="HEAD"
# --git [ref]: WHICH REVISION is about to be published. HEAD by default. Saying
# so matters: the log used to be read with `--all`, so a perfectly clean release
# branch failed on commit messages living in old local branches — a red you learn
# to ignore, which is exactly what a guard must never produce. Other refs are
# reported separately, as a warning.
if [[ "${1:-}" == "--git" ]]; then
  CHECK_GIT=1
  shift
  if [[ $# -gt 0 && "$1" != -* && -d "$1" ]]; then
    :   # that is the directory, not a ref
  elif [[ $# -gt 0 && "$1" != -* ]]; then
    GIT_REF="$1"
    shift
  fi
fi
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" || exit 1

# ── Installation-specific patterns: LOADED FROM A LOCAL FILE.
#
# They used to be hardcoded here. An audit pointed out the paradox: this script
# carried the full roster (27 names) and the history of the installation, so
# published as it stood, THE GUARD WAS THE LEAK. They now live in a gitignored
# file.
#
# When that file is missing the guard does NOT stay quiet: without it, only the
# generic patterns (IPs, paths, e-mails) are checked, and it has to say so — a
# tick that never looked at the names would be a false green, which is precisely
# what let the first version through.
PATTERNS_FILE="${PERSONAL_DATA_PATTERNS:-$(dirname "${BASH_SOURCE[0]}")/.personal-data-patterns}"
LOCAL_PATTERNS=""
if [ -r "$PATTERNS_FILE" ]; then
  while IFS= read -r l; do
    [ -z "$l" ] && continue
    case "$l" in \#*) continue ;; esac
    LOCAL_PATTERNS="${LOCAL_PATTERNS:+$LOCAL_PATTERNS|}$l"
  done < "$PATTERNS_FILE"
fi

# The AMBIGUOUS patterns — old identifiers that are also ordinary words — live in
# that local file too, with their word boundaries written into them: leaving them
# here would have meant publishing four former agent names, which is exactly the
# leak the file exists to fix.

PATTERNS=()
[ -n "$LOCAL_PATTERNS" ] && PATTERNS+=("$LOCAL_PATTERNS")
PATTERNS+=(
  '/home/[a-z][a-z0-9_-]*/'                        # a hardcoded home path
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b'           # RFC1918
  '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\bfd[0-9a-f]{2}:[0-9a-f:]+\b'                   # IPv6 ULA
  '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}'    # any e-mail address
)

# ── The allowlist is ANCHORED on the match, not on the line.
# v1 filtered out any LINE containing a tolerated pattern: putting `/home/you/`
# on the same line as real data was enough to make that data disappear.
ALLOW_MATCH='^(/home/(you|user|x|example|operator)/|100\.100\.100\.100'
ALLOW_MATCH+='|10\.0\.0\.[0-9]{1,3}'
ALLOW_MATCH+='|[a-z0-9._%+-]+@(example\.[a-z]+|remote\.example|users\.noreply\.github\.com|[a-z-]+\.iam\.gserviceaccount\.com))$'

FAIL=0
# The guard excludes itself AND its pattern file: the latter CONTAINS, by
# construction, everything it searches for. It is gitignored, so never published.
SELF='tools/(check-no-personal-data\.sh|\.personal-data-patterns)'

# Where a hit is, separately from what it says. Both scanners emit
# "<location>:<line number>:<content>", with location being "./a/b.sh" from the
# tree scan and "HEAD:a/b.sh" from git grep. Cutting at the first ":<digits>:"
# gives the location in either form.
_hit_location() {
  printf '%s' "$1" | sed -E 's/:[0-9]+:.*$//'
}

judge() {  # reads "file:line:content" lines, keeps only the real offences
  local pat="$1" line loc m
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    loc=$(_hit_location "$line")
    # The guard excludes ITSELF — matched on the LOCATION, never on the whole
    # line. Searching the whole line meant that merely naming this script inside
    # a line made that line invisible: `# see tools/check-no-personal-data.sh`
    # next to a real address hid the address, e-mails and IPs included. That is
    # an escape hatch anyone could write by accident.
    printf '%s' "$loc" | grep -qE "$SELF" && continue
    # The copyright line of the ROOT LICENSE is the ONE place the rights holder's
    # name belongs. Three things narrow this tolerance, each because the wider
    # version was tried and probed:
    #
    #  * only the root file — "./LICENSE" or "<ref>:LICENSE". Matching LICENSE at
    #    any depth let a docs/x/LICENSE borrow the tolerance;
    #  * only its "Copyright YYYY" line;
    #  * only for the local NAME patterns. Skipping the whole line spared an
    #    e-mail sitting on it too — the very escape hatch judged below. E-mails,
    #    IPs and home paths come from other patterns, which get no tolerance
    #    here, so they are still reported on that line.
    if [ -n "${LOCAL_PATTERNS:-}" ] && [ "$pat" = "$LOCAL_PATTERNS" ] \
       && printf '%s' "$loc" | grep -qE '^((\./)?|[^:]+:)LICENSE$'; then
      printf '%s' "$line" | grep -qE ':[0-9]+: *Copyright [0-9]{4} ' && continue
    fi
    # EVERY match on the line is judged, not just the first one. With `head -1`,
    # a line holding a tolerated match followed by a real one was dropped whole:
    # `/home/you/` then `/home/bobby/`, or `a@example.com` then a personal
    # address, both passed. That is the same defect an audit had already found in
    # the first version — the allowlist usable as an escape hatch — reintroduced
    # in another shape. A line is reported when ANY of its matches is not allowed.
    while IFS= read -r m; do
      [ -z "$m" ] && continue
      if ! printf '%s' "$m" | grep -qiE "$ALLOW_MATCH"; then
        printf '%s\n' "$line"
        break
      fi
    done < <(printf '%s' "$line" | grep -ioE "$pat")
  done
}

scan_tree() {
  local pat hits
  for pat in "${PATTERNS[@]}"; do
    hits=$(grep -rInIiE --exclude-dir=.git --exclude-dir=__pycache__ \
                --exclude-dir=node_modules --exclude-dir=.venv \
                --exclude=".personal-data-patterns" \
                -- "$pat" . 2>/dev/null | judge "$pat")
    if [ -n "$hits" ]; then
      echo "✗ [working tree] ${pat:0:60}…" >&2
      printf '%s\n' "$hits" | head -12 >&2
      FAIL=1
    fi
  done
}

scan_head() {
  local ref="${1:-HEAD}" pat hits
  for pat in "${PATTERNS[@]}"; do
    hits=$(git grep -InIiE -- "$pat" "$ref" 2>/dev/null | judge "$pat")
    if [ -n "$hits" ]; then
      echo "✗ [$ref, committed] ${pat:0:60}…" >&2
      printf '%s\n' "$hits" | head -12 >&2
      FAIL=1
    fi
  done
}

scan_tree

# FILE NAMES — the data can be in the name alone: a test file named after a
# facade, a backup suffixed with an agent's name. The real examples are NOT
# written here: this script is published, and an audit noted that citing them
# amounts to publishing what it protects — the guard skips itself while scanning,
# so it would never have flagged its own examples.
for pat in ${LOCAL_PATTERNS:+"$LOCAL_PATTERNS"}; do
  if names=$(find . -path ./.git -prune -o -type f -print 2>/dev/null \
             | grep -iE "$pat" | grep -vE "$SELF"); then
    echo "✗ [file names] ${pat:0:60}…" >&2
    printf '%s\n' "$names" | head -12 >&2
    FAIL=1
  fi
done

# The committed tree: THAT is what gets published, not the working directory.
if [ "$CHECK_GIT" -eq 1 ] && git rev-parse --git-dir >/dev/null 2>&1; then
  scan_head "$GIT_REF"
fi

# GIT METADATA — a blind spot: the guard read file CONTENT and never the commit
# messages, although those are published with the repository. A branch can be
# perfectly anonymised and still describe the whole fleet in its log.
if [ "$CHECK_GIT" -eq 1 ] && git rev-parse --git-dir >/dev/null 2>&1 && [ -n "$LOCAL_PATTERNS" ]; then
  # The commit ADDRESS, checked separately from the name patterns: it escaped
  # this guard once more than it should have. A repository anonymised everywhere,
  # log included, had been committed with the author's personal e-mail — because
  # git takes the global identity when the repository sets none, and that field
  # is neither in the content nor in the message. The only accepted e-mail is a
  # no-reply address from the forge.
  authors=$(git log "$GIT_REF" --format="%ae%n%ce" 2>/dev/null | sort -u \
            | grep -viE "^[^@]+@users\.noreply\.github\.com$|^[^@]+@noreply\..+$" || true)
  if [ -n "$authors" ]; then
    echo "✗ [git identity] a personal address in the author or committer of $GIT_REF:" >&2
    printf '%s\n' "$authors" | head -8 >&2
    echo "  → git config user.email <id>+<login>@users.noreply.github.com, then" >&2
    echo "    git commit --amend --reset-author (or filter-repo --mailmap over history)." >&2
    FAIL=1
  fi

  # The MESSAGE only — deliberately not %an. The author's name legitimately
  # belongs to the repository's owner, and the identity is already checked just
  # above, by e-mail. Including %an here made every commit of a correctly
  # attributed repository look like a leak, which is how a guard teaches people
  # to ignore it.
  meta=$(git log "$GIT_REF" --format="%H %s%n%b" 2>/dev/null | grep -inIE "$LOCAL_PATTERNS" | head -12)
  if [ -n "$meta" ]; then
    echo "✗ [commit messages] an identifier appears in the log of $GIT_REF" >&2
    printf '%s\n' "$meta" >&2
    echo "  -> only a history rewrite removes them (git filter-repo --message-callback)." >&2
    FAIL=1
  fi

  # The OTHER refs do not travel with an ordinary push, but they do travel with
  # `push --mirror` or a copy of .git. Warn, never block: deciding what gets
  # published is not this script's call.
  others=$(git for-each-ref --format='%(refname)' refs/heads refs/tags refs/stash 2>/dev/null \
           | while read -r ref; do
               [ "$(git rev-parse "$ref" 2>/dev/null)" = "$(git rev-parse "$GIT_REF" 2>/dev/null)" ] && continue
               if git log "$ref" --format="%H %s%n%b" 2>/dev/null | grep -qiE "$LOCAL_PATTERNS"; then
                 echo "$ref"
               fi
             done)
  if [ -n "$others" ]; then
    echo "⚠ other refs contain identifiers (outside $GIT_REF):" >&2
    printf '%s\n' "$others" | head -8 >&2
    echo "  An ordinary push does not send them; push --mirror or a copy of .git does." >&2
  fi
fi

# Backup files: they carry the history of the naming.
if bak=$(find . -path ./.git -prune -o \( -name '*.bak' -o -name '*.bak-*' -o -name '*.orig' \) -print 2>/dev/null | grep .); then
  echo "✗ backup files present:" >&2
  printf '%s\n' "$bak" | head -12 >&2
  FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
  suffix=""; [ "$CHECK_GIT" -eq 1 ] && suffix=" (working tree + HEAD)"
  if [ -z "$LOCAL_PATTERNS" ]; then
    echo "⚠ local patterns missing ($PATTERNS_FILE): the NAMES were NOT checked," >&2
    echo "  only IPs, paths and e-mails. This is a PARTIAL check." >&2
    exit 2
  fi
  echo "✓ no personal data detected$suffix"
else
  echo "" >&2
  echo "Make the value configurable instead of hardcoding it. If the match is" >&2
  echo "legitimate, add it to ALLOW_MATCH — which judges the MATCH, not the line." >&2
fi
exit "$FAIL"
