#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <GL/gl.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iterator>
#include <memory>
#include <mutex>
#include <new>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "session_protocol.h"

namespace {

constexpr std::uintptr_t kSpeedTreeExportRva = 0x135A20;
constexpr std::uintptr_t kMainWindowOnIdleRva = 0x13B7F0;
constexpr std::uintptr_t kMainWindowOnIdleDrawRva = 0x13C390;
constexpr std::uintptr_t kMainWindowOpenFileListRva = 0x137750;
constexpr std::uintptr_t kMainWindowConfirmDiscardRva = 0x131880;
constexpr std::uintptr_t kMainWindowRecoveryCheckRva = 0x1325E0;
constexpr std::uintptr_t kMarkCollisionDirtyRva = 0x3D90B0;
constexpr std::uintptr_t kForceGeneratorRefreshRva = 0x3D90C0;
constexpr std::uintptr_t kCollisionDoneRva = 0x3D25D0;
constexpr std::uintptr_t kCollisionComputeRva = 0x3EE760;
constexpr std::uintptr_t kNativeExportBuildRva = 0xAA530;
constexpr std::uintptr_t kNativeModelUpdateRva = 0x3D1170;
constexpr std::uintptr_t kNativeExportFinalizeGeometryRva = 0xA7580;
constexpr std::uintptr_t kNativeExportFinalizeDocumentRva = 0xB1510;
constexpr std::uintptr_t kResolveBranchBoneIdRva = 0x4B5C00;
constexpr std::uintptr_t kInsertExportBoneRva = 0x37D4A0;
constexpr std::uintptr_t kExportVertexWeightsRva = 0x6B4FE0;
constexpr std::uintptr_t kFindExportBoneMappingRva = 0x6B4DF0;
constexpr std::uintptr_t kFbxClusterAddControlPointRva = 0x1259E50;
constexpr std::uintptr_t kFbxClusterAppendIndexRva = 0x1248300;
constexpr std::uintptr_t kFbxClusterAppendWeightRva = 0x125AB20;
constexpr std::uintptr_t kFbxNodeCreateRva = 0x122FC30;
constexpr std::uintptr_t kFbxSingleRootWrapperBranchRva = 0x6B6C40;
constexpr std::uintptr_t kFbxRootWrapperSkeletonTypeRva = 0x6B6C55;
constexpr std::uintptr_t kTreeDocumentPrepareRva = 0x728F00;
constexpr std::uintptr_t kTreeDocumentModelStageRva = 0x366AC0;
constexpr std::uintptr_t kGenerateShadeVolumeRva = 0x2FE10;
constexpr std::uintptr_t kScheduleCollisionRva = 0x3BF790;
constexpr std::uintptr_t kApplicationControllerPointerRva = 0x22A0BF8;
constexpr std::uintptr_t kCollisionThreadVtableRva = 0x19DA008;
constexpr std::uintptr_t kRlmConnectAttemptLimitImmediateRva = 0x1831689;
constexpr std::ptrdiff_t kTreeWindowModelOffset = 0x68;
constexpr std::ptrdiff_t kCoreModelCollisionQualityOffset = 0x9BD8;
constexpr std::ptrdiff_t kEmbeddedCollisionThreadOffset = 0x9C70;
constexpr DWORD kNoCollisionThreadExitCode = 0xC0111001;
constexpr DWORD kCollisionTimeoutExitCode = 0xC0111002;
constexpr DWORD kHookRuntimeFailureExitCode = 0xC0111003;
constexpr DWORD kNoGeneratedCollisionInputsExitCode = 0xC0111004;
constexpr DWORD kDefaultTimeoutMs = 10 * 60 * 1000;
constexpr DWORD kPersistentDocumentLoadTimeoutMs = 10 * 60 * 1000;

constexpr unsigned char kSpeedTreeExportPrologue[12] = {
    0x48, 0x89, 0x5c, 0x24, 0x20, 0x55,
    0x56, 0x57, 0x41, 0x56, 0x41, 0x57,
};

constexpr unsigned char kQThreadStartPrologue[12] = {
    0x48, 0x89, 0x5c, 0x24, 0x10, 0x48,
    0x89, 0x6c, 0x24, 0x18, 0x56, 0x57,
};

constexpr unsigned char kMainWindowOnIdlePrologue[13] = {
    0x40, 0x55, 0x53, 0x56, 0x57, 0x41, 0x54,
    0x41, 0x55, 0x41, 0x56, 0x41, 0x57,
};

constexpr unsigned char kMainWindowOnIdleDrawPrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x08,
    0x48, 0x89, 0x6c, 0x24, 0x10,
    0x48, 0x89, 0x74, 0x24, 0x18,
};

constexpr unsigned char kMainWindowConfirmDiscardPrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x10,
    0x48, 0x89, 0x74, 0x24, 0x18,
    0x48, 0x89, 0x7c, 0x24, 0x20,
};

constexpr unsigned char kMainWindowOpenFileListPrologue[13] = {
    0x40, 0x55, 0x53, 0x56, 0x57,
    0x41, 0x54, 0x41, 0x55, 0x41,
    0x56, 0x41, 0x57,
};

constexpr unsigned char kMainWindowRecoveryCheckPrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x10,
    0x48, 0x89, 0x74, 0x24, 0x18,
    0x48, 0x89, 0x7c, 0x24, 0x20,
};

constexpr unsigned char kNativeExportBuildPrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x10,
    0x48, 0x89, 0x74, 0x24, 0x18,
    0x48, 0x89, 0x7c, 0x24, 0x20,
};

constexpr unsigned char kNativeModelUpdatePrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x08,
    0x48, 0x89, 0x74, 0x24, 0x10,
    0x57, 0x48, 0x83, 0xec, 0x20,
};

constexpr unsigned char kNativeExportFinalizeGeometryPrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x10,
    0x48, 0x89, 0x74, 0x24, 0x18,
    0x48, 0x89, 0x7c, 0x24, 0x20,
};

constexpr unsigned char kNativeExportFinalizeDocumentPrologue[15] = {
    0x48, 0x89, 0x5c, 0x24, 0x10,
    0x48, 0x89, 0x74, 0x24, 0x18,
    0x48, 0x89, 0x7c, 0x24, 0x20,
};

constexpr unsigned char kQDialogExecPrologue[12] = {
    0x40, 0x55, 0x56, 0x57,
    0x48, 0x83, 0xec, 0x50,
    0x48, 0x8b, 0x79, 0x08,
};


constexpr unsigned char kInsertExportBonePrologue[15] = {
    0x48, 0x8b, 0xc4,
    0x48, 0x89, 0x58, 0x08,
    0x48, 0x89, 0x68, 0x10,
    0x48, 0x89, 0x70, 0x18,
};

constexpr unsigned char kExportVertexWeightsPrologue[15] = {
    0x48, 0x8B, 0xC4,
    0x48, 0x89, 0x58, 0x08,
    0x48, 0x89, 0x70, 0x10,
    0x48, 0x89, 0x78, 0x20,
};


constexpr unsigned char kFbxClusterAddControlPointPrologue[14] = {
    0x85, 0xD2,
    0x78, 0x39,
    0xF2, 0x0F, 0x11, 0x54, 0x24, 0x18,
    0x89, 0x54, 0x24, 0x10,
};

constexpr unsigned char kFbxNodeCreatePrologue[13] = {
    0x48, 0x89, 0x5C, 0x24, 0x08,
    0x57,
    0x48, 0x83, 0xEC, 0x30,
    0x48, 0x8B, 0xFA,
};

constexpr int kQMessageBoxQuestionIcon = 4;
constexpr int kQMessageBoxNoButton = 0x00010000;

constexpr char kCollisionThreadRttiName[] = ".?AVCCollisionThread@@";

using QThreadStartFn = void(__fastcall*)(void* thread, int priority);
using QThreadWaitFn = bool(__fastcall*)(void* thread, unsigned long timeoutMs);
using QThreadIsRunningFn = bool(__fastcall*)(const void* thread);
using ProcessEventsFn = void(__cdecl*)(int flags, int maximumTimeMs);
using SendPostedEventsFn = void(__cdecl*)(void* receiver, int eventType);
using SpeedTreeExportFn = void(__fastcall*)(void* arg1, void* arg2, void* arg3, bool gameExport);
using QWidgetFindFn = void*(__cdecl*)(std::uintptr_t windowId);
using QObjectChildrenFn = const void*(__fastcall*)(const void* object);
using QObjectInheritsFn = bool(__fastcall*)(const void* object, const char* className);
using QObjectParentFn = void*(__fastcall*)(const void* object);
using MarkCollisionDirtyFn = void(__fastcall*)(void* treeModel);
using ForceGeneratorRefreshFn = void(__fastcall*)(void* treeModel, bool force);
using ApplicationUpdateFn = void(__fastcall*)(void* controller);
using MainWindowIdleFn = void(__fastcall*)(void* mainWindow);
using MainWindowConfirmDiscardFn = bool(__fastcall*)(void* mainWindow);
using MainWindowRecoveryCheckFn = void(__fastcall*)(void* mainWindow);
using QDialogExecFn = int(__fastcall*)(void* dialog);
using QMessageBoxIconFn = int(__fastcall*)(const void* messageBox);
using MainWindowOpenFileListFn = void*(__fastcall*)(
    void* mainWindow,
    void* result,
    void* parent,
    const void* caption,
    const void* directory,
    const void* filter,
    int options);
using CollisionDoneFn = void(__fastcall*)(void* model);
using CollisionComputeFn = void(__fastcall*)(void* model);
using NativeExportBuildFn = void(__fastcall*)(void* exportBuilder);
using NativeModelUpdateFn = bool(__fastcall*)(void* model, int variation);
using NativeExportFinalizeGeometryFn = void(__fastcall*)(void* exportBuilder, bool separate);
using NativeExportFinalizeDocumentFn = void(__fastcall*)(void* exportBuilder);
using ResolveBranchBoneIdFn = int(__fastcall*)(void* branch, float position, int section);
using InsertExportBoneFn = void(__fastcall*)(
    void* exportData,
    void* sourceBoneRecord,
    void* sourceBranch);
using ExportVertexWeightsFn = void(__fastcall*)(
    void* exporter,
    const float* position,
    int sourceBoneId,
    int vertexIndex,
    void* clusterMap);
using FindExportBoneMappingFn = void*(__fastcall*)(void* boneMap, const int* boneId);
using FbxClusterAddControlPointFn = void(__fastcall*)(
    void* cluster,
    int vertexIndex,
    double weight);
using FbxClusterAppendIndexFn = void*(__fastcall*)(void* array, const int* value);
using FbxClusterAppendWeightFn = void*(__fastcall*)(void* array, const double* value);
using FbxNodeCreateFn = void*(__fastcall*)(void* manager, const char* name);
using TreeDocumentPrepareFn = void(__fastcall*)(void* treeDocument);
using TreeDocumentModelStageFn = void(__fastcall*)(void* modelInterface);
using ScheduleCollisionFn = void(__fastcall*)(void* model, bool force);
using QCoreApplicationInstanceFn = void*(__cdecl*)();

struct QGenericArgumentCompat {
    const void* data;
    const char* name;
};

constexpr unsigned char kQCoreNotifyInternalPrologue[12] = {
    0x48, 0x89, 0x5c, 0x24, 0x18,
    0x55, 0x56, 0x57,
    0x48, 0x83, 0xec, 0x50,
};

struct QStringStorage {
    alignas(void*) unsigned char bytes[24];
};

struct QEventStorage {
    alignas(16) unsigned char bytes[64];
};

using QStringCtorFn = void*(__fastcall*)(
    void* storage,
    const std::uint16_t* characters,
    std::ptrdiff_t length);
using QEventCtorFn = void*(__fastcall*)(void* storage, int eventType);
using PostEventFn = void(__cdecl*)(void* receiver, void* event, int priority);
using NotifyInternalFn = bool(__cdecl*)(void* receiver, void* event);
using QArrayDataAllocateFn = void*(__cdecl*)(
    void** allocationHeader,
    std::ptrdiff_t objectSize,
    std::ptrdiff_t alignment,
    std::ptrdiff_t capacity,
    int option);
using QMdiAreaSetActiveSubWindowFn = void(__fastcall*)(void* mdiArea, void* subWindow);
using QMdiAreaActiveSubWindowFn = void*(__fastcall*)(const void* mdiArea);
using QMetaInvokeFn = bool(__cdecl*)(
    void* object,
    const char* member,
    int connectionType,
    QGenericArgumentCompat arg0,
    QGenericArgumentCompat arg1,
    QGenericArgumentCompat arg2,
    QGenericArgumentCompat arg3,
    QGenericArgumentCompat arg4,
    QGenericArgumentCompat arg5,
    QGenericArgumentCompat arg6,
    QGenericArgumentCompat arg7,
    QGenericArgumentCompat arg8,
    QGenericArgumentCompat arg9);

struct QtPointerListView {
    void* allocationHeader;
    void** items;
    std::ptrdiff_t size;
};

using QApplicationAllWidgetsFn = QtPointerListView(__cdecl*)();
using QMdiAreaSubWindowListFn = void*(__fastcall*)(
    const void* mdiArea,
    QtPointerListView* result,
    int order);

struct HookRecord {
    void* target = nullptr;
    void* trampoline = nullptr;
    unsigned char original[32]{};
    std::size_t originalBytes = 0;
    bool installed = false;
};

struct NativeStateProbe {
    std::uintptr_t rva;
    const char* label;
    unsigned char original = 0;
    bool armed = false;
};

#pragma pack(push, 1)
struct RttiCompleteObjectLocator64 {
    std::uint32_t signature;
    std::uint32_t offset;
    std::uint32_t constructorDisplacementOffset;
    std::int32_t typeDescriptorRva;
    std::int32_t classDescriptorRva;
    std::int32_t selfRva;
};
#pragma pack(pop)

std::atomic<void*> gCollisionThread{nullptr};
QThreadStartFn gOriginalQThreadStart = nullptr;
SpeedTreeExportFn gOriginalSpeedTreeExport = nullptr;
NativeExportBuildFn gOriginalNativeExportBuild = nullptr;
NativeModelUpdateFn gOriginalNativeModelUpdate = nullptr;
NativeExportFinalizeGeometryFn gOriginalNativeExportFinalizeGeometry = nullptr;
NativeExportFinalizeDocumentFn gOriginalNativeExportFinalizeDocument = nullptr;
ResolveBranchBoneIdFn gResolveBranchBoneId = nullptr;
InsertExportBoneFn gOriginalInsertExportBone = nullptr;
ExportVertexWeightsFn gOriginalExportVertexWeights = nullptr;
FindExportBoneMappingFn gFindExportBoneMapping = nullptr;
FbxClusterAddControlPointFn gUnusedOriginalFbxClusterAddControlPoint = nullptr;
FbxClusterAppendIndexFn gFbxClusterAppendIndex = nullptr;
FbxClusterAppendWeightFn gFbxClusterAppendWeight = nullptr;
FbxNodeCreateFn gOriginalFbxNodeCreate = nullptr;
void* gCurrentExportSourceVertexRecord = nullptr;
void* gCurrentExportGeometry = nullptr;
void* gExportVertexWeightsEntryStub = nullptr;
SpeedTreeExportFn gNativeSpeedTreeExport = nullptr;
MainWindowIdleFn gOriginalMainWindowOnIdle = nullptr;
MainWindowIdleFn gOriginalMainWindowOnIdleDraw = nullptr;
QThreadWaitFn gQThreadWait = nullptr;
QThreadIsRunningFn gQThreadIsRunning = nullptr;
ProcessEventsFn gProcessEvents = nullptr;
SendPostedEventsFn gSendPostedEvents = nullptr;
QWidgetFindFn gQWidgetFind = nullptr;
QObjectChildrenFn gQObjectChildren = nullptr;
QObjectInheritsFn gQObjectInherits = nullptr;
QObjectParentFn gQObjectParent = nullptr;
MarkCollisionDirtyFn gMarkCollisionDirty = nullptr;
CollisionDoneFn gCollisionDone = nullptr;
CollisionComputeFn gCollisionCompute = nullptr;
MainWindowIdleFn gMainWindowOnIdle = nullptr;
MainWindowIdleFn gMainWindowOnIdleDraw = nullptr;
MainWindowConfirmDiscardFn gOriginalMainWindowConfirmDiscard = nullptr;
MainWindowRecoveryCheckFn gOriginalMainWindowRecoveryCheck = nullptr;
QDialogExecFn gOriginalQDialogExec = nullptr;
QMessageBoxIconFn gQMessageBoxIcon = nullptr;
MainWindowOpenFileListFn gOriginalMainWindowOpenFileList = nullptr;
std::atomic<void*> gCollisionModel{nullptr};
std::atomic<bool> gSynchronousCollisionCompleted{false};
std::atomic<unsigned int> gCollisionStartCount{0};
std::atomic<bool> gGuiExportStarted{false};
std::atomic<bool> gNativeCliExportActive{false};
std::atomic<bool> gSecondaryNativeSerializationActive{false};
bool gFbxDeformRootPatchInstalled = false;
std::atomic<unsigned char*> gNativeMainWindow{nullptr};
std::atomic<unsigned int> gGuiModelUpdateCount{0};
std::atomic<bool> gGuiBakeRequested{false};
QCoreApplicationInstanceFn gQCoreApplicationInstance = nullptr;
QApplicationAllWidgetsFn gQApplicationAllWidgets = nullptr;
QStringCtorFn gQStringCtor = nullptr;
QEventCtorFn gQEventCtor = nullptr;
PostEventFn gPostEvent = nullptr;
QMetaInvokeFn gQMetaInvoke = nullptr;
NotifyInternalFn gOriginalNotifyInternal = nullptr;
QArrayDataAllocateFn gQArrayDataAllocate = nullptr;
QMdiAreaSetActiveSubWindowFn gQMdiAreaSetActiveSubWindow = nullptr;
QMdiAreaActiveSubWindowFn gQMdiAreaActiveSubWindow = nullptr;
QMdiAreaSubWindowListFn gQMdiAreaSubWindowList = nullptr;
HookRecord gQThreadStartHook;
HookRecord gSpeedTreeExportHook;
HookRecord gNativeExportBuildHook;
HookRecord gNativeModelUpdateHook;
HookRecord gNativeExportFinalizeGeometryHook;
HookRecord gNativeExportFinalizeDocumentHook;
HookRecord gInsertExportBoneHook;
HookRecord gExportVertexWeightsHook;
HookRecord gFbxClusterAddControlPointHook;
HookRecord gFbxNodeCreateHook;
HookRecord gMainWindowOnIdleHook;
HookRecord gMainWindowOnIdleDrawHook;
HookRecord gNotifyInternalHook;
HookRecord gMainWindowConfirmDiscardHook;
HookRecord gMainWindowRecoveryCheckHook;
HookRecord gQDialogExecHook;
HookRecord gMainWindowOpenFileListHook;

struct FbxWeightAddCapture {
    void* cluster = nullptr;
    int vertexIndex = -1;
    double weight = 0.0;
};

struct FbxWeightExportContext {
    FbxWeightAddCapture additions[4]{};
    int additionCount = 0;
    bool overrideWeight = false;
    bool suppressWrite = false;
    double replacementWeight = 0.0;
};

thread_local FbxWeightExportContext* gFbxWeightExportContext = nullptr;

struct NativeReceiptInfluence {
    int boneId = 0;
    std::string mappingNode;
    std::string exportedClusterName;
    double weight = 0.0;
};

struct NativeReceiptBone {
    int boneId = 0;
    int parentId = 0;
    float start[3]{};
    float end[3]{};
    std::string sourceType;
};

struct NativeReceiptRange {
    int firstVertex = -1;
    int lastVertex = -1;
};

struct NativeReceiptGeometry {
    void* geometry = nullptr;
    int maximumVertexIndex = -1;
};

struct NativeReceiptProxyKey {
    int geometryOrdinal = -1;
    int sourceBoneId = 0;
    int recordType = 0;
    int instanceId = 0;
    std::uintptr_t sourceObject = 0;

    bool operator==(const NativeReceiptProxyKey& other) const noexcept {
        return geometryOrdinal == other.geometryOrdinal &&
            sourceBoneId == other.sourceBoneId &&
            recordType == other.recordType &&
            instanceId == other.instanceId &&
            sourceObject == other.sourceObject;
    }
};

struct NativeReceiptProxyKeyHash {
    std::size_t operator()(const NativeReceiptProxyKey& key) const noexcept {
        std::size_t result = 1469598103934665603ull;
        const auto mix = [&result](std::uint32_t value) {
            result ^= static_cast<std::size_t>(value);
            result *= 1099511628211ull;
        };
        mix(static_cast<std::uint32_t>(key.geometryOrdinal));
        mix(static_cast<std::uint32_t>(key.sourceBoneId));
        mix(static_cast<std::uint32_t>(key.recordType));
        mix(static_cast<std::uint32_t>(key.instanceId));
        mix(static_cast<std::uint32_t>(key.sourceObject));
        mix(static_cast<std::uint32_t>(key.sourceObject >> 32));
        return result;
    }
};

struct NativeReceiptProxy {
    NativeReceiptProxyKey key{};
    std::string sourceType;
    std::string nodeGuid;
    std::string parentGuid;
    std::string generatorGuid;
    bool hasAuthoredPosition = false;
    float authoredPositionNative[3]{};
    bool hasAuthoredTangent = false;
    float authoredTangentNativeUnit[3]{};
    std::vector<NativeReceiptInfluence> influences;
    std::vector<NativeReceiptRange> vertexRanges;
};

std::mutex gNativeReceiptMutex;
std::vector<NativeReceiptGeometry> gNativeReceiptGeometries;
std::unordered_map<void*, std::string> gNativeReceiptFbxNodeNames;
std::vector<NativeReceiptBone> gNativeReceiptBones;
std::vector<NativeReceiptProxy> gNativeReceiptProxies;
std::unordered_map<
    NativeReceiptProxyKey,
    std::size_t,
    NativeReceiptProxyKeyHash> gNativeReceiptProxyIndexes;
NativeStateProbe gNativeStateProbes[] = {
    {0x135A59, "native export probe 135A59"},
    {0x135A9D, "native export probe 135A9D"},
    {0x135ABF, "native export probe 135ABF"},
    {0x135D31, "native export probe 135D31"},
    {0x135D95, "native export probe 135D95"},
    {0x135DA6, "native export probe 135DA6"},
    {0xA1F52, "native exporter probe A1F52"},
    {0xA1F62, "native exporter probe A1F62"},
    {0xA2186, "native exporter probe A2186"},
    {0xA3B38, "native exporter probe A3B38"},
    {0xA411A, "native exporter probe A411A"},
    {0xA4C7C, "native exporter probe A4C7C"},
    {0xA4CB0, "native exporter probe A4CB0"},
    {0xA4D67, "native exporter probe A4D67"},
    {0xA4E94, "native exporter probe A4E94"},
    {0xA4F57, "native exporter probe A4F57"},
    {0xA513B, "native exporter probe A513B"},
    {0xA5455, "native exporter probe A5455"},
};
NativeStateProbe gCollisionTreeProbe{
    0x3E0789,
    "collision tree before spatial resolution",
};
NativeStateProbe gCollisionSpatialInputProbe{
    0x3E0813,
    "collision spatial input",
};
PVOID gNativeStateProbeHandler = nullptr;
std::atomic<bool> gLoggedNativeAccessViolation{false};
HMODULE gSpeedTreeModule = nullptr;
std::uintptr_t gSpeedTreeBase = 0;
std::size_t gSpeedTreeImageSize = 0;
HWND gHeadlessOpenGlWindow = nullptr;
HDC gHeadlessOpenGlDc = nullptr;
HGLRC gHeadlessOpenGlContext = nullptr;
wchar_t gLogPath[32768]{};
wchar_t gGuiExportPath[32768]{};
wchar_t gGuiExportOptionsPath[32768]{};
wchar_t gSecondaryExportPath[32768]{};
wchar_t gSecondaryExportOptionsPath[32768]{};
wchar_t gNativeInputPath[32768]{};
wchar_t gNativeReceiptPath[32768]{};
wchar_t gPersistentInputPath[32768]{};
DWORD gTimeoutMs = kDefaultTimeoutMs;
ULONGLONG gHookStartTick = 0;
bool gGuiBakeMode = false;
bool gGuiGameExport = false;
bool gSessionServerMode = false;
bool gVerificationOnly = false;
bool gRlmConnectFailFast = false;
bool gRlmConnectFailFastPatchInstalled = false;
std::atomic<bool> gSessionJobActive{false};
std::atomic<bool> gSessionJobComplete{false};
std::atomic<DWORD> gSessionJobStatus{ERROR_SUCCESS};
std::atomic<void*> gSessionMainWindow{nullptr};
std::atomic<bool> gSessionOpenPathArmed{false};
std::atomic<bool> gSessionForceDiscardClose{false};
std::atomic<bool> gSessionClosingTarget{false};
std::atomic<bool> gSessionOpenCallActive{false};
void* gSessionAnchorMdiSubWindow = nullptr;
void* gSessionTargetTreeWindow = nullptr;
void* gSessionTargetMdiSubWindow = nullptr;
bool gSessionTargetStateLogged = false;

enum class PersistentJobPhase {
    WaitForAnchor,
    OpenTarget,
    WaitForTarget,
    Ready,
    Complete,
};

PersistentJobPhase gPersistentJobPhase = PersistentJobPhase::Complete;

void Log(const char* message) {
    if (gLogPath[0] == L'\0') {
        OutputDebugStringA(message);
        OutputDebugStringA("\n");
        return;
    }

    HANDLE file = CreateFileW(
        gLogPath,
        FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return;
    }

    DWORD written = 0;
    WriteFile(file, message, static_cast<DWORD>(std::strlen(message)), &written, nullptr);
    static constexpr char newline[] = "\r\n";
    WriteFile(file, newline, 2, &written, nullptr);
    CloseHandle(file);
}

void LogPointer(const char* prefix, const void* pointer) {
    char buffer[256]{};
    _snprintf_s(buffer, sizeof(buffer), _TRUNCATE, "%s%p", prefix, pointer);
    Log(buffer);
}

void LogSpeedTreeCallStack(const char* phase) {
    void* frames[32]{};
    const USHORT count = CaptureStackBackTrace(0, 32, frames, nullptr);
    char header[160]{};
    _snprintf_s(
        header,
        sizeof(header),
        _TRUNCATE,
        "%s: %u captured frames",
        phase,
        static_cast<unsigned int>(count));
    Log(header);
    for (USHORT index = 0; index < count; ++index) {
        const auto address = reinterpret_cast<std::uintptr_t>(frames[index]);
        if (address < gSpeedTreeBase ||
            address >= gSpeedTreeBase + gSpeedTreeImageSize) {
            continue;
        }
        char row[160]{};
        _snprintf_s(
            row,
            sizeof(row),
            _TRUNCATE,
            "SpeedTree stack[%u] RVA=0x%llX",
            static_cast<unsigned int>(index),
            static_cast<unsigned long long>(address - gSpeedTreeBase));
        Log(row);
    }
}

