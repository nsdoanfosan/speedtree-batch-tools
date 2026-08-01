"""Measure issue #88 PCG startup phases on a production-shaped safe fixture."""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
TOOL_DIR = REPO_DIR / "pcg_st9_texture_batch"


def _load_gui():
    name = "_pcg_startup_benchmark_gui"
    loader = importlib.machinery.SourceFileLoader(
        name, str(TOOL_DIR / "pcg_texture_gui.pyw")
    )
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def _write_spm(path, index):
    path.write_text(
        '<?xml version="1.0"?><SpeedTree><Materials>'
        f'<Material_v8 ID="1" Name="M_fixture_{index:02d}" />'
        '</Materials><Generators></Generators><Nodes></Nodes></SpeedTree>',
        encoding="utf-8",
    )


def _write_sbs(path, index):
    path.write_text(
        '<package><graph><identifier '
        f'v="T_fixture_{index:02d}" /></graph></package>',
        encoding="utf-8",
    )


def _build_fixture(root, *, folder_count, sync_file_count):
    tree = root / "Tree"
    tree.mkdir()
    for index in range(folder_count):
        folder = tree / f"tree_fixture_{index:02d}"
        texture = folder / "texture"
        texture.mkdir(parents=True)
        _write_spm(folder / f"SK_tree_fixture_{index:02d}.spm", index)
        _write_sbs(texture / f"fixture_{index:02d}.sbs", index)

    reports = root / "reports"
    reports.mkdir()
    entries = []
    for index in range(sync_file_count):
        source = root / "textures" / f"T_fixture_{index:03d}_color.tga"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes((f"fixture-{index:03d}".encode("ascii") * 64))
        md5 = hashlib.md5(source.read_bytes()).hexdigest()
        entries.append({
            "source": str(source),
            "source_md5": md5,
            "imported_md5_after": md5,
            "status": "created",
            "asset_path": f"/Game/Textures/{source.stem}",
        })
    (reports / "unreal_texture_sync_fixture.json").write_text(
        json.dumps({"mode": "fixture", "entries": entries}),
        encoding="utf-8",
    )
    return tree, reports


def _measure(callable_):
    started = time.perf_counter()
    value = callable_()
    return value, time.perf_counter() - started


