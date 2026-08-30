import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LAUNCHER_SOURCE = REPO / "speedtree_collision_cli" / "launcher.cpp"
HOOK_SOURCE = REPO / "speedtree_collision_cli" / "hook.cpp"
INTEGRATED_BAT = REPO / "SpeedTree_Batch_Tools.bat"
SK_BATCH_BAT = REPO / "sk_batch" / "SK_Batch.bat"
SK_EXACT_PUSH_BAT = REPO / "sk_batch" / "SK_Exact_Push.bat"
BUILD_SCRIPT = REPO / "speedtree_collision_cli" / "build.ps1"
LAUNCH_GUARD = REPO / "launch_guard.pyw"


class SpeedTreeCollisionCliLauncherTests(unittest.TestCase):
    def test_native_cli_is_hidden_and_gui_bake_remains_diagnostic_only(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("CreateDesktopW", source)
        self.assertIn("startup.lpDesktop = isolatedDesktopName.data();", source)
        self.assertIn("startup.wShowWindow = SW_SHOW;", source)
        self.assertIn("nativeCli ? SW_HIDE : SW_SHOWNOACTIVATE", source)
        self.assertNotIn("startup.wShowWindow = SW_SHOWMINNOACTIVE;", source)
        self.assertIn("STARTF_USESHOWWINDOW", source)
        self.assertNotIn("SetWindowPos", source)
        self.assertIn('value == L"--interactive-window"', source)
        self.assertIn('value == L"--gui-bake"', source)
        self.assertIn('L"SPEEDTREE_COLLISION_NATIVE_CLI"', source)
        self.assertIn("if (isolateWindow && persistent)", source)
        self.assertIn("persistent mode is disabled for this export", source)

    def test_batch_launchers_force_the_headless_native_cli(self):
        for launcher in (INTEGRATED_BAT, SK_BATCH_BAT, SK_EXACT_PUSH_BAT):
            source = launcher.read_text(encoding="utf-8")
            self.assertIn('set "SPEEDTREE_COLLISION_NATIVE_CLI=1"', source)
            self.assertIn('set "SPEEDTREE_COLLISION_PERSISTENT=0"', source)
            self.assertNotIn("SPEEDTREE_COLLISION_SESSION_ANCHOR", source)
        guard = LAUNCH_GUARD.read_text(encoding="utf-8")
        for launcher in (INTEGRATED_BAT, SK_BATCH_BAT):
            source = launcher.read_text(encoding="utf-8")
            self.assertNotIn("powershell.exe", source)
            self.assertNotIn("findstr.exe", source)
        self.assertIn("run_collision_cli_preflight", guard)
        self.assertIn('"-WindowStyle",', guard)
        self.assertIn('"Hidden",', guard)
        self.assertIn('"-IfNeeded",', guard)
        self.assertIn('build.ps1" -IfNeeded', SK_EXACT_PUSH_BAT.read_text(
            encoding="utf-8"
        ))

    def test_build_freshness_covers_every_cli_source_input(self):
        source = BUILD_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("[switch]$IfNeeded", source)
        self.assertIn('$PSCommandPath,', source)
        self.assertIn('$hookSource,', source)
        self.assertIn('$launcherSource,', source)
        self.assertIn('Join-Path $sourceDirectory "session_protocol.h"', source)
        self.assertIn("$oldestOutput -ge $latestInput", source)
        self.assertIn("$diagnoseOutput -contains $capabilityContract", source)

    def test_batch_launchers_reject_a_stale_cli_feature_contract(self):
        launcher = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        contract = (
            "SPEEDTREE_COLLISION_CLI_CONTRACT="
            "native-runtime-receipt-v17"
        )

        self.assertIn(contract, launcher)
        guard = LAUNCH_GUARD.read_text(encoding="utf-8")
        self.assertIn(contract, guard)
        for batch_launcher in (INTEGRATED_BAT, SK_BATCH_BAT):
            source = batch_launcher.read_text(encoding="utf-8")
            self.assertNotIn("findstr.exe", source)
        exact = SK_EXACT_PUSH_BAT.read_text(encoding="utf-8")
        self.assertIn(contract, exact)
        self.assertIn("findstr.exe /x", exact)

    def test_fresh_verification_marker_is_after_all_launcher_seals(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        marker_write = source.rindex(
            "std::wcout << kFreshVerificationSealedMarker"
        )

        self.assertIn(
            'L"SPEEDTREE_FRESH_VERIFICATION_EXPORT_SEALED=1"',
            source,
        )
        self.assertIn(
            "if (verificationOnly && !secondaryExportOutput.empty() &&",
            source,
        )
        for required_check in (
            "the requested FBX was not created",
            "The requested FBX was not updated",
            "the secondary output was not created",
            "The secondary output was not updated",
            "the native receipt was not created",
            "The native receipt was not updated",
        ):
            self.assertLess(source.rindex(required_check), marker_write)
        self.assertLess(
            marker_write,
            source.rindex('std::wcout << L"Post-collision export completed.'),
        )

    def test_fbx_root_weights_use_the_native_id_zero_path(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        weight_hook = source[
            source.index("void __fastcall HookedExportVertexWeights"):
            source.index("void __fastcall HookedInsertExportBone")
        ]
        entry_stub = source[
            source.index("bool BuildExportVertexWeightsEntryStub"):
            source.index("void FreeExportVertexWeightsEntryStub")
        ]

        self.assertIn("kExportVertexWeightsRva = 0x6B4FE0", source)
        self.assertIn("kFindExportBoneMappingRva = 0x6B4DF0", source)
        self.assertIn("if (parentId != 0)", weight_hook)
        self.assertIn("const float rootWeight = 1.0f - childWeight;", weight_hook)
        self.assertIn('"omitted_no_exact_bone_record"', source)
        self.assertIn('"not_applicable_boneless_export"', source)
        self.assertIn(
            "const char* idZeroClusterWrite =\n"
            "        gNativeReceiptBones.empty()",
            source,
        )
        self.assertNotIn(
            "gNativeReceiptBones.empty() && gNativeReceiptProxies.empty()",
            source,
        )
        self.assertIn(
            "gMissingIdZeroBoneRecordLogged.store(false",
            source,
        )
        missing_root_guard = (
            "if (FindExactExportBoneMapping(exporter, 0) == nullptr)"
        )
        self.assertIn(missing_root_guard, weight_hook)
        primary_call = (
            "gOriginalExportVertexWeights(\n"
            "        exporter,\n"
            "        position,\n"
            "        sourceBoneId,"
        )
        self.assertIn(primary_call, weight_hook)
        self.assertLess(
            weight_hook.index(primary_call),
            weight_hook.index(missing_root_guard),
        )
        self.assertNotIn(
            "if (sourceBoneId == 0 &&\n"
            "        FindExactExportBoneMapping(exporter, 0) == nullptr)",
            weight_hook,
        )
        self.assertIn(
            "gOriginalExportVertexWeights(\n"
            "            exporter,\n"
            "            position,\n"
            "            0,",
            weight_hook,
        )
        self.assertLess(
            weight_hook.index(missing_root_guard),
            weight_hook.index(
                "gOriginalExportVertexWeights(\n"
                "            exporter,\n"
                "            position,\n"
                "            0,"
            ),
        )
        self.assertIn("SpeedTree_Modeler+0x6B5185", weight_hook)
        self.assertIn("test r8d, r8d", entry_stub)
        self.assertIn("&gOriginalExportVertexWeights", entry_stub)
        self.assertIn("CaptureNativeReceiptIdZero", entry_stub)
        self.assertIn("tail-jump to", entry_stub)
        self.assertIn("IDs enter the compiled weight hook", entry_stub)
        self.assertIn("FbxSkeleton::eLimbNode", source)
        self.assertIn("No spatial lookup or normalization", weight_hook)

    def test_root_zone_leaf_meshes_get_exact_rigid_bones(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")

        scope = source[
            source.index("bool IsRootZoneLeafMesh"):
            source.index("void LogMissingIdZeroBoneRecordOnce")
        ]
        leaf_export = source[
            source.index("void __fastcall HookedLeafMeshExport"):
            source.index("void LogCollisionInputTypes")
        ]
        weight_hook = source[
            source.index("void __fastcall HookedExportVertexWeights"):
            source.index("int __fastcall CaptureNativeReceiptIdZero")
        ]

        self.assertIn(".?AVCZoneNode@@", scope)
        self.assertIn(".?AVCStartNode@@", scope)
        self.assertIn(".?AVCBranchNode@@", scope)
        self.assertIn(".?AVCFrondNode@@", scope)
        self.assertIn("return sawZone", scope)
        self.assertIn("!clusterSource && IsRootZoneLeafMesh", leaf_export)
        self.assertIn("ClassifyBaseRefBranchLeaf", leaf_export)
        self.assertIn("rootZoneLeaf || baseRefBranchLeaf", leaf_export)
        self.assertIn("geometryEndBefore == geometryEndAfter", leaf_export)
        self.assertIn("if (!needsSyntheticBone)", leaf_export)
        self.assertIn("ReserveSyntheticLeafBoneId", leaf_export)
        self.assertIn("primary.overrideWeight = syntheticLeafWeight", weight_hook)
        self.assertIn("primary.replacementWeight = 1.0", weight_hook)
        self.assertIn("RemoveHook(gLeafMeshExportHook)", source)
        self.assertIn("HookedLeafMeshExport", source[source.index("bool InstallHooks"):])

    def test_baseref_branch_leaf_meshes_get_exact_rigid_bones(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        scope = source[
            source.index("BaseRefBranchLeafStatus ClassifyBaseRefBranchLeaf"):
            source.index("void LogMissingIdZeroBoneRecordOnce")
        ]

        self.assertIn(".?AVCBranchNode@@", scope)
        self.assertIn(".?AVCBaseNode@@", scope)
        self.assertIn(".?AVCBaseRefNode@@", scope)
        self.assertIn("baseBytes + 0x2C8", scope)
        self.assertIn("baseBytes + 0x2D0", scope)
        self.assertIn("targetBranch", scope)
        self.assertIn("immediateBranch", scope)
        self.assertIn("if (immediateBranch == nullptr)", scope)
        self.assertIn("ResolveExactBaseRefLeafParentBoneId", scope)
        self.assertIn("0x500 / sizeof(void*)", scope)
        self.assertIn("0x6F8 / sizeof(void*)", scope)
        self.assertIn("leafNode) + 0x140", scope)
        self.assertIn("resolveAttachmentBone", scope)
        root_scope = source[
            source.index("bool IsRootZoneLeafMesh"):
            source.index("BaseRefBranchLeafStatus ClassifyBaseRefBranchLeaf")
        ]
        self.assertNotIn(
            'std::strcmp(type, ".?AVCBaseNode@@") == 0', root_scope
        )

    def test_cluster_sources_keep_single_axis_reference_bone_policy(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        classifier = source[
            source.index("bool IsClusterSourceInput"):
            source.index("void LogMissingIdZeroBoneRecordOnce")
        ]
        leaf_export = source[
            source.index("void __fastcall HookedLeafMeshExport"):
            source.index("void LogCollisionInputTypes")
        ]

        self.assertIn('L"cluster"', classifier)
        self.assertIn('L"SK_cluster_"', classifier)
        self.assertIn("!clusterSource && IsRootZoneLeafMesh", leaf_export)
        self.assertIn("ClassifyBaseRefBranchLeaf", leaf_export)
        self.assertIn("NativeParsedBoneCount() == 0", leaf_export)

    def test_zero_bone_spm_gets_one_absolute_rigid_fallback(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        leaf_export = source[
            source.index("void __fastcall HookedLeafMeshExport"):
            source.index("void LogCollisionInputTypes")
        ]
        id_zero = source[
            source.index("int __fastcall CaptureNativeReceiptIdZero"):
            source.index("void FreeExportVertexWeightsEntryStub")
        ]
        weight_hook = source[
            source.index("void __fastcall HookedExportVertexWeights"):
            source.index("int __fastcall CaptureNativeReceiptIdZero")
        ]

        self.assertIn(
            "clusterSource && !needsSyntheticBone && NativeParsedBoneCount() == 0",
            leaf_export,
        )
        self.assertIn("ReserveZeroBoneAbsoluteFallbackId", leaf_export)
        self.assertIn("fallback.parentId = 0", leaf_export)
        self.assertIn("fallback.start[0] = 0.0f", leaf_export)
        self.assertIn("fallback.end[2] = 1.0f", leaf_export)
        self.assertIn("gZeroBoneAbsoluteFallbackId", id_zero)
        self.assertIn("effectiveBoneId = gZeroBoneAbsoluteFallbackId", id_zero)
        self.assertIn("sourceBoneId == gZeroBoneAbsoluteFallbackId", weight_hook)
        self.assertIn("primary.replacementWeight = 1.0", weight_hook)

    def test_baseref_leaf_parent_and_cluster_fallback_are_fail_closed(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        leaf_export = source[
            source.index("void __fastcall HookedLeafMeshExport"):
            source.index("void LogCollisionInputTypes")
        ]
        insert_hook = source[
            source.index("void __fastcall HookedInsertExportBone"):
            source.index("bool TryReadExportGeometryEnd")
        ]

        self.assertIn("ResolveExactBaseRefLeafParentBoneId", leaf_export)
        self.assertIn("baseRefParentBranch", leaf_export)
        self.assertIn("gObservedExportBoneIds[row.boneId]", source)
        self.assertIn("return gObservedExportBoneIds.size()", source)
        self.assertIn("RequireExactParentBoneId(parentId)", source)
        self.assertIn("gRequiredExactParentBoneIds", source)
        self.assertIn("was never serialized by the native exporter", source)
        self.assertIn("could not resolve an exact native parent bone ID", source)
        self.assertIn("BaseRefBranchLeafStatus::Invalid", leaf_export)
        self.assertIn("gZeroBoneAbsoluteFallbackId > 0", insert_hook)
        self.assertIn("cannot absorb a later native bone", insert_hook)
        self.assertNotIn("FindNearest", source)
        self.assertNotIn("nearestBone", source)
        native_build_start = source.index("void __fastcall HookedNativeExportBuild")
        native_build = source[native_build_start:]
        self.assertIn("IsClusterSourceInput() && NativeParsedBoneCount() == 0", native_build)
        self.assertIn("required reference-axis bone", native_build)

    def test_disappeared_persistent_pipe_starts_a_replacement(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("IsRestartableSessionPipeError", source)
        self.assertIn("ERROR_BROKEN_PIPE", source)
        self.assertIn("ERROR_PIPE_NOT_CONNECTED", source)
        self.assertIn("starting a replacement", source)

    def test_entire_recovery_check_is_bypassed_for_every_gui_bake_mode(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")

        recovery_hook = source[
            source.index("void __fastcall HookedMainWindowRecoveryCheck"):
            source.index("bool BuildSessionTargetPathList")
        ]

        self.assertIn(
            "if (!InstallHook(\n"
            "                gMainWindowRecoveryCheckHook,",
            source,
        )
        self.assertNotIn(
            "if (gSessionServerMode && !InstallHook(\n"
            "                gMainWindowRecoveryCheckHook,",
            source,
        )
        self.assertIn("entire recovery-check logic", recovery_hook)
        self.assertIn("its .sbk lookup, recovery decision, and QMessageBox", recovery_hook)
        self.assertNotIn("gOriginalMainWindowRecoveryCheck(mainWindow)", recovery_hook)
        self.assertIn("backup file itself remains intact", recovery_hook)

    def test_qt_question_modal_is_the_no_semantics_fallback(self):
        source = HOOK_SOURCE.read_text(encoding="utf-8")
        dialog_hook = source[
            source.index("int __fastcall HookedQDialogExec"):
            source.index("bool BuildSessionTargetPathList")
        ]

        self.assertIn('gQObjectInherits(dialog, "QMessageBox")', dialog_hook)
        self.assertIn("kQMessageBoxQuestionIcon", dialog_hook)
        self.assertIn("return kQMessageBoxNoButton;", dialog_hook)
        self.assertIn("suppressed a Qt Question modal", dialog_hook)
        self.assertIn('GetProcAddress(qtWidgets, "?exec@QDialog@@UEAAHXZ")', source)

    def test_one_shot_process_wait_has_a_progress_watchdog(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn("SPEEDTREE_COLLISION_STALL_TIMEOUT_MS", source)
        self.assertIn("ReadProcessActivity", source)
        self.assertIn("GetProcessMemoryInfo", source)
        self.assertIn("FileWriteTime(logPath)", source)
        self.assertIn("kProgressStallExitCode", source)
        self.assertIn("no meaningful CPU, I/O, memory, or hook-log", source)
        self.assertIn("SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS", source)
        self.assertIn("const DWORD processWaitMs = timeoutMs;", source)

    def test_modeler_child_uses_the_shortest_rlm_connect_window(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        hook = HOOK_SOURCE.read_text(encoding="utf-8")

        self.assertIn(
            'SetTemporaryEnvironment(\n'
            '            L"RLM_CONNECT_TIMEOUT",\n'
            '            L"1")',
            source,
        )
        self.assertIn(
            "RestoreEnvironment(restoreRlmConnectTimeout);",
            source,
        )
        self.assertIn('L"SPEEDTREE_COLLISION_RLM_FAIL_FAST"', source)
        self.assertIn('L"SPEEDTREE_COLLISION_RLM_FAIL_FAST"', hook)
        self.assertIn(
            "kRlmConnectAttemptLimitImmediateRva = 0x1831689",
            hook,
        )
        self.assertIn("SetRlmConnectFailFastPatch(true)", hook)
        self.assertIn("SetRlmConnectFailFastPatch(false)", hook)
        self.assertLess(
            source.index('L"RLM_CONNECT_TIMEOUT"'),
            source.index("CreateProcessW("),
        )
        self.assertGreater(
            source.index("RestoreEnvironment(restoreRlmConnectTimeout);"),
            source.index("CreateProcessW("),
        )

    def test_native_cli_can_bundle_a_second_export_in_one_process(self):
        launcher = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        hook = HOOK_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--secondary-export-options"', launcher)
        self.assertIn('value == L"--secondary-export"', launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_SECONDARY_OUTPUT", launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_SECONDARY_OPTIONS", launcher)
        self.assertIn("RunSecondaryNativeExport", hook)
        self.assertIn("native CLI bundled secondary export completed", hook)
        self.assertIn("gSecondaryNativeSerializationActive", hook)
        self.assertIn(
            "bundled secondary model update suppressed",
            hook,
        )
        self.assertIn(
            "bundled secondary collision refresh suppressed",
            hook,
        )

    def test_native_cli_logs_large_native_phases_with_qpc(self):
        hook = HOOK_SOURCE.read_text(encoding="utf-8")
        receipt_capture = hook[
            hook.index("void CaptureNativeReceiptProxy"):
            hook.index("void CaptureNativeReceiptBone")
        ]
        vertex_weights = hook[
            hook.index("void __fastcall HookedExportVertexWeights"):
            hook.index("int __fastcall CaptureNativeReceiptIdZero")
        ]

        self.assertIn("QueryPerformanceCounter", hook)
        self.assertIn("QueryPerformanceFrequency", hook)
        self.assertIn(
            '"QPC1 phase=%s export=%u collision_pass=%u start_ticks=%lld "',
            hook,
        )
        for phase in (
            "native_raw_model_update",
            "interactive_generator_prepare",
            "interactive_generator_rebuild",
            "native_full_document_stage",
            "shade_pruning_volume_generation",
            "native_export_geometry_build",
            "native_receipt_json_serialization",
            "native_secondary_export_total",
        ):
            self.assertIn(f'"{phase}"', hook)
        self.assertIn('"collision_post_input_compute",\n        1', hook)
        self.assertIn('"collision_post_regeneration_compute",\n                2', hook)
        self.assertIn(
            '"collision_post_regeneration_direct_fallback_compute",',
            hook,
        )
        self.assertIn('"collision_prebuild_safety_fallback_compute"', hook)
        self.assertNotIn("QueryPerformanceCounter", receipt_capture)
        self.assertNotIn("BeginNativeQpcPhase", receipt_capture)
        self.assertNotIn("QueryPerformanceCounter", vertex_weights)
        self.assertNotIn("BeginNativeQpcPhase", vertex_weights)

    def test_native_receipt_bone_identity_lookup_is_exact_and_reset(self):
        hook = HOOK_SOURCE.read_text(encoding="utf-8")
        capture = hook[
            hook.index("void CaptureNativeReceiptBone"):
            hook.index("void __fastcall HookedExportVertexWeights")
        ]
        reset = hook[
            hook.index("void ResetNativeReceiptCapture"):
            hook.index("int NativeReceiptGeometryOrdinal")
        ]

        self.assertIn(
            "std::unordered_map<int, std::size_t> "
            "gNativeReceiptBoneIndexes;",
            hook,
        )
        self.assertIn("gNativeReceiptBoneIndexes.find(row.boneId)", capture)
        self.assertNotIn("std::find_if", capture)
        self.assertIn("existing.parentId != row.parentId", capture)
        self.assertIn(
            "std::memcmp(existing.start, row.start, sizeof(row.start)) != 0",
            capture,
        )
        self.assertIn(
            "std::memcmp(existing.end, row.end, sizeof(row.end)) != 0",
            capture,
        )
        self.assertIn("conflicting parent or coordinates", capture)
        self.assertIn("gNativeReceiptFbxNodeNames.clear();", reset)
        self.assertIn("gNativeReceiptBoneIndexes.clear();", reset)

    def test_verification_exports_skip_only_the_expensive_post_bake(self):
        launcher = LAUNCHER_SOURCE.read_text(encoding="utf-8")
        hook = HOOK_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--verification-only"', launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_VERIFICATION_ONLY", launcher)
        self.assertIn("SPEEDTREE_COLLISION_CLI_VERIFICATION_ONLY", hook)
        self.assertIn(
            "native CLI verification-only export skips Collision/Prune bake",
            hook,
        )
        self.assertIn("gOriginalSpeedTreeExport(arg1, arg2, arg3, gameExport);", hook)
        self.assertIn("RunSecondaryNativeExport(arg1, gameExport);", hook)

    def test_persistent_session_host_and_busy_pipe_wait_are_available(self):
        source = LAUNCHER_SOURCE.read_text(encoding="utf-8")

        self.assertIn('value == L"--serve-session"', source)
        self.assertIn('value == L"--ping-session"', source)
        self.assertIn("WaitForSingleObject(process.hProcess, INFINITE)", source)
        self.assertIn("pipeError == ERROR_PIPE_BUSY", source)
        self.assertIn("timeoutMs + 5 * 60 * 1000", source)

if __name__ == "__main__":
    unittest.main()
