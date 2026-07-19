# Pending Merges

## [2026-06-01] Merge: "alice-pm"
- [ ] Approve this merge? Sources: user_alice_a.md, user_alice_b.md
**Rationale**: same person described in two auto-memories
**Sources**:
- /k/user/user_alice_a.md
- /k/user/user_alice_b.md
**Confidence**: 0.92
**Draft**:
```markdown
## From `user/user_alice_a.md`

Alice is the PM on the platform team.

To reproduce the flaky test run:

```
npm run test:flaky
```

## From `user/user_alice_b.md`

Alice owns the ingest pipeline roadmap.
```

---

## From `user/user_alice_b.md`

Alice owns the ingest pipeline roadmap.

---

## [2026-06-02] Merge: "bob-eng"
- [ ] Approve this merge? Sources: user_bob_a.md, user_bob_b.md
**Rationale**: duplicate notes about the same engineer
**Sources**:
- /k/user/user_bob_a.md
- /k/user/user_bob_b.md
**Confidence**: 0.81
**Draft**:
```markdown
## From `user/user_bob_a.md`

Bob is a staff engineer.

Example config:

```
export FOO=bar
```

## From `user/user_bob_b.md`

Bob mentors the backend guild.
```

---

## From `user/user_bob_b.md`

Bob mentors the backend guild.

---

## From `user/user_carol_orphan.md`

Wholly orphaned fragment left behind after an earlier merge was archived; it
carries no checkbox and can never resolve, so it accreted forever and flooded
one "malformed header" WARNING per run (issue #394 / #299 / #303 regression).
