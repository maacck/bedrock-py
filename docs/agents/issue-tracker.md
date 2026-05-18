# Issue Tracker

This repo uses **GitHub Issues** for issue tracking.

## Setup

The `gh` CLI must be available on PATH:

```bash
gh --version
```

If not installed: https://cli.github.com/

## Issue Commands

### Discovery

```bash
# List open issues
gh issue list

# List issues with specific label
gh issue list --label "bug"

# Search issues by title/body
gh issue list --search "keyword"

# View issue details
gh issue view <ISSUE-NUMBER>
```

### Issue Lifecycle

```bash
# Create a new issue
gh issue create --title "Title" --body "Description"

# Create issue from file (for markdown content)
gh issue create --title "Title" --body-file /tmp/description.md

# Close an issue
gh issue close <ISSUE-NUMBER>

# Reopen an issue
gh issue reopen <ISSUE-NUMBER>

# Edit an issue
gh issue edit <ISSUE-NUMBER> --title "New Title"
```

### Comments

```bash
# Add a comment
gh issue comment <ISSUE-NUMBER> --body "Comment text"

# Add comment from file (for markdown content)
gh issue comment <ISSUE-NUMBER> --body-file /tmp/comment.md

# List comments
gh issue view <ISSUE-NUMBER> --comments
```

### Labels

```bash
# List all labels
gh label list

# Create a label
gh label create "label-name" --color "HEXCOLOR" --description "Description"

# Add label to issue
gh issue edit <ISSUE-NUMBER> --add-label "label-name"

# Remove label from issue
gh issue edit <ISSUE-NUMBER> --remove-label "label-name"
```

## Commit Format

When committing code related to a GitHub issue, use conventional commits with the issue number:

```
<type>(<scope>): <description> (#<ISSUE-NUMBER>)
```

Examples:
```bash
git commit -m "feat(auth): add JWT token refresh (#123)"
git commit -m "fix(api): resolve null pointer in user lookup (#456)"
git commit -m "refactor(db): optimize query performance (#789)"
```

GitHub automatically links commits to issues when it sees `(#123)` format.

## Skills that use this

- `to-issues` — breaks plans into issues
- `triage` — issue state machine
- `to-prd` — publishes PRDs as issues
