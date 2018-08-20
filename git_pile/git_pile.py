#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1+

import argparse
import os
import os.path as op
import shutil
import subprocess
import sys
import tempfile

from contextlib import contextmanager
from time import strftime

try:
    import argcomplete
except ImportError:
    pass

from .helpers import run_wrapper
from .helpers import parse_raw_diff


# external commands
git = run_wrapper('git', capture=True)

nul_f = open(os.devnull, 'w')


def fatal(s):
    print("fatal: %s" % s, file=sys.stderr)
    sys.exit(1)

def error(s):
    print("error: %s" % s, file=sys.stderr)


class Config:
    def __init__(self):
        self.dir = ""
        self.result_branch = ""
        self.pile_branch = ""
        self.base_branch = ""

        s = git(["config", "--get-regex", "pile\\.*"]).stdout.strip()
        for kv in s.split('\n'):
            key, value = kv.strip().split()
            # pile.*
            key = key[5:].replace('-', '_')
            setattr(self, key, value)

    def is_valid(self):
        return self.dir != '' and self.result_branch != '' and self.pile_branch != ''

    def check_is_valid(self):
        if not self.is_valid():
            error("git-pile configuration is not valid. Configure it first with git-pile init")
            return False

        return True


def git_branch_exists(branch):
    return git("show-ref --verify --quiet refs/heads/%s" % branch, check=False).returncode == 0


def git_root():
    return git("rev-parse --show-toplevel").stdout.strip("\n")


def git_worktree_get_checkout_path(root, branch):
    state = dict()
    out = git("-C %s worktree list --porcelain" % root).stdout.split("\n")

    for l in out:
        if not l:
            # end block
            if state.get("branch", None) == "refs/heads/" + branch:
                return state["worktree"]

            state = dict()
            continue

        v = l.split(" ")
        state[v[0]] = v[1] if len(v) > 1 else None


def update_baseline(d, commit):
    with open(op.join(d, "config"), "w") as f:
        rev = git("rev-parse %s" % commit).stdout.strip()
        f.write("BASELINE=%s" % rev)


# Create a temporary directory to checkout a detached branch with git-worktree
# making sure it gets deleted (both the directory and from git-worktree) when
# we finished using it.
#
# To be used in `with` context handling.
@contextmanager
def temporary_worktree(commit, dir=git_root(), prefix=".git-pile-worktree"):
    try:
        with tempfile.TemporaryDirectory(dir=dir, prefix=prefix) as d:
            git("worktree add --detach --checkout %s %s" % (d, commit),
                stdout=nul_f, stderr=nul_f)
            yield d
    finally:
        git("worktree remove %s" % d)


def cmd_init(args):
    # TODO: check if already initialized
    # TODO: check if arguments make sense
    git("config pile.dir %s" % args.dir)
    git("config pile.pile-branch %s" % args.pile_branch)
    git("config pile.base-branch %s" % args.base_branch)
    git("config pile.result-branch %s" % args.result_branch)

    config = Config()

    # TODO: remove prints
    print("dir=%s\npile-branch=%s\nbase-branch=%s\nresult-branch=%s" %
          (config.dir, config.pile_branch, config.base_branch,
           config.result_branch))
    print("is-valid=%s" % config.is_valid())

    if not git_branch_exists(config.pile_branch):
        # Create and checkout an orphan branch named `config.pile_branch` at the
        # `config.dir` location. Unfortunately git-branch can't do that;
        # git-checkout has a --orphan option, but that would necessarily
        # checkout the branch and the user would be left wondering what
        # happened if any command here on fails.
        #
        # Workaround is to do that ourselves with a temporary repository
        with tempfile.TemporaryDirectory() as d:
            git("-C %s init" % d)
            update_baseline(d, config.base_branch)
            git("-C %s add -A" % d)
            git(["-C", d, "commit", "-m", "Initial git-pile configuration"])

            # Temporary repository created, now let's fetch and create our branch
            git("fetch %s master:%s" % (d, config.pile_branch), stdout=nul_f, stderr=nul_f)
            git("worktree add --checkout %s %s" % (config.dir, config.pile_branch),
                stdout=nul_f, stderr=nul_f)

    return 0


def fix_duplicate_patch_name(dest, path, max_retries):
    fn = op.basename(path)
    l = len(fn)
    n = 1

    while op.exists(op.join(dest, fn)):
        n += 1
        # arbitrary number of retries
        if n > max_retries:
            raise Exception("wat!?! %s (max_retries=%d)" % (path, max_retries))
        fn = "%s-%d.patch" % (fn[:l - 6], n)

    return op.join(dest, fn)


