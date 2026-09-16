# ccfleet

Own-account Claude Code fleet manager: **one owner, one account, one node.**

ccfleet helps a small group of people each run the unmodified Claude Code CLI on
their own hosted node, signed in with their own Claude subscription, and gives
the operator a read-only view of the fleet. It never proxies model traffic and
never touches credentials.

Status: scaffold. Code, docs and the guidebook land in the first pull requests.

## Continuous integration

The GitHub Actions workflow lives at `deploy/ci/github-ci.yml`. Pushing files under
`.github/workflows/` needs a token with the `workflow` scope, so enable it once from
a machine where that scope is granted:

```bash
gh auth refresh -h github.com -s workflow
mkdir -p .github/workflows && git mv deploy/ci/github-ci.yml .github/workflows/ci.yml
git commit -m "ci: enable GitHub Actions" && git push
```