bool IsInSpeedTreeImage(const void* address, std::size_t bytes) {
    const auto value = reinterpret_cast<std::uintptr_t>(address);
    if (value < gSpeedTreeBase || bytes > gSpeedTreeImageSize) {
        return false;
    }
    return value - gSpeedTreeBase <= gSpeedTreeImageSize - bytes;
}

bool IsCollisionThread(const void* object) {
    if (object == nullptr || gSpeedTreeBase == 0 || gSpeedTreeImageSize == 0) {
        return false;
    }

    __try {
        const auto vtable = *reinterpret_cast<void* const* const*>(object);
        if (vtable == nullptr || !IsInSpeedTreeImage(vtable - 1, sizeof(void*))) {
            return false;
        }

        const auto locator = reinterpret_cast<const RttiCompleteObjectLocator64*>(vtable[-1]);
        if (!IsInSpeedTreeImage(locator, sizeof(*locator)) || locator->signature != 1) {
            return false;
        }

        const auto typeDescriptor = reinterpret_cast<const unsigned char*>(
            gSpeedTreeBase + static_cast<std::uint32_t>(locator->typeDescriptorRva));
        constexpr std::size_t kTypeDescriptorHeaderBytes = sizeof(void*) * 2;
        if (!IsInSpeedTreeImage(typeDescriptor, kTypeDescriptorHeaderBytes + sizeof(kCollisionThreadRttiName))) {
            return false;
        }

        const char* name = reinterpret_cast<const char*>(typeDescriptor + kTypeDescriptorHeaderBytes);
        return std::strcmp(name, kCollisionThreadRttiName) == 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

[[noreturn]] void AbortExport(DWORD exitCode, const char* reason);
void WriteAbsoluteJump(unsigned char* destination, const void* target);

const char* ReadSpeedTreeRttiName(const void* object) {
    if (object == nullptr || gSpeedTreeBase == 0 || gSpeedTreeImageSize == 0) {
        return nullptr;
    }
    __try {
        const auto vtable = *reinterpret_cast<void* const* const*>(object);
        if (vtable == nullptr || !IsInSpeedTreeImage(vtable - 1, sizeof(void*))) {
            return nullptr;
        }
        const auto locator = reinterpret_cast<const RttiCompleteObjectLocator64*>(vtable[-1]);
        if (!IsInSpeedTreeImage(locator, sizeof(*locator)) || locator->signature != 1) {
            return nullptr;
        }
        const auto typeDescriptor = reinterpret_cast<const unsigned char*>(
            gSpeedTreeBase + static_cast<std::uint32_t>(locator->typeDescriptorRva));
        constexpr std::size_t kTypeDescriptorHeaderBytes = sizeof(void*) * 2;
        if (!IsInSpeedTreeImage(typeDescriptor, kTypeDescriptorHeaderBytes + 2)) {
            return nullptr;
        }
        return reinterpret_cast<const char*>(typeDescriptor + kTypeDescriptorHeaderBytes);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return nullptr;
    }
}

struct ExportBoneMapping {
    void* boneRecord;
    void* startNode;
    void* endNode;
};

std::atomic_bool gMissingIdZeroBoneRecordLogged{false};

ExportBoneMapping* FindExactExportBoneMapping(void* exporter, int boneId) {
    if (exporter == nullptr) {
        return nullptr;
    }
    int lookupId = boneId;
    auto* mapping = static_cast<ExportBoneMapping*>(gFindExportBoneMapping(
        static_cast<unsigned char*>(exporter) + 0x60,
        &lookupId));
    return mapping != nullptr && mapping->boneRecord != nullptr
        ? mapping
        : nullptr;
}

void LogMissingIdZeroBoneRecordOnce() {
    bool expected = false;
    if (gMissingIdZeroBoneRecordLogged.compare_exchange_strong(
            expected,
            true,
            std::memory_order_acq_rel)) {
        Log(
            "Synthetic complementary FBX ID-0 cluster write skipped because "
            "this export has no exact ID-0 bone record");
    }
}

void __fastcall HookedFbxClusterAddControlPoint(
    void* cluster,
    int vertexIndex,
    double weight) {
    if (vertexIndex < 0) {
        return;
    }
    FbxWeightExportContext* context = gFbxWeightExportContext;
    const double effectiveWeight =
        context != nullptr && context->overrideWeight
        ? context->replacementWeight
        : weight;
    if (!(effectiveWeight > 0.0) || !std::isfinite(effectiveWeight)) {
        return;
    }
    if (context == nullptr || !context->suppressWrite) {
        int storedIndex = vertexIndex;
        double storedWeight = effectiveWeight;
        gFbxClusterAppendIndex(
            static_cast<unsigned char*>(cluster) + 0xA8,
            &storedIndex);
        gFbxClusterAppendWeight(
            static_cast<unsigned char*>(cluster) + 0xB0,
            &storedWeight);
    }
    if (context != nullptr) {
        if (context->additionCount < static_cast<int>(std::size(context->additions))) {
            context->additions[context->additionCount] = {
                cluster,
                vertexIndex,
                effectiveWeight,
            };
        }
        ++context->additionCount;
    }
}

void* __fastcall HookedFbxNodeCreate(void* manager, const char* name) {
    void* node = gOriginalFbxNodeCreate(manager, name);
    if (node != nullptr && name != nullptr && name[0] != '\0' &&
        gNativeReceiptPath[0] != L'\0' &&
        !gSecondaryNativeSerializationActive.load(std::memory_order_acquire)) {
        std::lock_guard<std::mutex> lock(gNativeReceiptMutex);
        gNativeReceiptFbxNodeNames[node] = name;
    }
    return node;
}

void ResetNativeReceiptCapture() {
    std::lock_guard<std::mutex> lock(gNativeReceiptMutex);
    gMissingIdZeroBoneRecordLogged.store(false, std::memory_order_release);
    gNativeReceiptGeometries.clear();
    gNativeReceiptBones.clear();
    gNativeReceiptProxies.clear();
    gNativeReceiptProxyIndexes.clear();
}

int NativeReceiptGeometryOrdinal(void* geometry, int vertexIndex) {
    const auto found = std::find_if(
        gNativeReceiptGeometries.begin(),
        gNativeReceiptGeometries.end(),
        [geometry](const NativeReceiptGeometry& row) {
            return row.geometry == geometry;
        });
    if (found != gNativeReceiptGeometries.end()) {
        found->maximumVertexIndex = (std::max)(
            found->maximumVertexIndex,
            vertexIndex);
        return static_cast<int>(found - gNativeReceiptGeometries.begin());
    }
    gNativeReceiptGeometries.push_back({geometry, vertexIndex});
    return static_cast<int>(gNativeReceiptGeometries.size() - 1);
}

std::string Base64Guid(const unsigned char* bytes) {
    static constexpr char alphabet[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string encoded;
    encoded.reserve(24);
    for (std::size_t offset = 0; offset < 15; offset += 3) {
        const std::uint32_t value =
            (static_cast<std::uint32_t>(bytes[offset]) << 16) |
            (static_cast<std::uint32_t>(bytes[offset + 1]) << 8) |
            static_cast<std::uint32_t>(bytes[offset + 2]);
        encoded.push_back(alphabet[(value >> 18) & 0x3F]);
        encoded.push_back(alphabet[(value >> 12) & 0x3F]);
        encoded.push_back(alphabet[(value >> 6) & 0x3F]);
        encoded.push_back(alphabet[value & 0x3F]);
    }
    const std::uint32_t tail = static_cast<std::uint32_t>(bytes[15]) << 16;
    encoded.push_back(alphabet[(tail >> 18) & 0x3F]);
    encoded.push_back(alphabet[(tail >> 12) & 0x3F]);
    encoded += "==";
    return encoded;
}

bool CaptureNativeAuthoredPose(
    void* sourceObject,
    NativeReceiptProxy* proxy) {
    if (sourceObject == nullptr || proxy == nullptr) {
        return false;
    }
    __try {
        auto** vtable = *reinterpret_cast<void***>(sourceObject);
        constexpr std::size_t kPlacementPoseMethod = 0xC58 / sizeof(void*);
        if (vtable == nullptr ||
            !IsInSpeedTreeImage(
                vtable + kPlacementPoseMethod,
                sizeof(void*)) ||
            !IsInSpeedTreeImage(
                vtable[kPlacementPoseMethod],
                sizeof(unsigned char))) {
            return false;
        }
        const auto readPose = reinterpret_cast<
            void(__fastcall*)(void*, float*, float*)>(
                vtable[kPlacementPoseMethod]);
        readPose(
            sourceObject,
            proxy->authoredPositionNative,
            proxy->authoredTangentNativeUnit);
        proxy->hasAuthoredPosition = true;
        proxy->hasAuthoredTangent = true;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

void CaptureNativeReceiptProxy(
    void* exporter,
    const float* /*serializedPosition*/,
    int sourceBoneId,
    int vertexIndex,
    void* clusterMap) {
    if (gNativeReceiptPath[0] == L'\0' ||
        gSecondaryNativeSerializationActive.load(std::memory_order_acquire) ||
        gCurrentExportSourceVertexRecord == nullptr ||
        gCurrentExportGeometry == nullptr) {
        return;
    }

    const auto* record = static_cast<const unsigned char*>(
        gCurrentExportSourceVertexRecord);
    void* sourceObject = *reinterpret_cast<void* const*>(record + 0x110);

    NativeReceiptProxyKey key{};
    key.sourceBoneId = sourceBoneId;
    key.recordType = *reinterpret_cast<const int*>(record + 0x118);
    key.instanceId = *reinterpret_cast<const int*>(record + 0x120);
    key.sourceObject = reinterpret_cast<std::uintptr_t>(sourceObject);

    std::lock_guard<std::mutex> lock(gNativeReceiptMutex);
    key.geometryOrdinal = NativeReceiptGeometryOrdinal(
        gCurrentExportGeometry,
        vertexIndex);
    const auto existing = gNativeReceiptProxyIndexes.find(key);
    std::size_t proxyIndex = 0;
    if (existing == gNativeReceiptProxyIndexes.end()) {
        NativeReceiptProxy proxy{};
        proxy.key = key;
        const char* sourceType = ReadSpeedTreeRttiName(sourceObject);
        if (sourceType != nullptr) {
            proxy.sourceType = sourceType;
        }
        if (sourceObject != nullptr && sourceType != nullptr &&
            std::strstr(sourceType, "Node@@") != nullptr &&
            exporter != nullptr && clusterMap != nullptr) {
            const auto* sourceBytes = static_cast<const unsigned char*>(
                sourceObject);
            proxy.nodeGuid = Base64Guid(sourceBytes + 0x10);
            void* parentObject = *reinterpret_cast<void* const*>(
                sourceBytes + 0x98);
            if (ReadSpeedTreeRttiName(parentObject) != nullptr) {
                proxy.parentGuid = Base64Guid(
                    static_cast<const unsigned char*>(parentObject) + 0x10);
            }
            void* generatorObject = *reinterpret_cast<void* const*>(
                sourceBytes + 0x128);
            if (ReadSpeedTreeRttiName(generatorObject) != nullptr) {
                proxy.generatorGuid = Base64Guid(
                    static_cast<const unsigned char*>(generatorObject) + 0x10);
            }
            CaptureNativeAuthoredPose(sourceObject, &proxy);

            const auto* geometryBytes = static_cast<const unsigned char*>(
                gCurrentExportGeometry);
            const float authoredSolver[3] = {
                (proxy.authoredPositionNative[0] -
                    *reinterpret_cast<const float*>(geometryBytes + 0x2C)) * 30.48f,
                (proxy.authoredPositionNative[1] -
                    *reinterpret_cast<const float*>(geometryBytes + 0x30)) * 30.48f,
                (proxy.authoredPositionNative[2] -
                    *reinterpret_cast<const float*>(geometryBytes + 0x34)) * 30.48f,
            };
            int parentId = 0;
            auto* mapping = FindExactExportBoneMapping(exporter, sourceBoneId);
            if (mapping != nullptr) {
                parentId = *reinterpret_cast<const int*>(
                    static_cast<const unsigned char*>(mapping->boneRecord) + 0x04);
            }
            const auto exportedNodeName = [exporter](int boneId) {
                int requestedId = boneId;
                auto* requested = static_cast<ExportBoneMapping*>(
                    gFindExportBoneMapping(
                        static_cast<unsigned char*>(exporter) + 0x60,
                        &requestedId));
                if (requested == nullptr || requested->startNode == nullptr) {
                    return std::string{};
                }
                const auto found = gNativeReceiptFbxNodeNames.find(
                    requested->startNode);
                return found == gNativeReceiptFbxNodeNames.end()
                    ? std::string{}
                    : found->second;
            };

            FbxWeightExportContext probe{};
            if (sourceBoneId == 0) {
                // The serializer's exact source ID is the authored root.  The
                // entry stub preserves and executes the stock call unchanged;
                // this receipt row records that same explicit ownership.
                probe.additionCount = 1;
                probe.additions[0] = {
                    nullptr,
                    vertexIndex,
                    1.0,
                };
            } else {
                probe.suppressWrite = true;
                FbxWeightExportContext* previousContext = gFbxWeightExportContext;
                gFbxWeightExportContext = &probe;
                gOriginalExportVertexWeights(
                    exporter,
                    authoredSolver,
                    sourceBoneId,
                    vertexIndex,
                    clusterMap);
                gFbxWeightExportContext = previousContext;
            }
            if (probe.additionCount > 0) {
                proxy.influences.push_back({
                    sourceBoneId,
                    "start",
                    exportedNodeName(sourceBoneId),
                    probe.additions[0].weight,
                });
            }
            if (probe.additionCount > 1) {
                proxy.influences.push_back({
                    parentId,
                    "start",
                    exportedNodeName(parentId),
                    probe.additions[1].weight,
                });
            } else if (sourceBoneId > 0 && parentId == 0 &&
                       probe.additionCount == 1) {
                const float childWeight = static_cast<float>(
                    probe.additions[0].weight);
                const float rootWeight = 1.0f - childWeight;
                if (rootWeight > 0.0f) {
                    proxy.influences.push_back({
                        0,
                        "start",
                        exportedNodeName(0),
                        static_cast<double>(rootWeight),
                    });
                }
            }
        }
        proxyIndex = gNativeReceiptProxies.size();
        gNativeReceiptProxies.push_back(std::move(proxy));
        gNativeReceiptProxyIndexes.emplace(key, proxyIndex);
    } else {
        proxyIndex = existing->second;
    }

    auto& ranges = gNativeReceiptProxies[proxyIndex].vertexRanges;
    if (!ranges.empty() && ranges.back().lastVertex + 1 == vertexIndex) {
        ranges.back().lastVertex = vertexIndex;
    } else {
        ranges.push_back({vertexIndex, vertexIndex});
    }
}

void CaptureNativeReceiptBone(const void* sourceBoneRecord, const void* sourceBranch) {
    if (gNativeReceiptPath[0] == L'\0' ||
        gSecondaryNativeSerializationActive.load(std::memory_order_acquire) ||
        sourceBoneRecord == nullptr) {
        return;
    }
    const auto* bytes = static_cast<const unsigned char*>(sourceBoneRecord);
    NativeReceiptBone row{};
    row.boneId = *reinterpret_cast<const int*>(bytes);
    row.parentId = *reinterpret_cast<const int*>(bytes + 0x04);
    std::memcpy(row.start, bytes + 0x08, sizeof(row.start));
    std::memcpy(row.end, bytes + 0x14, sizeof(row.end));
    const char* sourceType = ReadSpeedTreeRttiName(sourceBranch);
    if (sourceType != nullptr) {
        row.sourceType = sourceType;
    }
    std::lock_guard<std::mutex> lock(gNativeReceiptMutex);
    const auto duplicate = std::find_if(
        gNativeReceiptBones.begin(),
        gNativeReceiptBones.end(),
        [&row](const NativeReceiptBone& existing) {
            return existing.boneId == row.boneId;
        });
    if (duplicate == gNativeReceiptBones.end()) {
        gNativeReceiptBones.push_back(std::move(row));
    }
}

void __fastcall HookedExportVertexWeights(
    void* exporter,
    const float* position,
    int sourceBoneId,
    int vertexIndex,
    void* clusterMap) {
    // Always preserve the stock call, including native sourceBoneId==0 calls.
    // A valid no-generator model can use that implicit native path even though
    // the export-bone map has no exact ID-0 record.  Only the later *synthetic*
    // complementary Root call needs the exact-record guard.
    FbxWeightExportContext primary{};
    FbxWeightExportContext* previousContext = gFbxWeightExportContext;
    gFbxWeightExportContext = &primary;
    gOriginalExportVertexWeights(
        exporter,
        position,
        sourceBoneId,
        vertexIndex,
        clusterMap);
    gFbxWeightExportContext = previousContext;

    CaptureNativeReceiptProxy(
        exporter,
        position,
        sourceBoneId,
        vertexIndex,
        clusterMap);
    if (sourceBoneId <= 0 || exporter == nullptr || position == nullptr ||
        clusterMap == nullptr) {
        return;
    }

    __try {
        int lookupId = sourceBoneId;
        auto* mapping = static_cast<ExportBoneMapping*>(gFindExportBoneMapping(
            static_cast<unsigned char*>(exporter) + 0x60,
            &lookupId));
        if (mapping == nullptr || mapping->boneRecord == nullptr) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "FBX weight export could not find the exact source bone record");
        }
        const auto* recordBytes =
            static_cast<const unsigned char*>(mapping->boneRecord);
        const int mappedId = *reinterpret_cast<const int*>(recordBytes);
        const int parentId = *reinterpret_cast<const int*>(recordBytes + 0x04);
        if (mappedId != sourceBoneId) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "FBX weight export resolved a mismatched source bone record");
        }
        if (parentId != 0) {
            return;
        }

        // The 10.1 serializer computes the exact child weight, then takes an
        // early return when that child's parsed parent ID is zero. Mirror its
        // existing nonzero-parent branch: the complementary float belongs to
        // the explicit ID-0 Root cluster. No spatial lookup or normalization
        // is involved.
        if (primary.additionCount != 1 ||
            primary.additions[0].vertexIndex != vertexIndex) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "FBX ID-0 influence restoration observed an unexpected native cluster sequence");
        }
        const float childWeight = static_cast<float>(primary.additions[0].weight);
        if (!(childWeight > 0.0f) || childWeight > 1.0f) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "FBX ID-0 influence restoration observed an invalid native child weight");
        }
        const float rootWeight = 1.0f - childWeight;
        if (rootWeight <= 0.0f) {
            return;
        }
        // This call is manufactured by the hook, unlike the native call above.
        // Without an exact ID-0 destination record it was observed to crash at
        // SpeedTree_Modeler+0x6B5185, so guard only this complementary write.
        if (FindExactExportBoneMapping(exporter, 0) == nullptr) {
            LogMissingIdZeroBoneRecordOnce();
            return;
        }

        FbxWeightExportContext root{};
        root.overrideWeight = true;
        root.replacementWeight = static_cast<double>(rootWeight);
        gFbxWeightExportContext = &root;
        gOriginalExportVertexWeights(
            exporter,
            position,
            0,
            vertexIndex,
            clusterMap);
        gFbxWeightExportContext = previousContext;
        if (root.additionCount != 1 ||
            root.additions[0].vertexIndex != vertexIndex) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "FBX ID-0 influence restoration could not emit the native Root cluster entry");
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        gFbxWeightExportContext = previousContext;
        AbortExport(
            kHookRuntimeFailureExitCode,
            "FBX ID-0 influence restoration raised an exception while reading exporter data");
    }
}

void __fastcall CaptureNativeReceiptIdZero(
    void* exporter,
    const float* position,
    int sourceBoneId,
    int vertexIndex,
    void* clusterMap);

bool BuildExportVertexWeightsEntryStub() {
    if (gExportVertexWeightsEntryStub != nullptr) {
        return true;
    }
    auto* stub = static_cast<unsigned char*>(VirtualAlloc(
        nullptr,
        192,
        MEM_RESERVE | MEM_COMMIT,
        PAGE_EXECUTE_READWRITE));
    if (stub == nullptr) {
        Log("FBX vertex-record entry stub allocation failed");
        return false;
    }
    // Preserve the caller's RBX vertex-record pointer before entering compiled
    // C++. ExportVertexWeights has no formal source-record parameter, even
    // though its only FBX caller keeps that exact record in nonvolatile RBX.
    stub[0] = 0x48;
    stub[1] = 0xB8;
    *reinterpret_cast<std::uintptr_t*>(stub + 2) =
        reinterpret_cast<std::uintptr_t>(&gCurrentExportSourceVertexRecord);
    stub[10] = 0x48;
    stub[11] = 0x89;
    stub[12] = 0x18;
    stub[13] = 0x48;
    stub[14] = 0xB8;
    *reinterpret_cast<std::uintptr_t*>(stub + 15) =
        reinterpret_cast<std::uintptr_t>(&gCurrentExportGeometry);
    stub[23] = 0x48;
    stub[24] = 0x89;
    stub[25] = 0x38;
    // Native ID-0 is an implicit serializer path. Capture its exact serializer
    // record while preserving the caller's volatile state, then tail-jump to
    // the stock routine with the original stack and registers. Only positive
    // IDs enter the compiled weight hook.
    stub[26] = 0x45;
    stub[27] = 0x85;
    stub[28] = 0xC0;             // test r8d, r8d
    stub[29] = 0x75;
    stub[30] = 0x00;             // jne displacement filled below
    std::size_t cursor = 31;
    stub[cursor++] = 0x9C;       // pushfq
    stub[cursor++] = 0x50;       // push rax
    stub[cursor++] = 0x51;       // push rcx
    stub[cursor++] = 0x52;       // push rdx
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x50;       // push r8
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x51;       // push r9
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x52;       // push r10
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x53;       // push r11
    stub[cursor++] = 0x48;
    stub[cursor++] = 0x83;
    stub[cursor++] = 0xEC;
    stub[cursor++] = 0x28;       // sub rsp, 0x28
    stub[cursor++] = 0x48;
    stub[cursor++] = 0x8B;
    stub[cursor++] = 0x84;
    stub[cursor++] = 0x24;
    *reinterpret_cast<std::uint32_t*>(stub + cursor) = 0x90;
    cursor += sizeof(std::uint32_t); // mov rax, [rsp+0x90]
    stub[cursor++] = 0x48;
    stub[cursor++] = 0x89;
    stub[cursor++] = 0x44;
    stub[cursor++] = 0x24;
    stub[cursor++] = 0x20;       // mov [rsp+0x20], rax
    stub[cursor++] = 0x48;
    stub[cursor++] = 0xB8;
    *reinterpret_cast<std::uintptr_t*>(stub + cursor) =
        reinterpret_cast<std::uintptr_t>(CaptureNativeReceiptIdZero);
    cursor += sizeof(std::uintptr_t);
    stub[cursor++] = 0xFF;
    stub[cursor++] = 0xD0;       // call rax
    stub[cursor++] = 0x48;
    stub[cursor++] = 0x83;
    stub[cursor++] = 0xC4;
    stub[cursor++] = 0x28;       // add rsp, 0x28
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x5B;       // pop r11
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x5A;       // pop r10
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x59;       // pop r9
    stub[cursor++] = 0x41;
    stub[cursor++] = 0x58;       // pop r8
    stub[cursor++] = 0x5A;       // pop rdx
    stub[cursor++] = 0x59;       // pop rcx
    stub[cursor++] = 0x58;       // pop rax
    stub[cursor++] = 0x9D;       // popfq
    stub[cursor++] = 0x48;
    stub[cursor++] = 0xB8;
    *reinterpret_cast<std::uintptr_t*>(stub + cursor) =
        reinterpret_cast<std::uintptr_t>(&gOriginalExportVertexWeights);
    cursor += sizeof(std::uintptr_t);
    stub[cursor++] = 0xFF;
    stub[cursor++] = 0x20;       // jmp qword ptr [rax]
    const std::size_t positiveHookOffset = cursor;
    const std::size_t positiveBranchDistance = positiveHookOffset - 31;
    if (positiveBranchDistance > 0x7F) {
        VirtualFree(stub, 0, MEM_RELEASE);
        Log("FBX vertex-record entry stub positive branch exceeded rel8");
        return false;
    }
    stub[30] = static_cast<unsigned char>(positiveBranchDistance);
    WriteAbsoluteJump(
        stub + positiveHookOffset,
        reinterpret_cast<const void*>(HookedExportVertexWeights));
    FlushInstructionCache(GetCurrentProcess(), stub, positiveHookOffset + 12);
    gExportVertexWeightsEntryStub = stub;
    return true;
}

void __fastcall CaptureNativeReceiptIdZero(
    void* exporter,
    const float* position,
    int sourceBoneId,
    int vertexIndex,
    void* clusterMap) {
    CaptureNativeReceiptProxy(
        exporter,
        position,
        sourceBoneId,
        vertexIndex,
        clusterMap);
}

void FreeExportVertexWeightsEntryStub() {
    if (gExportVertexWeightsEntryStub != nullptr) {
        VirtualFree(gExportVertexWeightsEntryStub, 0, MEM_RELEASE);
        gExportVertexWeightsEntryStub = nullptr;
    }
    gCurrentExportSourceVertexRecord = nullptr;
    gCurrentExportGeometry = nullptr;
}