def rm_patches(dest):
    with os.scandir(dest) as it:
        for entry in it:
            if not entry.is_file() or not entry.name.endswith(".patch"):
                continue

            try:
                os.remove(entry.path)
            except PermissionError:
                fatal("Could not remove %s: permission denied" % entry.path)


def has_patches(dest):
    try:
        with os.scandir(dest) as it:
            for entry in it:
                if entry.is_file() and entry.name.endswith(".patch"):
                    it.close()
                    return True
    except FileNotFoundError:
        pass

    return False


def parse_commit_range(commit_range, default_begin, default_end):
    if not commit_range:
        return default_begin, default_end

    # sanity checks
    try:
        base, result = commit_range.split("..")
        git("rev-parse %s" % base, stderr=nul_f, stdout=nul_f)
        git("rev-parse %s" % result, stderr=nul_f, stdout=nul_f)
    except (ValueError, subprocess.CalledProcessError) as e:
        fatal("Invalid commit range: %s" % commit_range)

    return base, result


def get_series_linenum_dict(d):
    series = dict()
    with open(op.join(d, "series"), "r") as f:
        linenumber = 0
        for l in f:
            series[l[:-1]] = linenumber
            linenumber += 1

    return series


# pre-existent patches are removed, all patches written from commit_range,
# "config" and "series" overwritten with new valid content
def genpatches(output, base_commit, result_commit):
    # Do's and don'ts to generate patches to be used as an "always evolving
    # series":
    #
    # 1) Do not add `git --version` to the signature to avoid changing every patches when regenerating
    #    from different machines
    # 2) Do not number the patches on the subject to avoid polluting the diff when patches are reordered,
    #    or new patches enter in the middle
    # 3) Do not number the files: numbers will change when patches are added/removed
    # 4) To avoid filename clashes due to (3), check for each patch if a file
    #    already exists and workaround it

    commit_range = "%s..%s" % (base_commit, result_commit)
    commit_list = git("rev-list --reverse %s" % commit_range).stdout.strip().split('\n')
    if not commit_list:
        fatal("No commits in range %s" % commit_range)

    # Do everything in a temporary directory and once we know it went ok, move
    # to the final destination - we can use os.rename() since we are creating
    # the directory and using a subdir as staging
    with tempfile.TemporaryDirectory() as d:
        staging = op.join(d, "staging")
        series = []
        for c in commit_list:
            path_orig = git(["format-patch", "--zero-commit", "--signature=", "-o", staging, "-N", "-1", c]).stdout.strip()
            path = fix_duplicate_patch_name(d, path_orig, len(commit_list))
            os.rename(path_orig, path)
            series.append(path)

        os.rmdir(staging)

        os.makedirs(output, exist_ok=True)
        rm_patches(output)

        for p in series:
            s = shutil.copy(p, output)

    with open(op.join(output, "series"), "w") as f:
        f.write("# Auto-generated by git-pile\n\n")
        for s in series:
            f.write(op.basename(s))
            f.write("\n")

    update_baseline(output, base_commit)

    return 0


def gen_cover_letter(diff, output, n_patches, baseline_commit):
    user = git("config --get user.name").stdout.strip()
    email = git("config --get user.email").stdout.strip()
    # RFC 2822-compliant date format
    now = strftime("%a, %d %b %Y %T %z")
    baseline = git("-C %s rev-parse %s" % (git_root(), baseline_commit)).stdout.strip()

    cover = op.join(output, "0000-cover-letter.patch")
    with open(cover, "w") as f:
        f.write("""From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: {user} <{email}>
Date: {date}
Subject: [PATCH 0/{n_patches}] *** SUBJECT HERE ***

*** BLURB HERE ***

---
Changes below are based on current pile tree with BASELINE={baseline}

""".format(user=user, email=email, date=now, n_patches=n_patches, baseline=baseline))
        for l in diff:
            f.write(l)

    return cover

def cmd_genpatches(args):
    config = Config()
    if not config.check_is_valid():
        return 1

    base, result = parse_commit_range(args.commit_range, config.base_branch,
                                      config.result_branch)

    # Be a little careful here: the user might have passed e.g. /tmp: we
    # don't want to remove patches there to avoid surprises
    if args.output_directory != "":
        output = args.output_directory
        if has_patches(output) and not args.force:
            fatal("'%s' is not default output directory and has patches in it.\n"
                  "Force with --force or pass an empty/non-existent directory" % output)
    else:
        output = config.dir

    return genpatches(output, base, result)


