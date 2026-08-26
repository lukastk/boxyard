# The 44 diverged boxmetas: which fix, and why

Written 2026-08-26, in reply to the analysis from the `feat/write-owner-claim`
thread. The diagnosis there is right and matches what this side found
independently while diffing `list --view groups` on the real yard: all 44 boxes
have the same shape — macbook added exactly one group (`archived` ×38,
`dormant` ×6) and changed nothing else, the remote gained `write_owner` and
changed nothing else, parents identical on both sides.

Two changes were proposed. They are not equally good, and the cheaper one is
the one that looks more expensive.

## 1. Give `write_owner` its own file and sync record — NO

The argument for it is real: ownership bookkeeping bumps META's sync record for
every box it touches, so a machine holding an unpushed metadata edit is
converted from "will push cleanly" into a hard conflict. That is exactly what
happened.

But the cost is recurring and paid by every box on every pass, to avoid an
event that has happened once. A fourth per-box synced artefact is one more
remote round trip per box per pass. **This repo has already hit that wall**, and
the comment recording it is still in `multi_sync.go`:

> Fetch the tombstones ONCE, not once per box. Asked per box that is one SFTP
> connection each — 587 per pass, per machine, every 20 minutes, which
> saturated the storage box's connection limit and was failing ~8 boxes per
> pass on three machines.

586 boxes × 5 machines × every 20 minutes is the same arithmetic. Trading a
standing per-pass cost for protection against a one-time claim sweep is the
wrong direction.

It is also narrow. It decouples ownership from `groups`/`parents`, but two
machines editing `groups` still deadlock in precisely the same way — it fixes
one pair of fields, not the class of problem. And it adds states: an ownership
file can now be present or absent independently of the boxmeta, which every
`may_push`, `owner` and `doctor` path has to handle, plus a migration across
586 boxes × 5 machines during which both layouts must be readable.

The sender says (2) "would make (1) much less load-bearing". Agreed — and once
(2) exists, (1) buys nothing that justifies it.

## 2. Three-way merge of META — YES, and it is the CHEAP one

There is no merge base today. `SyncRecord` carries `ulid`, `timestamp`,
`sync_complete` and `syncer_hostname` — no content, no hash. So this needs a
base to be added.

**The base does not need to go on the remote.** Keep the boxmeta as-synced next
to the LOCAL sync record (`~/.boxyard/sync_records/…`). That is a ~200-byte
local file write per META sync and **zero** extra remote operations. This is
why (2) is cheaper than (1), not more expensive: (1) costs network per box per
pass forever; (2) costs a local write on a sync that is already happening.

The merge semantics are well defined per field, which is what makes this
tractable rather than a heuristic:

| field | type | rule |
|---|---|---|
| `groups`, `parents` | set | base ∪ (local adds) ∪ (remote adds) − (local removes) − (remote removes) |
| `write_owner` | scalar | one-sided change wins; both changed to DIFFERENT values is a genuine conflict |
| `name`, `storage_location` | scalar | same rule |
| `unknown_keys` | opaque | preserved, as already |

This does not weaken the "refuse rather than pick a winner" principle, and it
is worth being precise about why: a three-way merge is not picking a winner. It
is applying BOTH edits, which is the correct answer when they touch disjoint
fields or are set-additions. Where the two sides genuinely contradict — both
set `write_owner` to different machines — it still refuses, loudly, exactly as
now. All 44 boxes would have resolved with no human involved; a real
double-claim still would not.

## 3. `add-to-group` defaulting `--sync-after` to False

The sender lists this as secondary. It is sharper than that, and the defect is
not the default in isolation — it is the ASYMMETRY:

* `claim_box` **always** pushes META. It has to; ownership nobody can see is
  not ownership.
* `add-to-group`, `remove-from-group`, `set-parent`, `remove-parent` **never**
  push unless asked (`--sync-after`, default False).

So one command's mandatory push races another's optional one, and the loser is
always the metadata edit. That is the collision, stated exactly.

Flipping the default would work but makes every group edit a network
operation. The better answer falls out of (2) for free: once the as-synced base
exists, `doctor` can compare the local boxmeta against it and report
**unpushed-meta-edit** — "this box's metadata differs from the last copy that
was synced, and no push has happened since". That catches the situation
BEFORE it becomes a conflict, on the machine that caused it, and it is
ambient in the tool the fleet already runs. It is strictly better than making
the edit slower.

## The constraint, preserved

META is deliberately not ownership-gated: `03_sync_box.pct.py` routes it
through plain `sync_helper` and decides ownership only AFTER the META sync,
before CONF/DATA. Nothing above changes that — the merge happens inside the
META sync, which a non-owner still performs. That property is what let the
repair be done from macbook for boxes mymain owns, and it is what lets
`groups`/`parents` be edited from a non-owner at all.

## Recommendation

Do (2) with a local base, then add the `unpushed-meta-edit` doctor check on top
of it. Skip (1). Leave `--sync-after` alone.

Not implemented here. It changes how sync resolves conflicts across 586 boxes
on 5 machines of live data, which is Lukas's call rather than a port task, and
by the workflow this project runs on it belongs in the Python first — with the
merge differentialled against a generated space of (base, local, remote)
triples before it goes anywhere near a real yard.
