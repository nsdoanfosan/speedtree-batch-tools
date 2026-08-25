#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <psapi.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <cwchar>
#include <filesystem>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "session_protocol.h"

#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "psapi.lib")
#pragma comment(lib, "user32.lib")

namespace {

constexpr wchar_t kCapabilityContract[] =
    L"SPEEDTREE_COLLISION_CLI_CONTRACT=native-runtime-receipt-v9";

constexpr wchar_t kDefaultModelerPath[] =
    L"C:\\Program Files\\SpeedTree\\SpeedTree Modeler v10.1.0\\win64\\SpeedTree_Modeler.exe";
constexpr wchar_t kExpectedModelerSha256[] =
    L"ed552d9b138690bc9d0812128876066b49a078310b70d84ba6d9459dda7af441";
constexpr wchar_t kExpectedQtCoreSha256[] =
    L"fe3c6e86e01acfcacdd9939031d501e6e6237999b99d17f26853a5ee2ccaf959";
constexpr DWORD kDefaultTimeoutMs = 10 * 60 * 1000;
constexpr DWORD kDefaultStallTimeoutMs = 30 * 1000;
constexpr DWORD kNoCollisionThreadExitCode = 0xC0111001;
constexpr DWORD kCollisionTimeoutExitCode = 0xC0111002;
constexpr DWORD kHookRuntimeFailureExitCode = 0xC0111003;
constexpr DWORD kNoGeneratedCollisionInputsExitCode = 0xC0111004;
constexpr DWORD kProgressStallExitCode = 0xC0111005;

struct EnvironmentRestore {
    std::wstring name;
    std::optional<std::wstring> value;
};

struct RegistryValueRestore {
    std::wstring keyPath;
    std::wstring valueName;
    DWORD type = REG_NONE;
    std::vector<unsigned char> data;
    bool active = false;
};

struct DesktopHandle {
    HDESK value = nullptr;

