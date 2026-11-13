#include <MaaTrace/MaaTrace.h>

#include "recorder.h"

#include <MaaFramework/MaaAPI.h>
#include <nlohmann/json.hpp>

#include <cstdint>
#include <cstdlib>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace {

using Json = nlohmann::json;

class StringBuffer {
public:
    StringBuffer() : value_(MaaStringBufferCreate())
    {
        if (!value_) {
            throw std::bad_alloc();
        }
    }
    ~StringBuffer() { MaaStringBufferDestroy(value_); }
    StringBuffer(const StringBuffer&) = delete;
    StringBuffer& operator=(const StringBuffer&) = delete;
    operator MaaStringBuffer*() const { return value_; }
    std::string str() const
    {
        const char* data = MaaStringBufferGet(value_);
        return data ? std::string(data, static_cast<size_t>(MaaStringBufferSize(value_))) : std::string();
    }

private:
    MaaStringBuffer* value_;
};

class ImageBuffer {
public:
    ImageBuffer() : value_(MaaImageBufferCreate())
    {
        if (!value_) {
            throw std::bad_alloc();
        }
    }
    ~ImageBuffer() { MaaImageBufferDestroy(value_); }
    ImageBuffer(const ImageBuffer&) = delete;
    ImageBuffer& operator=(const ImageBuffer&) = delete;
    operator MaaImageBuffer*() const { return value_; }

private:
    MaaImageBuffer* value_;
};

struct ImagePolicy {
    enum class Mode { None, Failed, All };
    Mode mode = Mode::None;
    uint64_t max_bytes = 8 * 1024 * 1024;
    bool valid = true;
    const char* name = "none";
};

const ImagePolicy& image_policy()
{
    // Environment values here are ASCII enums/integers; no path conversion is
    // involved. Retain configuration through callback and process teardown.
    static const auto* policy = [] {
        auto result = std::make_unique<ImagePolicy>();
        const char* enabled = std::getenv("MAA_TRACE_ENABLED");
        if (enabled && std::string_view(enabled) == "0") {
            return result.release();
        }
        const char* mode = std::getenv("MAA_TRACE_IMAGES");
        const std::string_view value = mode && *mode ? mode : "none";
        if (value == "all") {
            result->mode = ImagePolicy::Mode::All;
            result->name = "all";
        } else if (value == "failed") {
            result->mode = ImagePolicy::Mode::Failed;
            result->name = "failed";
        } else if (value != "none") {
            result->valid = false;
            maa_trace::report_error("MAA_TRACE_IMAGES must be none, failed, or all");
        }
        const char* limit = std::getenv("MAA_TRACE_MAX_IMAGE_BYTES");
        if (limit && *limit) {
            uint64_t parsed = 0;
            bool valid_limit = true;
            for (const char* cursor = limit; *cursor; ++cursor) {
                if (*cursor < '0' || *cursor > '9'
                    || parsed > (std::numeric_limits<uint64_t>::max() - static_cast<unsigned>(*cursor - '0')) / 10) {
                    valid_limit = false;
                    break;
                }
                parsed = parsed * 10 + static_cast<unsigned>(*cursor - '0');
            }
            if (!parsed) valid_limit = false;
            if (valid_limit) result->max_bytes = parsed;
            else {
                result->valid = false;
                maa_trace::report_error("MAA_TRACE_MAX_IMAGE_BYTES must be a positive integer");
            }
        }
        return result.release();
    }();
    return *policy;
}

Json parse_json(const char* text)
{
    if (!text) {
        return Json::object();
    }
    auto result = Json::parse(text, nullptr, false);
    return result.is_discarded() ? Json::object() : result;
}

Json buffer_json(const StringBuffer& buffer)
{
    const auto text = buffer.str();
    auto parsed = Json::parse(text, nullptr, false);
    // Custom results can be arbitrary text. Preserve them without claiming JSON.
    return parsed.is_discarded() ? Json(text) : parsed;
}

MaaId native_id(const Json& data, const char* field)
{
    const auto it = data.find(field);
    if (it == data.end() || !it->is_number_integer()) {
        return MaaInvalidId;
    }
    if (it->is_number_unsigned() && it->get<uint64_t>() > static_cast<uint64_t>(std::numeric_limits<int64_t>::max())) {
        return MaaInvalidId;
    }
    return it->get<int64_t>();
}

Json rect(const MaaRect& box)
{
    return Json::array({box.x, box.y, box.width, box.height});
}

bool terminal(std::string_view message)
{
    const auto pos = message.rfind('.');
    if (pos == std::string_view::npos) {
        return false;
    }
    const auto suffix = message.substr(pos + 1);
    return suffix == "Succeeded" || suffix == "Failed" || suffix == "Cancelled";
}

