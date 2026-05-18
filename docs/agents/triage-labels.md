# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in GitHub Issues.

| Label in skills | Label in GitHub | Meaning |
|-----------------|-----------------|---------|
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate this issue |
| `needs-info` | `needs-info` | Waiting on reporter for more information |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for an AFK agent |
| `ready-for-human` | `ready-for-human` | Requires human implementation |
| `wontfix` | `wontfix` | Will not be actioned |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Creating labels

If these labels don't exist yet in your GitHub repo, create them:

```bash
gh label create "needs-triage" --color "F2994A" --description "Maintainer needs to evaluate"
gh label create "needs-info" --color "F2C94C" --description "Waiting on reporter"
gh label create "ready-for-agent" --color "27AE60" --description "Fully specified, AFK-ready"
gh label create "ready-for-human" --color "2D9CDB" --description "Requires human implementation"
gh label create "wontfix" --color "828282" --description "Will not be actioned"
```