void __fastcall HookedInsertExportBone(
    void* exportData,
    void* sourceBoneRecord,
    void* sourceBranch) {
    if (sourceBoneRecord != nullptr && sourceBranch != nullptr) {
        __try {
            auto* recordBytes = static_cast<unsigned char*>(sourceBoneRecord);
            const int boneId = *reinterpret_cast<const int*>(recordBytes);
            int& parentId = *reinterpret_cast<int*>(recordBytes + 0x04);
            if (boneId != 1 && parentId == 0) {
                const char* sourceType = ReadSpeedTreeRttiName(sourceBranch);
                if (sourceType != nullptr &&
                    std::strcmp(sourceType, ".?AVCBranchNode@@") == 0) {
                    const auto* sourceBytes =
                        static_cast<const unsigned char*>(sourceBranch);
                    void* baseNode = *reinterpret_cast<void* const*>(
                        sourceBytes + 0x98);
                    const char* baseType = ReadSpeedTreeRttiName(baseNode);
                    if (baseType != nullptr &&
                        std::strcmp(baseType, ".?AVCBaseNode@@") == 0) {
                        const auto* baseBytes =
                            static_cast<const unsigned char*>(baseNode);
                        void* targetBranch = *reinterpret_cast<void* const*>(
                            baseBytes + 0x2C8);
                        void* baseRef = *reinterpret_cast<void* const*>(
                            baseBytes + 0x2D0);
                        const char* targetType = ReadSpeedTreeRttiName(targetBranch);
                        const char* baseRefType = ReadSpeedTreeRttiName(baseRef);
                        if (targetType == nullptr ||
                            std::strcmp(targetType, ".?AVCBranchNode@@") != 0 ||
                            baseRefType == nullptr ||
                            std::strcmp(baseRefType, ".?AVCBaseRefNode@@") != 0 ||
                            *reinterpret_cast<void* const*>(
                                static_cast<const unsigned char*>(baseRef) + 0x98) !=
                                targetBranch) {
                            AbortExport(
                                kHookRuntimeFailureExitCode,
                                "BaseRef bone serialization rejected an incomplete parsed reference chain");
                        }

                        const std::int16_t anchorIndex =
                            *reinterpret_cast<const std::int16_t*>(sourceBytes + 0x1B8);
                        float anchorPosition = 0.0f;
                        if (anchorIndex != -1) {
                            const auto* anchorBegin =
                                *reinterpret_cast<const unsigned char* const*>(
                                    sourceBytes + 0x1D8);
                            const auto* anchorEnd =
                                *reinterpret_cast<const unsigned char* const*>(
                                    sourceBytes + 0x1E0);
                            const std::size_t requiredBytes =
                                (static_cast<std::size_t>(anchorIndex) + 1) * 24;
                            if (anchorIndex < 0 || anchorBegin == nullptr ||
                                anchorEnd < anchorBegin ||
                                static_cast<std::size_t>(anchorEnd - anchorBegin) <
                                    requiredBytes) {
                                AbortExport(
                                    kHookRuntimeFailureExitCode,
                                    "BaseRef bone serialization rejected an invalid parsed anchor record");
                            }
                            anchorPosition = *reinterpret_cast<const float*>(
                                anchorBegin + static_cast<std::size_t>(anchorIndex) * 24);
                        }

                        const float branchOffset =
                            *reinterpret_cast<const float*>(sourceBytes + 0x144);
                        const int section =
                            *reinterpret_cast<const int*>(sourceBytes + 0x140);
                        const int resolvedParentId = gResolveBranchBoneId(
                            targetBranch,
                            anchorPosition + branchOffset,
                            section);
                        if (resolvedParentId <= 0 || resolvedParentId == boneId) {
                            AbortExport(
                                kHookRuntimeFailureExitCode,
                                "BaseRef bone serialization could not resolve an exact parent bone ID");
                        }
                        parentId = resolvedParentId;
                        char message[384]{};
                        _snprintf_s(
                            message,
                            sizeof(message),
                            _TRUNCATE,
                            "BaseRef bone graph restored child=%d parent=%d "
                            "target_branch=%p anchor_index=%d position=%.9g section=%d",
                            boneId,
                            resolvedParentId,
                            targetBranch,
                            static_cast<int>(anchorIndex),
                            static_cast<double>(anchorPosition + branchOffset),
                            section);
                        Log(message);
                    }
                }
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "BaseRef bone serialization raised an exception while reading parsed data");
        }
        CaptureNativeReceiptBone(sourceBoneRecord, sourceBranch);
    }
    gOriginalInsertExportBone(exportData, sourceBoneRecord, sourceBranch);
}


void LogCollisionInputTypes(const char* phase, void* model) {
    if (model == nullptr) {
        return;
    }
    __try {
        auto* bytes = static_cast<unsigned char*>(model);
        auto** begin = *reinterpret_cast<void***>(bytes + 0xD8);
        auto** end = *reinterpret_cast<void***>(bytes + 0xE0);
        if (begin == nullptr || end < begin || end - begin > 10000) {
            return;
        }
        for (auto** current = begin; current != end; ++current) {
            void* input = *current;
            const char* typeName = ReadSpeedTreeRttiName(input);
            std::uintptr_t vtableRva = 0;
            if (input != nullptr) {
                const auto vtable = *reinterpret_cast<void* const* const*>(input);
                if (IsInSpeedTreeImage(vtable, sizeof(void*))) {
                    vtableRva = reinterpret_cast<std::uintptr_t>(vtable) - gSpeedTreeBase;
                }
            }
            std::uintptr_t collisionSourceVtableRva = 0;
            std::uintptr_t collisionSourceMethodRva = 0;
            std::uintptr_t generatorDefinitionVtableRva = 0;
            std::uintptr_t generatorDefinitionMethodRva = 0;
            std::size_t childSourceCount = 0;
            std::uintptr_t firstChildSourceVtableRva = 0;
            std::uintptr_t firstChildSourceMethodRva = 0;
            int sourceGeometryMode = -1;
            int sourcePruneMode = -1;
            float sourcePruneAmount = -1.0f;
            unsigned int sourceDisabledFlags = 0;
            unsigned int sourceCachedFlags = 0;
            std::uintptr_t sourceMeshData = 0;
            std::uintptr_t sourceOwnerModel = 0;
            unsigned int sourceOwnerShade = 0;
            int sourceOwnerQuality = -1;
            int sourceInactive = -1;
            int sourceOwnerInactive = -1;
            if (input != nullptr) {
                void* generatorDefinition = *reinterpret_cast<void**>(
                    static_cast<unsigned char*>(input) + 0xC0);
                if (generatorDefinition != nullptr) {
                    const auto definitionVtable =
                        *reinterpret_cast<void* const* const*>(generatorDefinition);
                    if (IsInSpeedTreeImage(definitionVtable, 0xDD8)) {
                        generatorDefinitionVtableRva =
                            reinterpret_cast<std::uintptr_t>(definitionVtable) - gSpeedTreeBase;
                        generatorDefinitionMethodRva =
                            reinterpret_cast<std::uintptr_t>(definitionVtable[0xDD0 / sizeof(void*)]) -
                            gSpeedTreeBase;
                    }
                }
                void* collisionSource = *reinterpret_cast<void**>(
                    static_cast<unsigned char*>(input) + 0x330);
                if (collisionSource != nullptr) {
                    const auto sourceVtable =
                        *reinterpret_cast<void* const* const*>(collisionSource);
                    if (IsInSpeedTreeImage(sourceVtable, 0xA18)) {
                        collisionSourceVtableRva =
                            reinterpret_cast<std::uintptr_t>(sourceVtable) - gSpeedTreeBase;
                        collisionSourceMethodRva =
                            reinterpret_cast<std::uintptr_t>(sourceVtable[0xA10 / sizeof(void*)]) -
                            gSpeedTreeBase;
                    }
                }
                auto** childSources = *reinterpret_cast<void***>(
                    static_cast<unsigned char*>(input) + 0xE0);
                childSourceCount = *reinterpret_cast<unsigned int*>(
                    static_cast<unsigned char*>(input) + 0xF8);
                if (childSources != nullptr && childSourceCount != 0 &&
                    childSourceCount < 100000 && childSources[0] != nullptr) {
                    const auto childVtable =
                        *reinterpret_cast<void* const* const*>(childSources[0]);
                    if (IsInSpeedTreeImage(childVtable, 0xA18)) {
                        firstChildSourceVtableRva =
                            reinterpret_cast<std::uintptr_t>(childVtable) - gSpeedTreeBase;
                        firstChildSourceMethodRva =
                            reinterpret_cast<std::uintptr_t>(childVtable[0xA10 / sizeof(void*)]) -
                            gSpeedTreeBase;
                        auto* childBytes = static_cast<unsigned char*>(childSources[0]);
                        const auto sourceInactiveFn = reinterpret_cast<bool(__fastcall*)(void*)>(
                            childVtable[0x1E0 / sizeof(void*)]);
                        sourceInactive = sourceInactiveFn(childSources[0]) ? 1 : 0;
                        void* ownerGenerator = *reinterpret_cast<void**>(childBytes + 0x128);
                        if (ownerGenerator != nullptr) {
                            const auto ownerVtable =
                                *reinterpret_cast<void* const* const*>(ownerGenerator);
                            const auto ownerInactiveFn = reinterpret_cast<bool(__fastcall*)(void*)>(
                                ownerVtable[0x1E0 / sizeof(void*)]);
                            sourceOwnerInactive = ownerInactiveFn(ownerGenerator) ? 1 : 0;
                            void* ownerModel = *reinterpret_cast<void**>(
                                static_cast<unsigned char*>(ownerGenerator) + 0xB8);
                            if (ownerModel != nullptr) {
                                sourceOwnerModel = reinterpret_cast<std::uintptr_t>(ownerModel);
                                auto* ownerBytes = static_cast<unsigned char*>(ownerModel);
                                sourceOwnerShade = ownerBytes[0x9BDC];
                                sourceOwnerQuality =
                                    *reinterpret_cast<int*>(ownerBytes + 0x9BD8);
                            }
                        }
                        sourceDisabledFlags =
                            static_cast<unsigned int>(childBytes[0x240]) |
                            (static_cast<unsigned int>(childBytes[0x241]) << 8);
                        auto* propertyBlock = *reinterpret_cast<unsigned char**>(childBytes + 0x138);
                        const auto evaluateInt = reinterpret_cast<int(__fastcall*)(void*, void*, bool)>(
                            gSpeedTreeBase + 0x316A80);
                        const auto evaluateFloat = reinterpret_cast<float(__fastcall*)(void*, void*)>(
                            gSpeedTreeBase + 0x316AB0);
                        if (propertyBlock != nullptr && firstChildSourceMethodRva == 0x556A40) {
                            sourceGeometryMode = evaluateInt(
                                propertyBlock + 0xA0, childBytes + 0x1D8, false);
                            sourcePruneMode = evaluateInt(
                                propertyBlock + 0x2F0, childBytes + 0x1D8, false);
                            sourcePruneAmount = evaluateFloat(
                                propertyBlock + 0x2F8, childBytes + 0x1D8);
                            sourceCachedFlags = childBytes[0x718];
                            sourceMeshData = reinterpret_cast<std::uintptr_t>(
                                *reinterpret_cast<void**>(childBytes + 0x588));
                        } else if (propertyBlock != nullptr &&
                                   firstChildSourceMethodRva == 0x5886F0) {
                            sourceGeometryMode = evaluateInt(
                                propertyBlock + 0x190, childBytes + 0x1D8, false);
                            sourcePruneMode = evaluateInt(
                                propertyBlock + 0x3D0, childBytes + 0x1D8, false);
                            sourcePruneAmount = evaluateFloat(
                                propertyBlock + 0x3D8, childBytes + 0x1D8);
                            sourceCachedFlags = childBytes[0x7CC];
                            sourceMeshData = reinterpret_cast<std::uintptr_t>(
                                *reinterpret_cast<void**>(childBytes + 0x638));
                        }
                    }
                }
            }
            char message[512]{};
            _snprintf_s(
                message,
                sizeof(message),
                _TRUNCATE,
                "%s input[%td] vtable=0x%llX type=%s definition=0x%llX method_DD0=0x%llX direct_source=0x%llX method_A10=0x%llX children=%zu first_child=0x%llX method_A10=0x%llX geom=%d prune=%d amount=%.6f disabled=0x%X cached=0x%X mesh=%p inactive=%d owner=%p owner_inactive=%d owner_quality=%d owner_shade=%u selected=%p",
                phase,
                current - begin,
                static_cast<unsigned long long>(vtableRva),
                typeName != nullptr ? typeName : "<unknown>",
                static_cast<unsigned long long>(generatorDefinitionVtableRva),
                static_cast<unsigned long long>(generatorDefinitionMethodRva),
                static_cast<unsigned long long>(collisionSourceVtableRva),
                static_cast<unsigned long long>(collisionSourceMethodRva),
                childSourceCount,
                static_cast<unsigned long long>(firstChildSourceVtableRva),
                static_cast<unsigned long long>(firstChildSourceMethodRva),
                sourceGeometryMode,
                sourcePruneMode,
                static_cast<double>(sourcePruneAmount),
                sourceDisabledFlags,
                sourceCachedFlags,
                reinterpret_cast<void*>(sourceMeshData),
                sourceInactive,
                reinterpret_cast<void*>(sourceOwnerModel),
                sourceOwnerInactive,
                sourceOwnerQuality,
                sourceOwnerShade,
                model);
            Log(message);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("collision input type inspection raised an exception");
    }
}

void WriteAbsoluteJump(unsigned char* destination, const void* target) {
    // mov rax, <64-bit address>; jmp rax
    destination[0] = 0x48;
    destination[1] = 0xb8;
    *reinterpret_cast<std::uintptr_t*>(destination + 2) = reinterpret_cast<std::uintptr_t>(target);
    destination[10] = 0xff;
    destination[11] = 0xe0;
}

void WriteRipIndirectJump(unsigned char* destination, const void* target) {
    // jmp qword ptr [rip]; <64-bit address>. This preserves every register,
    // including RAX when a relocated prologue keeps the entry RSP there.
    destination[0] = 0xff;
    destination[1] = 0x25;
    *reinterpret_cast<std::uint32_t*>(destination + 2) = 0;
    *reinterpret_cast<std::uintptr_t*>(destination + 6) =
        reinterpret_cast<std::uintptr_t>(target);
}

template <std::size_t PrologueBytes>
bool InstallHook(
    HookRecord& record,
    void* target,
    const void* replacement,
    const unsigned char (&expectedPrologue)[PrologueBytes],
    void** originalFunction) {
    static_assert(PrologueBytes >= 12 && PrologueBytes <= 32);
    if (std::memcmp(target, expectedPrologue, PrologueBytes) != 0) {
        Log("hook rejected: target prologue does not match the supported build");
        return false;
    }

    record.target = target;
    record.originalBytes = PrologueBytes;
    std::memcpy(record.original, target, record.originalBytes);
    record.trampoline = VirtualAlloc(
        nullptr,
        record.originalBytes + 12,
        MEM_RESERVE | MEM_COMMIT,
        PAGE_EXECUTE_READWRITE);
    if (record.trampoline == nullptr) {
        Log("hook rejected: trampoline allocation failed");
        return false;
    }

    auto* trampolineBytes = static_cast<unsigned char*>(record.trampoline);
    std::memcpy(trampolineBytes, record.original, record.originalBytes);
    WriteAbsoluteJump(
        trampolineBytes + record.originalBytes,
        static_cast<unsigned char*>(target) + record.originalBytes);

    unsigned char patch[PrologueBytes]{};
    std::memset(patch, 0x90, sizeof(patch));
    WriteAbsoluteJump(patch, replacement);
    DWORD oldProtection = 0;
    if (!VirtualProtect(target, sizeof(patch), PAGE_EXECUTE_READWRITE, &oldProtection)) {
        VirtualFree(record.trampoline, 0, MEM_RELEASE);
        record.trampoline = nullptr;
        Log("hook rejected: VirtualProtect failed");
        return false;
    }
    std::memcpy(target, patch, sizeof(patch));
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    DWORD ignoredProtection = 0;
    VirtualProtect(target, sizeof(patch), oldProtection, &ignoredProtection);

    record.installed = true;
    *originalFunction = record.trampoline;
    return true;
}

template <std::size_t PrologueBytes>
bool InstallRegisterPreservingHook(
    HookRecord& record,
    void* target,
    const void* replacement,
    const unsigned char (&expectedPrologue)[PrologueBytes],
    void** originalFunction) {
    static_assert(PrologueBytes >= 14 && PrologueBytes <= 32);
    if (std::memcmp(target, expectedPrologue, PrologueBytes) != 0) {
        Log("hook rejected: target prologue does not match the supported build");
        return false;
    }

    record.target = target;
    record.originalBytes = PrologueBytes;
    std::memcpy(record.original, target, record.originalBytes);
    record.trampoline = VirtualAlloc(
        nullptr,
        record.originalBytes + 14,
        MEM_RESERVE | MEM_COMMIT,
        PAGE_EXECUTE_READWRITE);
    if (record.trampoline == nullptr) {
        Log("hook rejected: trampoline allocation failed");
        return false;
    }

    auto* trampolineBytes = static_cast<unsigned char*>(record.trampoline);
    std::memcpy(trampolineBytes, record.original, record.originalBytes);
    WriteRipIndirectJump(
        trampolineBytes + record.originalBytes,
        static_cast<unsigned char*>(target) + record.originalBytes);

    unsigned char patch[PrologueBytes]{};
    std::memset(patch, 0x90, sizeof(patch));
    WriteRipIndirectJump(patch, replacement);
    DWORD oldProtection = 0;
    if (!VirtualProtect(target, sizeof(patch), PAGE_EXECUTE_READWRITE, &oldProtection)) {
        VirtualFree(record.trampoline, 0, MEM_RELEASE);
        record.trampoline = nullptr;
        Log("hook rejected: VirtualProtect failed");
        return false;
    }
    std::memcpy(target, patch, sizeof(patch));
    FlushInstructionCache(GetCurrentProcess(), target, sizeof(patch));
    DWORD ignoredProtection = 0;
    VirtualProtect(target, sizeof(patch), oldProtection, &ignoredProtection);

    record.installed = true;
    *originalFunction = record.trampoline;
    return true;
}

void RemoveHook(HookRecord& record) {
    if (!record.installed || record.target == nullptr) {
        return;
    }
    DWORD oldProtection = 0;
    if (VirtualProtect(record.target, record.originalBytes, PAGE_EXECUTE_READWRITE, &oldProtection)) {
        std::memcpy(record.target, record.original, record.originalBytes);
        FlushInstructionCache(GetCurrentProcess(), record.target, record.originalBytes);
        DWORD ignoredProtection = 0;
        VirtualProtect(record.target, record.originalBytes, oldProtection, &ignoredProtection);
    }
    record.installed = false;
    record.originalBytes = 0;
    if (record.trampoline != nullptr) {
        VirtualFree(record.trampoline, 0, MEM_RELEASE);
        record.trampoline = nullptr;
    }
}

bool ReplaceModelUpdateBranch(
    std::uintptr_t rva,
    const unsigned char* expected,
    const unsigned char* replacement,
    std::size_t byteCount);

bool SetFbxDeformRootPatch(bool enabled) {
    // SpeedTree normally creates an FBX skeleton wrapper only when it
    // sees more than one top-level bone. Once the exact BaseRef graph reduces
    // the tree to one root, its lone Bone_1_Start is instead emitted as the
    // special FBX eRoot container. Importers do not expose that container as a
    // deform bone, so its already-computed skin cluster is lost. Change the
    // existing count > 1 condition to count > 0; the original serializer then
    // uses its own wrapper path and preserves Bone_1_Start as a limb node.
    constexpr unsigned char original[] = {0x83, 0xFB, 0x01, 0x7E, 0x71};
    constexpr unsigned char wrappedSingleRoot[] = {0x83, 0xFB, 0x00, 0x7E, 0x71};
    constexpr unsigned char rootType[] = {
        0x48, 0x8B, 0xD8,  // mov rbx, rax
        0x33, 0xD2,        // xor edx, edx (FbxSkeleton::eRoot)
        0x48, 0x8B, 0xC8,  // mov rcx, rax
    };
    constexpr unsigned char limbType[] = {
        0x48, 0x8B, 0xD8,  // mov rbx, rax
        0x6A, 0x02,        // push FbxSkeleton::eLimbNode
        0x5A,              // pop rdx
        0x50,              // push rax
        0x59,              // pop rcx
    };
    if (!ReplaceModelUpdateBranch(
            kFbxSingleRootWrapperBranchRva,
            enabled ? original : wrappedSingleRoot,
            enabled ? wrappedSingleRoot : original,
            sizeof(original))) {
        Log(enabled
                ? "FBX single-root wrapper patch rejected: supported bytes do not match"
                : "FBX single-root wrapper patch could not be restored");
        return false;
    }
    if (!ReplaceModelUpdateBranch(
            kFbxRootWrapperSkeletonTypeRva,
            enabled ? rootType : limbType,
            enabled ? limbType : rootType,
            sizeof(rootType))) {
        ReplaceModelUpdateBranch(
            kFbxSingleRootWrapperBranchRva,
            enabled ? wrappedSingleRoot : original,
            enabled ? original : wrappedSingleRoot,
            sizeof(original));
        Log(enabled
                ? "FBX deformable root-wrapper patch rejected: supported bytes do not match"
                : "FBX deformable root-wrapper patch could not be restored");
        return false;
    }
    gFbxDeformRootPatchInstalled = enabled;
    Log(enabled
            ? "FBX exporter will preserve the exact ID-0 and single-root deform clusters"
            : "FBX deform-root patch restored");
    return true;
}

bool SetRlmConnectFailFastPatch(bool enabled) {
    // RLM checks a five-attempt budget immediately before calling connect().
    // Set only that budget to zero so it follows its existing exhausted-retry
    // error path without opening a server socket.  The connection failure and
    // cached-license paths remain otherwise untouched.
    constexpr unsigned char original[] = {0x05};
    constexpr unsigned char failFast[] = {0x00};
    if (!ReplaceModelUpdateBranch(
            kRlmConnectAttemptLimitImmediateRva,
            enabled ? original : failFast,
            enabled ? failFast : original,
            sizeof(original))) {
        Log(enabled
                ? "RLM connect fail-fast patch rejected: supported bytes do not match"
                : "RLM connect fail-fast patch could not be restored");
        return false;
    }
    gRlmConnectFailFastPatchInstalled = enabled;
    Log(enabled
            ? "RLM server connection-attempt budget reduced to zero for this CLI process"
            : "RLM server connection-attempt budget restored");
    return true;
}

void RemoveCommonHooks() {
    RemoveHook(gInsertExportBoneHook);
    RemoveHook(gExportVertexWeightsHook);
    FreeExportVertexWeightsEntryStub();
    RemoveHook(gFbxNodeCreateHook);
    RemoveHook(gFbxClusterAddControlPointHook);
    RemoveHook(gQThreadStartHook);
    if (gFbxDeformRootPatchInstalled) {
        SetFbxDeformRootPatch(false);
    }
    if (gRlmConnectFailFastPatchInstalled) {
        SetRlmConnectFailFastPatch(false);
    }
}

void RemovePersistentSessionHooks() {
    RemoveHook(gQDialogExecHook);
    RemoveHook(gMainWindowRecoveryCheckHook);
    RemoveHook(gMainWindowOpenFileListHook);
}

void __fastcall HookedMainWindowRecoveryCheck(void* mainWindow) {
    (void)mainWindow;
    // This detour replaces the recovery-check entry point. Deliberately do not
    // call the original: its .sbk lookup, recovery decision, and QMessageBox
    // construction logic must not execute at all in a wrapper-owned bake.
    // The autosave/backup file itself remains intact for manual Modeler use.
    Log("wrapper GUI bake bypassed SpeedTree's entire recovery-check logic before .sbk lookup");
}

int __fastcall HookedQDialogExec(void* dialog) {
    if (dialog != nullptr && gQObjectInherits(dialog, "QMessageBox")) {
        int icon = -1;
        __try {
            icon = gQMessageBoxIcon(dialog);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            icon = -1;
        }
        if (icon == kQMessageBoxQuestionIcon) {
            // Wrapper-owned GUI bake processes are intentionally non-interactive.
            // Suppress the final Qt Question modal itself, independently of which
            // SpeedTree recovery/file-open path created it or which .sbk exists.
            Log("wrapper GUI bake suppressed a Qt Question modal with No semantics");
            return kQMessageBoxNoButton;
        }
    }
    return gOriginalQDialogExec(dialog);
}

bool BuildSessionTargetPathList(void* result) {
    auto* list = static_cast<QtPointerListView*>(result);
    std::memset(list, 0, sizeof(*list));
    void* allocationHeader = nullptr;
    void* itemStorage = gQArrayDataAllocate(
        &allocationHeader,
        static_cast<std::ptrdiff_t>(sizeof(QStringStorage)),
        static_cast<std::ptrdiff_t>(alignof(QStringStorage)),
        1,
        0);
    if (allocationHeader == nullptr || itemStorage == nullptr) {
        Log("persistent session could not allocate the target path list");
        return false;
    }
    gQStringCtor(
        itemStorage,
        reinterpret_cast<const std::uint16_t*>(gPersistentInputPath),
        static_cast<std::ptrdiff_t>(std::wcslen(gPersistentInputPath)));
    list->allocationHeader = allocationHeader;
    list->items = static_cast<void**>(itemStorage);
    list->size = 1;
    return true;
}

void* __fastcall HookedMainWindowOpenFileList(
    void* mainWindow,
    void* result,
    void* parent,
    const void* caption,
    const void* directory,
    const void* filter,
    int options) {
    if (!gSessionOpenPathArmed.exchange(false, std::memory_order_acq_rel)) {
        return gOriginalMainWindowOpenFileList(
            mainWindow,
            result,
            parent,
            caption,
            directory,
            filter,
            options);
    }
    Log("persistent session intercepted SpeedTree's internal Open file-list helper");
    if (!BuildSessionTargetPathList(result)) {
        return result;
    }
    Log("persistent session supplied the target SPM without opening a file dialog");
    return result;
}

bool __fastcall HookedMainWindowConfirmDiscard(void* mainWindow) {
    if (gSessionForceDiscardClose.load(std::memory_order_acquire) ||
        gSessionOpenCallActive.load(std::memory_order_acquire)) {
        Log("persistent session bypassed the document prompt with discard semantics");
        return true;
    }
    return gOriginalMainWindowConfirmDiscard(mainWindow);
}

void LogCollisionResultState(const char* phase, void* model);
std::ptrdiff_t GeneratedCollisionInputCount(void* model);

void LogCollisionScheduleState(const char* phase, void* model) {
    if (model == nullptr) {
        return;
    }
    __try {
        const auto* bytes = static_cast<const unsigned char*>(model);
        char message[448]{};
        _snprintf_s(
            message,
            sizeof(message),
            _TRUNCATE,
            "%s: flags[1d3=%u 1d5=%u 9818=%u 9938=%u 9bdc=%u 9c89=%u 9f21=%u 9cb0=%u] rebuild[5384=%.6f 9cb4=%d]",
            phase,
            static_cast<unsigned int>(bytes[0x1D3]),
            static_cast<unsigned int>(bytes[0x1D5]),
            static_cast<unsigned int>(bytes[0x9818]),
            static_cast<unsigned int>(bytes[0x9938]),
            static_cast<unsigned int>(bytes[0x9BDC]),
            static_cast<unsigned int>(bytes[0x9C89]),
            static_cast<unsigned int>(bytes[0x9F21]),
            static_cast<unsigned int>(bytes[0x9CB0]),
            *reinterpret_cast<const float*>(bytes + 0x5384),
            *reinterpret_cast<const int*>(bytes + 0x9CB4));
        Log(message);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("collision schedule state could not be read");
    }
}

void __fastcall HookedQThreadStart(void* thread, int priority) {
    if (IsCollisionThread(thread)) {
        gCollisionThread.store(thread, std::memory_order_release);
        const unsigned int startCount = gCollisionStartCount.fetch_add(
            1,
            std::memory_order_acq_rel) + 1;
        char countMessage[128]{};
        _snprintf_s(
            countMessage,
            sizeof(countMessage),
            _TRUNCATE,
            "CCollisionThread start count is %u",
            startCount);
        Log(countMessage);
        LogCollisionResultState(
            "collision thread state before start",
            gCollisionModel.load(std::memory_order_acquire));
        if (gSecondaryNativeSerializationActive.load(std::memory_order_acquire)) {
            // The primary export already committed the exact quality-3
            // Collision/Prune result. ExportCommandLineTree schedules another
            // refresh for the bundled XML serializer even though the loaded
            // model has not changed. Do not recompute or recommit that model;
            // let the secondary builder serialize the same frozen geometry.
            gSynchronousCollisionCompleted.store(true, std::memory_order_release);
            Log("native CLI bundled secondary collision refresh suppressed; reusing committed model");
            return;
        }
        if (gNativeCliExportActive.load(std::memory_order_acquire)) {
            void* collisionModel = nullptr;
            __try {
                collisionModel = *reinterpret_cast<void**>(
                    static_cast<unsigned char*>(thread) + 0x10);
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                collisionModel = nullptr;
            }
            if (collisionModel == nullptr) {
                AbortExport(
                    kHookRuntimeFailureExitCode,
                    "native CLI collision thread has no owning model");
            }
            gCollisionModel.store(collisionModel, std::memory_order_release);
            LogCollisionResultState(
                "native CLI export collision refresh started",
                collisionModel);
            gCollisionCompute(collisionModel);
            gCollisionDone(collisionModel);
            gSynchronousCollisionCompleted.store(true, std::memory_order_release);
            LogCollisionResultState(
                "native CLI export collision refresh completed",
                collisionModel);
            return;
        }
        if (gGuiBakeMode &&
            gGuiExportStarted.load(std::memory_order_acquire) &&
            !gSessionClosingTarget.load(std::memory_order_acquire)) {
            void* collisionModel = gCollisionModel.load(std::memory_order_acquire);
            if (collisionModel != nullptr) {
                // ExportCommandLineTree rebuilds geometry, starts this refresh, and
                // otherwise continues writing before the worker applies shade pruning.
                Log("executing the export-time collision refresh synchronously");
                gCollisionCompute(collisionModel);
                gCollisionDone(collisionModel);
                Log("synchronous export-time collision refresh completed");
                return;
            }
            Log("export-time synchronous collision refresh skipped: model is null");
        }
    }
    gOriginalQThreadStart(thread, priority);
}

bool ReplaceModelUpdateBranch(
    std::uintptr_t rva,
    const unsigned char* expected,
    const unsigned char* replacement,
    std::size_t byteCount) {
    auto* target = reinterpret_cast<unsigned char*>(gSpeedTreeBase + rva);
    if (std::memcmp(target, expected, byteCount) != 0) {
        return false;
    }
    DWORD oldProtection = 0;
    if (!VirtualProtect(target, byteCount, PAGE_EXECUTE_READWRITE, &oldProtection)) {
        return false;
    }
    std::memcpy(target, replacement, byteCount);
    FlushInstructionCache(GetCurrentProcess(), target, byteCount);
    DWORD ignoredProtection = 0;
    VirtualProtect(target, byteCount, oldProtection, &ignoredProtection);
    return true;
}

bool EnsureHeadlessOpenGlContext() {
    if (gHeadlessOpenGlContext != nullptr) {
        return wglMakeCurrent(gHeadlessOpenGlDc, gHeadlessOpenGlContext) == TRUE;
    }

    constexpr wchar_t className[] = L"SpeedTreeCollisionCliOffscreen";
    WNDCLASSW windowClass{};
    windowClass.style = CS_OWNDC;
    windowClass.lpfnWndProc = DefWindowProcW;
    windowClass.hInstance = GetModuleHandleW(nullptr);
    windowClass.lpszClassName = className;
    if (RegisterClassW(&windowClass) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
        Log("native CLI could not register its offscreen OpenGL window class");
        return false;
    }

    gHeadlessOpenGlWindow = CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        className,
        L"",
        WS_POPUP,
        0,
        0,
        1,
        1,
        nullptr,
        nullptr,
        GetModuleHandleW(nullptr),
        nullptr);
    if (gHeadlessOpenGlWindow == nullptr) {
        Log("native CLI could not create its offscreen OpenGL window");
        return false;
    }

    gHeadlessOpenGlDc = GetDC(gHeadlessOpenGlWindow);
    if (gHeadlessOpenGlDc == nullptr) {
        Log("native CLI could not acquire its offscreen OpenGL device context");
        return false;
    }

    PIXELFORMATDESCRIPTOR pixelFormat{};
    pixelFormat.nSize = sizeof(pixelFormat);
    pixelFormat.nVersion = 1;
    pixelFormat.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pixelFormat.iPixelType = PFD_TYPE_RGBA;
    pixelFormat.cColorBits = 32;
    pixelFormat.cAlphaBits = 8;
    pixelFormat.cDepthBits = 24;
    pixelFormat.cStencilBits = 8;
    pixelFormat.iLayerType = PFD_MAIN_PLANE;
    const int selectedPixelFormat = ChoosePixelFormat(gHeadlessOpenGlDc, &pixelFormat);
    if (selectedPixelFormat == 0 ||
        !SetPixelFormat(gHeadlessOpenGlDc, selectedPixelFormat, &pixelFormat)) {
        Log("native CLI could not configure its offscreen OpenGL pixel format");
        return false;
    }

    HGLRC bootstrapContext = wglCreateContext(gHeadlessOpenGlDc);
    if (bootstrapContext == nullptr || !wglMakeCurrent(gHeadlessOpenGlDc, bootstrapContext)) {
        Log("native CLI could not activate its bootstrap OpenGL context");
        return false;
    }

    using WglCreateContextAttribsArbFn = HGLRC(WINAPI*)(HDC, HGLRC, const int*);
    const auto createContextAttribs = reinterpret_cast<WglCreateContextAttribsArbFn>(
        wglGetProcAddress("wglCreateContextAttribsARB"));
    if (createContextAttribs != nullptr) {
        constexpr int kWglContextMajorVersionArb = 0x2091;
        constexpr int kWglContextMinorVersionArb = 0x2092;
        constexpr int kWglContextProfileMaskArb = 0x9126;
        constexpr int kWglContextCompatibilityProfileBitArb = 0x00000002;
        const int attributes[] = {
            kWglContextMajorVersionArb,
            4,
            kWglContextMinorVersionArb,
            5,
            kWglContextProfileMaskArb,
            kWglContextCompatibilityProfileBitArb,
            0,
        };
        HGLRC modernContext = createContextAttribs(
            gHeadlessOpenGlDc,
            nullptr,
            attributes);
        if (modernContext != nullptr) {
            wglMakeCurrent(nullptr, nullptr);
            wglDeleteContext(bootstrapContext);
            bootstrapContext = modernContext;
            if (!wglMakeCurrent(gHeadlessOpenGlDc, bootstrapContext)) {
                wglDeleteContext(bootstrapContext);
                Log("native CLI could not activate its OpenGL 4.5 compatibility context");
                return false;
            }
        }
    }

    gHeadlessOpenGlContext = bootstrapContext;
    const auto* version = glGetString(GL_VERSION);
    char message[256]{};
    _snprintf_s(
        message,
        sizeof(message),
        _TRUNCATE,
        "native CLI activated an offscreen OpenGL context: %s",
        version == nullptr ? "unknown" : reinterpret_cast<const char*>(version));
    Log(message);
    return true;
}