bool starts_with(std::string_view value, std::string_view prefix)
{
    return value.substr(0, prefix.size()) == prefix;
}

std::string context_id(MaaTasker* tasker, void* handle, MaaId task_id);

Json recognition_detail(MaaTasker* tasker, const Json& details)
{
    if (details.contains("reco_details")) {
        return {{"availability", "available"}, {"source", "callback"}, {"value", details["reco_details"]}};
    }
    const auto id = native_id(details, "reco_id");
    if (id == MaaInvalidId) {
        return {{"availability", "unavailable"}, {"reason", "missing_id"}};
    }
    StringBuffer name, algorithm, detail;
    MaaBool hit = false;
    MaaRect box{};
    // Null image outputs intentionally avoid copying/retaining screenshots.
    if (!MaaTaskerGetRecognitionDetail(tasker, id, name, algorithm, &hit, &box, detail, nullptr, nullptr)) {
        return {{"availability", "unavailable"}, {"reason", "cache_miss"}};
    }
    return {
        {"availability", "available"},
        {"source", "runtime_cache"},
        {"value", {{"reco_id", std::to_string(id)}, {"name", name.str()}, {"algorithm", algorithm.str()},
                   {"box", hit ? rect(box) : Json(nullptr)}, {"detail", buffer_json(detail)}}},
        // Older caches contain the algorithm result before Pipeline.inverse.
        // Only the native event determines the final recognition outcome.
        {"before_pipeline_inverse", true},
    };
}

Json recognition_image(MaaTasker* tasker, const Json& details, std::string_view message,
                       std::vector<maa_trace::Attachment>& attachments)
{
    const auto& policy = image_policy();
    Json status = {{"policy", policy.name}, {"source", "recognition_runtime_cache"}};
    auto unavailable = [&](const char* availability, const char* reason) {
        status["availability"] = availability;
        status["reason"] = reason;
        return status;
    };
    if (!policy.valid) return unavailable("unavailable", "invalid_image_configuration");
    if (policy.mode == ImagePolicy::Mode::None) return unavailable("disabled", "policy_none");
    if (policy.mode == ImagePolicy::Mode::Failed && message != "Node.Recognition.Failed") {
        return unavailable("skipped", "policy_failed_only");
    }
    const auto id = native_id(details, "reco_id");
    if (id == MaaInvalidId) return unavailable("unavailable", "missing_recognition_id");

    StringBuffer name, algorithm, detail;
    ImageBuffer raw;
    MaaBool hit = false;
    MaaRect box{};
    if (!MaaTaskerGetRecognitionDetail(tasker, id, name, algorithm, &hit, &box, detail, raw, nullptr)) {
        return unavailable("unavailable", "recognition_cache_miss");
    }
    if (MaaImageBufferIsEmpty(raw)) {
        // Do not enable debug mode or capture a new controller frame: that would
        // change host behavior and substitute a different image as evidence.
        return unavailable("unavailable", "debug_required_or_not_retained");
    }
    const auto size = MaaImageBufferGetEncodedSize(raw);
    status["encoded_bytes"] = size;
    if (size > policy.max_bytes || size > std::numeric_limits<size_t>::max()) {
        status["max_bytes"] = policy.max_bytes;
        return unavailable("omitted", "image_size_limit");
    }
    const auto* encoded = MaaImageBufferGetEncoded(raw);
    if (!encoded || size < 8) return unavailable("unavailable", "encoded_image_empty");
    // Framework ImageBuffer encoding is PNG. Validate the signature so an ABI or
    // framework change cannot silently label a different format as PNG.
    constexpr uint8_t signature[] = {137, 80, 78, 71, 13, 10, 26, 10};
    for (size_t i = 0; i < sizeof(signature); ++i) {
        if (encoded[i] != signature[i]) return unavailable("unavailable", "unsupported_image_encoding");
    }
    maa_trace::Attachment image;
    image.id = "recognition-" + std::to_string(id);
    image.mime_type = "image/png";
    image.bytes.assign(encoded, encoded + static_cast<size_t>(size));
    image.metadata = {
        {"role", "recognition_input"}, {"source", "recognition_runtime_cache"},
        {"reco_id", std::to_string(id)}, {"coordinate_space", "recognition_image"},
        {"width", MaaImageBufferWidth(raw)}, {"height", MaaImageBufferHeight(raw)},
        {"channels", MaaImageBufferChannels(raw)}
    };
    status["availability"] = "captured";
    status["artifact_id"] = image.id;
    // Captured means bytes have been copied at the callback. The writer provides
    // the final persisted/omitted result and relative path in data.artifacts.
    attachments.push_back(std::move(image));
    return status;
}

