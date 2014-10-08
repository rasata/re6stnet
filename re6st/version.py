import subprocess as _S
from os.path import dirname as _d
_d = _d(__file__)

def _git_call(*args):
    return _S.call(("git",) + args, cwd=_d)

def _git_output(*args):
    p = _S.Popen(("git",) + args, cwd=_d, stdout=_S.PIPE, stderr=_S.PIPE)
    out, err = p.communicate()
    if p.returncode:
        raise _S.CalledProcessError(p.returncode, "git", err)
    return out.strip()

_git_call("update-index", "-q", "--refresh")
dirty = _git_call("diff-index", "--quiet", "HEAD", "--")
if dirty not in (0, 1):
    raise _S.CalledProcessError(dirty, "git")

revision = int(_git_output("rev-list", "--topo-order", "--count", "HEAD"))
short = _git_output("rev-parse", "--short", "HEAD")
version = "0-%s.g%s" % (revision, short)

if dirty:
    version += ".dirty"

if __name__ == "__main__":
    print version