# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply or remove labels**: `gh issue edit <number> --add-label "..."` or `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`. `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: yes.** _(Set to `no` if this repo does not treat external PRs as feature requests. `/triage` reads this flag.)_

PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`, then keep only `authorAssociation` values of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE`. Drop `OWNER`, `MEMBER`, and `COLLABORATOR`.
- **Comment, label, or close**: `gh pr comment`, `gh pr edit --add-label` or `--remove-label`, and `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either. Resolve it with `gh pr view 42`, then fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

The `/wayfinder` skill uses a single issue as a map and child issues as tickets.

- **Map**: Create one issue with the `wayfinder:map` label. Its body contains Notes, Decisions-so-far, and Fog. Use `gh issue create --label wayfinder:map`.
- **Child ticket**: Link an issue to the map as a GitHub sub-issue with `gh api` on the sub-issues endpoint. If sub-issues are not enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Use a `wayfinder:<type>` label, where type is `research`, `prototype`, `grilling`, or `task`. Assign the ticket to the driving developer after it is claimed.
- **Blocking**: Use GitHub native issue dependencies as the canonical, visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`. The `<blocker-db-id>` value is the blocker's numeric database ID from `gh api repos/<owner>/<repo>/issues/<n> --jq .id`. It is not the issue number or `node_id`. GitHub reports open blockers in `issue_dependencies_summary.blocked_by`. If dependencies are unavailable, add `Blocked by: #<n>, #<n>` at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: List the map's open children with `gh issue list --state open`, scoped to the map's sub-issues or task list. Drop issues that have an open blocker or an assignee. The first issue in map order wins.
- **Claim**: Run `gh issue edit <n> --add-assignee @me`. This is the session's first write.
- **Resolve**: Comment on the issue with the answer, close it, then append a context pointer and link to the map's Decisions-so-far section.
