"""Git operations run by the CLI. List-form subprocess only (never a shell); never push, fetch, rebase, force or delete."""
import subprocess
from pathlib import Path


class GitError(Exception):
    def __init__(self, args, code, output):
        super().__init__(f"git {' '.join(args)} failed ({code}): {output.strip()[-600:]}")
        self.code, self.output = code, output


def git(repo, *args, timeout=60, check=True):
    """Run git -C <repo> <args>. Returns (exit code, stdout+stderr). Raises GitError on failure when check is true."""
    try:
        process = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise GitError(args, 127, "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise GitError(args, 124, f"timed out after {timeout} s")
    output = (process.stdout or "") + (process.stderr or "")
    if check and process.returncode != 0:
        raise GitError(args, process.returncode, output)
    return process.returncode, output.strip()


def toplevel(folder):
    return Path(git(folder, "rev-parse", "--show-toplevel")[1].splitlines()[-1]).resolve()


def rev(repo, ref):
    """The commit hash for ref, or None if it does not exist."""
    code, out = git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    return out.splitlines()[-1] if code == 0 and out else None


def dirty(path):
    """Uncommitted changes (porcelain lines), empty if clean."""
    return git(path, "status", "--porcelain")[1]


def current_branch(repo):
    return git(repo, "branch", "--show-current")[1]


def is_ancestor(repo, commit, ref):
    return git(repo, "merge-base", "--is-ancestor", commit, ref, check=False)[0] == 0


def commits_between(repo, older, newer):
    out = git(repo, "rev-list", f"{older}..{newer}")[1]
    return [x for x in out.splitlines() if x.strip()]


def add_worktree(repo, path, branch, start_ref):
    """Create the attempt worktree on a new branch. If the folder exists already, leave it alone."""
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if rev(repo, "refs/heads/" + branch):
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), start_ref)
    return True


def merge(repo, commit, message):
    """Merge exactly this commit into the checked-out branch; abort on conflict. Returns the merge commit."""
    code, out = git(repo, "merge", "--no-ff", "--no-edit", "-m", message, commit, check=False, timeout=300)
    if code != 0:
        git(repo, "merge", "--abort", check=False)
        raise GitError(("merge", commit), code, out)
    return rev(repo, "HEAD")


def remove_worktree(repo, path):
    git(repo, "worktree", "remove", str(path))
