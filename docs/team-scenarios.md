<!-- SPDX-License-Identifier: Apache-2.0 -->

# Team deployment scenarios

**Status:** PURPOSE DOCUMENT, companion to [`north-star.md`](north-star.md) and
[`use-cases.md`](use-cases.md). Written 2026-11-28 (athenaeum#3276) from the
maintainer rulings of 2026-11-27. It states the two multi-person deployments
that the organisation use cases (use-cases §3) describe, so that a design for
access, identity or federation has something to be checked against. It fixes
the target, not the design. Every name in it is invented.

## Scenario one: the company

Lanternworks is a 15-person company.

| Group | People | Notes |
|---|---|---|
| Executives | a CEO and two VPs | Share strategy documents |
| Product | 5 | Code and product decisions |
| Marketing | 4 | Campaigns, positioning |
| HR and operations | 2 | One HR representative holds personnel records |
| Automated writers | a calendar importer, a CI bot | Not people; they write on a schedule |

Each person uses one or more assistants (for example Claude on a laptop, ChatGPT on a phone, an IDE assistant at work) and expects the same memory from each.

## The stores

| Store | Who reads | Who writes | Who approves what is published |
|---|---|---|---|
| Strategy | executives | executives | executives |
| Company handbook (policies, procedures, SOPs) | everyone | executives and HR | executives and HR |
| Product | product; marketing read-only | product | product leads |
| Marketing | everyone | marketing | marketing leads |
| HR records | HR; executives by need | HR | HR |
| Personal (one per person) | that person | that person | that person |

"Writes" means the store accepts that person's notes as intake. "Approves" means answering the store's decision queue: merges, contradictions, corrections. Anyone who can read a store may *propose* a change to it; only writers' proposals are filed without review.

Nobody chooses a store when saving a note. The librarian files each note in the store it belongs to, among the stores its writer may write to; when the right store is one the writer cannot write to, the note becomes a proposal to that store.

The table is this company's configuration, not a built-in model. Which groups exist, what each may do, and whether executives can open personal stores are all for the deployment to set.

## What the scenario requires

1. **Who can read and write what, where** is answerable from one place, and enforced there.
2. **A person is one identity** across every assistant and device they use. A person may have several agents; each agent is its own identity acting on that person's behalf. The assistant and the device are recorded as well, because they affect how much the record can be trusted.
3. **Every raw intake item records its source**: which person or automated writer, through which assistant, on which device, into which store, and whether that was verified or merely declared. Three things stay distinguishable for ever: what a person stated, what an agent inferred on that person's behalf, and what someone else said.
4. **Nobody plants data** in a store they cannot write to, and nobody can pass off an assistant's guess as something a person said.
5. **Identity proof is pluggable**: the company's identity provider where there is one, a local list where there is not.
6. **Policies layer, as the deployment configures.** A team follows the handbook unless its own policy says otherwise, and whether that is a contradiction is the deployment's call.
7. **Contradictions are found across stores.** HR's store says an employee is American; a colleague's notes say British. Whoever can read both sees one question, raised once, even though the two facts live in different stores. (Both may be true. The point is that it is asked.)
8. **A store may restrict which assistants read it**, because a provider's terms or a model's quality may rule it out for some data.

## Scenario two: independent consultants on a shared project

Four independent consultants work together on one client engagement. Each runs their own installation on their own laptop, with their own private store, their own assistants and their own bills. There is no company, no server and no shared identity provider.

They share one project store. All four read and write it. Nothing else is shared: no one can see another's private store, and nobody is in charge.

| Store | Who reads | Who writes | Who approves |
|---|---|---|---|
| Each consultant's own | that consultant | that consultant | that consultant |
| The shared project | all four | all four | any of them, or as they agree |

What this scenario adds:

- **No central enforcement.** Access to the shared store is whatever the place that holds it enforces (a shared repository, a shared folder, a bucket). An installation cannot stop a peer who has write access to that place; it can only check what arrives.
- **Identity without a provider.** Each consultant is known to the others by an identity they exchanged directly, such as a key, not by a sign-in service.
- **Checking on arrival matters more than gatekeeping.** What a peer contributed is marked as that peer's, at whatever level of proof came with it, and is checked like any other intake.
- **Leaving is clean.** When the engagement ends, each consultant keeps their own store and their copy of the project store; nobody's private notes were ever in it.

## Where the scenario needs sharpening

These are the places where the first version of the scenario was too simple. Each is drawn from how existing systems had to handle the same problem.

### "Executives can access everything" should not, by default, include personal stores

Workspace administrators in most office suites *can* open any user's files, and every such product ended up adding audit logs and a separate, explicit takeover step, because routine access destroyed trust in the personal space. A personal store that the CEO can read is one nobody will put anything candid in. The default: executives read every *company* store; a personal store is readable only by its owner, with a break-glass grant that is always logged. This is configurable. A small company whose staff work on company laptops, with no expectation of privacy there, may reasonably open personal stores to its executives, and the deployment says so.

HR records need the same care in the other direction. Personnel systems restrict even senior managers to need-to-know. "Executives by need" above means a named grant, not membership of the executive group.

### Two kinds of company rule: defaults and mandates

"Teams follow the handbook but their own policies override it" is right for most rules and wrong for some. Directory and device-management systems settled on the same answer decades ago: a setting inherits downward, a narrower scope may override it, and an administrator may mark a setting as enforced so that it cannot be overridden. Lanternworks' handbook says "use the shared task tracker for task tracking"; the product team's policy says "code issues live in the code host". That is a default with a narrower override, and recall for a product engineer should return the team policy, with the handbook rule visible as the general case. The handbook also says "customer personal data never goes into a public store". No team policy may override that, and one that tries is a contradiction for the handbook's owners to resolve.

That is one common arrangement, and the engine must be able to express it. It is not built in. The deployment defines who has the final word on what (a CEO over the vacation policy, a team lead over the team's tooling), what order sources take when they disagree, and therefore what counts as a contradiction at all. Scope (company, team, person) and strength (default or mandate) are the vocabulary; the rules are the deployment's.

### Access is enforced where the data is served, or not at all

If every member holds a full copy of a store, every member can read all of it and edit any of it, whatever a configuration file says. That is true of any storage medium, not only a code host. Systems that share files solve this with permissions on the place the data lives, not with a file inside it. A team deployment therefore has two honest topologies:

- **A served deployment.** One server holds the stores; people connect through their assistants and sign in. The server enforces every read and write. This is the reference shape for scenario one.
- **Separately held stores.** Each store lives in its own place (a repository, a bucket, a shared folder) and that place's permissions are the access control: no access to the place, no access to the store. This is scenario two, and an accepted way to run scenario one.

Both are legitimate. What is not legitimate is a rights file inside a shared folder being described as access control.

### Finding a contradiction needs someone who can see both sides

Requirement 7 has a consequence. If HR's store is closed to marketing and a marketer's personal store is closed to everyone, no person can see both facts. The contradiction is found only by an identity with read access to both stores: in practice a company-level librarian, which is an automated writer with its own grants. What it may read is the deployment's choice, and that choice decides which contradictions can ever be found. A deployment that keeps personal stores private from it accepts that a contradiction hiding there surfaces only when its owner shares the fact.

### Read-only for a whole store is coarser than it sounds

"Marketing reads the product store" is fine until the product store holds an unannounced security fix. Issue trackers handle this with restricted projects, not per-item rules. The cheap answer here is the same: put what marketing should not see in a second store. Routing a page type or a field to a restricted location covers the cases a second store does not.

### Which assistant is used is part of the access question

Reading the HR store through a consumer chat assistant sends that content to the assistant's provider. Identity platforms already express this as conditional access: the same person is allowed or refused depending on the application and device. A store should be able to say which assistants may read it.

The assistant also limits the evidence. An assistant with a local hook can produce a signed record that a person typed a prompt. A hosted chat assistant that connects over the network cannot. The source record must therefore carry an assurance level, and "declared, not verified" has to be a normal, first-class value rather than a failure.

### Automated writers are identities too

A calendar importer or CI bot writes more intake than any person. Platforms that started with only human accounts all later added service accounts and app identities, because shared human credentials made attribution impossible. Each automated writer gets its own identity, its own grants and an owner who answers for it.

### People join and leave

Revoking a leaver's access must not remove what they contributed or rewrite who said it: provenance outlives membership. Their personal store is theirs to export, and is then removed. Groups should come from the identity provider where one exists, so that leaving the company is a single action.

### Each store needs someone to answer its questions

A store with writers and no approver accumulates unanswered merges and contradictions. Each store names who answers its decision queue. A contradiction between two stores goes to someone with authority over both, once, not to both teams.

## What this scenario deliberately leaves out

- Hundreds of users, nested departments, or delegated administration.
- Per-page or per-paragraph permissions. Stores and routed locations are the unit.
- Deciding what an assistant may *do* with what it reads. Consumers own their policies.