Json action_detail(MaaTasker* tasker, const Json& details)
{
    if (details.contains("action_details")) {
        return {{"availability", "available"}, {"source", "callback"}, {"value", details["action_details"]}};
    }
    const auto id = native_id(details, "action_id");
    if (id == MaaInvalidId) {
        return {{"availability", "unavailable"}, {"reason", "missing_id"}};
    }
    StringBuffer name, action, detail;
    MaaBool success = false;
    MaaRect box{};
    if (!MaaTaskerGetActionDetail(tasker, id, name, action, &box, &success, detail)) {
        return {{"availability", "unavailable"}, {"reason", "cache_miss"}};
    }
    return {
        {"availability", "available"}, {"source", "runtime_cache"},
        {"value", {{"action_id", std::to_string(id)}, {"name", name.str()}, {"action", action.str()},
                   {"box", rect(box)}, {"success", success != 0}, {"detail", buffer_json(detail)}}},
    };
}

Json node_detail(MaaTasker* tasker, const Json& details)
{
    if (details.contains("node_details")) {
        return {{"availability", "available"}, {"source", "callback"}, {"value", details["node_details"]}};
    }
    const auto id = native_id(details, "node_id");
    if (id == MaaInvalidId) {
        return {{"availability", "unavailable"}, {"reason", "missing_id"}};
    }
    StringBuffer name;
    MaaRecoId reco_id = MaaInvalidId;
    MaaActId action_id = MaaInvalidId;
    MaaBool completed = false;
    if (!MaaTaskerGetNodeDetail(tasker, id, name, &reco_id, &action_id, &completed)) {
        return {{"availability", "unavailable"}, {"reason", "cache_miss"}};
    }
    return {
        {"availability", "available"}, {"source", "runtime_cache"},
        {"value", {{"node_id", std::to_string(id)}, {"name", name.str()}, {"reco_id", std::to_string(reco_id)},
                   {"action_id", std::to_string(action_id)}, {"completed", completed != 0}}},
    };
}

void MAA_CALL context_event(void* handle, const char* message, const char* details_json, void* trans_arg)
{
    auto* tasker = static_cast<MaaTasker*>(trans_arg);
    const auto owner = reinterpret_cast<uintptr_t>(tasker);
    try {
        Json extra = Json::object();
        std::vector<maa_trace::Attachment> attachments;
        const std::string_view event = message ? message : "";
        const auto details = parse_json(details_json);
        if (tasker && handle && details.is_object()) {
            extra["context_id"] = context_id(tasker, handle, native_id(details, "task_id"));
            extra["context_identity_source"] = "callback_handle_and_native_task";
        }
        if (handle && details.is_object() && event.size() >= 9 && event.substr(event.size() - 9) == ".Starting") {
            const auto name = details.find("name");
            if (name != details.end() && name->is_string() && !name->get_ref<const std::string&>().empty()) {
                StringBuffer node;
                const auto& node_name = name->get_ref<const std::string&>();
                if (MaaContextGetNodeData(static_cast<MaaContext*>(handle), node_name.c_str(), node)) {
                    // This is the named node's effective data at notification time,
                    // not an assertion that a candidate has entered execution.
                    extra["effective_node"] = {{"name", node_name}, {"value", buffer_json(node)},
                                               {"source", "context_query_at_notification"}};
                }
            }
        }
        if (tasker && details.is_object() && terminal(event)) {
            if (starts_with(event, "Node.Recognition.")) {
                extra["recognition"] = recognition_detail(tasker, details);
                try {
                    extra["image"] = recognition_image(tasker, details, event, attachments);
                } catch (...) {
                    maa_trace::report_error("recognition_image_capture_failed");
                    extra["image"] = {{"availability", "unavailable"}, {"reason", "capture_error"}};
                }
            } else if (starts_with(event, "Node.Action.")) {
                extra["action"] = action_detail(tasker, details);
            } else if (starts_with(event, "Node.PipelineNode.") || starts_with(event, "Node.RecognitionNode.")
                       || starts_with(event, "Node.ActionNode.")) {
                extra["node"] = node_detail(tasker, details);
            }
            // WaitFreezes writes its cache AFTER notifying; native details already
            // contain elapsed/reco_ids/roi. Do not query the not-yet-existing cache.
        }
        maa_trace::record("context", owner, message, details_json, std::move(extra), std::move(attachments));
    } catch (...) {
        maa_trace::report_error("context_enrichment_failed");
        maa_trace::record("context", owner, message, details_json);
    }
}

