#!/usr/bin/env python3
"""Ship commands and code across an SSH (→WSL) boundary without quoting hell.

Two frictions surfaced during the first cross-host autonomous run (addup-1):

1. **Quoting fragility.** Inlining a shell one-liner through
   ``ssh host 'wsl -d Ubuntu -- bash -lc "..."'`` breaks the moment the payload
   contains quotes, ``$vars`` or parens — local shell, ssh, the Windows login
   shell, WSL and bash each re-interpret them, and several commands silently
   came back empty (``$f`` expanded away, ``cut -d" "`` mangled). The robust fix
   is to **never inline**: base64-encode the whole script. The base64 alphabet
   (``A-Za-z0-9+/=``) carries no shell-special characters, so the blob survives
   any number of quoting layers untouched; the far side decodes and runs it.

2. **Remote reviewers have no checkout.** An agent on a phone that only mirrors
   docs had no copy of the repo to audit — the core source had to be
   pasted inline by hand. The fix is a ``git bundle``: a single file carrying
   the requested refs *with history*, which the reviewer clones/fetches offline,
   shipped over the same base64 channel when there's no shared filesystem.

Everything here returns **argv lists** (never shell strings) so the local shell
layer is removed entirely — pass them straight to ``subprocess.run(...)`` without
``shell=True``. Stdlib only; nothing here runs SSH itself, so it is fully unit
testable by decoding/executing the payload locally.
"""
from __future__ import annotations

import base64
import shlex


def encode_script(script: str) -> str:
    """base64-encode a script to a single transport-safe line (no newlines)."""
    return base64.b64encode(script.encode("utf-8")).decode("ascii")


def decode_script(blob: str) -> str:
    """Inverse of :func:`encode_script`."""
    return base64.b64decode(blob.encode("ascii")).decode("utf-8")


def _decode_and_run(blob: str, interpreter: str = "bash") -> str:
    """The far-side command that decodes ``blob`` and feeds it to ``interpreter``.

    Contains only the base64 blob and fixed, quote-free tokens, so it is safe to
    embed inside any further quoting layer (``bash -lc "..."``, WSL, etc.)."""
    return f"printf %s {blob} | base64 -d | {interpreter}"


def remote_script_command(host, script, *, wsl_distro=None, interpreter="bash",
                          ssh_opts=None) -> list[str]:
    """Build an argv that runs ``script`` on ``host`` over SSH, base64-wrapped.

    ``wsl_distro`` routes the script through ``wsl -d <distro> -- bash -lc`` for a
    Windows host whose real shell lives in WSL (the Asus case). Returned as an
    argv list — pass it to ``subprocess.run(cmd, ...)`` *without* ``shell=True``,
    which is what eliminates the local quoting layer. Because the payload is
    base64, no layer past this one can corrupt it."""
    blob = encode_script(script)
    inner = _decode_and_run(blob, interpreter)
    if wsl_distro:
        # One quoting layer remains (the WSL login shell parses bash -lc's arg),
        # but `inner` is quote-free base64 + pipes, so a plain double-quote holds.
        remote = f'wsl -d {wsl_distro} -- bash -lc "{inner}"'
    else:
        remote = inner
    return ["ssh", *(ssh_opts or []), host, remote]


def git_bundle_command(repo, out, refs=("HEAD",)) -> list[str]:
    """argv to pack ``refs`` of ``repo`` (with history) into a single bundle file.

    The reviewer then ``git clone <bundle>`` or ``git fetch <bundle> <ref>`` —
    a complete offline checkout over any transport, no live access to the repo."""
    return ["git", "-C", str(repo), "bundle", "create", str(out), *refs]


def git_bundle_verify_command(bundle, repo=None) -> list[str]:
    """argv to verify a received bundle is intact and self-contained.

    ``git bundle verify`` needs a repository context (it checks the bundle's
    prerequisites against one); without ``-C`` it depends on the caller's cwd and
    fails outside a repo. Pass ``repo`` (the reviewer's target checkout) to make
    it cwd-independent."""
    prefix = ["git", "-C", str(repo)] if repo is not None else ["git"]
    return [*prefix, "bundle", "verify", str(bundle)]


def pack_file_command(path) -> list[str]:
    """argv that emits a file as one base64 line on stdout (binary-safe transport
    over SSH when there is no shared filesystem — e.g. shipping a bundle or a
    build artifact to a remote host). Decode the captured stdout with :func:`unpack_file`."""
    return ["base64", "-w0", str(path)]


def unpack_file(blob: str, dest) -> int:
    """Write base64 ``blob`` (as captured from :func:`pack_file_command`) to
    ``dest``. Returns the number of bytes written."""
    from pathlib import Path
    data = base64.b64decode(blob.encode("ascii"))
    Path(dest).write_bytes(data)
    return len(data)


def quote(arg: str) -> str:
    """POSIX-quote a single argument for the rare case a string command is
    unavoidable. Prefer the argv builders above; reach for this only at the
    very last layer you control."""
    return shlex.quote(arg)


def _main(argv=None) -> int:
    """Minimal CLI so the coordinator can drive these from the shell::

        # run a local script on a remote host, quote-safe (reads from stdin/-f)
        python3 -m autonomy.remote exec user@remote-host --wsl Ubuntu -f build.sh
        # pack a review bundle for a remote reviewer
        python3 -m autonomy.remote bundle --repo . -o /tmp/review.bundle main
    """
    import argparse
    import subprocess
    import sys

    p = argparse.ArgumentParser(prog="autonomy.remote", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("exec", help="run a script on a remote host, base64-wrapped")
    pe.add_argument("host")
    pe.add_argument("-f", "--file", help="script file (default: read stdin)")
    pe.add_argument("--wsl", metavar="DISTRO", help="route through WSL distro")
    pe.add_argument("--interpreter", default="bash")
    pe.add_argument("-o", "--ssh-opt", action="append", default=[],
                    help="extra ssh option, repeatable; use the =form for dash-leading "
                         "values, e.g. --ssh-opt=-p --ssh-opt=8022 to reach a phone-hosted agent on :8022")
    pe.add_argument("--print", action="store_true", help="print the argv instead of running it")

    pb = sub.add_parser("bundle", help="pack a git bundle for a remote reviewer")
    pb.add_argument("refs", nargs="*", default=["HEAD"], help="refs to include (default HEAD)")
    pb.add_argument("--repo", default=".")
    pb.add_argument("-o", "--output", required=True)
    pb.add_argument("--print", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "exec":
        script = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        cmd = remote_script_command(args.host, script, wsl_distro=args.wsl,
                                    interpreter=args.interpreter, ssh_opts=args.ssh_opt)
    else:
        cmd = git_bundle_command(args.repo, args.output, tuple(args.refs))
    if getattr(args, "print", False):
        print(" ".join(shlex.quote(c) for c in cmd))
        return 0
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(_main())
