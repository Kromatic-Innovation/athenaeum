<!-- SPDX-License-Identifier: Apache-2.0 -->

# Method rejections — approaches considered and declined

**Status:** DESIGN NOTE. Issue athenaeum#2809, operator ruling of 2026-11-20.
These three rejections used to sit in [`docs/north-star.md`](../north-star.md)
§3. The operator ruled them method rather than vision: they say how the engine
reaches the north star, not what it is for, so they can change without
amending the purpose document. Each entry's reasoning is carried over
unchanged. The north star's §3 keeps the vision-level rejections and links
here.

If you want to reverse one of these, that is a design discussion on this note,
not an amendment to the north star. Weigh it against the north star's three
failure directions (§1) as any design change is.

- **Per-source parsers or typed interfaces in code.** A handler per producer
  forks the seam: N interfaces to keep in sync and per-user wiki structure
  hardcoded into a shipped library. Recurring shapes get rules (north star
  §2.7).
- **An LLM as the shape detector.** Deterministic fingerprint matching is
  free and sufficient for machine-emitted shapes; a model in the match path
  reintroduces per-file cost exactly where the design removes it — and puts
  untrusted content in charge of routing.
- **A separate short-term memory store.** Duration is frontmatter in the one
  wiki (north star §2.5); a second store forks the read seam.