def cmd_format_patch(args):
    config = Config()
    if not config.check_is_valid():
        return 1

    # Allow the user to name the topic/feature branch as they please so
    # default to base_branch..HEAD
    base, result = parse_commit_range(args.commit_range, config.base_branch, "HEAD")

    with temporary_worktree(config.pile_branch) as tmpdir:
        ret = genpatches(tmpdir, base, result)
        if ret != 0:
            return 1

        git("-C %s add -A" % tmpdir)


        with subprocess.Popen(["git", "-C", tmpdir, "diff", "--cached", "-p", "--raw" ],
                              stdout=subprocess.PIPE, universal_newlines=True) as proc:
            # get a list of (state, new_name) tuples
            changed_files = parse_raw_diff(proc.stdout)
            # and finish reading all the diff
            diff = proc.stdout.readlines()
            if not diff:
                fatal("Nothing changed from %s..%s to %s..%s"
                      % (config.base_branch, config.result_branch, base, result))
       
        patches = []
        for action, fn in changed_files:
            # deleted files will appear in the series file diff, we can skip them here
            if action == 'D':
                continue

            # only collect the patch files
            if not fn.endswith(".patch"):
                continue

            if action not in "RACMT":
                fatal("Unknown state in diff '%s' for file '%s'" % (action, fn))

            patches.append(fn)

        series = get_series_linenum_dict(tmpdir)
        patches.sort(key=lambda p: series.get(p, 0))

        # From here on, use the real directory
        output = args.output_directory
        os.makedirs(output, exist_ok=True)
        rm_patches(output)

        cover = gen_cover_letter(diff, output, len(patches), config.base_branch)
        print(cover)

        for i, p in enumerate(patches):
            old = op.join(tmpdir, p)
            new = op.join(output, "%04d-%s" % (i + 1, p[5:]))
            shutil.copy(old, new)
            print(new)

    return 0

def cmd_genbranch(args):
    config = Config()
    if not config.check_is_valid():
        return 1

    baseline = None
    branch = args.branch if args.branch else config.result_branch
    root = git_root()

    with open(op.join(config.dir, "config"), "r") as f:
        for l in f:
            if l.startswith("BASELINE="):
                baseline = l[9:].strip()
                break

    new_baseline = git("-C %s rev-parse %s" % (root, config.base_branch)).stdout.strip()
    if baseline != new_baseline:
        print("Applying patches from different baseline %s (saved) to %s (%s)" %
              (baseline, new_baseline, config.base_branch),
              file=sys.stderr)

    patches = []
    with open(op.join(config.dir, "series"), "r") as f:
        for l in f:
            l = l.strip()
            if not l or l.startswith("#"):
                continue

            p = op.join(config.dir, l)
            if not op.isfile(p):
                fatal("series file reference '%s', but it doesn't exist" % p)

            patches.append(p)

    # work in a separate directory to avoid cluttering whatever the user is doing
    # on the main one
    with temporary_worktree(config.base_branch) as d:
        for p in patches:
            if args.verbose:
                print(p)
            git("-C %s am %s" % (d, op.join(root, p)))

        # always save HEAD to PILE_RESULT_HEAD
        shutil.copyfile(op.join(root, ".git", "worktrees", op.basename(d), "HEAD"),
                        op.join(root, ".git", "PILE_RESULT_HEAD"))

        path = git_worktree_get_checkout_path(root, branch)
        if path:
            error("final result is PILE_RESULT_HEAD but branch '%s' could not be updated to it "
                  "because it is checked out at '%s'" % (branch, path))
            return 1

        git("-C %s checkout -B %s HEAD" % (d, branch), stdout=nul_f, stderr=nul_f)

    return 0


# Temporary command to help with development
def cmd_destroy(args):
    config = Config()

    git_ = run_wrapper('git', capture=True, check=False, print_error_as_ignored=True)
    if config.dir:
        git_("worktree remove --force %s" % config.dir)
    git_("branch -D %s" % config.pile_branch)

    # implode
    git_("config --remove-section pile")


