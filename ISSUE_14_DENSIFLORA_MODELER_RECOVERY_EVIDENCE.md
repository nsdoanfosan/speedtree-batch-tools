# Issue #14 Densiflora Modeler recovery evidence

Captured on 2026-08-02 KST from the existing
`codex/issue-14-densiflora-modeler-recovery` worktree. The production SPMs
were audited from immutable in-memory byte snapshots. No raw XML/SPM editing
or automated UI/key input was used.

## Current production acceptance state

| asset | raw SHA-256 | generators | stale | orphan owners/nodes | total nodes | generator membership |
| --- | --- | ---: | --- | ---: | ---: | --- |
| `SK_tree_densiflora_03.spm` | `9818626404c5c57913e68ca60449204f11063c7cde5598abdd634fe9975510ba` | 48 | false | 0 / 0 | 9,526 | `bd77d8c30ce2d95dda1a3ad6053d0c544891288989dd32754b04070fe1f8d590` |
| `SK_tree_densiflora_04.spm` | `19845b0e9881d4d113bf0e8e76fdaf51ef7700bea07c4d0de314089da21df439` | 64 | false | 0 / 0 | 6,344 | `67fca1a524fc7e3796ff23f6dd0a49f36b3d5dd8709919a49cf48cb6881ec517` |
| `SK_tree_densiflora_05.spm` | `218a2e2414044060b3f481fd1590379dae1e50464ead35c0767c56225f6f34c4` | 58 | true | 2 / 43,625 | 48,309 | `bce57bc731ed433296dd1acb4cc404e7bef4b9bb16358598844caedcb6848a8f` |
| `SK_tree_densiflora_06.spm` | `43eb940680dd97c25fff355c79e99ce4ee6441824fa312e1aeba14261531566d` | 59 | true | 2 / 61,250 | 66,075 | `e860ae409974ec11ef5c639b15a4d52b9b154ab551e63f23b245990bafeac7ea` |

The `_03` and `_04` membership fingerprints exactly match their immutable
preimage receipts, so both satisfy the requested membership-continuity gate.

## Newly sealed pending preimages

The official `stale_node_table_recovery` implementation created and verified
the following immutable backup/receipt pairs without writing either production
source:

- `_05`: preimage `218a2e241404...`, authored Mesh IDs `[21,22,23,24]`,
  receipt SHA-256
  `9c6832715fe888c4231d72b399741d4049554aa2e6654edca9c431853043e22b`.
- `_06`: preimage `43eb940680dd...`, authored Mesh IDs `[19,20,21,22]`,
  receipt SHA-256
  `6b724dd3afea4e1d1a7348bb2440972e75f2574583316c58547d31cb442c36a8`.

Both sources were re-hashed after receipt creation and remain byte-identical
to their sealed preimages.

## Non-UI Modeler save feasibility

The installed Modeler 10.1.0 command-line help exposes only model open and
mesh/game export (`-export`, `-export_game`, and `-export_options`); it has no
SPM save/resave option. Controlled runs against an isolated `_05` copy proved:

- command-line OBJ export evaluates and exports the model but leaves the SPM
  byte-identical;
- the installed internal `-test` path also leaves the SPM byte-identical;
- requesting `.spm` as an export target produces no resaved SPM and leaves the
  source byte-identical.

The repository's official recovery contract intentionally records
`modeler_save_automation=false`, `ui_keystroke_simulation=false`, and launches
Modeler only to wait for a manual `File > Save`. Therefore `_05` and `_06`
cannot reach `stale=false` through any supported non-UI Modeler interface on
this installation. No production write was attempted through an unsupported
or raw-file path.