bool SetNativeRawExportBranchBypass(bool enabled) {
    constexpr unsigned char firstOriginal[] = {0x0F, 0x84, 0x0F, 0x01, 0x00, 0x00};
    constexpr unsigned char firstBypass[] = {0xE9, 0x10, 0x01, 0x00, 0x00, 0x90};
    constexpr unsigned char secondOriginal[] = {0x74, 0x15};
    constexpr unsigned char secondBypass[] = {0xEB, 0x15};
    constexpr unsigned char thirdOriginal[] = {0x74, 0x37};
    constexpr unsigned char thirdBypass[] = {0xEB, 0x37};
    const unsigned char* firstExpected = enabled ? firstOriginal : firstBypass;
    const unsigned char* firstReplacement = enabled ? firstBypass : firstOriginal;
    const unsigned char* secondExpected = enabled ? secondOriginal : secondBypass;
    const unsigned char* secondReplacement = enabled ? secondBypass : secondOriginal;
    const unsigned char* thirdExpected = enabled ? thirdOriginal : thirdBypass;
    const unsigned char* thirdReplacement = enabled ? thirdBypass : thirdOriginal;
    return ReplaceModelUpdateBranch(
               0x3D119F, firstExpected, firstReplacement, sizeof(firstOriginal)) &&
        ReplaceModelUpdateBranch(
               0x3D12D6, secondExpected, secondReplacement, sizeof(secondOriginal)) &&
        ReplaceModelUpdateBranch(
               0x3D132C, thirdExpected, thirdReplacement, sizeof(thirdOriginal));
}

bool SetNativeModelUpdateInteractiveBranches(bool enabled) {
    constexpr unsigned char firstOriginal[] = {0x0F, 0x84, 0x0F, 0x01, 0x00, 0x00};
    constexpr unsigned char firstEnabled[] = {0x90, 0x90, 0x90, 0x90, 0x90, 0x90};
    constexpr unsigned char secondOriginal[] = {0x74, 0x15};
    constexpr unsigned char secondEnabled[] = {0x90, 0x90};
    constexpr unsigned char thirdOriginal[] = {0x74, 0x37};
    constexpr unsigned char thirdEnabled[] = {0x90, 0x90};
    const unsigned char* firstExpected = enabled ? firstOriginal : firstEnabled;
    const unsigned char* firstReplacement = enabled ? firstEnabled : firstOriginal;
    const unsigned char* secondExpected = enabled ? secondOriginal : secondEnabled;
    const unsigned char* secondReplacement = enabled ? secondEnabled : secondOriginal;
    const unsigned char* thirdExpected = enabled ? thirdOriginal : thirdEnabled;
    const unsigned char* thirdReplacement = enabled ? thirdEnabled : thirdOriginal;
    return ReplaceModelUpdateBranch(
               0x3D119F, firstExpected, firstReplacement, sizeof(firstOriginal)) &&
        ReplaceModelUpdateBranch(
               0x3D12D6, secondExpected, secondReplacement, sizeof(secondOriginal)) &&
        ReplaceModelUpdateBranch(
               0x3D132C, thirdExpected, thirdReplacement, sizeof(thirdOriginal));
}