struct Subscriptions {
    struct Subscription {
        MaaSinkId sink = MaaInvalidId;
        std::map<std::pair<uintptr_t, MaaId>, std::string> contexts;
        uint64_t next_context = 0;
    };
    std::mutex mutex;
    std::unordered_map<MaaTasker*, Subscription> sinks;
};

Subscriptions& subscriptions()
{
    // Process-lifetime storage avoids calling into MaaFramework at static teardown.
    // Native plugin libraries are retained by PluginMgr for the process lifetime.
    static auto* state = new Subscriptions;
    return *state;
}

std::string context_id(MaaTasker* tasker, void* handle, MaaId task_id)
{
    auto& state = subscriptions();
    std::lock_guard<std::mutex> lock(state.mutex);
    const auto sub = state.sinks.find(tasker);
    if (sub == state.sinks.end()) {
        return "unavailable";
    }
    auto& subscription = sub->second;
    const auto key = std::make_pair(reinterpret_cast<uintptr_t>(handle), task_id);
    const auto existing = subscription.contexts.find(key);
    if (existing != subscription.contexts.end()) {
        return existing->second;
    }
    auto id = "context-" + std::to_string(++subscription.next_context);
    subscription.contexts.emplace(key, id);
    return id;
}

bool attach_context(MaaTasker* tasker)
{
    if (!tasker) {
        return false;
    }
    auto& state = subscriptions();
    std::lock_guard<std::mutex> lock(state.mutex);
    auto [it, inserted] = state.sinks.try_emplace(tasker);
    if (!inserted) {
        return it->second.sink != MaaInvalidId;
    }
    const auto id = MaaTaskerAddContextSink(tasker, context_event, tasker);
    if (id == MaaInvalidId) {
        state.sinks.erase(it);
        return false;
    }
    it->second.sink = id;
    return true;
}

void detach_context(MaaTasker* tasker) noexcept
{
    try {
        auto& state = subscriptions();
        MaaSinkId id = MaaInvalidId;
        {
            std::lock_guard<std::mutex> lock(state.mutex);
            const auto it = state.sinks.find(tasker);
            if (it == state.sinks.end()) {
                return;
            }
            id = it->second.sink;
            state.sinks.erase(it);
        }
        MaaTaskerRemoveContextSink(tasker, id);
    } catch (...) {
        maa_trace::report_error("context_detach_failed");
    }
}

} // namespace

extern "C" MAA_TRACE_API const char* GetPluginVersion()
{
    return MAA_TRACE_VERSION;
}

extern "C" MAA_TRACE_API uint32_t GetApiVersion()
{
    return 1;
}

extern "C" MAA_TRACE_API void OnTaskerEvent(void* handle, const char* message, const char* details_json, void*)
{
    auto* tasker = static_cast<MaaTasker*>(handle);
    const auto owner = reinterpret_cast<uintptr_t>(handle);
    const std::string_view event = message ? message : "";
    const bool finished = event == "Tasker.Task.Succeeded" || event == "Tasker.Task.Failed";
    try {
        Json extra = Json::object();
        if (event == "Tasker.Task.Starting") {
            const bool attached = attach_context(tasker);
            const auto* version = MaaVersion();
            extra["framework_version"] = version ? version : "unknown";
            extra["context_capture"] = {{"method", "explicit_tasker_sink"}, {"attached", attached}};
            if (!attached) {
                maa_trace::report_error("context_attach_failed");
            }
        }
        maa_trace::record("tasker", owner, message, details_json, std::move(extra));
    } catch (...) {
        maa_trace::report_error("tasker_enrichment_failed");
        maa_trace::record("tasker", owner, message, details_json);
    }
    if (finished) {
        detach_context(tasker);
        maa_trace::flush();
    }
}

extern "C" MAA_TRACE_API void OnResourceEvent(void* handle, const char* message, const char* details_json, void*)
{
    try {
        maa_trace::record("resource", reinterpret_cast<uintptr_t>(handle), message, details_json);
    } catch (...) {
        maa_trace::report_error("resource_capture_failed");
    }
}

extern "C" MAA_TRACE_API void OnControllerEvent(void* handle, const char* message, const char* details_json, void*)
{
    try {
        maa_trace::record("controller", reinterpret_cast<uintptr_t>(handle), message, details_json);
    } catch (...) {
        maa_trace::report_error("controller_capture_failed");
    }
}

// Deliberately do NOT export OnContextEvent. API-v1 loaders in affected MaaFW
// versions assign that symbol to the controller slot. Explicit per-task context
// registration works with both affected and corrected loaders, without duplicates.
