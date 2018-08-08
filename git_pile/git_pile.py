#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1+

import argparse

try:
    import argcomplete
except ImportError:
    pass

from .helpers import run_wrapper

# external commands
git = run_wrapper('git', capture=True)


class Config:
    def __init__(self):
        self.dir = ""
        self.branch = ""
        self.remote_branch = ""
        self.tracking_branch = ""

        s = git(["config", "--get-regex", "pile\\.*"]).stdout.strip()
        for kv in s.split('\n'):
            key, value = kv.strip().split()
            # pile.*
            key = key[5:].replace('-', '_')
            setattr(self, key, value)

    def is_valid(self):
        return self.dir != '' and self.branch != ''

def cmd_init(args):
    # TODO: check if already initialized
    # TODO: check if arguments make sense
    git("config pile.dir %s" % args.dir)
    git("config pile.branch %s" % args.branch)
    git("config pile.tracking-branch %s" % args.tracking_branch)
    if args.remote_branch:
        git("config pile.remote-branch=%s" % args.remote_branch)

    config = Config()

    # TODO: remove prints
    print("dir=%s\nbranch=%s\nremote-branch=%s\ntracking-branch=%s\n" %
          (config.dir, config.branch, config.remote_branch, config.tracking_branch))
    print("is-valid=%s" % config.is_valid())

    return 0


def parse_args(cmd_args):
    parser = argparse.ArgumentParser(
        description="Manage a pile of patches on top of git branches")
    subparsers = parser.add_subparsers(title="Commands", dest="command")

    # init
    parser_init = subparsers.add_parser('init', help="Initialize configuration of git-pile in this repository")
    parser_init.add_argument(
        "-d", "--dir",
        help="Directory in which to place patches (default: %(default)s)",
        metavar="DIR",
        default="pile")
    parser_init.add_argument(
        "-b", "--branch",
        help="Branch name to use for patches (default: %(default)s)",
        metavar="BRANCH",
        default="pile")
    parser_init.add_argument(
        "-r", "--remote-branch",
        help="Remote branch to which patches will be pushed (default: empty - configure it later with `git config pile.remote`)",
        metavar="REMOTE",
        default="")
    parser_init.add_argument(
        "-t", "--tracking-branch",
        help="Base remote or local branch on top of which the patches from BRANCH should be applied (default: %(default)s)",
        metavar="TRACKING_BRANCH",
        default="master")
    parser_init.set_defaults(func=cmd_init)

    try:
        argcomplete.autocomplete(parser)
    except NameError:
        pass

    args = parser.parse_args(cmd_args)
    if not hasattr(args, "func"):
        parser.print_help()
        return None

    return args


def main(*cmd_args):
    args = parse_args(cmd_args)
    if not args:
        return 1

    return args.func(args)