bool SetNativeMainWindowUiTailBypass(bool enabled) {
    constexpr unsigned char original[] = {
        0x48, 0x8B, 0x81, 0x18, 0x04, 0x00, 0x00,
    };
    constexpr unsigned char bypass[] = {
        0xE9, 0x10, 0x00, 0x00, 0x00, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x13B1E5,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativePrepareUiCallbackBypass(bool enabled) {
    // The interactive generator-preparation routine ends with a MainWindow
    // scene/view refresh pair. Native CLI mode owns neither target object, so
    // keep all generator work and jump over only those two UI-only callbacks.
    constexpr unsigned char original[] = {
        0x48, 0x8B, 0x0D, 0xAB, 0x34, 0xEC, 0x01,
    };
    constexpr unsigned char bypass[] = {
        0xE9, 0x1B, 0x00, 0x00, 0x00, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x3DD746,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeGeneratorUiRefreshBypass(bool enabled) {
    // A generator refresh optionally updates the interactive renderer. The
    // renderer test reports GUI mode during the scoped core rebuild, but the
    // native CLI owns no renderer object. Avoid the renderer query itself and
    // supply its normal "not available" result in eax.
    constexpr unsigned char original[] = {
        0x48, 0x8B, 0x0D, 0xB0, 0x89, 0xFD, 0x01,
    };
    constexpr unsigned char bypass[] = {
        0x31, 0xC0, 0xE9, 0x09, 0x00, 0x00, 0x00,
    };
    return ReplaceModelUpdateBranch(
        0x2C8241,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeCurrentViewLookupBypass(bool enabled) {
    // Post-generation notification asks the MainWindow for its active editing
    // view. There is no such view in the native exporter; return null and let
    // the model's normal null-view branch continue.
    constexpr unsigned char original[] = {
        0x48, 0x8B, 0x0D, 0xE9, 0xE6, 0xEC, 0x01,
    };
    constexpr unsigned char bypass[] = {
        0x31, 0xC0, 0xE9, 0x09, 0x00, 0x00, 0x00,
    };
    return ReplaceModelUpdateBranch(
        0x3D2508,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeUiModeQueryBypass(bool enabled) {
    // This post-generation UI-state query leads into QML object discovery in
    // GUI mode. Report the state that selects its existing no-view branch.
    constexpr unsigned char original[] = {
        0x48, 0x8B, 0x0D, 0xCC, 0xE6, 0xEC, 0x01,
    };
    constexpr unsigned char bypass[] = {
        0xB0, 0x01, 0xE9, 0x09, 0x00, 0x00, 0x00,
    };
    return ReplaceModelUpdateBranch(
        0x3D2525,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeModelWindowNotificationBypass(bool enabled) {
    // Model finalization emits a window-only notification whose return value
    // is unused. Skip it in native CLI mode after all model work is complete.
    constexpr unsigned char original[] = {
        0x40, 0x53, 0x48, 0x83, 0xEC, 0x30,
    };
    constexpr unsigned char bypass[] = {
        0xC3, 0x90, 0x90, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x148630,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeSelectedNodeRedrawBypass(bool enabled) {
    // Modeler's active tree view reports this predicate true; that path also
    // performs generator refresh work needed by collision-pruned branches.
    constexpr unsigned char original[] = {
        0x48, 0x89, 0x5C, 0x24, 0x08,
    };
    constexpr unsigned char bypass[] = {
        0xB0, 0x01, 0xC3, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x149030,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeRendererAvailabilityBypass(bool enabled) {
    // Several core rebuild loops ask the application whether an interactive
    // renderer exists. During native CLI regeneration it never does. Patch the
    // shared query for the scoped rebuild instead of chasing every call site.
    constexpr unsigned char original[] = {
        0x48, 0x89, 0x5C, 0x24, 0x08,
    };
    constexpr unsigned char bypass[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x136A90,
        enabled ? original : bypass,
        enabled ? bypass : original,
        sizeof(original));
}

bool SetNativeEditorModeFallback(bool enabled) {
    // The active tree view reports editor mode 6 during the correct GUI bake.
    constexpr unsigned char original[] = {
        0x40, 0x53, 0x48, 0x83, 0xEC, 0x30,
    };
    constexpr unsigned char fallback[] = {
        0xB8, 0x06, 0x00, 0x00, 0x00, 0xC3,
    };
    return ReplaceModelUpdateBranch(
        0x1395B0,
        enabled ? original : fallback,
        enabled ? fallback : original,
        sizeof(original));
}

bool SetNativeEditorStateFallback(bool enabled) {
    // This UI-state predicate already returns false when the native CLI owns no
    // editor object. Avoid QML discovery and use that same fallback directly.
    constexpr unsigned char original[] = {
        0x40, 0x53, 0x48, 0x83, 0xEC, 0x30,
    };
    constexpr unsigned char fallback[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x139AF0,
        enabled ? original : fallback,
        enabled ? fallback : original,
        sizeof(original));
}

bool SetNativeEditingViewFallback(bool enabled) {
    // Active-view lookup is optional: its callers have an explicit null-view
    // path with default model values. Native CLI should take that path.
    constexpr unsigned char original[] = {
        0x48, 0x83, 0xEC, 0x28, 0x80, 0xB9,
    };
    constexpr unsigned char fallback[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x1364F0,
        enabled ? original : fallback,
        enabled ? fallback : original,
        sizeof(original));
}

bool SetNativeViewOptionFallback(bool enabled) {
    // A view-option predicate has a built-in false result when no editor view
    // exists. Use that result directly during the scoped native rebuild.
    constexpr unsigned char original[] = {
        0x40, 0x53, 0x48, 0x83, 0xEC, 0x30,
    };
    constexpr unsigned char fallback[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x139910,
        enabled ? original : fallback,
        enabled ? fallback : original,
        sizeof(original));
}

bool SetNativeQmlViewLookupFallback(bool enabled) {
    // Shared QML view discovery used by multiple typed view accessors. Native
    // CLI has no QML document, and every accessor accepts a null result.
    constexpr unsigned char original[] = {
        0x48, 0x83, 0xEC, 0x38, 0x80, 0xB9,
    };
    constexpr unsigned char fallback[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x1365B0,
        enabled ? original : fallback,
        enabled ? fallback : original,
        sizeof(original));
}

bool SetNativeSecondaryViewOptionFallback(bool enabled) {
    // A second view predicate also defines false as its no-editor result.
    constexpr unsigned char original[] = {
        0x40, 0x53, 0x48, 0x83, 0xEC, 0x30,
    };
    constexpr unsigned char fallback[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x139E70,
        enabled ? original : fallback,
        enabled ? fallback : original,
        sizeof(original));
}

bool SetNativeWindowRefreshNoop(bool enabled) {
    // MainWindow vtable +0xE0 is a pure interactive refresh notification. Its
    // return value is never consumed by the model path.
    constexpr unsigned char original[] = {
        0x48, 0x83, 0xEC, 0x28, 0x80, 0xB9, 0xED,
    };
    constexpr unsigned char noop[] = {
        0xC3, 0x90, 0x90, 0x90, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x13CF70,
        enabled ? original : noop,
        enabled ? noop : original,
        sizeof(original));
}

bool SetNativeWindowModelNotificationNoop(bool enabled) {
    // MainWindow vtable +0xD0 is another void editor notification embedded in
    // an otherwise model-only bounds/update pass.
    constexpr unsigned char original[] = {
        0x48, 0x83, 0xEC, 0x38, 0x80, 0xB9,
    };
    constexpr unsigned char noop[] = {
        0xC3, 0x90, 0x90, 0x90, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x13AE90,
        enabled ? original : noop,
        enabled ? noop : original,
        sizeof(original));
}

bool SetNativeOnIdleNoop(bool enabled) {
    // MainWindow::OnIdle is exclusively interactive UI maintenance. Native
    // export pumps Qt events for progress, but must not enter this GUI loop.
    constexpr unsigned char original[] = {
        0x40, 0x55, 0x53, 0x56, 0x57,
    };
    constexpr unsigned char noop[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x13B7F0,
        enabled ? original : noop,
        enabled ? noop : original,
        sizeof(original));
}

bool SetNativeDeferredUiUpdateNoop(bool enabled) {
    // A second Qt-driven MainWindow handler performs deferred editor/view
    // maintenance. It is not part of model generation or FBX serialization.
    constexpr unsigned char original[] = {
        0x48, 0x89, 0x5C, 0x24, 0x08,
    };
    constexpr unsigned char noop[] = {
        0x31, 0xC0, 0xC3, 0x90, 0x90,
    };
    return ReplaceModelUpdateBranch(
        0x13C390,
        enabled ? original : noop,
        enabled ? noop : original,
        sizeof(original));
}

bool InstallCollisionTreeProbe();

bool __fastcall HookedNativeModelUpdate(void* model, int variation) {
    if (gSecondaryNativeSerializationActive.load(std::memory_order_acquire)) {
        // A second exporter preset must not regenerate the unchanged document.
        // Returning success preserves the primary export's committed pruned
        // model while still allowing the original serializer/build stages to
        // run for XML (or another bundled format).
        Log("native CLI bundled secondary model update suppressed; reusing committed Collision/Prune result");
        return true;
    }
    if (!gNativeCliExportActive.load(std::memory_order_acquire)) {
        if (!gGuiBakeMode) {
            return gOriginalNativeModelUpdate(model, variation);
        }
        const unsigned int updateNumber =
            gGuiModelUpdateCount.fetch_add(1, std::memory_order_acq_rel) + 1;
        char phase[160]{};
        _snprintf_s(
            phase,
            sizeof(phase),
            _TRUNCATE,
            "GUI model update %u entry",
            updateNumber);
        LogCollisionResultState(phase, model);
        __try {
            void* application = *reinterpret_cast<void**>(
                gSpeedTreeBase + 0x22A0BF8);
            void** applicationVtable = *reinterpret_cast<void***>(application);
            const auto getBool = [&](std::size_t byteOffset) {
                const auto function = reinterpret_cast<bool(__fastcall*)(void*)>(
                    applicationVtable[byteOffset / sizeof(void*)]);
                return function(application);
            };
            const auto getInt = [&](std::size_t byteOffset) {
                const auto function = reinterpret_cast<int(__fastcall*)(void*)>(
                    applicationVtable[byteOffset / sizeof(void*)]);
                return function(application);
            };
            const auto getPointer = [&](std::size_t byteOffset) {
                const auto function = reinterpret_cast<void*(__fastcall*)(void*)>(
                    applicationVtable[byteOffset / sizeof(void*)]);
                return function(application);
            };
            char uiState[384]{};
            _snprintf_s(
                uiState,
                sizeof(uiState),
                _TRUNCATE,
                "GUI application getters: renderer=%d selected_redraw=%d editor_mode=%d editor_state=%d view_option=%d secondary_option=%d editing_view=%p ui_mode=%d",
                getInt(0x208),
                getBool(0xF8) ? 1 : 0,
                getInt(0x5F8),
                getBool(0x390) ? 1 : 0,
                getBool(0x230) ? 1 : 0,
                getBool(0x1E8) ? 1 : 0,
                getPointer(0x140),
                getBool(0x388) ? 1 : 0);
            Log(uiState);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            Log("GUI application getter diagnostics raised an exception");
        }
        const bool guiResult = gOriginalNativeModelUpdate(model, variation);
        _snprintf_s(
            phase,
            sizeof(phase),
            _TRUNCATE,
            "GUI model update %u exit",
            updateNumber);
        LogCollisionResultState(phase, model);
        return guiResult;
    }
    if (!EnsureHeadlessOpenGlContext()) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI model update could not create its headless render context");
    }
    auto* modelBytes = static_cast<unsigned char*>(model);
    modelBytes[0x9BDC] = 1;
    const bool result = gOriginalNativeModelUpdate(model, variation);
    LogCollisionResultState(
        "native CLI raw model update with headless render context",
        model);

    // The interactive model-update path performs these two core model operations
    // before tagging every generator cache for a complete rebuild.  Calling the
    // surrounding GUI controller path is unsafe in the native exporter because
    // it dereferences widgets that do not exist in CLI mode.
    const auto prepareInteractiveGenerators =
        reinterpret_cast<bool(__fastcall*)(void*)>(gSpeedTreeBase + 0x3DA640);
    const auto rebuildInteractiveGenerators =
        reinterpret_cast<void(__fastcall*)(void*, bool)>(gSpeedTreeBase + 0x3E9490);
    prepareInteractiveGenerators(model);
    rebuildInteractiveGenerators(model, true);
    auto* generatorState = *reinterpret_cast<unsigned char**>(modelBytes + 0x7B0);
    auto* generatorStateEnd = *reinterpret_cast<unsigned char**>(modelBytes + 0x7B8);
    for (; generatorState < generatorStateEnd; generatorState += 0x2420) {
        constexpr std::size_t dirtyFlagOffsets[] = {
            0x3F8, 0x6C0, 0x988, 0xC50, 0xF18, 0x11E0,
            0x14A8, 0x1770, 0x1A38, 0x1D00, 0x1FC8, 0x2290,
        };
        for (const std::size_t offset : dirtyFlagOffsets) {
            *reinterpret_cast<unsigned int*>(generatorState + offset) |= 5u;
        }
    }
    LogCollisionResultState(
        "native CLI interactive generator preparation completed",
        model);
    void* treeDocument = modelBytes - kTreeWindowModelOffset;
    const auto prepareTreeDocument = reinterpret_cast<TreeDocumentPrepareFn>(
        gSpeedTreeBase + kTreeDocumentPrepareRva);
    const auto stageTreeDocumentModel = reinterpret_cast<TreeDocumentModelStageFn>(
        gSpeedTreeBase + kTreeDocumentModelStageRva);
    prepareTreeDocument(treeDocument);
    void* modelInterface = static_cast<unsigned char*>(treeDocument) + 0x18;
    stageTreeDocumentModel(modelInterface);
    void** modelInterfaceVtable = *reinterpret_cast<void***>(modelInterface);
    auto finishModelStage = reinterpret_cast<bool(__fastcall*)(void*)>(
        modelInterfaceVtable[0xD8 / sizeof(void*)]);
    finishModelStage(modelInterface);
    LogCollisionResultState(
        "native CLI full document stage after raw generation",
        model);
    const auto generateShadeVolume = reinterpret_cast<void(__fastcall*)(void*, int)>(
        gSpeedTreeBase + kGenerateShadeVolumeRva);
    generateShadeVolume(model, 5);
    Log("native CLI shade-pruning volume generation completed");
    gMarkCollisionDirty(model);
    modelBytes[0x9C68] = 1;
    modelBytes[0x9C89] = 1;
    *reinterpret_cast<int*>(modelBytes + 0x9C8C) = 0;
    *reinterpret_cast<int*>(modelBytes + 0x9C94) = 0;
    LogCollisionResultState(
        "native CLI model marked dirty after full input generation",
        model);
    gCollisionCompute(model);
    gCollisionDone(model);
    gSynchronousCollisionCompleted.store(true, std::memory_order_release);
    LogCollisionResultState(
        "native CLI full post-input collision computation completed",
        model);
    auto* nativeMainWindow = gNativeMainWindow.load(std::memory_order_acquire);
    if (nativeMainWindow == nullptr) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI post-prune model update has no owning MainWindow");
    }
    if (!SetNativeMainWindowUiTailBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard the post-prune model-update UI tail");
    }
    if (!SetNativePrepareUiCallbackBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard the generator-preparation UI callback");
    }
    if (!SetNativeGeneratorUiRefreshBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard the optional generator renderer refresh");
    }
    if (!SetNativeCurrentViewLookupBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard the optional current-view lookup");
    }
    if (!SetNativeUiModeQueryBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard the post-generation UI-mode query");
    }
    if (!SetNativeModelWindowNotificationBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard the model-window notification");
    }
    if (!SetNativeSelectedNodeRedrawBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not guard selected-node redraws");
    }
    if (!SetNativeRendererAvailabilityBypass(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not override interactive-renderer availability");
    }
    if (!SetNativeEditorModeFallback(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not apply the no-view editor-mode fallback");
    }
    if (!SetNativeEditorStateFallback(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not apply the no-view editor-state fallback");
    }
    if (!SetNativeEditingViewFallback(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not apply the null editing-view fallback");
    }
    if (!SetNativeViewOptionFallback(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not apply the no-view option fallback");
    }
    if (!SetNativeQmlViewLookupFallback(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not apply the shared QML-view fallback");
    }
    if (!SetNativeSecondaryViewOptionFallback(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not apply the secondary no-view option fallback");
    }
    if (!SetNativeWindowRefreshNoop(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not disable interactive window refreshes");
    }
    if (!SetNativeWindowModelNotificationNoop(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not disable the window model notification");
    }
    const unsigned char originalCliMode = nativeMainWindow[0x615];
    void* const originalIdleViewState =
        *reinterpret_cast<void**>(nativeMainWindow + 0x4B0);
    nativeMainWindow[0x615] = 0;
    const bool postCollisionResult = gOriginalNativeModelUpdate(model, variation);
    // Restore UI-only idle state changed by the scoped interactive rebuild.
    // The model itself remains regenerated, while the native exporter's
    // original non-interactive window state is preserved.
    *reinterpret_cast<unsigned short*>(nativeMainWindow + 0x3C8) = 0;
    *reinterpret_cast<void**>(nativeMainWindow + 0x4B0) = originalIdleViewState;
    // Interactive Modeler schedules one final collision worker after the
    // regenerated meshes and shade volume are committed. Invoke that exact
    // scheduler so it prepares the thread/commit state; HookedQThreadStart
    // executes the worker synchronously without relying on a GUI event loop.
    gSynchronousCollisionCompleted.store(false, std::memory_order_release);
    const auto scheduleCollision =
        reinterpret_cast<void(__fastcall*)(void*, bool)>(gSpeedTreeBase + 0x3BF790);
    scheduleCollision(model, false);
    if (!gSynchronousCollisionCompleted.load(std::memory_order_acquire)) {
        Log("native CLI collision scheduler did not start a worker; using direct fallback");
        gCollisionCompute(model);
        gCollisionDone(model);
        gSynchronousCollisionCompleted.store(true, std::memory_order_release);
    }
    LogCollisionScheduleState(
        "native CLI collision-complete commit preconditions",
        model);
    const auto commitCollisionGeneratorChanges =
        reinterpret_cast<void(__fastcall*)(void*, bool)>(gSpeedTreeBase + 0x3DDD30);
    commitCollisionGeneratorChanges(model, true);
    LogCollisionResultState(
        "native CLI collision-complete generator commit finished",
        model);
    nativeMainWindow[0x615] = originalCliMode;
    LogCollisionResultState(
        "native CLI final post-regeneration collision computation completed",
        model);
    if (!SetNativeWindowModelNotificationNoop(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the window model notification");
    }
    if (!SetNativeWindowRefreshNoop(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore interactive window refreshes");
    }
    if (!SetNativeSecondaryViewOptionFallback(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the secondary view-option query");
    }
    if (!SetNativeQmlViewLookupFallback(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore shared QML-view discovery");
    }
    if (!SetNativeViewOptionFallback(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the view-option query");
    }
    if (!SetNativeEditingViewFallback(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the editing-view query");
    }
    if (!SetNativeEditorStateFallback(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the editor-state query");
    }
    if (!SetNativeEditorModeFallback(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the editor-mode query");
    }
    if (!SetNativeRendererAvailabilityBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore interactive-renderer availability");
    }
    if (!SetNativeSelectedNodeRedrawBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore selected-node redraws");
    }
    if (!SetNativeModelWindowNotificationBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the model-window notification");
    }
    if (!SetNativeUiModeQueryBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the post-generation UI-mode query");
    }
    if (!SetNativeCurrentViewLookupBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the optional current-view lookup");
    }
    if (!SetNativeGeneratorUiRefreshBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the optional generator renderer refresh");
    }
    if (!SetNativePrepareUiCallbackBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the generator-preparation UI callback");
    }
    if (!SetNativeMainWindowUiTailBypass(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore the post-prune model-update UI tail");
    }
    LogCollisionResultState(
        "native CLI post-prune model regeneration completed",
        model);
    return result && postCollisionResult;
}

void __fastcall HookedNativeExportFinalizeGeometry(void* exportBuilder, bool separate) {
    if (gNativeCliExportActive.load(std::memory_order_acquire)) {
        LogCollisionResultState(
            "native CLI finalize geometry entry",
            gCollisionModel.load(std::memory_order_acquire));
    }
    gOriginalNativeExportFinalizeGeometry(exportBuilder, separate);
    if (gNativeCliExportActive.load(std::memory_order_acquire)) {
        LogCollisionResultState(
            "native CLI finalize geometry exit",
            gCollisionModel.load(std::memory_order_acquire));
    }
}

void __fastcall HookedNativeExportFinalizeDocument(void* exportBuilder) {
    if (gNativeCliExportActive.load(std::memory_order_acquire)) {
        LogCollisionResultState(
            "native CLI finalize document entry",
            gCollisionModel.load(std::memory_order_acquire));
    }
    gOriginalNativeExportFinalizeDocument(exportBuilder);
    if (gNativeCliExportActive.load(std::memory_order_acquire)) {
        LogCollisionResultState(
            "native CLI finalize document exit",
            gCollisionModel.load(std::memory_order_acquire));
    }
}

void __fastcall HookedNativeExportBuild(void* exportBuilder) {
    if (gNativeCliExportActive.load(std::memory_order_acquire) &&
        !gSynchronousCollisionCompleted.load(std::memory_order_acquire)) {
        void* collisionModel = gCollisionModel.load(std::memory_order_acquire);
        LogCollisionResultState(
            "native CLI geometry build entry",
            collisionModel);
        if (GeneratedCollisionInputCount(collisionModel) <= 0) {
            AbortExport(
                kNoGeneratedCollisionInputsExitCode,
                "native CLI geometry build received no collision inputs");
        }
        auto* collisionBytes = static_cast<unsigned char*>(collisionModel);
        collisionBytes[0x9C68] = 1;
        *reinterpret_cast<int*>(collisionBytes + 0x9C8C) = 0;
        *reinterpret_cast<int*>(collisionBytes + 0x9C94) = 0;
        LogCollisionResultState(
            "native CLI collision state normalized for full pruning",
            collisionModel);
        Log("executing collision core before native CLI geometry build");
        gCollisionCompute(collisionModel);
        gCollisionDone(collisionModel);
        gSynchronousCollisionCompleted.store(true, std::memory_order_release);
        LogCollisionResultState(
            "native CLI pre-build collision computation completed",
            collisionModel);
    }
    gOriginalNativeExportBuild(exportBuilder);
}

[[noreturn]] void AbortExport(DWORD exitCode, const char* reason) {
    Log(reason);
    TerminateProcess(GetCurrentProcess(), exitCode);
    for (;;) {
        Sleep(INFINITE);
    }
}

void PumpMainThreadEvents() {
    if (gSendPostedEvents != nullptr) {
        gSendPostedEvents(nullptr, 0);
    }
    if (gProcessEvents != nullptr) {
        gProcessEvents(0, 25);
    }
}

void* FindQObjectRecursiveByClass(
    void* object,
    const char* className,
    int depth,
    std::size_t& inspected) {
    if (object == nullptr || depth > 64 || inspected >= 100000) {
        return nullptr;
    }
    ++inspected;

    __try {
        if (gQObjectInherits(object, className)) {
            return object;
        }
        const auto* children = static_cast<const QtPointerListView*>(gQObjectChildren(object));
        if (children == nullptr || children->size < 0 || children->size > 100000 ||
            (children->size > 0 && children->items == nullptr)) {
            return nullptr;
        }
        for (std::ptrdiff_t index = 0; index < children->size; ++index) {
            if (void* found = FindQObjectRecursiveByClass(
                    children->items[index],
                    className,
                    depth + 1,
                    inspected)) {
                return found;
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return nullptr;
    }
    return nullptr;
}

void* FindTreeWindowRecursive(void* object, int depth, std::size_t& inspected) {
    return FindQObjectRecursiveByClass(object, "CTreeWindow", depth, inspected);
}

struct WindowSearchContext {
    DWORD processId;
    void* treeWindow;
    std::size_t inspected;
};

BOOL CALLBACK FindTreeWindowFromHwnd(HWND window, LPARAM parameter) {
    auto* context = reinterpret_cast<WindowSearchContext*>(parameter);
    DWORD ownerProcess = 0;
    GetWindowThreadProcessId(window, &ownerProcess);
    if (ownerProcess != context->processId) {
        return TRUE;
    }

    if (void* root = gQWidgetFind(reinterpret_cast<std::uintptr_t>(window))) {
        context->treeWindow = FindTreeWindowRecursive(root, 0, context->inspected);
        if (context->treeWindow != nullptr) {
            return FALSE;
        }
    }
    return TRUE;
}

void* FindTreeWindow() {
    WindowSearchContext context{GetCurrentProcessId(), nullptr, 0};
    EnumWindows(FindTreeWindowFromHwnd, reinterpret_cast<LPARAM>(&context));

    if (context.treeWindow == nullptr && gQCoreApplicationInstance != nullptr) {
        if (void* application = gQCoreApplicationInstance()) {
            context.treeWindow = FindTreeWindowRecursive(application, 0, context.inspected);
        }
    }

    if (context.treeWindow == nullptr && gQApplicationAllWidgets != nullptr) {
        __try {
            // QApplication::allWidgets() returns a temporary QList<QWidget*>. The
            // three-word Qt 6.6 list payload is intentionally viewed without taking
            // ownership; this short-lived CLI process can safely leave its ref intact.
            const QtPointerListView widgets = gQApplicationAllWidgets();
            if (widgets.size >= 0 && widgets.size <= 100000 &&
                (widgets.size == 0 || widgets.items != nullptr)) {
                for (std::ptrdiff_t index = 0; index < widgets.size; ++index) {
                    if (gQObjectInherits(widgets.items[index], "CTreeWindow")) {
                        context.treeWindow = widgets.items[index];
                        break;
                    }
                }
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            context.treeWindow = nullptr;
        }
    }
    if (context.treeWindow != nullptr) {
        LogPointer("found CTreeWindow at ", context.treeWindow);
    } else {
        char message[256]{};
        _snprintf_s(
            message,
            sizeof(message),
            _TRUNCATE,
            "CTreeWindow not found after inspecting %zu QObject instances",
            context.inspected);
        Log(message);
    }
    return context.treeWindow;
}

bool ContainsPointer(const std::vector<void*>& values, void* value) {
    return std::find(values.begin(), values.end(), value) != values.end();
}

void* FindQObjectParentByClass(void* object, const char* className) {
    void* current = object;
    for (int depth = 0; depth < 16 && current != nullptr; ++depth) {
        if (gQObjectInherits(current, className)) {
            return current;
        }
        current = gQObjectParent(current);
    }
    return nullptr;
}

bool InvokeQtNoArgumentWithConnection(void* object, const char* method, int connectionType) {
    if (object == nullptr || gQMetaInvoke == nullptr) {
        return false;
    }
    const QGenericArgumentCompat empty{nullptr, nullptr};
    return gQMetaInvoke(
        object,
        method,
        connectionType,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty);
}

bool InvokeQtNoArgument(void* object, const char* method) {
    return InvokeQtNoArgumentWithConnection(object, method, 1);
}

bool InvokeQtInt(void* object, const char* method, int value) {
    if (object == nullptr || gQMetaInvoke == nullptr) {
        return false;
    }
    const QGenericArgumentCompat argument{&value, "int"};
    const QGenericArgumentCompat empty{nullptr, nullptr};
    return gQMetaInvoke(
        object,
        method,
        1,
        argument,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty);
}

bool CollectMdiSubWindows(void* mdiArea, std::vector<void*>& result) {
    result.clear();
    if (mdiArea == nullptr || gQMdiAreaSubWindowList == nullptr) {
        return false;
    }
    __try {
        QtPointerListView list{};
        gQMdiAreaSubWindowList(mdiArea, &list, 0);
        if (list.size < 0 || list.size > 4096 ||
            (list.size > 0 && list.items == nullptr)) {
            return false;
        }
        result.assign(list.items, list.items + list.size);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

bool PostQtWakeEvent(void* receiver) {
    if (receiver == nullptr || gQEventCtor == nullptr || gPostEvent == nullptr) {
        return false;
    }
    void* eventStorage = ::operator new(sizeof(QEventStorage), std::nothrow);
    if (eventStorage == nullptr) {
        return false;
    }
    constexpr int kQEventUser = 1000;
    void* event = gQEventCtor(eventStorage, kQEventUser);
    if (event == nullptr) {
        ::operator delete(eventStorage);
        return false;
    }
    gPostEvent(receiver, event, 0);
    return true;
}

bool IsReadableWritablePage(DWORD protection) {
    if ((protection & (PAGE_GUARD | PAGE_NOACCESS)) != 0) {
        return false;
    }
    const DWORD basicProtection = protection & 0xff;
    return basicProtection == PAGE_READWRITE || basicProtection == PAGE_WRITECOPY ||
        basicProtection == PAGE_EXECUTE_READWRITE || basicProtection == PAGE_EXECUTE_WRITECOPY;
}

void* FindCollisionModelFromEmbeddedThread() {
    const auto expectedVtable = reinterpret_cast<void*>(gSpeedTreeBase + kCollisionThreadVtableRva);
    SYSTEM_INFO systemInfo{};
    GetSystemInfo(&systemInfo);
    auto address = reinterpret_cast<std::uintptr_t>(systemInfo.lpMinimumApplicationAddress);
    const auto maximum = reinterpret_cast<std::uintptr_t>(systemInfo.lpMaximumApplicationAddress);
    void* firstCandidate = nullptr;
    int candidateCount = 0;

    while (address < maximum) {
        MEMORY_BASIC_INFORMATION region{};
        if (VirtualQuery(reinterpret_cast<const void*>(address), &region, sizeof(region)) == 0) {
            break;
        }
        const auto regionBase = reinterpret_cast<std::uintptr_t>(region.BaseAddress);
        const std::size_t regionBytes = region.RegionSize;
        if (region.State == MEM_COMMIT && IsReadableWritablePage(region.Protect) &&
            regionBytes >= sizeof(void*)) {
            __try {
                const auto* words = static_cast<void* const*>(region.BaseAddress);
                const std::size_t wordCount = regionBytes / sizeof(void*);
                for (std::size_t index = 0; index < wordCount; ++index) {
                    if (words[index] != expectedVtable) {
                        continue;
                    }
                    auto* collisionThread = const_cast<void**>(words + index);
                    auto* model = reinterpret_cast<unsigned char*>(collisionThread) -
                        kEmbeddedCollisionThreadOffset;
                    if (reinterpret_cast<std::uintptr_t>(model) < regionBase ||
                        reinterpret_cast<std::uintptr_t>(model) + kEmbeddedCollisionThreadOffset + 0x18 >
                            regionBase + regionBytes) {
                        continue;
                    }
                    if (*reinterpret_cast<void**>(
                            reinterpret_cast<unsigned char*>(collisionThread) + 0x10) != model) {
                        continue;
                    }
                    const int quality = *reinterpret_cast<int*>(
                        model + kCoreModelCollisionQualityOffset);
                    if (quality < 0 || quality > 3 || !IsCollisionThread(collisionThread)) {
                        continue;
                    }
                    ++candidateCount;
                    char candidateMessage[256]{};
                    _snprintf_s(
                        candidateMessage,
                        sizeof(candidateMessage),
                        _TRUNCATE,
                        "collision model candidate %d at %p has quality %d",
                        candidateCount,
                        model,
                        quality);
                    Log(candidateMessage);
                    if (quality == 3) {
                        LogPointer("selected quality-3 collision model at ", model);
                        return model;
                    }
                    if (firstCandidate == nullptr) {
                        firstCandidate = model;
                    }
                }
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                // A page can change protection while scanning. Continue at the next region.
            }
        }
        if (regionBytes == 0 || regionBase > maximum - regionBytes) {
            break;
        }
        address = regionBase + regionBytes;
    }
    if (firstCandidate != nullptr) {
        Log("no quality-3 model was present; selecting the first collision model candidate");
        return firstCandidate;
    }
    Log("collision model scan found no embedded CCollisionThread");
    return nullptr;
}

void LogCollisionResultState(const char* phase, void* model);

LONG LogIdleException(EXCEPTION_POINTERS* information, const char* name) {
    char failureMessage[256]{};
    _snprintf_s(
        failureMessage,
        sizeof(failureMessage),
        _TRUNCATE,
        "MainWindow::%s raised structured exception 0x%08lX at %p",
        name,
        information->ExceptionRecord->ExceptionCode,
        information->ExceptionRecord->ExceptionAddress);
    Log(failureMessage);
    return EXCEPTION_EXECUTE_HANDLER;
}

bool InvokeMainWindowIdle(MainWindowIdleFn function, void* mainWindow, const char* name) {
    __try {
        char beginMessage[128]{};
        _snprintf_s(beginMessage, sizeof(beginMessage), _TRUNCATE, "invoking MainWindow::%s", name);
        Log(beginMessage);
        function(mainWindow);
        char endMessage[128]{};
        _snprintf_s(endMessage, sizeof(endMessage), _TRUNCATE, "MainWindow::%s returned", name);
        Log(endMessage);
        return true;
    } __except (LogIdleException(GetExceptionInformation(), name)) {
        return false;
    }
}

std::ptrdiff_t GeneratedCollisionInputCount(void* model) {
    if (model == nullptr) {
        return 0;
    }
    __try {
        auto* bytes = static_cast<unsigned char*>(model);
        const auto begin = *reinterpret_cast<std::uintptr_t*>(bytes + 0xD8);
        const auto end = *reinterpret_cast<std::uintptr_t*>(bytes + 0xE0);
        if (end < begin || (end - begin) % sizeof(void*) != 0) {
            return -1;
        }
        return static_cast<std::ptrdiff_t>((end - begin) / sizeof(void*));
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -1;
    }
}

bool ForcePostCollisionComputation() {
    void* collisionModel = FindCollisionModelFromEmbeddedThread();
    if (collisionModel == nullptr) {
        if (void* treeWindow = FindTreeWindow()) {
            collisionModel = static_cast<unsigned char*>(treeWindow) + kTreeWindowModelOffset;
        }
    }
    if (collisionModel == nullptr) {
        return false;
    }

    __try {
        auto* modelBytes = static_cast<unsigned char*>(collisionModel);
        gCollisionModel.store(collisionModel, std::memory_order_release);
        auto* quality = reinterpret_cast<int*>(modelBytes + kCoreModelCollisionQualityOffset);
        char qualityMessage[128]{};
        _snprintf_s(
            qualityMessage,
            sizeof(qualityMessage),
            _TRUNCATE,
            "collision quality before forced compute: %d; setting quality 3 in memory",
            *quality);
        Log(qualityMessage);
        *quality = 3;

        LogCollisionResultState("before model dirty/update", collisionModel);
        gMarkCollisionDirty(modelBytes);

        auto** controllerStorage = reinterpret_cast<void**>(
            gSpeedTreeBase + kApplicationControllerPointerRva);
        void* controller = *controllerStorage;
        if (controller == nullptr) {
            Log("force compute failed: application controller is null");
            return false;
        }
        void** controllerVtable = *reinterpret_cast<void***>(controller);
        if (controllerVtable == nullptr) {
            Log("force compute failed: application controller vtable is null");
            return false;
        }
        auto update = reinterpret_cast<ApplicationUpdateFn>(controllerVtable[0xA0 / sizeof(void*)]);
        if (update == nullptr) {
            Log("force compute failed: application update function is null");
            return false;
        }
        LogPointer("application controller update target is ", reinterpret_cast<void*>(update));
        auto* mainWindow = static_cast<unsigned char*>(controller) - 0x28;
        if (!gQObjectInherits(mainWindow, "MainWindow")) {
            Log("force compute failed: adjusted application object is not MainWindow");
            return false;
        }
        LogPointer("application controller interface at ", controller);
        LogPointer("resolved MainWindow at ", mainWindow);
        char mainWindowState[256]{};
        _snprintf_s(
            mainWindowState,
            sizeof(mainWindowState),
            _TRUNCATE,
            "MainWindow flags: cli=%u idle_block=%u idle_draw_block=%u busy=%u timers=%p/%p",
            static_cast<unsigned int>(mainWindow[0x615]),
            static_cast<unsigned int>(mainWindow[0x616]),
            static_cast<unsigned int>(mainWindow[0x519]),
            static_cast<unsigned int>(mainWindow[0x3B8]),
            *reinterpret_cast<void**>(mainWindow + 0x310),
            *reinterpret_cast<void**>(mainWindow + 0x318));
        Log(mainWindowState);
        Log("collision model marked dirty; invoking SpeedTree application update");
        update(controller);
        LogCollisionResultState("after application update", collisionModel);

        Log("running MainWindow OnIdle/OnIdleDraw bake path before collision");
        bool canInvokeOnIdle = InvokeMainWindowIdle(gMainWindowOnIdle, mainWindow, "OnIdle");
        bool canInvokeOnIdleDraw = InvokeMainWindowIdle(
            gMainWindowOnIdleDraw,
            mainWindow,
            "OnIdleDraw");

        ULONGLONG lastDirectIdle = GetTickCount64();
        const ULONGLONG generationDeadline = lastDirectIdle + gTimeoutMs;
        while (GetTickCount64() < generationDeadline) {
            PumpMainThreadEvents();
            if (gCollisionThread.load(std::memory_order_acquire) != nullptr) {
                Log("full model bake started CCollisionThread");
                return true;
            }
            if (GeneratedCollisionInputCount(collisionModel) > 0) {
                break;
            }

            const ULONGLONG now = GetTickCount64();
            if (now - lastDirectIdle >= 250) {
                if (canInvokeOnIdle) {
                    canInvokeOnIdle = InvokeMainWindowIdle(
                        gMainWindowOnIdle,
                        mainWindow,
                        "OnIdle");
                }
                if (canInvokeOnIdleDraw) {
                    canInvokeOnIdleDraw = InvokeMainWindowIdle(
                        gMainWindowOnIdleDraw,
                        mainWindow,
                        "OnIdleDraw");
                }
                lastDirectIdle = now;
            }
            Sleep(5);
        }

        if (gCollisionThread.load(std::memory_order_acquire) != nullptr) {
            Log("full model bake started CCollisionThread");
            return true;
        }

        const std::ptrdiff_t inputCount = GeneratedCollisionInputCount(collisionModel);
        LogCollisionResultState("after MainWindow bake path", collisionModel);
        if (inputCount <= 0) {
            AbortExport(
                kNoGeneratedCollisionInputsExitCode,
                "export aborted: MainWindow bake did not generate collision inputs");
        }

        Log("model bake generated collision inputs; executing collision core synchronously");
        gCollisionCompute(collisionModel);
        LogCollisionResultState("after synchronous collision core", collisionModel);
        Log("invoking collision Done finalizer synchronously");
        gCollisionDone(collisionModel);
        LogCollisionResultState("after synchronous Done", collisionModel);
        gSynchronousCollisionCompleted.store(true, std::memory_order_release);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("force compute failed with a structured exception");
        return false;
    }
}

std::ptrdiff_t PendingCollisionResultCount(void* model) {
    if (model == nullptr) {
        return 0;
    }
    __try {
        auto* bytes = static_cast<unsigned char*>(model);
        const auto begin = *reinterpret_cast<std::uintptr_t*>(bytes + 0x9BE0);
        const auto end = *reinterpret_cast<std::uintptr_t*>(bytes + 0x9BE8);
        if (end < begin || (end - begin) % 0x20 != 0) {
            return -1;
        }
        return static_cast<std::ptrdiff_t>((end - begin) / 0x20);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -1;
    }
}

void LogCollisionResultState(const char* phase, void* model) {
    char message[256]{};
    if (model == nullptr) {
        _snprintf_s(message, sizeof(message), _TRUNCATE, "%s: model is null", phase);
    } else {
        __try {
            auto* bytes = static_cast<unsigned char*>(model);
            _snprintf_s(
                message,
                sizeof(message),
                _TRUNCATE,
                "%s: pending=%td quality=%d shade=%u cancel=%u post=%u inputs=%td/%td counters=%d/%d",
                phase,
                PendingCollisionResultCount(model),
                *reinterpret_cast<int*>(bytes + 0x9BD8),
                static_cast<unsigned int>(bytes[0x9BDC]),
                static_cast<unsigned int>(bytes[0x9C68]),
                static_cast<unsigned int>(bytes[0x9C89]),
                (*reinterpret_cast<std::uintptr_t*>(bytes + 0xE0) -
                    *reinterpret_cast<std::uintptr_t*>(bytes + 0xD8)) / sizeof(void*),
                (*reinterpret_cast<std::uintptr_t*>(bytes + 0xC8) -
                    *reinterpret_cast<std::uintptr_t*>(bytes + 0xC0)) / sizeof(void*),
                *reinterpret_cast<int*>(bytes + 0x9C8C),
                *reinterpret_cast<int*>(bytes + 0x9C94));
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            _snprintf_s(message, sizeof(message), _TRUNCATE, "%s: state read failed", phase);
        }
    }
    Log(message);
}

struct CollisionRecordAggregates {
    double floats[9]{};
    std::uint64_t primitiveCount = 0;
    std::uint64_t flags[9]{};
};

struct CollisionTreeCounts {
    std::size_t nodes = 0;
    std::size_t primaryRecords = 0;
    std::size_t secondaryRecords = 0;
    std::size_t primaryPruned = 0;
    std::size_t secondaryPruned = 0;
    std::uint64_t primaryHashSum = 0;
    std::uint64_t secondaryHashSum = 0;
    std::uint64_t primaryHashXor = 0;
    std::uint64_t secondaryHashXor = 0;
    CollisionRecordAggregates primaryAggregates{};
    CollisionRecordAggregates secondaryAggregates{};
};

std::uint64_t HashCollisionRecordBytes(const unsigned char* record) {
    std::uint64_t hash = 1469598103934665603ULL;
    const auto append = [&hash](const unsigned char* bytes, std::size_t size) {
        for (std::size_t index = 0; index < size; ++index) {
            hash ^= bytes[index];
            hash *= 1099511628211ULL;
        }
    };
    // Skip pointer and padding fields.  These ranges contain only the bounds,
    // primitive counts, prune weights, and flags used by spatial resolution.
    append(record, 0x22);
    append(record + 0x28, 0x01);
    append(record + 0x40, 0x07);
    append(record + 0x48, 0x06);
    return hash;
}

void CountCollisionRecordVector(
    const unsigned char* begin,
    const unsigned char* end,
    std::size_t& records,
    std::size_t& pruned,
    std::uint64_t& hashSum,
    std::uint64_t& hashXor,
    CollisionRecordAggregates& aggregates) {
    constexpr std::size_t kRecordSize = 0x68;
    if (begin == nullptr || end < begin ||
        static_cast<std::size_t>(end - begin) % kRecordSize != 0) {
        return;
    }
    const std::size_t count = static_cast<std::size_t>(end - begin) / kRecordSize;
    if (count > 10000000) {
        return;
    }
    records += count;
    for (std::size_t index = 0; index < count; ++index) {
        const auto* record = begin + index * kRecordSize;
        const std::uint64_t recordHash = HashCollisionRecordBytes(record);
        hashSum += recordHash;
        hashXor ^= recordHash;
        constexpr std::size_t floatOffsets[] = {
            0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x1C, 0x48, 0x64,
        };
        for (std::size_t field = 0; field < std::size(floatOffsets); ++field) {
            aggregates.floats[field] +=
                *reinterpret_cast<const float*>(record + floatOffsets[field]);
        }
        aggregates.primitiveCount +=
            *reinterpret_cast<const std::uint32_t*>(record + 0x40);
        constexpr std::size_t flagOffsets[] = {
            0x20, 0x21, 0x28, 0x44, 0x45, 0x46, 0x4C, 0x4D, 0x60,
        };
        for (std::size_t field = 0; field < std::size(flagOffsets); ++field) {
            aggregates.flags[field] += record[flagOffsets[field]];
        }
        if (record[0x44] != 0) {
            ++pruned;
        }
    }
}

void LogCollisionTreeSummary(const char* phase, void* model) {
    if (model == nullptr) {
        return;
    }
    __try {
        auto* map = static_cast<unsigned char*>(model) + 0x9BC8;
        auto* header = *reinterpret_cast<unsigned char**>(map);
        if (header == nullptr) {
            return;
        }
        auto* node = *reinterpret_cast<unsigned char**>(header);
        CollisionTreeCounts counts{};
        while (node != header && counts.nodes < 1000000) {
            ++counts.nodes;
            auto* primaryBegin = *reinterpret_cast<unsigned char**>(node + 0x48);
            auto* primaryEnd = *reinterpret_cast<unsigned char**>(node + 0x50);
            auto* secondaryBegin = *reinterpret_cast<unsigned char**>(node + 0x88);
            auto* secondaryEnd = *reinterpret_cast<unsigned char**>(node + 0x90);
            CountCollisionRecordVector(
                primaryBegin,
                primaryEnd,
                counts.primaryRecords,
                counts.primaryPruned,
                counts.primaryHashSum,
                counts.primaryHashXor,
                counts.primaryAggregates);
            CountCollisionRecordVector(
                secondaryBegin,
                secondaryEnd,
                counts.secondaryRecords,
                counts.secondaryPruned,
                counts.secondaryHashSum,
                counts.secondaryHashXor,
                counts.secondaryAggregates);

            auto* right = *reinterpret_cast<unsigned char**>(node + 0x10);
            if (right[0x19] == 0) {
                node = right;
                auto* left = *reinterpret_cast<unsigned char**>(node);
                while (left[0x19] == 0) {
                    node = left;
                    left = *reinterpret_cast<unsigned char**>(node);
                }
            } else {
                auto* parent = *reinterpret_cast<unsigned char**>(node + 0x08);
                while (parent[0x19] == 0 &&
                       node == *reinterpret_cast<unsigned char**>(parent + 0x10)) {
                    node = parent;
                    parent = *reinterpret_cast<unsigned char**>(parent + 0x08);
                }
                node = parent;
            }
        }
        char message[512]{};
        _snprintf_s(
            message,
            sizeof(message),
            _TRUNCATE,
            "%s: nodes=%zu primary=%zu pruned=%zu hash=%016llX/%016llX secondary=%zu pruned=%zu hash=%016llX/%016llX map_size=%zu",
            phase,
            counts.nodes,
            counts.primaryRecords,
            counts.primaryPruned,
            static_cast<unsigned long long>(counts.primaryHashSum),
            static_cast<unsigned long long>(counts.primaryHashXor),
            counts.secondaryRecords,
            counts.secondaryPruned,
            static_cast<unsigned long long>(counts.secondaryHashSum),
            static_cast<unsigned long long>(counts.secondaryHashXor),
            *reinterpret_cast<std::size_t*>(map + sizeof(void*)));
        Log(message);
        const auto logAggregates = [phase](
            const char* group,
            const CollisionRecordAggregates& values) {
            char aggregateMessage[1024]{};
            _snprintf_s(
                aggregateMessage,
                sizeof(aggregateMessage),
                _TRUNCATE,
                "%s %s aggregates: f=%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,%.9g primitives=%llu flags=%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu",
                phase,
                group,
                values.floats[0],
                values.floats[1],
                values.floats[2],
                values.floats[3],
                values.floats[4],
                values.floats[5],
                values.floats[6],
                values.floats[7],
                values.floats[8],
                static_cast<unsigned long long>(values.primitiveCount),
                static_cast<unsigned long long>(values.flags[0]),
                static_cast<unsigned long long>(values.flags[1]),
                static_cast<unsigned long long>(values.flags[2]),
                static_cast<unsigned long long>(values.flags[3]),
                static_cast<unsigned long long>(values.flags[4]),
                static_cast<unsigned long long>(values.flags[5]),
                static_cast<unsigned long long>(values.flags[6]),
                static_cast<unsigned long long>(values.flags[7]),
                static_cast<unsigned long long>(values.flags[8]));
            Log(aggregateMessage);
        };
        logAggregates("primary", counts.primaryAggregates);
        logAggregates("secondary", counts.secondaryAggregates);
        auto* modelBytes = static_cast<unsigned char*>(model);
        const int shadeResolution =
            *reinterpret_cast<const int*>(modelBytes + 0x9C38);
        auto* shadeBegin = *reinterpret_cast<const unsigned char**>(modelBytes + 0x9C40);
        auto* shadeEnd = *reinterpret_cast<const unsigned char**>(modelBytes + 0x9C48);
        std::ptrdiff_t shadeCount = -1;
        double shadeSum = 0.0;
        float shadeMinimum = 0.0f;
        float shadeMaximum = 0.0f;
        if (shadeBegin != nullptr && shadeEnd >= shadeBegin &&
            static_cast<std::size_t>(shadeEnd - shadeBegin) % 0x10 == 0) {
            shadeCount = (shadeEnd - shadeBegin) / 0x10;
            if (shadeCount > 0 && shadeCount <= 10000000) {
                shadeMinimum = *reinterpret_cast<const float*>(shadeBegin + 0x0C);
                shadeMaximum = shadeMinimum;
                for (std::ptrdiff_t index = 0; index < shadeCount; ++index) {
                    const float value = *reinterpret_cast<const float*>(
                        shadeBegin + index * 0x10 + 0x0C);
                    shadeSum += value;
                    if (value < shadeMinimum) {
                        shadeMinimum = value;
                    }
                    if (value > shadeMaximum) {
                        shadeMaximum = value;
                    }
                }
            }
        }
        const auto* shadeBounds = reinterpret_cast<const float*>(modelBytes + 0x5258);
        char shadeMessage[768]{};
        _snprintf_s(
            shadeMessage,
            sizeof(shadeMessage),
            _TRUNCATE,
            "%s shade volume: resolution=%d count=%td sum=%.9g min=%.9g max=%.9g bounds=%.9g,%.9g,%.9g,%.9g,%.9g,%.9g",
            phase,
            shadeResolution,
            shadeCount,
            shadeSum,
            shadeMinimum,
            shadeMaximum,
            shadeBounds[0],
            shadeBounds[1],
            shadeBounds[2],
            shadeBounds[3],
            shadeBounds[4],
            shadeBounds[5]);
        Log(shadeMessage);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("collision tree inspection raised an exception");
    }
}

void LogCollisionSpatialInput(CONTEXT* context) {
    if (context == nullptr) {
        return;
    }
    __try {
        auto* model = static_cast<unsigned char*>(
            gCollisionModel.load(std::memory_order_acquire));
        auto* frame = reinterpret_cast<unsigned char*>(context->Rbp);
        auto* begin = *reinterpret_cast<unsigned char**>(frame + 0x10);
        auto* end = *reinterpret_cast<unsigned char**>(frame + 0x18);
        std::ptrdiff_t count = -1;
        if (begin != nullptr && end >= begin &&
            static_cast<std::size_t>(end - begin) % 0x40 == 0) {
            count = (end - begin) / 0x40;
        }
        void* source = model == nullptr
            ? nullptr
            : *reinterpret_cast<void**>(model + 0x50F0);
        std::uintptr_t vtableRva = 0;
        std::uintptr_t methodA20Rva = 0;
        const char* typeName = "<null>";
        if (source != nullptr) {
            auto** vtable = *reinterpret_cast<void***>(source);
            vtableRva = reinterpret_cast<std::uintptr_t>(vtable) - gSpeedTreeBase;
            methodA20Rva = reinterpret_cast<std::uintptr_t>(vtable[0xA20 / sizeof(void*)]) -
                gSpeedTreeBase;
            if (const char* name = ReadSpeedTreeRttiName(source); name != nullptr) {
                typeName = name;
            }
        }
        char message[512]{};
        _snprintf_s(
            message,
            sizeof(message),
            _TRUNCATE,
            "%s: count=%td begin=%p end=%p source=%p type=%s vtable=0x%llX method_A20=0x%llX",
            gCollisionSpatialInputProbe.label,
            count,
            begin,
            end,
            source,
            typeName,
            static_cast<unsigned long long>(vtableRva),
            static_cast<unsigned long long>(methodA20Rva));
        Log(message);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("collision spatial input inspection raised an exception");
    }
}

LONG CALLBACK HandleNativeStateProbe(EXCEPTION_POINTERS* information) {
    if (information == nullptr || information->ExceptionRecord == nullptr ||
        information->ContextRecord == nullptr) {
        return EXCEPTION_CONTINUE_SEARCH;
    }
    if (information->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION &&
        !gLoggedNativeAccessViolation.exchange(true, std::memory_order_acq_rel)) {
        const auto address = reinterpret_cast<std::uintptr_t>(
            information->ExceptionRecord->ExceptionAddress);
        char message[512]{};
        _snprintf_s(
            message,
            sizeof(message),
            _TRUNCATE,
            "native CLI access violation at RVA=0x%llX operation=%llu address=%p rcx=%p rdx=%p r8=%p r9=%p",
            static_cast<unsigned long long>(address - gSpeedTreeBase),
            static_cast<unsigned long long>(information->ExceptionRecord->ExceptionInformation[0]),
            reinterpret_cast<void*>(information->ExceptionRecord->ExceptionInformation[1]),
            reinterpret_cast<void*>(information->ContextRecord->Rcx),
            reinterpret_cast<void*>(information->ContextRecord->Rdx),
            reinterpret_cast<void*>(information->ContextRecord->R8),
            reinterpret_cast<void*>(information->ContextRecord->R9));
        Log(message);
        LogSpeedTreeCallStack("native CLI access violation call stack");
        return EXCEPTION_CONTINUE_SEARCH;
    }
    if (information->ExceptionRecord->ExceptionCode != EXCEPTION_BREAKPOINT) {
        return EXCEPTION_CONTINUE_SEARCH;
    }
    auto* address = static_cast<unsigned char*>(
        information->ExceptionRecord->ExceptionAddress);
    auto* collisionTreeTarget = reinterpret_cast<unsigned char*>(
        gSpeedTreeBase + gCollisionTreeProbe.rva);
    if (gCollisionTreeProbe.armed && address == collisionTreeTarget) {
        DWORD oldProtection = 0;
        if (!VirtualProtect(
                collisionTreeTarget,
                1,
                PAGE_EXECUTE_READWRITE,
                &oldProtection)) {
            return EXCEPTION_CONTINUE_SEARCH;
        }
        *collisionTreeTarget = gCollisionTreeProbe.original;
        FlushInstructionCache(GetCurrentProcess(), collisionTreeTarget, 1);
        DWORD ignoredProtection = 0;
        VirtualProtect(
            collisionTreeTarget,
            1,
            oldProtection,
            &ignoredProtection);
        gCollisionTreeProbe.armed = false;
        LogCollisionTreeSummary(
            gCollisionTreeProbe.label,
            gCollisionModel.load(std::memory_order_acquire));
        information->ContextRecord->Rip =
            reinterpret_cast<DWORD64>(collisionTreeTarget);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    auto* collisionSpatialInputTarget = reinterpret_cast<unsigned char*>(
        gSpeedTreeBase + gCollisionSpatialInputProbe.rva);
    if (gCollisionSpatialInputProbe.armed && address == collisionSpatialInputTarget) {
        DWORD oldProtection = 0;
        if (!VirtualProtect(
                collisionSpatialInputTarget,
                1,
                PAGE_EXECUTE_READWRITE,
                &oldProtection)) {
            return EXCEPTION_CONTINUE_SEARCH;
        }
        *collisionSpatialInputTarget = gCollisionSpatialInputProbe.original;
        FlushInstructionCache(GetCurrentProcess(), collisionSpatialInputTarget, 1);
        DWORD ignoredProtection = 0;
        VirtualProtect(
            collisionSpatialInputTarget,
            1,
            oldProtection,
            &ignoredProtection);
        gCollisionSpatialInputProbe.armed = false;
        LogCollisionSpatialInput(information->ContextRecord);
        information->ContextRecord->Rip =
            reinterpret_cast<DWORD64>(collisionSpatialInputTarget);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    for (auto& probe : gNativeStateProbes) {
        auto* target = reinterpret_cast<unsigned char*>(gSpeedTreeBase + probe.rva);
        if (!probe.armed || address != target) {
            continue;
        }
        DWORD oldProtection = 0;
        if (!VirtualProtect(target, 1, PAGE_EXECUTE_READWRITE, &oldProtection)) {
            return EXCEPTION_CONTINUE_SEARCH;
        }
        *target = probe.original;
        FlushInstructionCache(GetCurrentProcess(), target, 1);
        DWORD ignoredProtection = 0;
        VirtualProtect(target, 1, oldProtection, &ignoredProtection);
        probe.armed = false;
        LogCollisionResultState(
            probe.label,
            gCollisionModel.load(std::memory_order_acquire));
        information->ContextRecord->Rip = reinterpret_cast<DWORD64>(target);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

bool EnsureNativeStateProbeHandler() {
    if (gNativeStateProbeHandler != nullptr) {
        return true;
    }
    gNativeStateProbeHandler = AddVectoredExceptionHandler(1, HandleNativeStateProbe);
    if (gNativeStateProbeHandler == nullptr) {
        Log("native exporter state probes could not install their exception handler");
        return false;
    }
    return true;
}

bool InstallCollisionTreeProbe() {
    if (!EnsureNativeStateProbeHandler()) {
        return false;
    }
    auto* target = reinterpret_cast<unsigned char*>(
        gSpeedTreeBase + gCollisionTreeProbe.rva);
    gCollisionTreeProbe.original = *target;
    DWORD oldProtection = 0;
    if (!VirtualProtect(target, 1, PAGE_EXECUTE_READWRITE, &oldProtection)) {
        Log("collision tree probe could not change page protection");
        return false;
    }
    *target = 0xCC;
    FlushInstructionCache(GetCurrentProcess(), target, 1);
    DWORD ignoredProtection = 0;
    VirtualProtect(target, 1, oldProtection, &ignoredProtection);
    gCollisionTreeProbe.armed = true;
    auto* spatialInputTarget = reinterpret_cast<unsigned char*>(
        gSpeedTreeBase + gCollisionSpatialInputProbe.rva);
    gCollisionSpatialInputProbe.original = *spatialInputTarget;
    if (!VirtualProtect(
            spatialInputTarget,
            1,
            PAGE_EXECUTE_READWRITE,
            &oldProtection)) {
        Log("collision spatial input probe could not change page protection");
        return false;
    }
    *spatialInputTarget = 0xCC;
    FlushInstructionCache(GetCurrentProcess(), spatialInputTarget, 1);
    VirtualProtect(spatialInputTarget, 1, oldProtection, &ignoredProtection);
    gCollisionSpatialInputProbe.armed = true;
    return true;
}

bool InstallNativeStateProbes() {
    if (!EnsureNativeStateProbeHandler()) {
        return false;
    }
    for (auto& probe : gNativeStateProbes) {
        auto* target = reinterpret_cast<unsigned char*>(gSpeedTreeBase + probe.rva);
        if (*target != 0xE8) {
            Log("native exporter state probe rejected a non-call instruction");
            return false;
        }
        probe.original = *target;
        DWORD oldProtection = 0;
        if (!VirtualProtect(target, 1, PAGE_EXECUTE_READWRITE, &oldProtection)) {
            Log("native exporter state probe could not change page protection");
            return false;
        }
        *target = 0xCC;
        FlushInstructionCache(GetCurrentProcess(), target, 1);
        DWORD ignoredProtection = 0;
        VirtualProtect(target, 1, oldProtection, &ignoredProtection);
        probe.armed = true;
    }
    return true;
}

void RemoveNativeStateProbes() {
    for (auto& probe : gNativeStateProbes) {
        if (!probe.armed) {
            continue;
        }
        auto* target = reinterpret_cast<unsigned char*>(gSpeedTreeBase + probe.rva);
        DWORD oldProtection = 0;
        if (VirtualProtect(target, 1, PAGE_EXECUTE_READWRITE, &oldProtection)) {
            *target = probe.original;
            FlushInstructionCache(GetCurrentProcess(), target, 1);
            DWORD ignoredProtection = 0;
            VirtualProtect(target, 1, oldProtection, &ignoredProtection);
        }
        probe.armed = false;
    }
    if (gCollisionTreeProbe.armed) {
        auto* target = reinterpret_cast<unsigned char*>(
            gSpeedTreeBase + gCollisionTreeProbe.rva);
        DWORD oldProtection = 0;
        if (VirtualProtect(target, 1, PAGE_EXECUTE_READWRITE, &oldProtection)) {
            *target = gCollisionTreeProbe.original;
            FlushInstructionCache(GetCurrentProcess(), target, 1);
            DWORD ignoredProtection = 0;
            VirtualProtect(target, 1, oldProtection, &ignoredProtection);
        }
        gCollisionTreeProbe.armed = false;
    }
    if (gCollisionSpatialInputProbe.armed) {
        auto* target = reinterpret_cast<unsigned char*>(
            gSpeedTreeBase + gCollisionSpatialInputProbe.rva);
        DWORD oldProtection = 0;
        if (VirtualProtect(target, 1, PAGE_EXECUTE_READWRITE, &oldProtection)) {
            *target = gCollisionSpatialInputProbe.original;
            FlushInstructionCache(GetCurrentProcess(), target, 1);
            DWORD ignoredProtection = 0;
            VirtualProtect(target, 1, oldProtection, &ignoredProtection);
        }
        gCollisionSpatialInputProbe.armed = false;
    }
    if (gNativeStateProbeHandler != nullptr) {
        RemoveVectoredExceptionHandler(gNativeStateProbeHandler);
        gNativeStateProbeHandler = nullptr;
    }
}

std::string WideToUtf8(const wchar_t* text) {
    if (text == nullptr || *text == L'\0') {
        return {};
    }
    const int required = WideCharToMultiByte(
        CP_UTF8,
        0,
        text,
        -1,
        nullptr,
        0,
        nullptr,
        nullptr);
    if (required <= 1) {
        return {};
    }
    std::string result(static_cast<std::size_t>(required), '\0');
    WideCharToMultiByte(
        CP_UTF8,
        0,
        text,
        -1,
        result.data(),
        required,
        nullptr,
        nullptr);
    result.pop_back();
    return result;
}

void* ReadMainWindowMdiArea(void* mainWindow) {
    __try {
        return *reinterpret_cast<void**>(
            static_cast<unsigned char*>(mainWindow) + 0x298);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return nullptr;
    }
}

bool ReadPipeExact(HANDLE pipe, void* destination, std::size_t byteCount) {
    auto* bytes = static_cast<unsigned char*>(destination);
    std::size_t completed = 0;
    while (completed < byteCount) {
        const DWORD chunk = static_cast<DWORD>((std::min)(
            byteCount - completed,
            static_cast<std::size_t>(0x7ffff000)));
        DWORD transferred = 0;
        if (!ReadFile(pipe, bytes + completed, chunk, &transferred, nullptr) ||
            transferred == 0) {
            return false;
        }
        completed += transferred;
    }
    return true;
}

bool WritePipeExact(HANDLE pipe, const void* source, std::size_t byteCount) {
    const auto* bytes = static_cast<const unsigned char*>(source);
    std::size_t completed = 0;
    while (completed < byteCount) {
        const DWORD chunk = static_cast<DWORD>((std::min)(
            byteCount - completed,
            static_cast<std::size_t>(0x7ffff000)));
        DWORD transferred = 0;
        if (!WriteFile(pipe, bytes + completed, chunk, &transferred, nullptr) ||
            transferred == 0) {
            return false;
        }
        completed += transferred;
    }
    return true;
}

bool SessionRequestPathsAreTerminated(
    const speedtree_collision_cli::SessionRequest& request) {
    return std::wmemchr(
               request.input,
               L'\0',
               speedtree_collision_cli::kSessionPathCapacity) != nullptr &&
        std::wmemchr(
               request.output,
               L'\0',
               speedtree_collision_cli::kSessionPathCapacity) != nullptr &&
        std::wmemchr(
               request.exportOptions,
               L'\0',
               speedtree_collision_cli::kSessionPathCapacity) != nullptr;
}

bool StartPersistentSessionJob(
    const speedtree_collision_cli::SessionRequest& request,
    speedtree_collision_cli::SessionResponse& response) {
    if (!SessionRequestPathsAreTerminated(request) ||
        request.input[0] == L'\0' || request.output[0] == L'\0' ||
        request.exportOptions[0] == L'\0') {
        response.status = ERROR_INVALID_DATA;
        wcscpy_s(response.message, L"The persistent export request contains an invalid path.");
        return false;
    }
    if (GetFileAttributesW(request.input) == INVALID_FILE_ATTRIBUTES ||
        GetFileAttributesW(request.exportOptions) == INVALID_FILE_ATTRIBUTES) {
        response.status = ERROR_FILE_NOT_FOUND;
        wcscpy_s(response.message, L"The input SPM or export-options file was not found.");
        return false;
    }
    if (gSessionJobActive.load(std::memory_order_acquire)) {
        response.status = ERROR_BUSY;
        wcscpy_s(response.message, L"The persistent SpeedTree session is already exporting.");
        return false;
    }

    wcsncpy_s(gPersistentInputPath, request.input, _TRUNCATE);
    wcsncpy_s(gGuiExportPath, request.output, _TRUNCATE);
    wcsncpy_s(gGuiExportOptionsPath, request.exportOptions, _TRUNCATE);
    gGuiGameExport = request.gameExport != 0;
    gTimeoutMs = request.timeoutMs >= 1000 ? request.timeoutMs : kDefaultTimeoutMs;
    gHookStartTick = GetTickCount64();
    gSessionAnchorMdiSubWindow = nullptr;
    gSessionTargetTreeWindow = nullptr;
    gSessionTargetMdiSubWindow = nullptr;
    gSessionTargetStateLogged = false;
    gSessionClosingTarget.store(false, std::memory_order_release);
    gSessionOpenCallActive.store(false, std::memory_order_release);
    gSessionForceDiscardClose.store(false, std::memory_order_release);
    gCollisionModel.store(nullptr, std::memory_order_release);
    gCollisionThread.store(nullptr, std::memory_order_release);
    gCollisionStartCount.store(0, std::memory_order_release);
    gSynchronousCollisionCompleted.store(false, std::memory_order_release);
    gGuiBakeRequested.store(false, std::memory_order_release);
    gGuiExportStarted.store(false, std::memory_order_release);
    gSessionJobStatus.store(ERROR_IO_PENDING, std::memory_order_release);
    gSessionJobComplete.store(false, std::memory_order_release);
    gPersistentJobPhase = PersistentJobPhase::WaitForAnchor;
    // Publish the active state only after every per-job field is initialized.
    // The GUI driver uses this release/acquire edge before reading those fields.
    gSessionJobActive.store(true, std::memory_order_release);

    const std::string input = WideToUtf8(request.input);
    char message[1024]{};
    _snprintf_s(
        message,
        sizeof(message),
        _TRUNCATE,
        "persistent session accepted export job: %s",
        input.c_str());
    Log(message);
    return true;
}

void CompletePersistentSessionJob(DWORD status) {
    gSessionJobStatus.store(status, std::memory_order_release);
    gSessionJobComplete.store(true, std::memory_order_release);
}

DWORD WINAPI RunPersistentSessionPipeServer(void*) {
    Log("persistent session named-pipe server started");
    for (;;) {
        HANDLE pipe = CreateNamedPipeW(
            speedtree_collision_cli::kSessionPipeName,
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,
            4096,
            4096,
            0,
            nullptr);
        if (pipe == INVALID_HANDLE_VALUE) {
            Log("persistent session could not create its named pipe");
            return GetLastError();
        }
        const bool connected = ConnectNamedPipe(pipe, nullptr) != FALSE ||
            GetLastError() == ERROR_PIPE_CONNECTED;
        auto request = std::unique_ptr<speedtree_collision_cli::SessionRequest>(
            new (std::nothrow) speedtree_collision_cli::SessionRequest{});
        speedtree_collision_cli::SessionResponse response{};
        response.speedTreeProcessId = GetCurrentProcessId();
        bool terminateAfterResponse = false;
        if (!connected || request == nullptr ||
            !ReadPipeExact(pipe, request.get(), sizeof(*request))) {
            response.status = ERROR_BROKEN_PIPE;
            wcscpy_s(response.message, L"The persistent session request could not be read.");
        } else if (request->magic != speedtree_collision_cli::kSessionProtocolMagic ||
                   request->version != speedtree_collision_cli::kSessionProtocolVersion) {
            response.status = ERROR_REVISION_MISMATCH;
            wcscpy_s(response.message, L"The persistent session protocol version does not match.");
        } else if (request->command == speedtree_collision_cli::SessionCommand::Ping) {
            response.status = ERROR_SUCCESS;
            wcscpy_s(response.message, L"The persistent SpeedTree session is ready.");
        } else if (request->command == speedtree_collision_cli::SessionCommand::Shutdown) {
            response.status = ERROR_SUCCESS;
            wcscpy_s(response.message, L"The persistent SpeedTree session is shutting down.");
            terminateAfterResponse = true;
        } else if (request->command != speedtree_collision_cli::SessionCommand::Export) {
            response.status = ERROR_INVALID_FUNCTION;
            wcscpy_s(response.message, L"The persistent session command is not supported.");
        } else if (StartPersistentSessionJob(*request, response)) {
            const ULONGLONG deadline = GetTickCount64() + gTimeoutMs +
                kPersistentDocumentLoadTimeoutMs;
            while (!gSessionJobComplete.load(std::memory_order_acquire) &&
                   GetTickCount64() < deadline) {
                Sleep(25);
            }
            if (gSessionJobComplete.load(std::memory_order_acquire)) {
                response.status = gSessionJobStatus.load(std::memory_order_acquire);
                wcscpy_s(
                    response.message,
                    response.status == ERROR_SUCCESS
                        ? L"Post-collision export completed in the persistent SpeedTree session."
                        : L"The persistent SpeedTree export failed.");
            } else {
                response.status = WAIT_TIMEOUT;
                wcscpy_s(response.message, L"The persistent SpeedTree export timed out.");
                terminateAfterResponse = true;
            }
            gSessionJobActive.store(false, std::memory_order_release);
        }

        if (connected) {
            WritePipeExact(pipe, &response, sizeof(response));
            FlushFileBuffers(pipe);
            DisconnectNamedPipe(pipe);
        }
        CloseHandle(pipe);
        if (terminateAfterResponse) {
            TerminateProcess(GetCurrentProcess(), response.status);
            return response.status;
        }
    }
}

bool TargetCollisionInitializationComplete(void* model, void* collisionThread) {
    if (model == nullptr || collisionThread == nullptr ||
        gQThreadIsRunning(collisionThread)) {
        return false;
    }
    __try {
        if (*(static_cast<unsigned char*>(model) + 0x9C89) != 0) {
            return false;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
    return GeneratedCollisionInputCount(model) > 0;
}

bool CollisionThreadBelongsToModel(void* collisionThread, void* model) {
    if (collisionThread == nullptr || model == nullptr) {
        return false;
    }
    __try {
        return *reinterpret_cast<void**>(
            static_cast<unsigned char*>(collisionThread) + 0x10) == model;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

bool PreparePersistentJob(void* mainWindow) {
    if (!gSessionServerMode) {
        return true;
    }
    switch (gPersistentJobPhase) {
        case PersistentJobPhase::WaitForAnchor: {
            void* mdiArea = ReadMainWindowMdiArea(mainWindow);
            std::vector<void*> anchorSubWindows;
            if (mdiArea == nullptr ||
                !CollectMdiSubWindows(mdiArea, anchorSubWindows) ||
                anchorSubWindows.empty()) {
                return false;
            }
            if (anchorSubWindows.size() != 1) {
                AbortExport(
                    kHookRuntimeFailureExitCode,
                    "persistent session expected exactly one blank anchor document");
            }
            gSessionAnchorMdiSubWindow = anchorSubWindows.front();
            LogPointer(
                "persistent session blank anchor QMdiSubWindow is ",
                gSessionAnchorMdiSubWindow);
            gCollisionThread.store(nullptr, std::memory_order_release);
            gCollisionStartCount.store(0, std::memory_order_release);
            gPersistentJobPhase = PersistentJobPhase::OpenTarget;
            return false;
        }

        case PersistentJobPhase::OpenTarget: {
            Log("persistent session invoking MainWindow::fileOpen with an in-memory target path");
            gSessionOpenPathArmed.store(true, std::memory_order_release);
            gSessionOpenCallActive.store(true, std::memory_order_release);
            gPersistentJobPhase = PersistentJobPhase::WaitForTarget;
            if (!InvokeQtNoArgument(mainWindow, "fileOpen")) {
                gSessionOpenCallActive.store(false, std::memory_order_release);
                gSessionOpenPathArmed.store(false, std::memory_order_release);
                AbortExport(
                    kHookRuntimeFailureExitCode,
                    "persistent session could not invoke MainWindow::fileOpen");
            }
            gSessionOpenCallActive.store(false, std::memory_order_release);
            if (gSessionOpenPathArmed.exchange(false, std::memory_order_acq_rel)) {
                AbortExport(
                    kHookRuntimeFailureExitCode,
                    "MainWindow::fileOpen did not request an intercepted file path list");
            }
            Log("persistent session MainWindow::fileOpen returned after loading the target path");
            return false;
        }

        case PersistentJobPhase::WaitForTarget: {
            if (gSessionOpenCallActive.load(std::memory_order_acquire)) {
                return false;
            }
            void* mdiArea = ReadMainWindowMdiArea(mainWindow);
            std::vector<void*> subWindows;
            if (mdiArea == nullptr || !CollectMdiSubWindows(mdiArea, subWindows)) {
                return false;
            }
            void* activeSubWindow = gQMdiAreaActiveSubWindow(mdiArea);
            if (activeSubWindow == nullptr || !ContainsPointer(subWindows, activeSubWindow)) {
                return false;
            }
            void* targetCollisionThread = gCollisionThread.load(std::memory_order_acquire);
            if (targetCollisionThread == nullptr) {
                return false;
            }
            void* targetModel = static_cast<unsigned char*>(targetCollisionThread) -
                kEmbeddedCollisionThreadOffset;
            if (!CollisionThreadBelongsToModel(targetCollisionThread, targetModel)) {
                return false;
            }
            gSessionTargetTreeWindow = static_cast<unsigned char*>(targetModel) -
                kTreeWindowModelOffset;
            gSessionTargetMdiSubWindow = activeSubWindow;
            if (!gSessionTargetStateLogged) {
                gSessionTargetStateLogged = true;
                LogPointer("persistent session target CTreeWindow is ", gSessionTargetTreeWindow);
                LogPointer("persistent session target collision thread is ", targetCollisionThread);
                char targetState[192]{};
                _snprintf_s(
                    targetState,
                    sizeof(targetState),
                    _TRUNCATE,
                    "persistent target initial state: collision_starts=%u running=%u inputs=%lld pending=%lld",
                    gCollisionStartCount.load(std::memory_order_acquire),
                    static_cast<unsigned int>(gQThreadIsRunning(targetCollisionThread)),
                    static_cast<long long>(GeneratedCollisionInputCount(targetModel)),
                    static_cast<long long>(PendingCollisionResultCount(targetModel)));
                Log(targetState);
            }
            if (!TargetCollisionInitializationComplete(targetModel, targetCollisionThread)) {
                return false;
            }
            LogCollisionResultState(
                "persistent target initial collision pass completed",
                targetModel);
            if (!ContainsPointer(subWindows, gSessionTargetMdiSubWindow)) {
                return false;
            }
            void* replacementAnchor = nullptr;
            for (void* subWindow : subWindows) {
                if (subWindow == gSessionTargetMdiSubWindow) {
                    continue;
                }
                if (replacementAnchor != nullptr) {
                    AbortExport(
                        kHookRuntimeFailureExitCode,
                        "persistent session observed more than one reusable anchor document");
                }
                replacementAnchor = subWindow;
            }
            if (replacementAnchor == nullptr) {
                AbortExport(
                    kHookRuntimeFailureExitCode,
                    "persistent session could not find the replacement blank anchor document");
            }
            gSessionAnchorMdiSubWindow = replacementAnchor;
            LogPointer(
                "persistent session replacement blank anchor QMdiSubWindow is ",
                gSessionAnchorMdiSubWindow);
            if (gQMdiAreaActiveSubWindow(mdiArea) != gSessionTargetMdiSubWindow) {
                gQMdiAreaSetActiveSubWindow(mdiArea, gSessionTargetMdiSubWindow);
                if (gQMdiAreaActiveSubWindow(mdiArea) != gSessionTargetMdiSubWindow) {
                    return false;
                }
            }
            LogPointer(
                "persistent session captured the target active QMdiSubWindow at ",
                gSessionTargetMdiSubWindow);
            gCollisionModel.store(targetModel, std::memory_order_release);
            gCollisionThread.store(nullptr, std::memory_order_release);
            gCollisionStartCount.store(0, std::memory_order_release);
            gSynchronousCollisionCompleted.store(false, std::memory_order_release);
            gGuiBakeRequested.store(false, std::memory_order_release);
            gGuiExportStarted.store(false, std::memory_order_release);
            gHookStartTick = GetTickCount64();
            gPersistentJobPhase = PersistentJobPhase::Ready;
            Log("persistent session target is ready for collision bake");
            return true;
        }

        case PersistentJobPhase::Ready:
            return true;

        case PersistentJobPhase::Complete:
            return false;
    }
    return false;
}

void ClosePersistentTarget(void* mainWindow) {
    Log("persistent session closing only the exported target document");
    gSessionClosingTarget.store(true, std::memory_order_release);
    void* mdiArea = ReadMainWindowMdiArea(mainWindow);
    void* targetSubWindow = gSessionTargetMdiSubWindow;
    if (targetSubWindow == nullptr) {
        targetSubWindow = FindQObjectParentByClass(
            gSessionTargetTreeWindow,
            "QMdiSubWindow");
    }
    if (mdiArea == nullptr || targetSubWindow == nullptr) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "persistent session could not resolve the target QMdiSubWindow");
    }
    LogPointer("persistent session target QMdiSubWindow is ", targetSubWindow);
    gQMdiAreaSetActiveSubWindow(mdiArea, targetSubWindow);
    void* activeBeforeClose = gQMdiAreaActiveSubWindow(mdiArea);
    LogPointer("persistent session active QMdiSubWindow before close is ", activeBeforeClose);
    if (activeBeforeClose != targetSubWindow) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "persistent session could not activate the exported target QMdiSubWindow");
    }

    std::vector<void*> subWindowsBeforeClose;
    if (!CollectMdiSubWindows(mdiArea, subWindowsBeforeClose)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "persistent session could not enumerate SpeedTree MDI documents");
    }
    const auto targetIterator = std::find(
        subWindowsBeforeClose.begin(),
        subWindowsBeforeClose.end(),
        targetSubWindow);
    if (targetIterator == subWindowsBeforeClose.end()) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "persistent session target was not present in the SpeedTree MDI document list");
    }
    const int targetIndex = static_cast<int>(
        std::distance(subWindowsBeforeClose.begin(), targetIterator));
    char closeState[160]{};
    _snprintf_s(
        closeState,
        sizeof(closeState),
        _TRUNCATE,
        "persistent session invoking SlotCloseTab(%d) for %zu MDI document(s)",
        targetIndex,
        subWindowsBeforeClose.size());
    Log(closeState);

    gSessionForceDiscardClose.store(true, std::memory_order_release);
    const bool closeInvoked = InvokeQtInt(mainWindow, "SlotCloseTab", targetIndex);
    gSessionForceDiscardClose.store(false, std::memory_order_release);
    if (!closeInvoked) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "persistent session could not invoke SpeedTree SlotCloseTab(int)");
    }
    LogPointer(
        "persistent session active QMdiSubWindow immediately after close is ",
        gQMdiAreaActiveSubWindow(mdiArea));

    const ULONGLONG deadline = GetTickCount64() + 30000;
    while (GetTickCount64() < deadline) {
        PumpMainThreadEvents();
        std::vector<void*> remainingSubWindows;
        const bool enumerated = CollectMdiSubWindows(mdiArea, remainingSubWindows);
        const bool targetAlive = enumerated && ContainsPointer(
            remainingSubWindows,
            targetSubWindow);
        const bool reusableDocumentState = enumerated &&
            (gSessionAnchorMdiSubWindow != nullptr
                ? ContainsPointer(remainingSubWindows, gSessionAnchorMdiSubWindow)
                : remainingSubWindows.empty());
        if (enumerated && !targetAlive && reusableDocumentState &&
            gQObjectInherits(mainWindow, "MainWindow")) {
            Log("persistent session target closed and the blank anchor remained alive");
            gPersistentJobPhase = PersistentJobPhase::Complete;
            return;
        }
        Sleep(25);
    }
    AbortExport(
        kHookRuntimeFailureExitCode,
        "persistent session target did not close while preserving the anchor");
}

void ProcessGuiBakeState(void* mainWindow) {
    static thread_local bool processing = false;
    if (processing) {
        return;
    }
    struct ProcessingGuard {
        bool& value;
        ~ProcessingGuard() { value = false; }
    } guard{processing};
    processing = true;

    if (!gGuiBakeMode || gGuiExportStarted.load(std::memory_order_acquire)) {
        return;
    }
    if (gSessionServerMode &&
        !gSessionJobActive.load(std::memory_order_acquire)) {
        return;
    }
    if (!PreparePersistentJob(mainWindow)) {
        return;
    }
    if (GetTickCount64() - gHookStartTick >= gTimeoutMs) {
        AbortExport(
            kCollisionTimeoutExitCode,
            "GUI bake mode timed out before a completed collision pass was available");
    }

    void* collisionModel = gCollisionModel.load(std::memory_order_acquire);
    if (collisionModel == nullptr) {
        collisionModel = FindCollisionModelFromEmbeddedThread();
        if (collisionModel == nullptr) {
            return;
        }
        gCollisionModel.store(collisionModel, std::memory_order_release);
        auto* quality = reinterpret_cast<int*>(
            static_cast<unsigned char*>(collisionModel) + kCoreModelCollisionQualityOffset);
        if (*quality != 3) {
            char qualityMessage[128]{};
            _snprintf_s(
                qualityMessage,
                sizeof(qualityMessage),
                _TRUNCATE,
                "GUI bake mode changed collision quality from %d to 3",
                *quality);
            Log(qualityMessage);
            *quality = 3;
        }
    }

    bool bakeExpected = false;
    if (gGuiBakeRequested.compare_exchange_strong(
            bakeExpected,
            true,
            std::memory_order_acq_rel,
            std::memory_order_acquire)) {
        Log("requesting a quality-3 collision bake through the initialized GUI controller");
        gMarkCollisionDirty(collisionModel);
        auto** controllerStorage = reinterpret_cast<void**>(
            gSpeedTreeBase + kApplicationControllerPointerRva);
        void* controller = *controllerStorage;
        if (controller == nullptr) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "GUI bake controller pointer is null");
        }
        void** controllerVtable = *reinterpret_cast<void***>(controller);
        auto* resolvedMainWindow = static_cast<unsigned char*>(controller) - 0x28;
        char controllerState[192]{};
        _snprintf_s(
            controllerState,
            sizeof(controllerState),
            _TRUNCATE,
            "GUI MainWindow flags: cli=%u idle_block=%u idle_draw_block=%u busy=%u",
            static_cast<unsigned int>(resolvedMainWindow[0x615]),
            static_cast<unsigned int>(resolvedMainWindow[0x616]),
            static_cast<unsigned int>(resolvedMainWindow[0x519]),
            static_cast<unsigned int>(resolvedMainWindow[0x3B8]));
        Log(controllerState);
        auto update = reinterpret_cast<ApplicationUpdateFn>(
            controllerVtable[0xA0 / sizeof(void*)]);
        LogPointer("GUI bake controller update target is ", reinterpret_cast<void*>(update));
        update(controller);
        return;
    }

    const unsigned int collisionStartCount = gCollisionStartCount.load(
        std::memory_order_acquire);
    if (collisionStartCount == 0) {
        return;
    }
    void* collisionThread = gCollisionThread.load(std::memory_order_acquire);
    if (collisionThread == nullptr || gQThreadIsRunning(collisionThread)) {
        return;
    }

    const std::ptrdiff_t inputs = GeneratedCollisionInputCount(collisionModel);
    const std::ptrdiff_t pending = PendingCollisionResultCount(collisionModel);
    if (inputs <= 0 && pending <= 0) {
        return;
    }
    // QThread::isRunning() becomes false before SpeedTree consumes the queued
    // collision completion callback.  A reused process can therefore reach
    // this point with the per-model post-collision flag still set.  Exporting
    // in that interval skips the export-time collision refresh and emits the
    // unpruned mesh.  Let the Qt event loop finish that callback first.
    if (*(static_cast<unsigned char*>(collisionModel) + 0x9C89) != 0 &&
        !gSynchronousCollisionCompleted.load(std::memory_order_acquire)) {
        return;
    }

    bool exportExpected = false;
    if (!gGuiExportStarted.compare_exchange_strong(
            exportExpected,
            true,
            std::memory_order_acq_rel,
            std::memory_order_acquire)) {
        return;
    }
    LogCollisionResultState("GUI bake collision pass completed", collisionModel);

    const std::string outputPath = WideToUtf8(gGuiExportPath);
    const std::string optionsPath = WideToUtf8(gGuiExportOptionsPath);
    if (outputPath.empty() || optionsPath.empty()) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "GUI bake export paths could not be converted to UTF-8");
    }

    Log("GUI bake is complete; invoking ExportCommandLineTree on the Qt main thread");
    gNativeSpeedTreeExport(
        mainWindow,
        const_cast<std::string*>(&outputPath),
        const_cast<std::string*>(&optionsPath),
        gGuiGameExport);

    void* finalCollisionThread = gCollisionThread.load(std::memory_order_acquire);
    if (finalCollisionThread != nullptr && gQThreadIsRunning(finalCollisionThread)) {
        Log("export started a final collision refresh; waiting before SpeedTree shutdown");
        if (!gQThreadWait(finalCollisionThread, gTimeoutMs)) {
            AbortExport(
                kCollisionTimeoutExitCode,
                "final collision refresh did not finish before shutdown");
        }
        Log("final collision refresh completed");
    }

    if (GetFileAttributesW(gGuiExportPath) == INVALID_FILE_ATTRIBUTES) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "ExportCommandLineTree returned without creating the requested FBX");
    }
    Log("GUI bake post-collision export completed");
    // ExportCommandLineTree can leave queued collision callbacks behind. Running
    // the normal Qt teardown here races those callbacks against model destruction.
    // This is a dedicated wrapper-owned process, so finish with a clean process
    // exit code after the output file and worker completion have been verified.
    if (gSessionServerMode) {
        ClosePersistentTarget(mainWindow);
        Log("persistent export is complete; blank anchor process remains running");
        CompletePersistentSessionJob(ERROR_SUCCESS);
        return;
    }
    TerminateProcess(GetCurrentProcess(), 0);
    for (;;) {
        Sleep(INFINITE);
    }
}

void __fastcall HookedMainWindowOnIdle(void* mainWindow) {
    gOriginalMainWindowOnIdle(mainWindow);
    ProcessGuiBakeState(mainWindow);
}

void __fastcall HookedMainWindowOnIdleDraw(void* mainWindow) {
    gOriginalMainWindowOnIdleDraw(mainWindow);
    ProcessGuiBakeState(mainWindow);
}

bool __cdecl HookedNotifyInternal(void* receiver, void* event) {
    void* mainWindow = gSessionMainWindow.load(std::memory_order_acquire);
    const bool result = gOriginalNotifyInternal(receiver, event);
    if (gSessionServerMode && mainWindow != nullptr && receiver == mainWindow) {
        ProcessGuiBakeState(mainWindow);
    }
    return result;
}

void* ResolveMainWindowFromController() {
    void* mainWindow = nullptr;
    __try {
        auto** controllerStorage = reinterpret_cast<void**>(
            gSpeedTreeBase + kApplicationControllerPointerRva);
        void* controller = *controllerStorage;
        if (controller != nullptr) {
            void* candidate = static_cast<unsigned char*>(controller) - 0x28;
            if (gQObjectInherits(candidate, "MainWindow")) {
                mainWindow = candidate;
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        mainWindow = nullptr;
    }
    return mainWindow;
}

DWORD WINAPI RunSessionGuiDriver(void*) {
    Log("persistent session GUI driver thread started");
    const ULONGLONG deadline = GetTickCount64() + gTimeoutMs;
    while (GetTickCount64() < deadline) {
        void* mainWindow = ResolveMainWindowFromController();
        if (mainWindow != nullptr) {
            LogPointer("persistent session controller MainWindow is ", mainWindow);
            gSessionMainWindow.store(mainWindow, std::memory_order_release);
            Log("persistent session attached to the blank anchor MainWindow");
            for (;;) {
                if (gSessionJobActive.load(std::memory_order_acquire) &&
                    !PostQtWakeEvent(mainWindow)) {
                    Log("persistent session could not post a Qt GUI wake event");
                    TerminateProcess(
                        GetCurrentProcess(),
                        kHookRuntimeFailureExitCode);
                    return kHookRuntimeFailureExitCode;
                }
                Sleep(50);
            }
        }
        Sleep(50);
    }
    Log("persistent session could not resolve the blank anchor MainWindow before timeout");
    TerminateProcess(GetCurrentProcess(), kHookRuntimeFailureExitCode);
    return kHookRuntimeFailureExitCode;
}

std::string WidePathToUtf8(const wchar_t* value) {
    const int required = WideCharToMultiByte(
        CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    if (required <= 1) {
        return {};
    }
    std::string result(static_cast<std::size_t>(required), '\0');
    WideCharToMultiByte(
        CP_UTF8,
        0,
        value,
        -1,
        result.data(),
        required,
        nullptr,
        nullptr);
    result.pop_back();
    return result;
}

std::string JsonEscape(const std::string& value) {
    std::string escaped;
    escaped.reserve(value.size() + 16);
    for (const unsigned char character : value) {
        switch (character) {
        case '"': escaped += "\\\""; break;
        case '\\': escaped += "\\\\"; break;
        case '\b': escaped += "\\b"; break;
        case '\f': escaped += "\\f"; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default:
            if (character < 0x20) {
                char encoded[7]{};
                _snprintf_s(
                    encoded,
                    sizeof(encoded),
                    _TRUNCATE,
                    "\\u%04x",
                    static_cast<unsigned int>(character));
                escaped += encoded;
            } else {
                escaped.push_back(static_cast<char>(character));
            }
            break;
        }
    }
    return escaped;
}

bool WriteNativeReceipt() {
    if (gNativeReceiptPath[0] == L'\0') {
        return true;
    }

    std::lock_guard<std::mutex> lock(gNativeReceiptMutex);
    std::sort(
        gNativeReceiptBones.begin(),
        gNativeReceiptBones.end(),
        [](const NativeReceiptBone& left, const NativeReceiptBone& right) {
            return left.boneId < right.boneId;
        });

    const std::filesystem::path destination(gNativeReceiptPath);
    std::error_code directoryError;
    if (!destination.parent_path().empty()) {
        std::filesystem::create_directories(
            destination.parent_path(),
            directoryError);
    }
    if (directoryError) {
        Log("native receipt parent directory could not be created");
        return false;
    }
    std::filesystem::path temporary = destination;
    temporary += L".tmp." + std::to_wstring(GetCurrentProcessId());

    WIN32_FILE_ATTRIBUTE_DATA sourceAttributes{};
    const bool hasSourceIdentity = GetFileAttributesExW(
        gNativeInputPath,
        GetFileExInfoStandard,
        &sourceAttributes) != FALSE;
    const std::uint64_t sourceSize = hasSourceIdentity
        ? (static_cast<std::uint64_t>(sourceAttributes.nFileSizeHigh) << 32) |
            sourceAttributes.nFileSizeLow
        : 0;
    const std::uint64_t sourceWriteTime = hasSourceIdentity
        ? (static_cast<std::uint64_t>(
               sourceAttributes.ftLastWriteTime.dwHighDateTime) << 32) |
            sourceAttributes.ftLastWriteTime.dwLowDateTime
        : 0;

    std::ofstream stream(temporary, std::ios::binary | std::ios::trunc);
    if (!stream) {
        Log("native receipt temporary file could not be opened");
        return false;
    }
    // Generated-instance proxy records describe geometry placement, not an
    // exported deform skeleton.  Blender imports an armature only when the
    // native FBX serializer emitted at least one exact bone record, so proxy
    // presence must never turn a genuinely bone-less FBX into the ID-0 deform
    // contract.
    const char* idZeroClusterWrite =
        gNativeReceiptBones.empty()
        ? "not_applicable_boneless_export"
        : (gMissingIdZeroBoneRecordLogged.load(std::memory_order_acquire)
               ? "omitted_no_exact_bone_record"
               : "native_exact_bone_record");
    stream << std::setprecision(17);
    stream << "{\n"
           << "  \"schema_version\": 5,\n"
           << "  \"kind\": \"speedtree_native_export_receipt\",\n"
           << "  \"status\": \"ready\",\n"
           << "  \"identity_policy\": "
               "\"modeler_runtime_pose_tangent_and_fbx_serializer_records_v5\",\n"
           << "  \"source\": {\"path\": \""
           << JsonEscape(WidePathToUtf8(gNativeInputPath))
           << "\", \"size\": " << sourceSize
           << ", \"last_write_time_100ns\": " << sourceWriteTime << "},\n"
           << "  \"output\": {\"path\": \""
           << JsonEscape(WidePathToUtf8(gGuiExportPath)) << "\"},\n"
           << "  \"id_zero_cluster_write\": \""
           << idZeroClusterWrite
           << "\",\n"
           << "  \"coordinate_contract\": {"
              "\"native_unit_to_meter\": 0.3048, "
              "\"native_unit_to_solver\": 30.48, "
              "\"blender_xyz_from_native_xyz\": ["
              "\"x*0.3048\", \"y*0.3048\", \"z*0.3048\"]},\n"
           << "  \"geometry_count\": "
           << gNativeReceiptGeometries.size() << ",\n"
           << "  \"geometries\": [\n";
    for (std::size_t index = 0; index < gNativeReceiptGeometries.size(); ++index) {
        const auto& geometry = gNativeReceiptGeometries[index];
        stream << "    {\"ordinal\": " << index
               << ", \"vertex_count\": "
               << geometry.maximumVertexIndex + 1 << "}"
               << (index + 1 == gNativeReceiptGeometries.size() ? "\n" : ",\n");
    }
    stream << "  ],\n"
           << "  \"bones\": [\n";
    for (std::size_t index = 0; index < gNativeReceiptBones.size(); ++index) {
        const auto& bone = gNativeReceiptBones[index];
        stream << "    {\"id\": " << bone.boneId
               << ", \"parent_id\": " << bone.parentId
               << ", \"start_native\": ["
               << bone.start[0] << ", " << bone.start[1] << ", "
               << bone.start[2] << "], \"end_native\": ["
               << bone.end[0] << ", " << bone.end[1] << ", "
               << bone.end[2] << "], \"source_rtti\": \""
               << JsonEscape(bone.sourceType) << "\"}"
               << (index + 1 == gNativeReceiptBones.size() ? "\n" : ",\n");
    }
    stream << "  ],\n  \"generated_instances\": [\n";
    for (std::size_t index = 0; index < gNativeReceiptProxies.size(); ++index) {
        const auto& proxy = gNativeReceiptProxies[index];
        stream << "    {\"geometry_ordinal\": " << proxy.key.geometryOrdinal
               << ", \"native_instance_id\": " << proxy.key.instanceId
               << ", \"record_type\": " << proxy.key.recordType
               << ", \"source_bone_id\": " << proxy.key.sourceBoneId
               << ", \"source_rtti\": \""
               << JsonEscape(proxy.sourceType) << "\"";
        if (proxy.hasAuthoredPosition) {
            stream << ", \"node_guid\": \""
                   << proxy.nodeGuid
                   << "\"";
            if (!proxy.parentGuid.empty()) {
                stream << ", \"parent_guid\": \""
                       << proxy.parentGuid << "\"";
            }
            if (!proxy.generatorGuid.empty()) {
                stream << ", \"generator_guid\": \""
                       << proxy.generatorGuid << "\"";
            }
            stream << ", \"authored_position_native\": ["
                   << proxy.authoredPositionNative[0] << ", "
                   << proxy.authoredPositionNative[1] << ", "
                   << proxy.authoredPositionNative[2]
                   << "]";
            if (proxy.hasAuthoredTangent) {
                stream << ", \"authored_tangent_native_unit\": ["
                       << proxy.authoredTangentNativeUnit[0] << ", "
                       << proxy.authoredTangentNativeUnit[1] << ", "
                       << proxy.authoredTangentNativeUnit[2] << "]";
            }
            stream << ", \"authored_position_influences\": [";
            for (std::size_t influenceIndex = 0;
                 influenceIndex < proxy.influences.size();
                 ++influenceIndex) {
                const auto& influence = proxy.influences[influenceIndex];
                stream << "{\"bone_id\": " << influence.boneId
                       << ", \"mapping_node\": \""
                       << JsonEscape(influence.mappingNode)
                       << "\", \"exported_cluster_name\": \""
                       << JsonEscape(influence.exportedClusterName)
                       << "\", \"native_root\": "
                       << (influence.boneId == 0 ? "true" : "false")
                       << ", \"weight\": " << influence.weight << "}";
                if (influenceIndex + 1 != proxy.influences.size()) {
                    stream << ", ";
                }
            }
            stream << "]";
        }
        stream << ", \"vertex_ranges\": [";
        for (std::size_t rangeIndex = 0;
             rangeIndex < proxy.vertexRanges.size();
             ++rangeIndex) {
            const auto& range = proxy.vertexRanges[rangeIndex];
            stream << "[" << range.firstVertex << ", " << range.lastVertex << "]";
            if (rangeIndex + 1 != proxy.vertexRanges.size()) {
                stream << ", ";
            }
        }
        stream << "]}"
               << (index + 1 == gNativeReceiptProxies.size() ? "\n" : ",\n");
    }
    stream << "  ]\n}\n";
    stream.close();
    if (!stream) {
        DeleteFileW(temporary.c_str());
        Log("native receipt write did not complete");
        return false;
    }
    if (!MoveFileExW(
            temporary.c_str(),
            destination.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        DeleteFileW(temporary.c_str());
        Log("native receipt atomic promotion failed");
        return false;
    }
    char message[256]{};
    _snprintf_s(
        message,
        sizeof(message),
        _TRUNCATE,
        "native export receipt completed bones=%llu instances=%llu",
        static_cast<unsigned long long>(gNativeReceiptBones.size()),
        static_cast<unsigned long long>(gNativeReceiptProxies.size()));
    Log(message);
    return true;
}

void RunSecondaryNativeExport(void* mainWindow, bool gameExport) {
    if (gSecondaryExportPath[0] == L'\0') {
        return;
    }
    std::string secondaryOutput = WidePathToUtf8(gSecondaryExportPath);
    std::string secondaryOptions = WidePathToUtf8(gSecondaryExportOptionsPath);
    if (secondaryOutput.empty() || secondaryOptions.empty()) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI bundled export paths could not be encoded as UTF-8");
    }
    Log("native CLI starting bundled secondary export in the loaded process");
    gSecondaryNativeSerializationActive.store(true, std::memory_order_release);
    gOriginalSpeedTreeExport(
        mainWindow,
        &secondaryOutput,
        &secondaryOptions,
        gameExport);
    gSecondaryNativeSerializationActive.store(false, std::memory_order_release);
    if (!gVerificationOnly &&
        !gSynchronousCollisionCompleted.load(std::memory_order_acquire)) {
        AbortExport(
            kNoGeneratedCollisionInputsExitCode,
            "native CLI bundled secondary export lost collision state");
    }
    Log("native CLI bundled secondary export completed");
}

void __fastcall HookedSpeedTreeExport(void* arg1, void* arg2, void* arg3, bool gameExport) {
    Log("SpeedTree native CLI export intercepted");
    ResetNativeReceiptCapture();
    __try {
        const auto* firstText = static_cast<const std::string*>(arg2);
        const auto* secondText = static_cast<const std::string*>(arg3);
        char argumentsMessage[2048]{};
        _snprintf_s(
            argumentsMessage,
            sizeof(argumentsMessage),
            _TRUNCATE,
            "native export arguments: first='%s' second='%s' game=%u",
            firstText->c_str(),
            secondText->c_str(),
            static_cast<unsigned int>(gameExport));
        Log(argumentsMessage);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("native export argument logging failed");
    }

    if (gVerificationOnly) {
        Log("native CLI verification-only export skips Collision/Prune bake");
        gOriginalSpeedTreeExport(arg1, arg2, arg3, gameExport);
        WriteNativeReceipt();
        RunSecondaryNativeExport(arg1, gameExport);
        Log("native CLI bundled verification export completed");
        return;
    }

    void* collisionModel = FindCollisionModelFromEmbeddedThread();
    if (collisionModel == nullptr) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI export could not resolve the loaded collision model");
    }
    gCollisionModel.store(collisionModel, std::memory_order_release);
    *reinterpret_cast<int*>(
        static_cast<unsigned char*>(collisionModel) +
        kCoreModelCollisionQualityOffset) = 3;
    static_cast<unsigned char*>(collisionModel)[0x9BDC] = 1;
    LogCollisionResultState("native CLI collision model before dirty", collisionModel);
    gMarkCollisionDirty(collisionModel);
    LogCollisionResultState("native CLI collision model after dirty", collisionModel);
    gSynchronousCollisionCompleted.store(false, std::memory_order_release);
    gCollisionStartCount.store(0, std::memory_order_release);
    gNativeMainWindow.store(static_cast<unsigned char*>(arg1), std::memory_order_release);
    gNativeCliExportActive.store(true, std::memory_order_release);
    if (!SetNativeOnIdleNoop(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not disable MainWindow::OnIdle during export");
    }
    if (!SetNativeDeferredUiUpdateNoop(true)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not disable deferred UI updates during export");
    }
    __try {
        auto* mainWindow = static_cast<unsigned char*>(arg1);
        if (mainWindow[0x615] == 0) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "native CLI model finalization requires the Modeler CLI mode flag");
        }
        void* treeDocument = *reinterpret_cast<void**>(mainWindow + 0x418);
        if (treeDocument == nullptr ||
            static_cast<unsigned char*>(treeDocument) + kTreeWindowModelOffset !=
                collisionModel) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "native CLI exporter document does not own the collision model");
        }

        const auto prepareTreeDocument = reinterpret_cast<TreeDocumentPrepareFn>(
            gSpeedTreeBase + kTreeDocumentPrepareRva);
        const auto stageTreeDocumentModel = reinterpret_cast<TreeDocumentModelStageFn>(
            gSpeedTreeBase + kTreeDocumentModelStageRva);
        prepareTreeDocument(treeDocument);
        void* modelInterface = static_cast<unsigned char*>(treeDocument) + 0x18;
        stageTreeDocumentModel(modelInterface);
        void** modelInterfaceVtable = *reinterpret_cast<void***>(modelInterface);
        auto finishModelStage = reinterpret_cast<bool(__fastcall*)(void*)>(
            modelInterfaceVtable[0xD8 / sizeof(void*)]);
        finishModelStage(modelInterface);

        void* controller = mainWindow + 0x28;
        void** controllerVtable = *reinterpret_cast<void***>(controller);
        auto updateModel = reinterpret_cast<bool(__fastcall*)(void*, bool)>(
            controllerVtable[0x558 / sizeof(void*)]);
        bool handled = updateModel(controller, false);
        if (!handled) {
            handled = updateModel(controller, true);
        }
        if (!handled) {
            auto finishCliModel = reinterpret_cast<void(__fastcall*)(void*)>(
                controllerVtable[0xD0 / sizeof(void*)]);
            Log("native CLI model update requested its CLI-only finalization branch");
            finishCliModel(controller);
        }
        LogCollisionResultState(
            "native CLI model finalization completed",
            collisionModel);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        gNativeCliExportActive.store(false, std::memory_order_release);
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI model finalization raised an internal exception");
    }
    gOriginalSpeedTreeExport(arg1, arg2, arg3, gameExport);
    WriteNativeReceipt();
    RunSecondaryNativeExport(arg1, gameExport);
    if (!SetNativeDeferredUiUpdateNoop(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore deferred UI updates after export");
    }
    if (!SetNativeOnIdleNoop(false)) {
        AbortExport(
            kHookRuntimeFailureExitCode,
            "native CLI could not restore MainWindow::OnIdle after export");
    }
    gNativeCliExportActive.store(false, std::memory_order_release);
    if (!gSynchronousCollisionCompleted.load(std::memory_order_acquire)) {
        AbortExport(
            kNoGeneratedCollisionInputsExitCode,
            "native CLI exporter never produced collision inputs before serialization");
    }
    Log("native CLI post-collision export completed");
}

template <typename FunctionType>
FunctionType Resolve(HMODULE module, const char* name) {
    return reinterpret_cast<FunctionType>(GetProcAddress(module, name));
}

bool ReadConfiguration() {
    gHookStartTick = GetTickCount64();
    GetEnvironmentVariableW(
        L"SPEEDTREE_COLLISION_CLI_LOG",
        gLogPath,
        static_cast<DWORD>(std::size(gLogPath)));
    GetEnvironmentVariableW(
        L"SPEEDTREE_COLLISION_CLI_SECONDARY_OUTPUT",
        gSecondaryExportPath,
        static_cast<DWORD>(std::size(gSecondaryExportPath)));
    GetEnvironmentVariableW(
        L"SPEEDTREE_COLLISION_CLI_SECONDARY_OPTIONS",
        gSecondaryExportOptionsPath,
        static_cast<DWORD>(std::size(gSecondaryExportOptionsPath)));
    GetEnvironmentVariableW(
        L"SPEEDTREE_COLLISION_CLI_INPUT",
        gNativeInputPath,
        static_cast<DWORD>(std::size(gNativeInputPath)));
    GetEnvironmentVariableW(
        L"SPEEDTREE_COLLISION_CLI_NATIVE_RECEIPT",
        gNativeReceiptPath,
        static_cast<DWORD>(std::size(gNativeReceiptPath)));
    wchar_t timeoutText[64]{};
    if (GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_TIMEOUT_MS",
            timeoutText,
            static_cast<DWORD>(std::size(timeoutText))) > 0) {
        wchar_t* end = nullptr;
        const unsigned long parsed = wcstoul(timeoutText, &end, 10);
        if (end != timeoutText && *end == L'\0' && parsed >= 1000) {
            gTimeoutMs = parsed;
        }
    }
    wchar_t modeText[16]{};
    if (GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_GUI_BAKE",
            modeText,
            static_cast<DWORD>(std::size(modeText))) > 0) {
        gGuiBakeMode = std::wcscmp(modeText, L"1") == 0;
    }
    wchar_t verificationText[16]{};
    if (GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_VERIFICATION_ONLY",
            verificationText,
            static_cast<DWORD>(std::size(verificationText))) > 0) {
        gVerificationOnly = std::wcscmp(verificationText, L"1") == 0;
    }
    wchar_t rlmFailFastText[16]{};
    if (GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_RLM_FAIL_FAST",
            rlmFailFastText,
            static_cast<DWORD>(std::size(rlmFailFastText))) > 0) {
        gRlmConnectFailFast = std::wcscmp(rlmFailFastText, L"1") == 0;
    }
    wchar_t serverModeText[16]{};
    if (GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_SESSION_SERVER",
            serverModeText,
            static_cast<DWORD>(std::size(serverModeText))) > 0) {
        gSessionServerMode = std::wcscmp(serverModeText, L"1") == 0;
        if (gSessionServerMode) {
            gGuiBakeMode = true;
        }
    }
    if (gGuiBakeMode) {
        GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_OUTPUT",
            gGuiExportPath,
            static_cast<DWORD>(std::size(gGuiExportPath)));
        GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_EXPORT_OPTIONS",
            gGuiExportOptionsPath,
            static_cast<DWORD>(std::size(gGuiExportOptionsPath)));
        wchar_t gameText[16]{};
        GetEnvironmentVariableW(
            L"SPEEDTREE_COLLISION_CLI_GAME_EXPORT",
            gameText,
            static_cast<DWORD>(std::size(gameText)));
        gGuiGameExport = std::wcscmp(gameText, L"1") == 0;
        if (gSessionServerMode) {
            gPersistentJobPhase = PersistentJobPhase::Complete;
            Log("persistent session will reuse the startup blank SPM anchor");
        }
    }
    return true;
}

bool ReadSpeedTreeImageBounds() {
    gSpeedTreeModule = GetModuleHandleW(nullptr);
    if (gSpeedTreeModule == nullptr) {
        return false;
    }
    gSpeedTreeBase = reinterpret_cast<std::uintptr_t>(gSpeedTreeModule);
    const auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(gSpeedTreeBase);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) {
        return false;
    }
    const auto nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(gSpeedTreeBase + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
        return false;
    }
    gSpeedTreeImageSize = nt->OptionalHeader.SizeOfImage;
    return kSpeedTreeExportRva + sizeof(kSpeedTreeExportPrologue) <= gSpeedTreeImageSize;
}

bool InstallHooks() {
    if (!ReadSpeedTreeImageBounds()) {
        Log("initialization failed: SpeedTree PE image could not be read");
        return false;
    }
    LogPointer("SpeedTree image base is ", reinterpret_cast<void*>(gSpeedTreeBase));

    HMODULE qtCore = GetModuleHandleW(L"Qt6Core.dll");
    HMODULE qtWidgets = GetModuleHandleW(L"Qt6Widgets.dll");
    if (qtCore == nullptr || qtWidgets == nullptr) {
        Log("initialization failed: required Qt 6.6 DLLs are not loaded");
        return false;
    }

    void* qThreadStart = GetProcAddress(qtCore, "?start@QThread@@QEAAXW4Priority@1@@Z");
    void* qNotifyInternal = GetProcAddress(
        qtCore,
        "?notifyInternal2@QCoreApplication@@CA_NPEAVQObject@@PEAVQEvent@@@Z");
    gQThreadWait = Resolve<QThreadWaitFn>(qtCore, "?wait@QThread@@QEAA_NK@Z");
    gQThreadIsRunning = Resolve<QThreadIsRunningFn>(qtCore, "?isRunning@QThread@@QEBA_NXZ");
    gProcessEvents = Resolve<ProcessEventsFn>(
        qtCore,
        "?processEvents@QCoreApplication@@SAXV?$QFlags@W4ProcessEventsFlag@QEventLoop@@@@H@Z");
    gSendPostedEvents = Resolve<SendPostedEventsFn>(
        qtCore,
        "?sendPostedEvents@QCoreApplication@@SAXPEAVQObject@@H@Z");
    gQObjectChildren = Resolve<QObjectChildrenFn>(
        qtCore,
        "?children@QObject@@QEBAAEBV?$QList@PEAVQObject@@@@XZ");
    gQObjectInherits = Resolve<QObjectInheritsFn>(qtCore, "?inherits@QObject@@QEBA_NPEBD@Z");
    gQObjectParent = Resolve<QObjectParentFn>(qtCore, "?parent@QObject@@QEBAPEAV1@XZ");
    gQWidgetFind = Resolve<QWidgetFindFn>(qtWidgets, "?find@QWidget@@SAPEAV1@_K@Z");
    gQCoreApplicationInstance = Resolve<QCoreApplicationInstanceFn>(
        qtCore,
        "?instance@QCoreApplication@@SAPEAV1@XZ");
    gQApplicationAllWidgets = Resolve<QApplicationAllWidgetsFn>(
        qtWidgets,
        "?allWidgets@QApplication@@SA?AV?$QList@PEAVQWidget@@@@XZ");
    void* qDialogExec = GetProcAddress(qtWidgets, "?exec@QDialog@@UEAAHXZ");
    gQMessageBoxIcon = Resolve<QMessageBoxIconFn>(
        qtWidgets,
        "?icon@QMessageBox@@QEBA?AW4Icon@1@XZ");
    gQStringCtor = Resolve<QStringCtorFn>(
        qtCore,
        "??0QString@@QEAA@PEBVQChar@@_J@Z");
    gQArrayDataAllocate = Resolve<QArrayDataAllocateFn>(
        qtCore,
        "?allocate@QArrayData@@SAPEAXPEAPEAU1@_J11W4AllocationOption@1@@Z");
    gQMdiAreaSetActiveSubWindow = Resolve<QMdiAreaSetActiveSubWindowFn>(
        qtWidgets,
        "?setActiveSubWindow@QMdiArea@@QEAAXPEAVQMdiSubWindow@@@Z");
    gQMdiAreaActiveSubWindow = Resolve<QMdiAreaActiveSubWindowFn>(
        qtWidgets,
        "?activeSubWindow@QMdiArea@@QEBAPEAVQMdiSubWindow@@XZ");
    gQMdiAreaSubWindowList = Resolve<QMdiAreaSubWindowListFn>(
        qtWidgets,
        "?subWindowList@QMdiArea@@QEBA?AV?$QList@PEAVQMdiSubWindow@@@@W4WindowOrder@1@@Z");
    gQEventCtor = Resolve<QEventCtorFn>(
        qtCore,
        "??0QEvent@@QEAA@W4Type@0@@Z");
    gPostEvent = Resolve<PostEventFn>(
        qtCore,
        "?postEvent@QCoreApplication@@SAXPEAVQObject@@PEAVQEvent@@H@Z");
    gQMetaInvoke = Resolve<QMetaInvokeFn>(
        qtCore,
        "?invokeMethod@QMetaObject@@SA_NPEAVQObject@@PEBDW4ConnectionType@Qt@@VQGenericArgument@@333333333@Z");
    gMarkCollisionDirty = reinterpret_cast<MarkCollisionDirtyFn>(
        gSpeedTreeBase + kMarkCollisionDirtyRva);
    gCollisionDone = reinterpret_cast<CollisionDoneFn>(gSpeedTreeBase + kCollisionDoneRva);
    gCollisionCompute = reinterpret_cast<CollisionComputeFn>(gSpeedTreeBase + kCollisionComputeRva);
    gMainWindowOnIdle = reinterpret_cast<MainWindowIdleFn>(
        gSpeedTreeBase + kMainWindowOnIdleRva);
    gMainWindowOnIdleDraw = reinterpret_cast<MainWindowIdleFn>(
        gSpeedTreeBase + kMainWindowOnIdleDrawRva);
    gNativeSpeedTreeExport = reinterpret_cast<SpeedTreeExportFn>(
        gSpeedTreeBase + kSpeedTreeExportRva);
    gResolveBranchBoneId = reinterpret_cast<ResolveBranchBoneIdFn>(
        gSpeedTreeBase + kResolveBranchBoneIdRva);
    gFindExportBoneMapping = reinterpret_cast<FindExportBoneMappingFn>(
        gSpeedTreeBase + kFindExportBoneMappingRva);
    gFbxClusterAppendIndex = reinterpret_cast<FbxClusterAppendIndexFn>(
        gSpeedTreeBase + kFbxClusterAppendIndexRva);
    gFbxClusterAppendWeight = reinterpret_cast<FbxClusterAppendWeightFn>(
        gSpeedTreeBase + kFbxClusterAppendWeightRva);
    if (qThreadStart == nullptr || qNotifyInternal == nullptr ||
        gQThreadWait == nullptr || gQThreadIsRunning == nullptr ||
        gProcessEvents == nullptr || gSendPostedEvents == nullptr || gQObjectChildren == nullptr ||
        gQObjectInherits == nullptr || gQObjectParent == nullptr || gQWidgetFind == nullptr ||
        gQCoreApplicationInstance == nullptr || gQApplicationAllWidgets == nullptr ||
        qDialogExec == nullptr || gQMessageBoxIcon == nullptr ||
        gQStringCtor == nullptr || gQArrayDataAllocate == nullptr ||
        gQMdiAreaSetActiveSubWindow == nullptr || gQMdiAreaActiveSubWindow == nullptr ||
        gQMdiAreaSubWindowList == nullptr ||
        gQEventCtor == nullptr ||
        gPostEvent == nullptr ||
        gQMetaInvoke == nullptr) {
        Log("initialization failed: required Qt 6.6 symbols were not found");
        return false;
    }

    if (gRlmConnectFailFast && !SetRlmConnectFailFastPatch(true)) {
        return false;
    }
    if (!SetFbxDeformRootPatch(true)) {
        if (gRlmConnectFailFastPatchInstalled) {
            SetRlmConnectFailFastPatch(false);
        }
        return false;
    }
    if (!InstallHook(
            gQThreadStartHook,
            qThreadStart,
            HookedQThreadStart,
            kQThreadStartPrologue,
            reinterpret_cast<void**>(&gOriginalQThreadStart))) {
        SetFbxDeformRootPatch(false);
        return false;
    }
    if (!InstallHook(
            gFbxClusterAddControlPointHook,
            reinterpret_cast<void*>(gSpeedTreeBase + kFbxClusterAddControlPointRva),
            HookedFbxClusterAddControlPoint,
            kFbxClusterAddControlPointPrologue,
            reinterpret_cast<void**>(&gUnusedOriginalFbxClusterAddControlPoint))) {
        RemoveCommonHooks();
        return false;
    }
    if (!BuildExportVertexWeightsEntryStub() ||
        !InstallRegisterPreservingHook(
            gExportVertexWeightsHook,
            reinterpret_cast<void*>(gSpeedTreeBase + kExportVertexWeightsRva),
            gExportVertexWeightsEntryStub,
            kExportVertexWeightsPrologue,
            reinterpret_cast<void**>(&gOriginalExportVertexWeights))) {
        FreeExportVertexWeightsEntryStub();
        RemoveCommonHooks();
        return false;
    }
    if (!InstallHook(
            gFbxNodeCreateHook,
            reinterpret_cast<void*>(gSpeedTreeBase + kFbxNodeCreateRva),
            HookedFbxNodeCreate,
            kFbxNodeCreatePrologue,
            reinterpret_cast<void**>(&gOriginalFbxNodeCreate))) {
        RemoveCommonHooks();
        return false;
    }
    if (!InstallRegisterPreservingHook(
            gInsertExportBoneHook,
            reinterpret_cast<void*>(gSpeedTreeBase + kInsertExportBoneRva),
            HookedInsertExportBone,
            kInsertExportBonePrologue,
            reinterpret_cast<void**>(&gOriginalInsertExportBone))) {
        RemoveCommonHooks();
        return false;
    }
    if (gGuiBakeMode) {
        if (!gSessionServerMode &&
            (gGuiExportPath[0] == L'\0' || gGuiExportOptionsPath[0] == L'\0')) {
            Log("initialization failed: GUI bake output or export-options path is empty");
            RemoveCommonHooks();
            return false;
        }
        if (gSessionServerMode && !InstallHook(
                gMainWindowConfirmDiscardHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kMainWindowConfirmDiscardRva),
                HookedMainWindowConfirmDiscard,
                kMainWindowConfirmDiscardPrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowConfirmDiscard))) {
            RemoveCommonHooks();
            return false;
        }
        if (!InstallHook(
                gMainWindowRecoveryCheckHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kMainWindowRecoveryCheckRva),
                HookedMainWindowRecoveryCheck,
                kMainWindowRecoveryCheckPrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowRecoveryCheck))) {
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemoveCommonHooks();
            return false;
        }
        if (!InstallHook(
                gQDialogExecHook,
                qDialogExec,
                HookedQDialogExec,
                kQDialogExecPrologue,
                reinterpret_cast<void**>(&gOriginalQDialogExec))) {
            RemoveHook(gMainWindowRecoveryCheckHook);
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemoveCommonHooks();
            return false;
        }
        if (gSessionServerMode && !InstallHook(
                gMainWindowOpenFileListHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kMainWindowOpenFileListRva),
                HookedMainWindowOpenFileList,
                kMainWindowOpenFileListPrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowOpenFileList))) {
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemovePersistentSessionHooks();
            RemoveCommonHooks();
            return false;
        }
        if (gSessionServerMode && !InstallHook(
                gNotifyInternalHook,
                qNotifyInternal,
                HookedNotifyInternal,
                kQCoreNotifyInternalPrologue,
                reinterpret_cast<void**>(&gOriginalNotifyInternal))) {
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemovePersistentSessionHooks();
            RemoveCommonHooks();
            return false;
        }
        if (!InstallHook(
                gMainWindowOnIdleHook,
                gMainWindowOnIdle,
                HookedMainWindowOnIdle,
                kMainWindowOnIdlePrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowOnIdle))) {
            RemoveHook(gNotifyInternalHook);
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemovePersistentSessionHooks();
            RemoveCommonHooks();
            return false;
        }
        if (!InstallHook(
                gMainWindowOnIdleDrawHook,
                gMainWindowOnIdleDraw,
                HookedMainWindowOnIdleDraw,
                kMainWindowOnIdleDrawPrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowOnIdleDraw))) {
            RemoveHook(gMainWindowOnIdleHook);
            RemoveHook(gNotifyInternalHook);
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemovePersistentSessionHooks();
            RemoveCommonHooks();
            return false;
        }
        if (!InstallHook(
                gNativeModelUpdateHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kNativeModelUpdateRva),
                HookedNativeModelUpdate,
                kNativeModelUpdatePrologue,
                reinterpret_cast<void**>(&gOriginalNativeModelUpdate))) {
            RemoveHook(gMainWindowOnIdleDrawHook);
            RemoveHook(gMainWindowOnIdleHook);
            RemoveHook(gNotifyInternalHook);
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemovePersistentSessionHooks();
            RemoveCommonHooks();
            return false;
        }
        Log("GUI bake hooks installed for SpeedTree Modeler 10.1.0 / Qt 6.6.0");
        if (gSessionServerMode) {
            HANDLE driverThread = CreateThread(
                nullptr,
                0,
                RunSessionGuiDriver,
                nullptr,
                0,
                nullptr);
            if (driverThread == nullptr) {
                Log("initialization failed: persistent GUI driver thread could not start");
                RemoveHook(gMainWindowOnIdleDrawHook);
                RemoveHook(gMainWindowOnIdleHook);
                RemoveHook(gNotifyInternalHook);
                RemoveHook(gMainWindowConfirmDiscardHook);
                RemovePersistentSessionHooks();
                RemoveCommonHooks();
                return false;
            }
            CloseHandle(driverThread);
            HANDLE pipeThread = CreateThread(
                nullptr,
                0,
                RunPersistentSessionPipeServer,
                nullptr,
                0,
                nullptr);
            if (pipeThread == nullptr) {
                Log("initialization failed: persistent session pipe thread could not start");
                TerminateProcess(GetCurrentProcess(), kHookRuntimeFailureExitCode);
                return false;
            }
            CloseHandle(pipeThread);
        }
    } else {
        if (!InstallHook(
                gNativeModelUpdateHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kNativeModelUpdateRva),
                HookedNativeModelUpdate,
                kNativeModelUpdatePrologue,
                reinterpret_cast<void**>(&gOriginalNativeModelUpdate))) {
            RemoveCommonHooks();
            return false;
        }
        if (!InstallHook(
                gSpeedTreeExportHook,
                gNativeSpeedTreeExport,
                HookedSpeedTreeExport,
                kSpeedTreeExportPrologue,
                reinterpret_cast<void**>(&gOriginalSpeedTreeExport))) {
            RemoveHook(gNativeModelUpdateHook);
            RemoveCommonHooks();
            return false;
        }
        Log("native CLI collision hooks installed for SpeedTree Modeler 10.1.0");
    }
    return true;
}

}  // namespace

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        ReadConfiguration();
        return InstallHooks() ? TRUE : FALSE;
    }
    if (reason == DLL_PROCESS_DETACH && reserved == nullptr) {
        RemoveHook(gMainWindowConfirmDiscardHook);
        RemovePersistentSessionHooks();
        RemoveHook(gNotifyInternalHook);
        RemoveHook(gMainWindowOnIdleDrawHook);
        RemoveHook(gMainWindowOnIdleHook);
        RemoveHook(gSpeedTreeExportHook);
        RemoveHook(gNativeModelUpdateHook);
        RemoveCommonHooks();
    }
    return TRUE;
}
