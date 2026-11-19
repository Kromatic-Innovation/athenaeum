# Store declaration

A store declares who may do what with it in one file in its pages repository,
`_store/access.yaml`. This page is the format. The file is read by
`athenaeum.store_access`, which returns one of three results:

- the parsed declaration, when the file is valid;
- `None`, when the surface has no `_store/access.yaml`;
- an invalid result that names every offending key, when the file is present but
  cannot be trusted.

**The file fails closed.** A declaration that is present but cannot be parsed or
validated grants an install nothing beyond read, and is reported. The parser never
raises on bad content, never fills in a default for a key that is present and
wrong, and never returns a half-filled declaration: an invalid result carries no
roles and no rights.

## Example

```yaml
version: 1
store_id: st_q7m2xk4vwz9d
directory: static
groups:
  leadership:
    - {iss: "https://git.example.net", sub: "1042"}
    - {iss: "https://git.example.net", sub: "1077"}
  department-heads:
    - {iss: "https://git.example.net", sub: "1203"}
roles:
  staff:
    rights: [read]
    members: ["*"]
  contributor:
    rights: [propose]
    members: ["group:department-heads"]
  editor:
    rights: [read, propose, compile, answer]
    members: ["group:leadership"]
sync:
  mode: integrate
  always_ask: [hr-record]
locations:
  raw: "git@git.example.net:acme/announcements-raw.git"
  bench: "git@git.example.net:acme/announcements-bench.git"
reviews: []
```

## Keys

| Key | Required | Meaning |
|---|---|---|
| `version` | yes | Must be `1`. |
| `store_id` | yes | The one identifier of the store: `st_` followed by 12 lowercase base32 characters (`0-9` and `a-z` without `i`, `l`, `o`, `u`). Opaque; never a name, URL or path. |
| `directory` | no | The directory adapter that resolves group names. Default `static`. |
| `groups` | no | A group name mapped to a list of principal blocks. Read as data only. |
| `roles` | no | A role name you choose, mapped to `{rights, members}`. |
| `sync.mode` | no | `fast_forward` (default) or `integrate`. |
| `sync.always_ask` | no | A list of page type names. Default empty. |
| `locations.bench` | no | A non-empty string. |
| `locations.raw` | no | One string, or a list of `{for, at}` entries (below). |
| `reviews` | no | A list of `{name, source, every, criteria}`. Checked for shape only. |

### Principals

A principal block is `{iss, sub}`, both non-empty strings. Two blocks name the
same principal when they have the same identity key (the same rule the rest of
athenaeum uses for an asserter), whatever order their keys are written in.

### Roles and rights

A role's `rights` is a non-empty list drawn from `read`, `propose`, `compile` and
`answer`. A role that holds `compile` or `answer` must also hold `read`. Its
`members` is a list of principal blocks and `group:<name>` strings. A member of a
role that holds `read` may instead be written `{principal: ..., purpose: review}`.

`"*"` (anyone the host admits) is valid only among the members of a role whose
rights are exactly `[read]`.

The role names are yours. Nothing reads a role name except to print it. A
principal's rights are the union of the rights of every role that lists them.

### `locations.raw`

Two shapes:

```yaml
# one location shared by every proposer
locations:
  raw: "git@git.example.net:acme/announcements-raw.git"
```

```yaml
# one location per proposer
locations:
  raw:
    - for: {iss: "https://git.example.net", sub: "1203"}
      at: "git@git.example.net:acme/announcements-raw-1203.git"
    - for: {iss: "https://git.example.net", sub: "1318"}
      at: "git@git.example.net:acme/announcements-raw-1318.git"
```

In the list form each `for` is one principal block, never a group and never `"*"`,
`at` is a non-empty string, the same principal may not appear twice, and an empty
list is invalid. Only the shape is checked here.

## What makes a declaration invalid

Every one of these is reported with the offending key, and the store is treated
as read-only:

- malformed YAML, or a top level that is not a mapping;
- a `version` other than `1`;
- a missing or malformed `store_id`;
- an unknown key anywhere, including at the top level (`sync_mod`, a bare
  `sync_mode`, `grants`, `compile`) and under `locations` (`inbox`);
- a right outside `read`, `propose`, `compile`, `answer`, or an empty `rights`;
- `compile` or `answer` without `read`;
- `"*"` in a role that holds anything but `read` alone;
- a principal block without `iss` or `sub`;
- a `locations.raw` list that is empty, names a group, `"*"` or a principal twice.

## Versioning

New top-level keys arrive only with a higher `version`. An install that does not
support a declaration's `version` treats it as invalid and the store as
read-only, and a version-1 file that carries a key version 1 does not define is
invalid for the same reason.

## Not indexed

Nothing under a directory whose name starts with `_` is indexed as a page, on any
surface. `_store/` holds the declaration and review criteria
(`_store/reviews/<name>.md`), which are configuration and are never recalled as
pages.
