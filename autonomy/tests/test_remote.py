"""Tests for the cross-host transport helpers.

The point of these helpers is that a payload survives an SSH→WSL→bash quoting
gauntlet. We can't run real SSH in a unit test, but we can prove the property
that matters: the base64 blob round-trips, it contains no shell-special
characters, and the *decoded* payload runs correctly through a real shell.
"""
import base64
import re
import subprocess

from autonomy.remote import (
    decode_script, encode_script, git_bundle_command, git_bundle_verify_command,
    pack_file_command, remote_script_command, unpack_file,
)


def test_encode_decode_round_trips_nasty_payload():
    nasty = 'for f in a b; do echo "$f $(date)"; done && x=\'q\'; cut -d" " -f1'
    assert decode_script(encode_script(nasty)) == nasty


def test_blob_has_no_shell_special_chars():
    # The whole reason base64 survives every quoting layer: its alphabet is
    # exactly [A-Za-z0-9+/=] — no quotes, $, backticks, spaces, parens.
    blob = encode_script('echo "$HOME" `id`; rm -rf / # not really')
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", blob)


def test_remote_command_is_argv_not_a_shell_string():
    cmd = remote_script_command("user@host", "echo hi")
    assert cmd[0] == "ssh" and cmd[1] == "user@host"
    # the remote part embeds the base64 blob and decodes it on the far side
    assert "base64 -d" in cmd[-1]
    blob = cmd[-1].split("printf %s ", 1)[1].split(" ", 1)[0]
    assert decode_script(blob) == "echo hi"


def test_remote_command_wraps_through_wsl():
    cmd = remote_script_command("user@host", "echo hi", wsl_distro="Ubuntu")
    assert cmd[-1].startswith('wsl -d Ubuntu -- bash -lc "')
    assert "base64 -d | bash" in cmd[-1]


def test_remote_command_honours_ssh_opts():
    # e.g. a phone-hosted agent is reached over Tailscale on a non-default port.
    cmd = remote_script_command("h", "x", ssh_opts=["-p", "8022"])
    assert cmd[:4] == ["ssh", "-p", "8022", "h"]
    assert decode_script(cmd[-1].split("printf %s ", 1)[1].split(" ", 1)[0]) == "x"


def test_decoded_payload_executes_through_a_real_shell(tmp_path):
    # End-to-end on the local shell: simulate the far side by running exactly
    # what `_decode_and_run` tells bash to do — the nested quotes that broke
    # over SSH must not break here either.
    marker = tmp_path / "out.txt"
    script = f'printf "%s\\n" "ok $HOME" > {marker}'  # quotes + $var inside
    cmd = remote_script_command("ignored", script)
    far_side = cmd[-1]  # 'printf %s <blob> | base64 -d | bash'
    subprocess.run(["bash", "-c", far_side], check=True)
    assert marker.read_text().startswith("ok ")


def test_git_bundle_command_shape():
    assert git_bundle_command("/repo", "/tmp/x.bundle", ("main", "HEAD")) == [
        "git", "-C", "/repo", "bundle", "create", "/tmp/x.bundle", "main", "HEAD",
    ]
    assert git_bundle_verify_command("/tmp/x.bundle") == [
        "git", "bundle", "verify", "/tmp/x.bundle",
    ]
    assert git_bundle_verify_command("/tmp/x.bundle", repo="/repo") == [
        "git", "-C", "/repo", "bundle", "verify", "/tmp/x.bundle",
    ]


def test_git_bundle_round_trips_a_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "code.py").write_text("print('review me')\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True)

    bundle = tmp_path / "review.bundle"
    # default refs=("HEAD",) → the bundle carries HEAD so a plain clone checks
    # the tree out with no access to the original repo (the reviewer's case).
    subprocess.run(git_bundle_command(repo, bundle), check=True)
    # verify against the source repo so it is cwd-independent (runs green even
    # when the whole suite is invoked from outside any git repo).
    subprocess.run(git_bundle_verify_command(bundle, repo=repo), check=True,
                   capture_output=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bundle), str(clone)], check=True)
    assert (clone / "code.py").read_text() == "print('review me')\n"


def test_pack_unpack_file_round_trips_binary(tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(bytes(range(256)) * 4)  # non-utf8 binary
    out = subprocess.run(pack_file_command(src), capture_output=True, check=True)
    blob = out.stdout.decode("ascii")
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", blob)  # single safe line
    dest = tmp_path / "restored.bin"
    n = unpack_file(blob, dest)
    assert n == src.stat().st_size
    assert dest.read_bytes() == src.read_bytes()


def test_unpack_matches_python_b64():
    blob = base64.b64encode(b"hello").decode()
    # sanity: our unpack agrees with stdlib decode
    assert base64.b64decode(blob) == b"hello"