def parse_args(cmd_args):
    desc = """Manage a pile of patches on top of a git branch

git-pile helps to manage a long running and always changing list of patches on
top of git branch. It is similar to quilt, but aims to retain the git work flow
exporting the final result as a branch.

There are 3 important branches to understand how to use git-pile:

    BASE_BRANCH: where patches will be applied on top of.
    RESULT_BRANCH: the result of applying the patches on BASE_BRANCH
    PILE_BRANCH: where to keep the patches and track their history

This is a typical scenario git-pile is used in which BASE_BRANCH is "master"
and RESULT_BRANCH is "internal".

A---B---C master
         \\
          X---Y---Z internal

PILE_BRANCH is a branch containing this file hierarchy based on the above
example:

series  config  X.patch  Y.patch  Z.patch

The "series" and "config" file are there to allow git-pile to do its job
and are retained for compatibility with quilt and qf. git-pile exposes
commands to convert between RESULT_BRANCH and the files on PILE_BRANCH.
This allows to save the history of the patches when BASE_BRANCH changes
or patches are added, modified or removed on RESULT_BRANCH. Below is an
example in which the BASE_BRANCH evolves adding more commits:

A---B---C---D---E master
         \\
          X---Y---Z internal

After a rebase of the RESULT_BRANCH we will have the following state, in
which X', Y' and Z' denote the rebased patches. They may have possibly
changed to solve conflicts or to apply cleanly:

A---B---C---D---E master
                 \\
                  X'---Y'---Z' internal

In turn, PILE_BRANCH will store the saved result:

series  config  X'.patch  Y'.patch  Z'.patch
"""

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=desc)

    subparsers = parser.add_subparsers(title="Commands", dest="command")

    # init
    parser_init = subparsers.add_parser('init', help="Initialize configuration of git-pile in this repository")
    parser_init.add_argument(
        "-d", "--dir",
        help="Directory in which to place patches (default: %(default)s)",
        metavar="DIR",
        default="pile")
    parser_init.add_argument(
        "-p", "--pile-branch",
        help="Branch name to use for patches (default: %(default)s)",
        metavar="PILE_BRANCH",
        default="pile")
    parser_init.add_argument(
        "-b", "--base-branch",
        help="Base remote or local branch on top of which the patches from PILE_BRANCH should be applied (default: %(default)s)",
        metavar="BASE_BRANCH",
        default="master")
    parser_init.add_argument(
        "-r", "--result-branch",
        help="Branch to be created when applying patches from PILE_BRANCH on top of BASE_BRANCH (default: %(default)s",
        metavar="RESULT_BRANCH",
        default="internal")
    parser_init.set_defaults(func=cmd_init)

    # genpatches
    parser_genpatches = subparsers.add_parser('genpatches', help="Generate patches from BASE_BRANCH..RESULT_BRANCH and save to output directory")
    parser_genpatches.add_argument(
        "-o", "--output-directory",
        help="Use OUTPUT_DIR to store the resulting files instead of the DIR from the configuration. This must be an empty/non-existent directory unless -f/--force is also used",
        metavar="OUTPUT_DIR",
        default="")
    parser_genpatches.add_argument(
        "-f", "--force",
        help="Force use of OUTPUT_DIR even if it has patches. The existent patches will be removed.",
        action="store_true",
        default=False)
    parser_genpatches.add_argument(
        "commit_range",
        help="Commit range to use for the generated patches (default: BASE_BRANCH..RESULT_BRANCH)",
        metavar="COMMIT_RANGE",
        nargs="?",
        default="")
    parser_genpatches.set_defaults(func=cmd_genpatches)

    # genbranch
    parser_genbranch = subparsers.add_parser('genbranch', help="Generate RESULT_BRANCH by applying patches from PILE_BRANCH on top of BASE_BRANCH")
    parser_genbranch.add_argument(
        "-b", "--branch",
        help="Use BRANCH to store the final result instead of RESULT_BRANCH",
        metavar="BRANCH",
        default="")
    parser_genbranch.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False)
    parser_genbranch.set_defaults(func=cmd_genbranch)

    # format-patch
    parser_format_patch = subparsers.add_parser('format-patch', help="Generate patches from BASE_BRANCH..HEAD and save patch series to output directory to be shared on a mailing list")
    parser_format_patch.add_argument(
        "-o", "--output-directory",
        help="Use OUTPUT_DIR to store the resulting files instead of the CWD. This must be an empty/non-existent directory unless -f/--force is also used",
        metavar="OUTPUT_DIR",
        default="")
    parser_format_patch.add_argument(
        "-f", "--force",
        help="Force use of OUTPUT_DIR even if it has patches. The existent patches will be removed.",
        action="store_true",
        default=False)
    parser_format_patch.add_argument(
        "commit_range",
        help="Commit range to use for the generated patches (default: BASE_BRANCH..HEAD)",
        metavar="COMMIT_RANGE",
        nargs="?",
        default="")
    parser_format_patch.set_defaults(func=cmd_format_patch)

    # destroy
    parser_destroy = subparsers.add_parser('destroy', help="Destroy all git-pile on this repo")
    parser_destroy.set_defaults(func=cmd_destroy)

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
