#!/usr/bin/env python3
"""Stand-in for ``ssh`` in the transfer tests: the "remote" is a local folder (FAKE_SSH_HOME).

Master mode (``-o ControlMaster=yes ... -fN dest``) asks SSH_ASKPASS for the password like OpenSSH
and compares it with FAKE_SSH_PASSWORD; it creates the ControlPath file. Commands then require that
file (as multiplexed sessions do) and run with bash in FAKE_SSH_HOME. ``-O exit`` removes it.
"""
import os
import subprocess
import sys


def main(argv):
    opts, i, ctl, master, exit_cmd = {}, 0, None, False, False
    while i < len(argv) and argv[i].startswith("-"):
        a = argv[i]
        if a in ("-p", "-o", "-O"):
            v = argv[i + 1]
            if a == "-o":
                k, _, val = v.partition("=")
                opts[k] = val
                if k == "ControlPath":
                    ctl = val
                if k == "ControlMaster" and val == "yes":
                    master = True
            if a == "-O" and v == "exit":
                exit_cmd = True
            i += 2
        else:
            i += 1  # -fN and similar flags
    dest, command = argv[i], (argv[i + 1] if i + 1 < len(argv) else None)
    if exit_cmd:
        if ctl and os.path.exists(ctl):
            os.remove(ctl)
        return 0
    if master:
        if os.environ.get("SSH_ASKPASS_REQUIRE") != "force":
            print("askpass not forced", file=sys.stderr)
            return 255
        pw = subprocess.run([os.environ["SSH_ASKPASS"], f"{dest}'s password: "], capture_output=True, text=True).stdout.rstrip("\n")
        if pw != os.environ.get("FAKE_SSH_PASSWORD"):
            print(f"{dest}: Permission denied (publickey,password).", file=sys.stderr)
            return 255
        open(ctl, "w").close()
        return 0
    if not ctl or not os.path.exists(ctl):
        print("Control socket connect: No such file or directory", file=sys.stderr)
        return 255
    home = os.environ["FAKE_SSH_HOME"]
    return subprocess.run(["bash", "-c", command], cwd=home, env=dict(os.environ, HOME=home, LC_ALL="C")).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
