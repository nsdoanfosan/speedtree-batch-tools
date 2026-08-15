#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <iterator>
#include <memory>
#include <new>
#include <string>
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
constexpr std::uintptr_t kCollisionDoneRva = 0x3D25D0;
constexpr std::uintptr_t kCollisionComputeRva = 0x3EE760;
constexpr std::uintptr_t kApplicationControllerPointerRva = 0x22A0BF8;
constexpr std::uintptr_t kCollisionThreadVtableRva = 0x19DA008;
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

constexpr unsigned char kQDialogExecPrologue[12] = {
    0x40, 0x55, 0x56, 0x57,
    0x48, 0x83, 0xec, 0x50,
    0x48, 0x8b, 0x79, 0x08,
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
HookRecord gMainWindowOnIdleHook;
HookRecord gMainWindowOnIdleDrawHook;
HookRecord gNotifyInternalHook;
HookRecord gMainWindowConfirmDiscardHook;
HookRecord gMainWindowRecoveryCheckHook;
HookRecord gQDialogExecHook;
HookRecord gMainWindowOpenFileListHook;
HMODULE gSpeedTreeModule = nullptr;
std::uintptr_t gSpeedTreeBase = 0;
std::size_t gSpeedTreeImageSize = 0;
wchar_t gLogPath[32768]{};
wchar_t gGuiExportPath[32768]{};
wchar_t gGuiExportOptionsPath[32768]{};
wchar_t gPersistentInputPath[32768]{};
DWORD gTimeoutMs = kDefaultTimeoutMs;
ULONGLONG gHookStartTick = 0;
bool gGuiBakeMode = false;
bool gGuiGameExport = false;
bool gSessionServerMode = false;
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

void WriteAbsoluteJump(unsigned char* destination, const void* target) {
    // mov rax, <64-bit address>; jmp rax
    destination[0] = 0x48;
    destination[1] = 0xb8;
    *reinterpret_cast<std::uintptr_t*>(destination + 2) = reinterpret_cast<std::uintptr_t>(target);
    destination[10] = 0xff;
    destination[11] = 0xe0;
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

void __fastcall HookedQThreadStart(void* thread, int priority) {
    if (IsCollisionThread(thread)) {
        gCollisionThread.store(thread, std::memory_order_release);
        const unsigned int startCount = gCollisionStartCount.fetch_add(
            1,
            std::memory_order_acq_rel) + 1;
        LogPointer("observed CCollisionThread at ", thread);
        char countMessage[128]{};
        _snprintf_s(
            countMessage,
            sizeof(countMessage),
            _TRUNCATE,
            "CCollisionThread start count is %u",
            startCount);
        Log(countMessage);
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

void __fastcall HookedSpeedTreeExport(void* arg1, void* arg2, void* arg3, bool gameExport) {
    Log("SpeedTree export intercepted; waiting for post-collision computation");
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

    if (gCollisionThread.load(std::memory_order_acquire) == nullptr) {
        Log("stock CLI did not start CCollisionThread; forcing the quality-3 compute path");
        if (!ForcePostCollisionComputation()) {
            AbortExport(
                kHookRuntimeFailureExitCode,
                "export aborted: SpeedTree collision compute path could not be invoked");
        }
    }

    if (gSynchronousCollisionCompleted.load(std::memory_order_acquire)) {
        Log("synchronous post-collision computation completed; continuing original export");
        gOriginalSpeedTreeExport(arg1, arg2, arg3, gameExport);
        Log("original export returned");
        return;
    }

    // The stock CLI calls export immediately after open. Give the normal open path a
    // short event-pumping window in case thread startup was posted to the main queue.
    const ULONGLONG discoveryDeadline = GetTickCount64() + 5000;
    void* collisionThread = gCollisionThread.load(std::memory_order_acquire);
    while (collisionThread == nullptr && GetTickCount64() < discoveryDeadline) {
        PumpMainThreadEvents();
        Sleep(10);
        collisionThread = gCollisionThread.load(std::memory_order_acquire);
    }
    if (collisionThread == nullptr) {
        AbortExport(
            kNoCollisionThreadExitCode,
            "export aborted: no CCollisionThread was observed; refusing to emit an unculled FBX");
    }

    const ULONGLONG deadline = GetTickCount64() + gTimeoutMs;
    bool completed = false;
    while (GetTickCount64() < deadline) {
        if (gQThreadWait(collisionThread, 50)) {
            completed = true;
            break;
        }
        PumpMainThreadEvents();
    }
    if (!completed) {
        AbortExport(kCollisionTimeoutExitCode, "export aborted: post-collision computation timed out");
    }

    if (gQThreadIsRunning(collisionThread)) {
        AbortExport(kHookRuntimeFailureExitCode, "export aborted: collision thread still reports running after wait");
    }

    void* collisionModel = gCollisionModel.load(std::memory_order_acquire);
    LogCollisionResultState("after worker wait", collisionModel);

    // CCollisionThread::Done is delivered back to the GUI/main thread. Pump queued
    // delivery before the exporter reads the model state.
    for (int pass = 0; pass < 20; ++pass) {
        PumpMainThreadEvents();
        Sleep(10);
    }
    LogCollisionResultState("after queued Done pump", collisionModel);
    if (PendingCollisionResultCount(collisionModel) > 0) {
        Log("queued Done was not delivered; invoking collision finalization on the main thread");
        gCollisionDone(collisionModel);
        LogCollisionResultState("after direct Done", collisionModel);
    }

    Log("post-collision computation completed; continuing original export");
    gOriginalSpeedTreeExport(arg1, arg2, arg3, gameExport);
    Log("original export returned");
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

    if (!InstallHook(
            gQThreadStartHook,
            qThreadStart,
            HookedQThreadStart,
            kQThreadStartPrologue,
            reinterpret_cast<void**>(&gOriginalQThreadStart))) {
        return false;
    }
    if (gGuiBakeMode) {
        if (!gSessionServerMode &&
            (gGuiExportPath[0] == L'\0' || gGuiExportOptionsPath[0] == L'\0')) {
            Log("initialization failed: GUI bake output or export-options path is empty");
            RemoveHook(gQThreadStartHook);
            return false;
        }
        if (gSessionServerMode && !InstallHook(
                gMainWindowConfirmDiscardHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kMainWindowConfirmDiscardRva),
                HookedMainWindowConfirmDiscard,
                kMainWindowConfirmDiscardPrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowConfirmDiscard))) {
            RemoveHook(gQThreadStartHook);
            return false;
        }
        if (!InstallHook(
                gMainWindowRecoveryCheckHook,
                reinterpret_cast<void*>(gSpeedTreeBase + kMainWindowRecoveryCheckRva),
                HookedMainWindowRecoveryCheck,
                kMainWindowRecoveryCheckPrologue,
                reinterpret_cast<void**>(&gOriginalMainWindowRecoveryCheck))) {
            RemoveHook(gMainWindowConfirmDiscardHook);
            RemoveHook(gQThreadStartHook);
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
            RemoveHook(gQThreadStartHook);
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
            RemoveHook(gQThreadStartHook);
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
            RemoveHook(gQThreadStartHook);
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
            RemoveHook(gQThreadStartHook);
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
            RemoveHook(gQThreadStartHook);
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
                RemoveHook(gQThreadStartHook);
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
                gSpeedTreeExportHook,
                gNativeSpeedTreeExport,
                HookedSpeedTreeExport,
                kSpeedTreeExportPrologue,
                reinterpret_cast<void**>(&gOriginalSpeedTreeExport))) {
            RemoveHook(gQThreadStartHook);
            return false;
        }
        Log("legacy CLI hooks installed for SpeedTree Modeler 10.1.0 / Qt 6.6.0");
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
        RemoveHook(gQThreadStartHook);
    }
    return TRUE;
}
