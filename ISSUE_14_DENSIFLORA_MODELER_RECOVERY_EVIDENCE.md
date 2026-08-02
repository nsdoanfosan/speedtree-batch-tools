# Issue #14 Densiflora final read-only recovery audit

The user reported completing the remaining Modeler work manually. A single
read-only audit then captured each current production `_03` through `_06` SPM
exactly once with the repository's immutable stat/read/stat snapshot routine.
The audit ran from 2026-08-02 17:37:57 through 17:45:45 KST. It did not use
Modeler, UI Automation, key input, raw XML/SPM editing, or production writes.

## Final production snapshot and acceptance

| asset | raw SHA-256 | mtime (KST) | generators (current/table) | total nodes | stale | orphan owners/nodes | core / membership / target continuity | result |
| --- | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| `SK_tree_densiflora_03.spm` | `9818626404c5c57913e68ca60449204f11063c7cde5598abdd634fe9975510ba` | 2026-08-02 12:51:12 | 48 / 42 | 9,526 | false | 0 / 0 | true / true / true | **accepted** |
| `SK_tree_densiflora_04.spm` | `19845b0e9881d4d113bf0e8e76fdaf51ef7700bea07c4d0de314089da21df439` | 2026-08-02 14:38:54 | 64 / 53 | 6,344 | false | 0 / 0 | true / true / true | **rejected**: required live Mesh 16 has no eligible Nodes and is not export-participating |
| `SK_tree_densiflora_05.spm` | `20d1f545b37ada2b836db92e1f80737e0c328d2dd72467d174a8018977fe03e1` | 2026-08-02 16:13:17 | 58 / 43 | 5,358 | false | 0 / 0 | true / true / true | **accepted** |
| `SK_tree_densiflora_06.spm` | `9b039450ac3909c1e09fd3cd30f253a55845c3ce828d72445773e412d21d5bfc` | 2026-08-02 16:21:42 | 59 / 49 | 5,595 | false | 0 / 0 | false / true / true | **rejected**: sealed authoring-core continuity failed |

All four current files differ from their exact stale preimages, have coherent
regex/ElementTree Node evidence, and now report `stale=false` with orphan
owners/nodes `0 / 0`. That is necessary but not sufficient for acceptance.
The official fail-closed recovery gate accepts `_03` and `_05`; it does not
accept `_04` or `_06` for the reasons above.

For `_06`, the sealed core fingerprint is
`aef547f1a6adbc115d0a6c1bcc02377a04a31f39c854e7fd12d8cf4643906027`
and the current fingerprint is
`a8096b98d8d9efdd12cb61e51bfb394a77cb28b3425d29f6572e58072737561d`.
No equivalence exception from unfinished issue #114 was used to reinterpret
this live asset result.

## Sealed preimage and receipt verification

| asset | preimage raw SHA-256 | preimage stale/orphans | receipt SHA-256 | schema/dialect | sealed scopes (authoring / required-live) |
| --- | --- | ---: | --- | --- | --- |
| `_03` | `8670bae361bb1d5dc65843f1223a7882b442e38137bfde4e0e04874cf090e0d1` | true, 2 / 216,495 | `74075c90632f43ba8b2bc6dddb2420712ecbe9cfbd7d9f7ace9e10a574c2b731` | 6 / `schema6_graph1_core4_target2_requirements1` | `[16,17,18,19]` / `[16,17,18,19]` |
| `_04` | `cfa210deeaa64f202d30c5e44faf4d5b83e08af7e8238301d6f9e83657aead12` | true, 2 / 62,160 | `7fc0b242005ec9015c891299b764bff0c7cd521643a48527fe7dcddc7542755a` | 7 / `schema7_graph1_core5_target2_requirements1` | `[16,17,18,19]` / `[16]` |
| `_05` | `218a2e2414044060b3f481fd1590379dae1e50464ead35c0767c56225f6f34c4` | true, 2 / 43,625 | `9c6832715fe888c4231d72b399741d4049554aa2e6654edca9c431853043e22b` | 7 / `schema7_graph1_core5_target2_requirements1` | `[21,22,23,24]` / `[]` |
| `_06` | `43eb940680dd97c25fff355c79e99ce4ee6441824fa312e1aeba14261531566d` | true, 2 / 61,250 | `6b724dd3afea4e1d1a7348bb2440972e75f2574583316c58547d31cb442c36a8` | 7 / `schema7_graph1_core5_target2_requirements1` | `[19,20,21,22]` / `[]` |

Every receipt remained byte-identical to its recorded receipt SHA, every
backup remained byte-identical to its exact preimage SHA, all receipt
projections replayed against the backup, and every sealed target scope was
complete before current-state acceptance was evaluated.

## Legacy branch selection

The untracked legacy
`test_issue_14_densiflora_node_table_evidence.py` was reviewed but not copied.
It asserts that all five family assets have `live_reaudit_valid`, that every
listed target is export-participating, and that `Ctrl+S` was the automation
input. Those assertions conflict with the final immutable audit and add no
valid unique evidence for the current issue result. The legacy branch's broad
recovery implementation is not part of this branch or PR.

## Ownership release

No #14-owned watcher, session lock, or helper process remains, so there was
nothing to terminate. Modeler itself and unrelated processes were not closed
or altered. The #14 Modeler/queue lease is explicitly released for issue #13;
this release does not claim that any user-owned Modeler window is closed.
