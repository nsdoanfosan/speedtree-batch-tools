#pragma once

#include <cstddef>
#include <cstdint>

namespace speedtree_collision_cli {

constexpr wchar_t kSessionPipeName[] =
    L"\\\\.\\pipe\\speedtree_collision_cli_10_1_v1";
constexpr std::uint32_t kSessionProtocolMagic = 0x53435443;
constexpr std::uint32_t kSessionProtocolVersion = 1;
constexpr std::size_t kSessionPathCapacity = 32768;
constexpr std::size_t kSessionMessageCapacity = 512;

enum class SessionCommand : std::uint32_t {
    Export = 1,
    Shutdown = 2,
    Ping = 3,
};

struct SessionRequest {
    std::uint32_t magic = kSessionProtocolMagic;
    std::uint32_t version = kSessionProtocolVersion;
    SessionCommand command = SessionCommand::Export;
    std::uint32_t timeoutMs = 0;
    std::uint32_t gameExport = 0;
    std::uint32_t reserved = 0;
    wchar_t input[kSessionPathCapacity]{};
    wchar_t output[kSessionPathCapacity]{};
    wchar_t exportOptions[kSessionPathCapacity]{};
};

struct SessionResponse {
    std::uint32_t magic = kSessionProtocolMagic;
    std::uint32_t version = kSessionProtocolVersion;
    std::uint32_t status = 0;
    std::uint32_t speedTreeProcessId = 0;
    wchar_t message[kSessionMessageCapacity]{};
};

}  // namespace speedtree_collision_cli