def run_benchmark(output_path, *, folder_count=24, sync_file_count=144):
    with tempfile.TemporaryDirectory() as temporary:
        fixture = Path(temporary)
        cache = fixture / "cache"
        os.environ["SPEEDTREE_BATCH_TOOLS_CACHE_DIR"] = str(cache)
        sys.path.insert(0, str(REPO_DIR))
        sys.path.insert(0, str(TOOL_DIR))

        tree, reports = _build_fixture(
            fixture,
            folder_count=folder_count,
            sync_file_count=sync_file_count,
        )
        import pcg_texture_audit as audit
        import pcg_board_snapshot as board
        import pcg_startup_latency as latency
        import unreal_texture_sync as sync

        gui = _load_gui()
        sync.REPORT_DIR = reports
        cfg = {
            "tree_root": str(tree),
            "atlas_root": str(tree / "atlas"),
            "source_texture_roots": [],
            "required_export_maps": [
                "color", "normal", "extra", "height",
                "opacity", "subsurface",
            ],
        }

        global_sbs_cold_metrics = {"cache_hits": 0, "cache_misses": 0}
        _graphs, cold_global_sbs = _measure(
            lambda: audit.global_m_graph_names(
                [], cfg, metrics=global_sbs_cold_metrics
            )
        )
        audit.save_spm_analysis_cache()
        global_sbs_warm_metrics = {"cache_hits": 0, "cache_misses": 0}
        _graphs, warm_global_sbs = _measure(
            lambda: audit.global_m_graph_names(
                [], cfg, metrics=global_sbs_warm_metrics
            )
        )

        cold_report, cold_primary = _measure(lambda: audit.make_report(cfg))
        audit.save_spm_analysis_cache()
        warm_report, warm_primary = _measure(lambda: audit.make_report(cfg))

        relation_cold_metrics = {}
        _value, cold_relations = _measure(
            lambda: gui.cache_blender_connection_rows(
                cold_report, metrics=relation_cold_metrics
            )
        )
        relation_warm_metrics = {}
        _value, warm_relations = _measure(
            lambda: gui.cache_blender_connection_rows(
                warm_report, metrics=relation_warm_metrics
            )
        )

        snapshot_path = cache / "benchmark_board.json"
        board.write_board_display_snapshot(
            warm_report, cfg, path=snapshot_path
        )
        snapshot, cached_board_paint = _measure(
            lambda: board.read_board_display_snapshot(
                cfg, path=snapshot_path
            )
        )

        cold_sync, cold_migration = _measure(
            lambda: sync.load_sync_state(migrate=True)
        )
        warm_sync, warm_migration = _measure(
            lambda: sync.load_sync_state(migrate=True)
        )

        cold_total = (
            cached_board_paint + cold_primary + cold_global_sbs
            + cold_relations + cold_migration
        )
        warm_total = (
            cached_board_paint + warm_primary + warm_global_sbs
            + warm_relations + warm_migration
        )
        budgets = latency.PRODUCTION_FIXTURE_LATENCY_BUDGET_SECONDS
        assertions = {
            "cached_board_paint": cached_board_paint
            < budgets["cached_board_paint"],
            "cold_total": cold_total < budgets["cold_total"],
            "warm_total": warm_total < budgets["warm_total"],
            "warm_provider_cache_hit": (
                warm_report["startup_timing"]["provider_metrics"]["cache_hit"]
                is True
            ),
            "warm_relation_cache_hit": (
                relation_warm_metrics.get("cache_hits") == folder_count
            ),
            "warm_global_sbs_cache_hit": (
                global_sbs_warm_metrics.get("cache_hits") == folder_count
                and global_sbs_warm_metrics.get("cache_misses") == 0
            ),
            "snapshot_display_only": snapshot.get("display_only") is True,
            "sync_migration_complete": (
                cold_sync.get("migration_complete") is True
                and warm_sync.get("migration_complete") is True
            ),
        }
        receipt = {
            "schema_version": 1,
            "kind": "pcg_issue_88_startup_benchmark",
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            "fixture": {
                "folder_count": folder_count,
                "sbs_count": folder_count,
                "sync_file_count": sync_file_count,
                "production_assets_touched": False,
            },
            "budgets_seconds": dict(budgets),
            "cold": {
                "cached_board_paint": round(cached_board_paint, 6),
                "primary_live_audit": round(cold_primary, 6),
                "global_sbs_discovery": round(cold_global_sbs, 6),
                "blender_relations": round(cold_relations, 6),
                "sync_migration": round(cold_migration, 6),
                "total": round(cold_total, 6),
                "audit_timing": cold_report["startup_timing"],
                "relation_metrics": relation_cold_metrics,
                "global_sbs_metrics": global_sbs_cold_metrics,
            },
            "warm": {
                "cached_board_paint": round(cached_board_paint, 6),
                "primary_live_audit": round(warm_primary, 6),
                "global_sbs_discovery": round(warm_global_sbs, 6),
                "blender_relations": round(warm_relations, 6),
                "sync_migration": round(warm_migration, 6),
                "total": round(warm_total, 6),
                "audit_timing": warm_report["startup_timing"],
                "relation_metrics": relation_warm_metrics,
                "global_sbs_metrics": global_sbs_warm_metrics,
            },
            "assertions": assertions,
            "passed": all(assertions.values()),
        }
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if not receipt["passed"]:
            failed = [name for name, passed in assertions.items() if not passed]
            raise RuntimeError(
                "PCG startup benchmark failed: " + ", ".join(failed)
            )
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--folders", type=int, default=24)
    parser.add_argument("--sync-files", type=int, default=144)
    args = parser.parse_args()
    receipt = run_benchmark(
        args.out,
        folder_count=max(1, args.folders),
        sync_file_count=max(1, args.sync_files),
    )
    print(json.dumps({
        "passed": receipt["passed"],
        "cold_total": receipt["cold"]["total"],
        "warm_total": receipt["warm"]["total"],
        "output": str(Path(args.out).resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