    DesktopHandle() = default;
    DesktopHandle(const DesktopHandle&) = delete;
    DesktopHandle& operator=(const DesktopHandle&) = delete;
    ~DesktopHandle() {
        if (value != nullptr) {
            CloseDesktop(value);
        }
    }
};

bool SetTemporaryStringRegistryValue(
    HKEY root,
    const wchar_t* keyPath,
    const wchar_t* valueName,
    const wchar_t* temporaryValue,
    RegistryValueRestore& restore) {
    HKEY key = nullptr;
    if (RegOpenKeyExW(
            root,
            keyPath,
            0,
            KEY_QUERY_VALUE | KEY_SET_VALUE,
            &key) != ERROR_SUCCESS) {
        return false;
    }
    DWORD type = REG_NONE;
    DWORD bytes = 0;
    LONG status = RegQueryValueExW(key, valueName, nullptr, &type, nullptr, &bytes);
    if (status != ERROR_SUCCESS) {
        RegCloseKey(key);
        return false;
    }
    std::vector<unsigned char> data(bytes);
    status = RegQueryValueExW(
        key,
        valueName,
        nullptr,
        &type,
        data.empty() ? nullptr : data.data(),
        &bytes);
    if (status == ERROR_SUCCESS) {
        const DWORD temporaryBytes = static_cast<DWORD>(
            (std::wcslen(temporaryValue) + 1) * sizeof(wchar_t));
        status = RegSetValueExW(
            key,
            valueName,
            0,
            REG_SZ,
            reinterpret_cast<const BYTE*>(temporaryValue),
            temporaryBytes);
    }
    RegCloseKey(key);
    if (status != ERROR_SUCCESS) {
        return false;
    }
    restore.keyPath = keyPath;
    restore.valueName = valueName;
    restore.type = type;
    restore.data = std::move(data);
    restore.active = true;
    return true;
}

void RestoreRegistryValue(HKEY root, RegistryValueRestore& restore) {
    if (!restore.active) {
        return;
    }
    HKEY key = nullptr;
    if (RegOpenKeyExW(root, restore.keyPath.c_str(), 0, KEY_SET_VALUE, &key) ==
        ERROR_SUCCESS) {
        RegSetValueExW(
            key,
            restore.valueName.c_str(),
            0,
            restore.type,
            restore.data.empty() ? nullptr : restore.data.data(),
            static_cast<DWORD>(restore.data.size()));
        RegCloseKey(key);
    }
    restore.active = false;
}

std::wstring QuoteWindowsArgument(const std::wstring& value) {
    if (value.empty()) {
        return L"\"\"";
    }
    if (value.find_first_of(L" \t\n\v\"") == std::wstring::npos) {
        return value;
    }

    std::wstring result = L"\"";
    std::size_t backslashes = 0;
    for (wchar_t character : value) {
        if (character == L'\\') {
            ++backslashes;
        } else if (character == L'\"') {
            result.append(backslashes * 2 + 1, L'\\');
            result.push_back(L'\"');
            backslashes = 0;
        } else {
            result.append(backslashes, L'\\');
            backslashes = 0;
            result.push_back(character);
        }
    }
    result.append(backslashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}

std::wstring GetExecutableDirectory() {
    std::vector<wchar_t> buffer(32768);
    const DWORD length = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (length == 0 || length >= buffer.size()) {
        throw std::runtime_error("GetModuleFileNameW failed");
    }
    return std::filesystem::path(std::wstring(buffer.data(), length)).parent_path().wstring();
}

std::optional<std::wstring> GetEnvironment(const wchar_t* name) {
    const DWORD required = GetEnvironmentVariableW(name, nullptr, 0);
    if (required == 0) {
        return std::nullopt;
    }
    std::vector<wchar_t> buffer(required);
    GetEnvironmentVariableW(name, buffer.data(), required);
    return std::wstring(buffer.data());
}

EnvironmentRestore SetTemporaryEnvironment(const wchar_t* name, const std::wstring& value) {
    EnvironmentRestore restore{name, GetEnvironment(name)};
    if (!SetEnvironmentVariableW(name, value.c_str())) {
        throw std::runtime_error("SetEnvironmentVariableW failed");
    }
    return restore;
}

void RestoreEnvironment(const EnvironmentRestore& restore) {
    SetEnvironmentVariableW(
        restore.name.c_str(),
        restore.value.has_value() ? restore.value->c_str() : nullptr);
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

bool SendSessionRequest(
    const speedtree_collision_cli::SessionRequest& request,
    speedtree_collision_cli::SessionResponse& response,
    DWORD pipeWaitMs,
    DWORD& error) {
    const ULONGLONG deadline = GetTickCount64() + pipeWaitMs;
    HANDLE pipe = INVALID_HANDLE_VALUE;
    do {
        pipe = CreateFileW(
            speedtree_collision_cli::kSessionPipeName,
            GENERIC_READ | GENERIC_WRITE,
            0,
            nullptr,
            OPEN_EXISTING,
            0,
            nullptr);
        if (pipe != INVALID_HANDLE_VALUE) {
            break;
        }
        error = GetLastError();
        if (error != ERROR_FILE_NOT_FOUND && error != ERROR_PIPE_BUSY) {
            return false;
        }
        if (GetTickCount64() >= deadline) {
            return false;
        }
        WaitNamedPipeW(speedtree_collision_cli::kSessionPipeName, 100);
    } while (true);

    const bool success = WritePipeExact(pipe, &request, sizeof(request)) &&
        ReadPipeExact(pipe, &response, sizeof(response));
    if (!success) {
        error = GetLastError();
    } else if (response.magic != speedtree_collision_cli::kSessionProtocolMagic ||
               response.version != speedtree_collision_cli::kSessionProtocolVersion) {
        error = ERROR_REVISION_MISMATCH;
        CloseHandle(pipe);
        return false;
    }
    CloseHandle(pipe);
    return success;
}

bool IsRestartableSessionPipeError(DWORD error) {
    switch (error) {
        case ERROR_SUCCESS:
        case ERROR_FILE_NOT_FOUND:
        case ERROR_PIPE_BUSY:
        case ERROR_BAD_PIPE:
        case ERROR_BROKEN_PIPE:
        case ERROR_NO_DATA:
        case ERROR_PIPE_NOT_CONNECTED:
        case ERROR_SEM_TIMEOUT:
            return true;
        default:
            return false;
    }
}

struct ProcessActivitySnapshot {
    ULONGLONG cpu100ns = 0;
    ULONGLONG ioBytes = 0;
    SIZE_T workingSetBytes = 0;
    DWORD pageFaultCount = 0;
};

ULONGLONG FileTimeValue(const FILETIME& value) {
    ULARGE_INTEGER converted{};
    converted.LowPart = value.dwLowDateTime;
    converted.HighPart = value.dwHighDateTime;
    return converted.QuadPart;
}

bool ReadProcessActivity(HANDLE process, ProcessActivitySnapshot& snapshot) {
    FILETIME creation{};
    FILETIME exit{};
    FILETIME kernel{};
    FILETIME user{};
    IO_COUNTERS io{};
    PROCESS_MEMORY_COUNTERS memory{};
    memory.cb = sizeof(memory);
    if (!GetProcessTimes(process, &creation, &exit, &kernel, &user) ||
        !GetProcessIoCounters(process, &io) ||
        !GetProcessMemoryInfo(process, &memory, sizeof(memory))) {
        return false;
    }
    snapshot.cpu100ns = FileTimeValue(kernel) + FileTimeValue(user);
    snapshot.ioBytes = io.ReadTransferCount + io.WriteTransferCount +
        io.OtherTransferCount;
    snapshot.workingSetBytes = memory.WorkingSetSize;
    snapshot.pageFaultCount = memory.PageFaultCount;
    return true;
}

template <typename T>
T AbsoluteDifference(T left, T right) {
    return left >= right ? left - right : right - left;
}

std::optional<std::filesystem::file_time_type> FileWriteTime(
    const std::filesystem::path& path) {
    std::error_code error;
    const auto value = std::filesystem::last_write_time(path, error);
    return error ? std::nullopt : std::optional(value);
}

bool CopySessionPath(
    wchar_t (&destination)[speedtree_collision_cli::kSessionPathCapacity],
    const std::filesystem::path& path) {
    const std::wstring value = std::filesystem::absolute(path).wstring();
    if (value.size() >= std::size(destination)) {
        return false;
    }
    wcscpy_s(destination, value.c_str());
    return true;
}

std::wstring Sha256File(const std::filesystem::path& path) {
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    DWORD objectBytes = 0;
    DWORD hashBytes = 0;
    DWORD resultBytes = 0;
    std::vector<unsigned char> object;
    std::vector<unsigned char> digest;
    HANDLE file = INVALID_HANDLE_VALUE;

    auto cleanup = [&]() {
        if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
        if (hash != nullptr) BCryptDestroyHash(hash);
        if (algorithm != nullptr) BCryptCloseAlgorithmProvider(algorithm, 0);
    };

    if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0 ||
        BCryptGetProperty(
            algorithm,
            BCRYPT_OBJECT_LENGTH,
            reinterpret_cast<PUCHAR>(&objectBytes),
            sizeof(objectBytes),
            &resultBytes,
            0) < 0 ||
        BCryptGetProperty(
            algorithm,
            BCRYPT_HASH_LENGTH,
            reinterpret_cast<PUCHAR>(&hashBytes),
            sizeof(hashBytes),
            &resultBytes,
            0) < 0) {
        cleanup();
        throw std::runtime_error("BCrypt initialization failed");
    }

    object.resize(objectBytes);
    digest.resize(hashBytes);
    if (BCryptCreateHash(
            algorithm,
            &hash,
            object.data(),
            static_cast<ULONG>(object.size()),
            nullptr,
            0,
            0) < 0) {
        cleanup();
        throw std::runtime_error("BCryptCreateHash failed");
    }

    file = CreateFileW(
        path.c_str(),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        OPEN_EXISTING,
        FILE_FLAG_SEQUENTIAL_SCAN,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        cleanup();
        throw std::runtime_error("input file could not be opened");
    }

    std::vector<unsigned char> buffer(1024 * 1024);
    for (;;) {
        DWORD bytesRead = 0;
        if (!ReadFile(file, buffer.data(), static_cast<DWORD>(buffer.size()), &bytesRead, nullptr)) {
            cleanup();
            throw std::runtime_error("input file read failed");
        }
        if (bytesRead == 0) break;
        if (BCryptHashData(hash, buffer.data(), bytesRead, 0) < 0) {
            cleanup();
            throw std::runtime_error("BCryptHashData failed");
        }
    }
    if (BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) < 0) {
        cleanup();
        throw std::runtime_error("BCryptFinishHash failed");
    }
    cleanup();

    static constexpr wchar_t digits[] = L"0123456789abcdef";
    std::wstring text;
    text.reserve(digest.size() * 2);
    for (unsigned char byte : digest) {
        text.push_back(digits[byte >> 4]);
        text.push_back(digits[byte & 0x0f]);
    }
    return text;
}

bool VerifySupportedInstallation(const std::filesystem::path& modeler, bool printDetails) {
    const auto qtCore = modeler.parent_path() / L"Qt6Core.dll";
    if (!std::filesystem::is_regular_file(modeler) || !std::filesystem::is_regular_file(qtCore)) {
        std::wcerr << L"SpeedTree Modeler or Qt6Core.dll was not found.\n";
        return false;
    }
    const std::wstring modelerHash = Sha256File(modeler);
    const std::wstring qtHash = Sha256File(qtCore);
    if (printDetails) {
        std::wcout << L"Modeler SHA-256: " << modelerHash << L"\n";
        std::wcout << L"Qt6Core SHA-256: " << qtHash << L"\n";
    }
    if (modelerHash != kExpectedModelerSha256 || qtHash != kExpectedQtCoreSha256) {
        std::wcerr
            << L"Unsupported SpeedTree build. No injection was attempted.\n"
            << L"Expected SpeedTree Modeler 10.1.0 and its bundled Qt 6.6.0 binaries.\n";
        return false;
    }
    return true;
}

bool InjectLibrary(HANDLE process, const std::filesystem::path& dllPath) {
    const std::wstring text = dllPath.wstring();
    const SIZE_T bytes = (text.size() + 1) * sizeof(wchar_t);
    void* remoteText = VirtualAllocEx(
        process,
        nullptr,
        bytes,
        MEM_RESERVE | MEM_COMMIT,
        PAGE_READWRITE);
    if (remoteText == nullptr) {
        return false;
    }

    bool success = false;
    SIZE_T written = 0;
    if (WriteProcessMemory(process, remoteText, text.c_str(), bytes, &written) && written == bytes) {
        HMODULE kernel32 = GetModuleHandleW(L"kernel32.dll");
        auto loadLibrary = reinterpret_cast<LPTHREAD_START_ROUTINE>(
            GetProcAddress(kernel32, "LoadLibraryW"));
        if (loadLibrary != nullptr) {
            HANDLE thread = CreateRemoteThread(
                process,
                nullptr,
                0,
                loadLibrary,
                remoteText,
                0,
                nullptr);
            if (thread != nullptr) {
                if (WaitForSingleObject(thread, 30000) == WAIT_OBJECT_0) {
                    DWORD moduleResult = 0;
                    if (GetExitCodeThread(thread, &moduleResult) && moduleResult != 0) {
                        success = true;
                    }
                }
                CloseHandle(thread);
            }
        }
    }
    VirtualFreeEx(process, remoteText, 0, MEM_RELEASE);
    return success;
}

void PrintUsage() {
    std::wcout
        << L"SpeedTree post-collision CLI (SpeedTree Modeler 10.1.0 only)\n\n"
        << L"Usage:\n"
        << L"  speedtree_collision_cli.exe [wrapper options] -- <SpeedTree CLI arguments>\n\n"
        << L"Wrapper options:\n"
        << L"  --modeler <path>      Override SpeedTree_Modeler.exe path\n"
        << L"  --timeout-ms <value>  Collision wait timeout (default: 600000)\n"
        << L"  --stall-timeout-ms <value>  Restart after no CPU/I/O/log progress (default: 30000)\n"
        << L"  --log <path>          Hook diagnostic log path\n"
        << L"  --persistent          Reuse one blank-anchored SpeedTree process\n"
        << L"  --no-persistent       Force the legacy one-process-per-export path\n"
        << L"  --serve-session       Keep one blank-anchored session alive for batch clients\n"
        << L"  --ping-session        Check whether the persistent session is ready\n"
        << L"  --shutdown-session    Stop the persistent SpeedTree process and exit\n"
        << L"  --session-anchor <spm>  Blank SPM kept open by the persistent process\n"
        << L"  --native-cli          Force the headless native CLI path (default)\n"
        << L"  --verification-only   Skip Collision/Prune bake for temporary audit exports\n"
        << L"  --gui-bake            Use the legacy Modeler bake path for diagnosis\n"
        << L"  --secondary-export-options <ini>  Native CLI second export preset\n"
        << L"  --secondary-export <path>  Native CLI second output in the same process\n"
        << L"  --native-receipt <path>  Exact parsed-runtime/export-serializer receipt\n"
        << L"  --isolated-window     Legacy GUI bake: use a private Windows desktop\n"
        << L"  --interactive-window  Show Modeler normally for diagnosis\n"
        << L"  --diagnose            Verify installed binary hashes and exit\n\n"
        << L"Example:\n"
        << L"  speedtree_collision_cli.exe -- tree.spm -export_options Options_MA_Fbx.ini -export tree.fbx\n";
}

int ExplainExitCode(DWORD code) {
    if (code == kNoCollisionThreadExitCode) {
        std::wcerr << L"No CCollisionThread was observed; unculled export was blocked.\n";
        return 20;
    }
    if (code == kCollisionTimeoutExitCode) {
        std::wcerr << L"Post-collision computation timed out; export was blocked.\n";
        return 21;
    }
    if (code == kHookRuntimeFailureExitCode) {
        std::wcerr << L"Post-collision synchronization failed; export was blocked.\n";
        return 22;
    }
    if (code == kNoGeneratedCollisionInputsExitCode) {
        std::wcerr << L"The GUI bake path produced no collision inputs; unculled export was blocked.\n";
        return 23;
    }
    if (code == kProgressStallExitCode) {
        std::wcerr << L"SpeedTree made no meaningful progress; the stalled export was stopped.\n";
        return 24;
    }
    return static_cast<int>(code);
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    try {
        std::filesystem::path modeler = kDefaultModelerPath;
        DWORD timeoutMs = kDefaultTimeoutMs;
        DWORD stallTimeoutMs = kDefaultStallTimeoutMs;
        std::filesystem::path logPath;
        std::filesystem::path sessionAnchor;
        std::filesystem::path secondaryExportOptions;
        std::filesystem::path secondaryExportOutput;
        std::filesystem::path nativeReceipt;
        bool diagnose = false;
        bool serveSession = false;
        bool pingSession = false;
        bool shutdownSession = false;
        bool verificationOnly = false;
        bool nativeCli =
            GetEnvironment(L"SPEEDTREE_COLLISION_NATIVE_CLI") !=
            std::optional<std::wstring>(L"0");
        bool isolateWindow =
            GetEnvironment(L"SPEEDTREE_COLLISION_ISOLATED_WINDOW") !=
            std::optional<std::wstring>(L"0");
        bool persistent = GetEnvironment(L"SPEEDTREE_COLLISION_PERSISTENT") ==
            std::optional<std::wstring>(L"1");
        if (const auto configuredTimeout =
                GetEnvironment(L"SPEEDTREE_COLLISION_WRAPPER_TIMEOUT_MS")) {
            wchar_t* end = nullptr;
            const unsigned long parsed = wcstoul(
                configuredTimeout->c_str(),
                &end,
                10);
            if (end != configuredTimeout->c_str() && *end == L'\0' &&
                parsed >= 1000) {
                timeoutMs = parsed;
            }
        }
        if (const auto configuredAnchor =
                GetEnvironment(L"SPEEDTREE_COLLISION_SESSION_ANCHOR")) {
            sessionAnchor = *configuredAnchor;
        }
        if (const auto configuredStallTimeout =
                GetEnvironment(L"SPEEDTREE_COLLISION_STALL_TIMEOUT_MS")) {
            wchar_t* end = nullptr;
            const unsigned long parsed = wcstoul(
                configuredStallTimeout->c_str(),
                &end,
                10);
            if (end != configuredStallTimeout->c_str() && *end == L'\0' &&
                parsed >= 5000) {
                stallTimeoutMs = parsed;
            }
        }
        std::vector<std::wstring> modelerArguments;

        bool passthrough = false;
        for (int index = 1; index < argc; ++index) {
            const std::wstring value = argv[index];
            if (passthrough) {
                modelerArguments.push_back(value);
            } else if (value == L"--") {
                passthrough = true;
            } else if (value == L"--help" || value == L"-h") {
                PrintUsage();
                return 0;
            } else if (value == L"--diagnose") {
                diagnose = true;
            } else if (value == L"--persistent") {
                persistent = true;
            } else if (value == L"--no-persistent") {
                persistent = false;
            } else if (value == L"--serve-session") {
                persistent = true;
                serveSession = true;
            } else if (value == L"--ping-session") {
                pingSession = true;
            } else if (value == L"--shutdown-session") {
                shutdownSession = true;
            } else if (value == L"--native-cli") {
                nativeCli = true;
            } else if (value == L"--verification-only") {
                verificationOnly = true;
            } else if (value == L"--gui-bake") {
                nativeCli = false;
            } else if (
                value == L"--secondary-export-options" && index + 1 < argc) {
                secondaryExportOptions = argv[++index];
            } else if (value == L"--secondary-export" && index + 1 < argc) {
                secondaryExportOutput = argv[++index];
            } else if (value == L"--native-receipt" && index + 1 < argc) {
                nativeReceipt = argv[++index];
            } else if (value == L"--session-anchor" && index + 1 < argc) {
                sessionAnchor = argv[++index];
            } else if (value == L"--isolated-window") {
                isolateWindow = true;
            } else if (value == L"--interactive-window") {
                isolateWindow = false;
            } else if (value == L"--modeler" && index + 1 < argc) {
                modeler = argv[++index];
            } else if (value == L"--timeout-ms" && index + 1 < argc) {
                wchar_t* end = nullptr;
                const unsigned long parsed = wcstoul(argv[++index], &end, 10);
                if (end == argv[index] || *end != L'\0' || parsed < 1000) {
                    std::wcerr << L"Invalid --timeout-ms value.\n";
                    return 2;
                }
                timeoutMs = parsed;
            } else if (value == L"--stall-timeout-ms" && index + 1 < argc) {
                wchar_t* end = nullptr;
                const unsigned long parsed = wcstoul(argv[++index], &end, 10);
                if (end == argv[index] || *end != L'\0' || parsed < 5000) {
                    std::wcerr << L"Invalid --stall-timeout-ms value.\n";
                    return 2;
                }
                stallTimeoutMs = parsed;
            } else if (value == L"--log" && index + 1 < argc) {
                logPath = argv[++index];
            } else {
                // Keep invocation compact: the -- separator is recommended but not required.
                modelerArguments.push_back(value);
                passthrough = true;
            }
        }

        // Production uses the real Modeler command-line exporter and extends
        // its model calculation before serialization. It owns no GUI document
        // session, so the legacy persistent/private-desktop modes do not apply.
        if (nativeCli) {
            persistent = false;
            isolateWindow = false;
        }
        if (secondaryExportOptions.empty() != secondaryExportOutput.empty()) {
            std::wcerr << L"--secondary-export-options and --secondary-export must be provided together.\n";
            return 2;
        }
        if (!secondaryExportOutput.empty() && !nativeCli) {
            std::wcerr << L"A bundled second export is supported only by native CLI mode.\n";
            return 2;
        }
        if (verificationOnly && !nativeCli) {
            std::wcerr << L"Verification-only export is supported only by native CLI mode.\n";
            return 2;
        }
        if (!nativeReceipt.empty() && !nativeCli) {
            std::wcerr << L"Native receipts are supported only by native CLI mode.\n";
            return 2;
        }

        if (!VerifySupportedInstallation(modeler, diagnose)) {
            return 3;
        }
        const std::filesystem::path hookDll =
            std::filesystem::path(GetExecutableDirectory()) / L"speedtree_collision_hook.dll";
        if (!std::filesystem::is_regular_file(hookDll)) {
            std::wcerr << L"Hook DLL was not found beside the launcher: " << hookDll << L"\n";
            return 4;
        }
        if (diagnose) {
            std::wcout << L"Supported installation verified.\n";
            std::wcout << kCapabilityContract << L"\n";
            return 0;
        }
        if (pingSession) {
            auto request = std::make_unique<speedtree_collision_cli::SessionRequest>();
            speedtree_collision_cli::SessionResponse response{};
            request->command = speedtree_collision_cli::SessionCommand::Ping;
            DWORD pipeError = ERROR_SUCCESS;
            if (!SendSessionRequest(*request, response, 1000, pipeError)) {
                std::wcerr << L"Persistent SpeedTree session is not ready (error "
                           << pipeError << L").\n";
                return 12;
            }
            std::wcout << response.message << L" PID "
                       << response.speedTreeProcessId << L".\n";
            return response.status == ERROR_SUCCESS ? 0 : 12;
        }
        if (shutdownSession) {
            auto request = std::make_unique<speedtree_collision_cli::SessionRequest>();
            speedtree_collision_cli::SessionResponse response{};
            request->command = speedtree_collision_cli::SessionCommand::Shutdown;
            DWORD pipeError = ERROR_SUCCESS;
            if (!SendSessionRequest(*request, response, 1000, pipeError)) {
                if (pipeError == ERROR_FILE_NOT_FOUND) {
                    std::wcout << L"No persistent SpeedTree session is running.\n";
                    return 0;
                }
                std::wcerr << L"Could not stop the persistent SpeedTree session (error "
                           << pipeError << L").\n";
                return 12;
            }
            std::wcout << response.message << L"\n";
            return response.status == ERROR_SUCCESS ? 0 : 12;
        }
        if (!serveSession && modelerArguments.empty()) {
            PrintUsage();
            return 2;
        }
        if (isolateWindow && serveSession) {
            std::wcerr << L"Persistent fileOpen sessions are not supported on a private "
                       << L"Windows desktop; use the default one-shot isolated export.\n";
            return 15;
        }
        if (isolateWindow && persistent) {
            std::wcout << L"Private-desktop isolation requires one process per SPM; "
                       << L"persistent mode is disabled for this export.\n";
            persistent = false;
        }

        std::filesystem::path inputModel;
        std::filesystem::path outputFbx;
        std::filesystem::path exportOptions;
        bool gameExport = false;
        if (!serveSession) {
            const bool hasExport = std::find(
                modelerArguments.begin(), modelerArguments.end(), L"-export") !=
                modelerArguments.end();
            if (!hasExport) {
                std::wcerr << L"The wrapper requires the native -export argument.\n";
                return 2;
            }
            inputModel = modelerArguments.front();
            for (std::size_t index = 1; index < modelerArguments.size(); ++index) {
                if (modelerArguments[index] == L"-export" && index + 1 < modelerArguments.size()) {
                    outputFbx = modelerArguments[++index];
                } else if (
                    modelerArguments[index] == L"-export_options" &&
                    index + 1 < modelerArguments.size()) {
                    exportOptions = modelerArguments[++index];
                } else if (modelerArguments[index] == L"-export_game") {
                    gameExport = true;
                }
            }
            if (!std::filesystem::is_regular_file(inputModel)) {
                std::wcerr << L"Input SPM was not found: " << inputModel << L"\n";
                return 2;
            }
        }
        if (persistent && !std::filesystem::is_regular_file(sessionAnchor)) {
            std::wcerr << L"Persistent mode requires a valid --session-anchor SPM.\n";
            return 2;
        }
        if (!serveSession &&
            (outputFbx.empty() || exportOptions.empty() ||
             !std::filesystem::is_regular_file(exportOptions))) {
            std::wcerr << L"Both a valid -export_options file and an -export output path are required.\n";
            return 2;
        }
        if (!secondaryExportOptions.empty() &&
            !std::filesystem::is_regular_file(secondaryExportOptions)) {
            std::wcerr << L"Secondary export options were not found: "
                       << secondaryExportOptions << L"\n";
            return 2;
        }
        std::optional<std::filesystem::file_time_type> outputWriteTimeBefore;
        std::optional<std::filesystem::file_time_type> secondaryWriteTimeBefore;
        std::optional<std::filesystem::file_time_type> receiptWriteTimeBefore;
        if (!serveSession && std::filesystem::is_regular_file(outputFbx)) {
            outputWriteTimeBefore = std::filesystem::last_write_time(outputFbx);
        }
        if (!secondaryExportOutput.empty() &&
            std::filesystem::is_regular_file(secondaryExportOutput)) {
            secondaryWriteTimeBefore =
                std::filesystem::last_write_time(secondaryExportOutput);
        }
        if (!nativeReceipt.empty() &&
            std::filesystem::is_regular_file(nativeReceipt)) {
            receiptWriteTimeBefore = std::filesystem::last_write_time(nativeReceipt);
        }

        if (logPath.empty()) {
            wchar_t tempDirectory[32768]{};
            GetTempPathW(static_cast<DWORD>(std::size(tempDirectory)), tempDirectory);
            std::wstringstream name;
            name << L"speedtree_collision_cli_" << GetCurrentProcessId() << L".log";
            logPath = std::filesystem::path(tempDirectory) / name.str();
        }

        auto sessionRequest = std::unique_ptr<speedtree_collision_cli::SessionRequest>();
        if (persistent && !serveSession) {
            sessionRequest = std::make_unique<speedtree_collision_cli::SessionRequest>();
            sessionRequest->command = speedtree_collision_cli::SessionCommand::Export;
            sessionRequest->timeoutMs = timeoutMs;
            sessionRequest->gameExport = gameExport ? 1u : 0u;
            if (!CopySessionPath(sessionRequest->input, inputModel) ||
                !CopySessionPath(sessionRequest->output, outputFbx) ||
                !CopySessionPath(sessionRequest->exportOptions, exportOptions)) {
                std::wcerr << L"A persistent-session path exceeds the Windows long-path limit.\n";
                return 2;
            }

            speedtree_collision_cli::SessionResponse existingResponse{};
            DWORD pipeError = ERROR_SUCCESS;
            bool existingRequestCompleted =
                SendSessionRequest(*sessionRequest, existingResponse, 0, pipeError);
            if (!existingRequestCompleted && pipeError == ERROR_PIPE_BUSY) {
                existingRequestCompleted = SendSessionRequest(
                    *sessionRequest,
                    existingResponse,
                    timeoutMs + 5 * 60 * 1000,
                    pipeError);
            }
            if (existingRequestCompleted) {
                if (existingResponse.status != ERROR_SUCCESS) {
                    std::wcerr << existingResponse.message << L" (error "
                               << existingResponse.status << L").\n";
                    return 22;
                }
                if (!std::filesystem::is_regular_file(outputFbx) ||
                    (outputWriteTimeBefore.has_value() &&
                     std::filesystem::last_write_time(outputFbx) == *outputWriteTimeBefore)) {
                    std::wcerr << L"The persistent session did not create a fresh FBX.\n";
                    return 9;
                }
                std::wcout << L"Post-collision export completed in persistent SpeedTree PID "
                           << existingResponse.speedTreeProcessId << L".\n";
                return 0;
            }
            if (!IsRestartableSessionPipeError(pipeError)) {
                std::wcerr << L"Could not contact the persistent SpeedTree session (error "
                           << pipeError << L").\n";
                return 12;
            }
            std::wcerr << L"The previous persistent SpeedTree session disappeared "
                       << L"(pipe error " << pipeError << L"); starting a replacement.\n";
        }

        if (serveSession) {
            auto pingRequest = std::make_unique<speedtree_collision_cli::SessionRequest>();
            speedtree_collision_cli::SessionResponse existingResponse{};
            pingRequest->command = speedtree_collision_cli::SessionCommand::Ping;
            DWORD pipeError = ERROR_SUCCESS;
            if (SendSessionRequest(*pingRequest, existingResponse, 1000, pipeError)) {
                std::wcout << L"Persistent SpeedTree session is already ready in PID "
                           << existingResponse.speedTreeProcessId << L".\n";
                return existingResponse.status == ERROR_SUCCESS ? 0 : 12;
            }
            if (pipeError != ERROR_FILE_NOT_FOUND && pipeError != ERROR_PIPE_BUSY) {
                std::wcerr << L"Could not inspect the persistent SpeedTree session (error "
                           << pipeError << L").\n";
                return 12;
            }
        }

        // The compatibility path opens the target at startup. Persistent mode
        // opens a known blank SPM at startup and keeps that document as the
        // reusable process anchor.
        std::wstring command = QuoteWindowsArgument(modeler.wstring());
        if (nativeCli) {
            for (const auto& argument : modelerArguments) {
                command.push_back(L' ');
                command += QuoteWindowsArgument(argument);
            }
        } else {
            const std::filesystem::path startupModel = persistent ? sessionAnchor : inputModel;
            command.push_back(L' ');
            command += QuoteWindowsArgument(std::filesystem::absolute(startupModel).wstring());
        }
        std::vector<wchar_t> mutableCommand(command.begin(), command.end());
        mutableCommand.push_back(L'\0');

        const auto restoreLog = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_LOG",
            std::filesystem::absolute(logPath).wstring());
        const auto restoreTimeout = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_TIMEOUT_MS",
            std::to_wstring(timeoutMs));
        // Keep the shortest supported timeout as a fallback, while the injected
        // CLI hook sends the unavailable endpoint directly through RLM's normal
        // exhausted-retry path. Restore both launch settings immediately after
        // CreateProcessW so they remain child-only.
        const auto restoreRlmConnectTimeout = SetTemporaryEnvironment(
            L"RLM_CONNECT_TIMEOUT",
            L"1");
        const auto restoreRlmFailFast = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_RLM_FAIL_FAST",
            L"1");
        const auto restoreGuiBake = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_GUI_BAKE",
            nativeCli ? L"0" : L"1");
        const auto restoreVerificationOnly = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_VERIFICATION_ONLY",
            verificationOnly ? L"1" : L"0");
        const auto restoreOutput = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_OUTPUT",
            serveSession ? L"" : std::filesystem::absolute(outputFbx).wstring());
        const auto restoreExportOptions = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_EXPORT_OPTIONS",
            serveSession ? L"" : std::filesystem::absolute(exportOptions).wstring());
        const auto restoreSecondaryOutput = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_SECONDARY_OUTPUT",
            secondaryExportOutput.empty()
                ? L""
                : std::filesystem::absolute(secondaryExportOutput).wstring());
        const auto restoreSecondaryOptions = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_SECONDARY_OPTIONS",
            secondaryExportOptions.empty()
                ? L""
                : std::filesystem::absolute(secondaryExportOptions).wstring());
        const auto restoreNativeInput = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_INPUT",
            serveSession ? L"" : std::filesystem::absolute(inputModel).wstring());
        const auto restoreNativeReceipt = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_NATIVE_RECEIPT",
            nativeReceipt.empty()
                ? L""
                : std::filesystem::absolute(nativeReceipt).wstring());
        const auto restoreGameExport = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_GAME_EXPORT",
            gameExport ? L"1" : L"0");
        const auto restoreSessionServer = SetTemporaryEnvironment(
            L"SPEEDTREE_COLLISION_CLI_SESSION_SERVER",
            persistent ? L"1" : L"0");

        DesktopHandle isolatedDesktop;
        std::wstring isolatedDesktopName;
        if (isolateWindow) {
            std::wstringstream name;
            name << L"SpeedTreeBatch_" << GetCurrentProcessId() << L"_"
                 << GetTickCount64();
            isolatedDesktopName = name.str();
            isolatedDesktop.value = CreateDesktopW(
                isolatedDesktopName.c_str(),
                nullptr,
                nullptr,
                0,
                GENERIC_ALL,
                nullptr);
            if (isolatedDesktop.value == nullptr) {
                std::wcerr << L"Could not create the private SpeedTree desktop (error "
                           << GetLastError() << L").\n";
                return 14;
            }
        }

        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        startup.dwFlags = STARTF_USESHOWWINDOW;
        if (isolateWindow) {
            // The private desktop is never switched onto the user's input
            // station. SpeedTree can therefore remain normally shown and active
            // for its GUI-driven collision path without taking the user's focus,
            // receiving their mouse input, or appearing on their taskbar.
            startup.wShowWindow = SW_SHOW;
            startup.lpDesktop = isolatedDesktopName.data();
        } else {
            startup.wShowWindow = nativeCli ? SW_HIDE : SW_SHOWNOACTIVATE;
        }
        PROCESS_INFORMATION process{};
        RegistryValueRestore showNewOnStartRestore{};
        if (persistent && !SetTemporaryStringRegistryValue(
                HKEY_CURRENT_USER,
                L"Software\\IDV, Inc.\\SpeedTreeModeler9",
                L"ShowNewOnStart",
                L"false",
                showNewOnStartRestore)) {
            std::wcerr << L"Could not temporarily disable SpeedTree ShowNewOnStart.\n";
            return 13;
        }
        const BOOL created = CreateProcessW(
            modeler.c_str(),
            mutableCommand.data(),
            nullptr,
            nullptr,
            FALSE,
            CREATE_SUSPENDED,
            nullptr,
            nullptr,
            &startup,
            &process);

        RestoreEnvironment(restoreSessionServer);
        RestoreEnvironment(restoreGameExport);
        RestoreEnvironment(restoreNativeReceipt);
        RestoreEnvironment(restoreNativeInput);
        RestoreEnvironment(restoreSecondaryOptions);
        RestoreEnvironment(restoreSecondaryOutput);
        RestoreEnvironment(restoreExportOptions);
        RestoreEnvironment(restoreOutput);
        RestoreEnvironment(restoreVerificationOnly);
        RestoreEnvironment(restoreGuiBake);
        RestoreEnvironment(restoreRlmFailFast);
        RestoreEnvironment(restoreRlmConnectTimeout);
        RestoreEnvironment(restoreTimeout);
        RestoreEnvironment(restoreLog);

        if (!created) {
            RestoreRegistryValue(HKEY_CURRENT_USER, showNewOnStartRestore);
            std::wcerr << L"CreateProcessW failed with error " << GetLastError() << L".\n";
            return 5;
        }

        if (!InjectLibrary(process.hProcess, std::filesystem::absolute(hookDll))) {
            RestoreRegistryValue(HKEY_CURRENT_USER, showNewOnStartRestore);
            std::wcerr << L"Hook injection failed. The suspended SpeedTree child will be terminated.\n";
            TerminateProcess(process.hProcess, 6);
            CloseHandle(process.hThread);
            CloseHandle(process.hProcess);
            return 6;
        }

        if (ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
            RestoreRegistryValue(HKEY_CURRENT_USER, showNewOnStartRestore);
            std::wcerr << L"ResumeThread failed. The SpeedTree child will be terminated.\n";
            TerminateProcess(process.hProcess, 7);
            CloseHandle(process.hThread);
            CloseHandle(process.hProcess);
            return 7;
        }
        CloseHandle(process.hThread);

        const DWORD initializationTimeoutMs = (std::min)(timeoutMs, stallTimeoutMs);
        const DWORD inputIdleResult = WaitForInputIdle(
            process.hProcess,
            initializationTimeoutMs);
        if (inputIdleResult != 0) {
            RestoreRegistryValue(HKEY_CURRENT_USER, showNewOnStartRestore);
            std::wcerr << L"SpeedTree did not reach its initialized input-idle state (error "
                       << inputIdleResult << L").\n";
            TerminateProcess(process.hProcess, 13);
            CloseHandle(process.hProcess);
            return 13;
        }
        if (isolateWindow) {
            std::wcout << L"SpeedTree is active on an isolated Windows desktop.\n";
        }

        if (serveSession) {
            auto pingRequest = std::make_unique<speedtree_collision_cli::SessionRequest>();
            speedtree_collision_cli::SessionResponse response{};
            pingRequest->command = speedtree_collision_cli::SessionCommand::Ping;
            DWORD pipeError = ERROR_SUCCESS;
            const bool ready = SendSessionRequest(*pingRequest, response, 30000, pipeError);
            RestoreRegistryValue(HKEY_CURRENT_USER, showNewOnStartRestore);
            if (!ready || response.status != ERROR_SUCCESS) {
                TerminateProcess(process.hProcess, 12);
                CloseHandle(process.hProcess);
                std::wcerr << L"The persistent SpeedTree session host did not become ready "
                           << L"(pipe error " << pipeError << L").\n";
                return 12;
            }
            std::wcout << L"Persistent SpeedTree session ready in PID "
                       << response.speedTreeProcessId << L".\n";
            std::wcout << L"Hook log: " << std::filesystem::absolute(logPath) << L"\n";
            const DWORD waitResult = WaitForSingleObject(process.hProcess, INFINITE);
            DWORD exitCode = 12;
            if (waitResult == WAIT_OBJECT_0) {
                GetExitCodeProcess(process.hProcess, &exitCode);
            }
            CloseHandle(process.hProcess);
            return waitResult == WAIT_OBJECT_0 ? static_cast<int>(exitCode) : 12;
        }

        if (persistent) {
            speedtree_collision_cli::SessionResponse response{};
            DWORD pipeError = ERROR_SUCCESS;
            const bool requestCompleted =
                SendSessionRequest(*sessionRequest, response, 30000, pipeError);
            RestoreRegistryValue(HKEY_CURRENT_USER, showNewOnStartRestore);
            if (!requestCompleted) {
                DWORD childExitCode = STILL_ACTIVE;
                GetExitCodeProcess(process.hProcess, &childExitCode);
                if (childExitCode == STILL_ACTIVE) {
                    TerminateProcess(process.hProcess, 12);
                }
                CloseHandle(process.hProcess);
                std::wcerr << L"The persistent SpeedTree session did not accept the first job "
                           << L"(pipe error " << pipeError << L").\n";
                std::wcerr << L"Hook log: " << std::filesystem::absolute(logPath) << L"\n";
                return childExitCode != STILL_ACTIVE && childExitCode != 0
                    ? ExplainExitCode(childExitCode)
                    : 12;
            }
            CloseHandle(process.hProcess);
            if (response.status != ERROR_SUCCESS) {
                std::wcerr << response.message << L" (error " << response.status << L").\n";
                std::wcerr << L"Hook log: " << std::filesystem::absolute(logPath) << L"\n";
                return 22;
            }
            if (!std::filesystem::is_regular_file(outputFbx) ||
                (outputWriteTimeBefore.has_value() &&
                 std::filesystem::last_write_time(outputFbx) == *outputWriteTimeBefore)) {
                std::wcerr << L"The persistent session did not create a fresh FBX.\n";
                return 9;
            }
            std::wcout << L"Post-collision export completed in persistent SpeedTree PID "
                       << response.speedTreeProcessId << L".\n";
            std::wcout << L"Hook log: " << std::filesystem::absolute(logPath) << L"\n";
            return 0;
        }

        // ``timeoutMs`` is the wrapper's absolute ownership budget. Process
        // CPU/memory churn may keep the shorter progress-stall detector alive,
        // but it must never extend this hard deadline or monopolize the shared
        // machine-wide SpeedTree export gate.
        const DWORD processWaitMs = timeoutMs;
        const ULONGLONG waitDeadline = GetTickCount64() + processWaitMs;
        ULONGLONG lastMeaningfulProgress = GetTickCount64();
        ProcessActivitySnapshot activityBaseline{};
        ReadProcessActivity(process.hProcess, activityBaseline);
        auto logWriteTime = FileWriteTime(logPath);
        DWORD waitResult = WAIT_TIMEOUT;
        while (GetTickCount64() < waitDeadline) {
            waitResult = WaitForSingleObject(process.hProcess, 250);
            if (waitResult == WAIT_OBJECT_0 || waitResult == WAIT_FAILED) {
                break;
            }
            ProcessActivitySnapshot currentActivity{};
            const bool activityRead = ReadProcessActivity(
                process.hProcess,
                currentActivity);
            const auto currentLogWriteTime = FileWriteTime(logPath);
            const bool logAdvanced = currentLogWriteTime.has_value() &&
                (!logWriteTime.has_value() || *currentLogWriteTime != *logWriteTime);
            const bool cpuAdvanced = activityRead &&
                currentActivity.cpu100ns >= activityBaseline.cpu100ns + 2'500'000;
            const bool ioAdvanced = activityRead &&
                currentActivity.ioBytes >= activityBaseline.ioBytes + 4 * 1024;
            const bool memoryAdvanced = activityRead &&
                AbsoluteDifference(
                    currentActivity.workingSetBytes,
                    activityBaseline.workingSetBytes) >= 512 * 1024;
            const bool pageFaultsAdvanced = activityRead &&
                currentActivity.pageFaultCount >= activityBaseline.pageFaultCount + 64;
            if (logAdvanced || cpuAdvanced || ioAdvanced ||
                memoryAdvanced || pageFaultsAdvanced) {
                lastMeaningfulProgress = GetTickCount64();
                if (activityRead) {
                    activityBaseline = currentActivity;
                }
                logWriteTime = currentLogWriteTime;
            }
            if (GetTickCount64() - lastMeaningfulProgress >= stallTimeoutMs) {
                std::wcerr << L"SpeedTree produced no meaningful CPU, I/O, memory, or hook-log "
                           << L"progress for " << stallTimeoutMs
                           << L" ms. The exact launched child will be terminated for retry.\n";
                TerminateProcess(process.hProcess, kProgressStallExitCode);
                WaitForSingleObject(process.hProcess, 5000);
                CloseHandle(process.hProcess);
                return ExplainExitCode(kProgressStallExitCode);
            }
        }
        if (waitResult != WAIT_OBJECT_0) {
            std::wcerr << L"SpeedTree did not exit within the wrapper timeout. The launched child will be terminated.\n";
            TerminateProcess(process.hProcess, 8);
            CloseHandle(process.hProcess);
            return 8;
        }

        DWORD exitCode = 0;
        GetExitCodeProcess(process.hProcess, &exitCode);
        CloseHandle(process.hProcess);
        if (exitCode != 0) {
            std::wcerr << L"SpeedTree exited with code 0x" << std::hex << exitCode << std::dec << L".\n";
            std::wcerr << L"Hook log: " << std::filesystem::absolute(logPath) << L"\n";
            return ExplainExitCode(exitCode);
        }
        if (!std::filesystem::is_regular_file(outputFbx)) {
            std::wcerr << L"SpeedTree exited successfully but the requested FBX was not created.\n";
            return 9;
        }
        if (outputWriteTimeBefore.has_value() &&
            std::filesystem::last_write_time(outputFbx) == *outputWriteTimeBefore) {
            std::wcerr << L"The requested FBX was not updated; stale output was rejected.\n";
            return 9;
        }
        if (!secondaryExportOutput.empty()) {
            if (!std::filesystem::is_regular_file(secondaryExportOutput)) {
                std::wcerr << L"SpeedTree exited successfully but the secondary output was not created.\n";
                return 9;
            }
            if (secondaryWriteTimeBefore.has_value() &&
                std::filesystem::last_write_time(secondaryExportOutput) ==
                    *secondaryWriteTimeBefore) {
                std::wcerr << L"The secondary output was not updated; stale output was rejected.\n";
                return 9;
            }
        }
        if (!nativeReceipt.empty()) {
            if (!std::filesystem::is_regular_file(nativeReceipt)) {
                std::wcerr << L"SpeedTree exited successfully but the native receipt was not created.\n";
                return 9;
            }
            if (receiptWriteTimeBefore.has_value() &&
                std::filesystem::last_write_time(nativeReceipt) ==
                    *receiptWriteTimeBefore) {
                std::wcerr << L"The native receipt was not updated; stale evidence was rejected.\n";
                return 9;
            }
        }

        std::wcout << L"Post-collision export completed.\n";
        std::wcout << L"Hook log: " << std::filesystem::absolute(logPath) << L"\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Fatal error: " << error.what() << "\n";
        return 10;
    }
}
