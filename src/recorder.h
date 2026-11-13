#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace maa_trace {
struct Attachment {
    std::string id;
    std::string mime_type;
    std::vector<uint8_t> bytes;
    nlohmann::json metadata = nlohmann::json::object();
};
void record(const char* source, uintptr_t owner, const char* message,
            const char* details, nlohmann::json enrichment = nlohmann::json::object(),
            std::vector<Attachment> attachments = {}) noexcept;
void report_error(const char* message) noexcept;
bool flush() noexcept;
}
