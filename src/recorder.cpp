#include "recorder.h"
#include "MaaTrace/MaaTrace.h"
#include "flow.h"
#include "sha256.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <deque>
#include <filesystem>
#include <fstream>
#include <future>
#include <iomanip>
#include <map>
#include <mutex>
#include <random>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#if defined(_WIN32)
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace maa_trace {
namespace {
using json = nlohmann::json;
using Clock = std::chrono::steady_clock;
std::atomic<uint64_t> errors{0};
std::atomic<uint64_t> accepted{0}, written{0}, dropped{0};
std::atomic<bool> stopped{false};
std::atomic<bool> initialized{false};
std::atomic<bool> truncated{false};
std::atomic<bool> capture_enabled{true};
std::mutex lifecycle;

std::string utc_now() {
    auto now = std::chrono::system_clock::now();
    auto time = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
#ifdef _WIN32
    gmtime_s(&tm, &time);
#else
    gmtime_r(&time, &tm);
#endif
    std::ostringstream out;
    out << std::put_time(&tm, "%Y-%m-%dT%H:%M:%S") << '.' << std::setfill('0')
        << std::setw(3) << (std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count() % 1000) << 'Z';
    return out.str();
}
std::string env(const char* key, const char* fallback) {
#ifdef _WIN32
    const std::string name(key);
    const std::wstring wide_name(name.begin(), name.end());
    const wchar_t* value = _wgetenv(wide_name.c_str());
    if (!value || !*value) return fallback;
    const int size = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
    if (size <= 0) throw std::runtime_error("Cannot read environment as UTF-8");
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), size, nullptr, nullptr);
    result.pop_back();
    return result;
#else
    const char* value = std::getenv(key);
    return value && *value ? value : fallback;
#endif
}
uint64_t env_limit(const char* key, uint64_t fallback) {
    auto value = env(key, "");
    if (value.empty()) return fallback;
    size_t end = 0;
    auto n = std::stoull(value, &end);
    if (end != value.size() || value.front() == '-' || n == 0) throw std::runtime_error(std::string("Invalid limit: ") + key);
    return n;
}
std::string new_id() {
    std::random_device rd;
    std::ostringstream s;
    s << std::hex << std::chrono::high_resolution_clock::now().time_since_epoch().count();
    for (int i = 0; i < 4; ++i) s << std::setw(8) << std::setfill('0') << rd();
    return s.str();
}
std::string id_of(const json& object, const char* key) {
    if (!object.is_object() || !object.contains(key)) return {};
    const auto& v = object[key];
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer() || v.is_number_unsigned()) return v.dump();
    return {};
}
void pin_module() {
    // The async writer must not execute unmapped plugin code during host teardown.
#ifdef _WIN32
    HMODULE module{};
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
            reinterpret_cast<LPCWSTR>(&MaaTraceFlush), &module)) throw std::runtime_error("Cannot pin plugin module");
#else
    Dl_info info{};
    if (!dladdr(reinterpret_cast<void*>(&MaaTraceFlush), &info) || !info.dli_fname
        || !dlopen(info.dli_fname, RTLD_NOW | RTLD_LOCAL)) throw std::runtime_error("Cannot retain plugin module");
#endif
}
struct Gap { uint64_t first = 0, last = 0, count = 0; };
struct Item {
    uintptr_t owner = 0;
    uint64_t seq = 0, elapsed = 0;
    std::string timestamp, source, message, details;
    json enrichment = json::object();
    std::vector<Attachment> attachments;
    size_t bytes = 0;
    Gap gap;
    std::shared_ptr<std::promise<bool>> barrier;
};

class Recorder {
public:
    Recorder() {
        enabled_ = env("MAA_TRACE_ENABLED", "1") != "0";
        capture_enabled = enabled_;
        if (!enabled_) return;
        capacity_ = env_limit("MAA_TRACE_QUEUE_CAPACITY", 8192);
        queue_limit_ = env_limit("MAA_TRACE_QUEUE_BYTES", 16 * 1024 * 1024);
        max_file_ = env_limit("MAA_TRACE_MAX_FILE_BYTES", 128 * 1024 * 1024);
        artifact_limit_ = env_limit("MAA_TRACE_MAX_ARTIFACT_BYTES", 256 * 1024 * 1024);
        config_limit_ = env_limit("MAA_TRACE_MAX_CONFIG_BYTES", 16 * 1024 * 1024);
        session_ = "session-" + new_id();
        auto root = std::filesystem::u8path(env("MAA_TRACE_DIR", "maa-traces"));
        std::filesystem::create_directories(root);
        directory_ = root / session_;
        if (!std::filesystem::create_directory(directory_)) throw std::runtime_error("Session directory already exists");
        output_.open(directory_ / "events.jsonl", std::ios::out | std::ios::binary);
        if (!output_) throw std::runtime_error("Cannot open trace output");
        manifest_ = {
            {"schema_version", "1.0"}, {"plugin_version", MAA_TRACE_VERSION},
            {"plugin_api_version", 1}, {"session_id", session_}, {"started_at", utc_now()},
            {"state", "recording"}, {"events", "events.jsonl"},
            {"capabilities", {{"native_events", true}, {"detail_snapshots", true},
                {"screenshots", true}, {"config_snapshots", true}, {"observed_flow_links", true}, {"explicit_transitions", false},
                {"complete_call_tree", false}, {"stop_reason", false},
                {"native_parent_links", false}, {"runtime_config_snapshots", false}}},
            {"limits", {{"queue_capacity", capacity_}, {"queue_bytes", queue_limit_}, {"max_file_bytes", max_file_},
                {"artifact_bytes", artifact_limit_}, {"config_bytes", config_limit_}}},
            {"capture", {{"images", env("MAA_TRACE_IMAGES", "none")}}},
            {"privacy", "Native events and effective parameters are recorded without redaction. Images are opt-in."}
        };
        save_manifest();
        worker_ = std::thread([this] { work(); });
    }
    void submit(Item item) {
        if (!enabled_) return;
        std::lock_guard<std::mutex> lock(mutex_);
        if (closing_) return;
        item.seq = ++seq_;
        item.elapsed = elapsed();
        item.timestamp = utc_now();
        if (queue_.size() >= capacity_ || item.bytes > queue_limit_ || queue_bytes_ > queue_limit_ - item.bytes) {
            if (!gap_.count) gap_.first = item.seq;
            gap_.last = item.seq;
            ++gap_.count;
            ++dropped;
        } else {
            item.gap = gap_;
            gap_ = {};
            queue_bytes_ += item.bytes;
            queue_.push_back(std::move(item));
            ++accepted;
        }
        cv_.notify_one();
    }
    bool flush() {
        if (!enabled_) return errors == 0;
        auto promise = std::make_shared<std::promise<bool>>();
        auto future = promise->get_future();
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (closing_) return errors == 0 && !truncated;
            Item item;
            item.barrier = promise;
            item.gap = gap_;
            gap_ = {};
            queue_.push_back(std::move(item));
        }
        cv_.notify_one();
        return future.get();
    }
    void shutdown() {
        if (!enabled_) return;
        { std::lock_guard<std::mutex> lock(mutex_); closing_ = true; }
        cv_.notify_one();
        if (worker_.joinable()) worker_.join();
    }
private:
    uint64_t elapsed() const { return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start_).count()); }
    void save_manifest() {
        // Events remain readable even if interrupted while updating this optional summary.
        auto temp = directory_ / "manifest.json.tmp";
        std::ofstream file(temp, std::ios::binary | std::ios::trunc);
        file << manifest_.dump(2) << '\n';
        file.close();
        if (!file) throw std::runtime_error("Cannot write manifest");
#ifdef _WIN32
        if (!MoveFileExW(temp.c_str(), (directory_ / "manifest.json").c_str(), MOVEFILE_REPLACE_EXISTING))
            throw std::runtime_error("Cannot replace manifest");
#else
        std::filesystem::rename(temp, directory_ / "manifest.json");
#endif
    }
    json envelope(uint64_t seq, uint64_t us, const std::string& ts, const std::string& type, const std::string& source) {
        return {{"schema_version", "1.0"}, {"session_id", session_}, {"run_id", nullptr},
            {"seq", seq}, {"timestamp", ts}, {"elapsed_us", us}, {"type", type}, {"source", source},
            {"task_id", nullptr}, {"span_id", nullptr}, {"parent_span_id", nullptr}};
    }
    void write(const json& event) {
        if (truncated || !output_) { ++dropped; return; }
        auto line = event.dump(-1, ' ', false, json::error_handler_t::replace) + '\n';
        if (file_bytes_ + line.size() > max_file_) {
            truncated = true;
            ++dropped;
            auto limit = envelope(event.at("seq").get<uint64_t>(), elapsed(), utc_now(), "trace.truncated", "recorder");
            limit["data"] = {{"reason", "file_size_limit"}, {"max_file_bytes", max_file_}};
            output_ << limit.dump() << '\n';
            output_.flush();
            if (!output_) throw std::runtime_error("Cannot write truncation marker");
            return;
        }
        output_ << line;
        output_.flush(); // Bound tail loss; no fsync promise.
        if (!output_) throw std::runtime_error("Cannot write trace events");
        file_bytes_ += line.size();
        ++written;
    }
    void write_gap(const Gap& gap) {
        if (!gap.count) return;
        // A dropped event may have started or ended a task. Never connect later
        // events to a potentially stale run; the next observed start restores it.
        runs_.clear();
        flow_.reset();
        auto event = envelope(gap.first, elapsed(), utc_now(), "trace.gap", "recorder");
        event["data"] = {{"reason", "queue_limit"}, {"first_seq", gap.first}, {"last_seq", gap.last}, {"count", gap.count}};
        write(event);
    }
    void consume(const Item& item) {
        if (truncated) { ++dropped; return; }
        json native = json::parse(item.details, nullptr, false);
        const bool valid = !native.is_discarded() && native.is_object();
        std::string kind = "framework", status;
        auto dot = item.message.rfind('.');
        auto prefix = dot == std::string::npos ? item.message : item.message.substr(0, dot);
        if (dot != std::string::npos) status = item.message.substr(dot + 1);
        // Keep lookup data alive while the writer drains from the exit hook.
        static const auto* kinds = new std::map<std::string, std::string>{
            {"Tasker.Task", "task"}, {"Context.Task", "subtask"}, {"Node.PipelineNode", "node"},
            {"Node.RecognitionNode", "recognition_node"}, {"Node.ActionNode", "action_node"},
            {"Node.Recognition", "recognition"}, {"Node.Action", "action"},
            {"Node.NextList", "decision"}, {"Node.WaitFreezes", "wait"},
            {"Controller.Action", "controller_action"}, {"Resource.Loading", "resource"}
        };
        auto found = kinds->find(prefix);
        if (found != kinds->end()) kind = found->second;
        const bool node_cancelled = found != kinds->end() && (prefix.rfind("Node.", 0) == 0 || prefix == "Context.Task")
            && status == "Cancelled";
        std::string type = "framework.event";
        if (found != kinds->end()) {
            if (status == "Starting") type = kind + ".started";
            if (status == "Succeeded" || status == "Failed" || node_cancelled) type = kind + ".finished";
        }
        if (item.source == "context" && item.message == "Node.Transition") type = "node.transition";
        if (item.source == "custom") type = "custom.event";
        if (item.source == "host" && item.message == "Host.Stop.Requested") type = "stop.requested";
        if (!valid) type = "trace.invalid_event";
        if (valid && item.source == "tasker" && item.message == "Tasker.Task.Starting") {
            runs_[item.owner] = "run-" + std::to_string(++run_counter_);
        }
        auto event = envelope(item.seq, item.elapsed, item.timestamp, type, item.source);
        if (auto run = runs_.find(item.owner); run != runs_.end() && (item.source == "tasker" || item.source == "context" || item.source == "custom" || item.source == "host")) event["run_id"] = run->second;
        json data = {{"native_message", item.message}, {"enrichment", item.enrichment}};
        if (valid) {
            data["native_details"] = native;
            auto task_id = id_of(native, "task_id");
            if (!task_id.empty()) event["task_id"] = task_id;
            json ids = json::object();
            for (auto it = native.begin(); it != native.end(); ++it) {
                const auto& key = it.key();
                if (key.size() >= 3 && key.substr(key.size() - 3) == "_id") {
                    auto id = id_of(native, key.c_str());
                    if (!id.empty()) ids[key] = id;
                }
            }
            data["native_ids"] = ids;
            const char* id_key = kind == "task" || kind == "subtask" ? "task_id" : kind == "recognition" ? "reco_id"
                : kind == "action" ? "action_id" : kind == "wait" ? "wf_id"
                : kind == "decision" ? "list_id"
                : (kind == "node" || kind == "recognition_node" || kind == "action_node") ? "node_id" : nullptr;
            if (id_key && !event["run_id"].is_null()) {
                auto id = id_of(native, id_key);
                auto span_kind = kind == "subtask" ? "task" : kind;
                if (native.contains("context_id") && (kind == "recognition_node" || kind == "action_node")) span_kind = "node";
                if (!id.empty() && id != "0" && id != "-1") event["span_id"] = event["run_id"].get<std::string>() + ":" + span_kind + ":" + id;
            }
            if (found != kinds->end() && (status == "Succeeded" || status == "Failed" || node_cancelled)) {
                data["native_status"] = status;
                // Legacy task status may report success for a cooperatively stopped task.
                const bool cancelled = node_cancelled || (found != kinds->end()
                    && native.contains("reason") && native["reason"] == "cancelled");
                // Failed recognition can mean a miss or an execution error. Do not guess.
                data["outcome"] = cancelled ? "cancelled" : kind == "recognition"
                    ? (status == "Succeeded" ? "matched" : "unknown")
                    : (status == "Succeeded" ? "succeeded" : "failed");
            }
            if (item.source == "context" && (native.contains("parent") || native.contains("task_parent"))) {
                manifest_["capabilities"]["native_parent_links"] = true;
            }
            if (type == "node.transition") manifest_["capabilities"]["explicit_transitions"] = true;
            if (item.source == "context" && (kind == "recognition" || kind == "action")
                && native.contains("pipeline") && native["pipeline"].is_object()) {
                manifest_["capabilities"]["runtime_config_snapshots"] = true;
            }
            if ((item.source == "context" || (item.source == "tasker" && prefix == "Tasker.Task"))
                && found != kinds->end() && native.value("reason", json()) == "cancelled") {
                manifest_["capabilities"]["stop_reason"] = true;
            }
        } else {
            data["raw_details"] = item.details;
            data["reason"] = "details_must_be_json_object";
        }
        event["data"] = std::move(data);
        flow_.enrich(event);
        save_config(event);
        save_attachments(event, item.attachments);
        write(event);
        if (item.source == "tasker" && (item.message == "Tasker.Task.Succeeded" || item.message == "Tasker.Task.Failed")) runs_.erase(item.owner);
    }
    void save_bytes(const std::string& relative, const uint8_t* bytes, size_t size) {
        const auto path = directory_ / relative;
        std::filesystem::create_directories(path.parent_path());
        auto temporary = path;
        temporary += ".tmp";
        std::ofstream file(temporary, std::ios::binary | std::ios::trunc);
        file.write(reinterpret_cast<const char*>(bytes), static_cast<std::streamsize>(size));
        file.close();
        if (!file) throw std::runtime_error("Cannot write trace evidence");
        std::filesystem::rename(temporary, path);
    }
    void save_config(json& event) {
        auto& extra = event["data"]["enrichment"];
        if (!extra.is_object() || !extra.contains("effective_node") || !extra["effective_node"].contains("value")) return;
        auto& node = extra["effective_node"];
        try {
            const auto content = node["value"].dump(2, ' ', false, json::error_handler_t::replace) + '\n';
            const auto hash = sha256(content);
            const auto path = "configs/" + hash + ".json";
            if (!configs_.count(hash)) {
                if (content.size() > config_limit_ - config_bytes_) {
                    event["data"]["config"] = {{"availability", "omitted"}, {"reason", "config_budget"}};
                    return;
                }
                save_bytes(path, reinterpret_cast<const uint8_t*>(content.data()), content.size());
                configs_.insert(hash);
                config_bytes_ += content.size();
            }
            json ref = {{"availability", "available"}, {"path", path}, {"sha256", hash}, {"size", content.size()}};
            event["data"]["config"] = ref;
            node["config_ref"] = ref;
            node.erase("value");
        } catch (const std::exception& e) {
            report_error(e.what());
            event["data"]["config"] = {{"availability", "omitted"}, {"reason", "write_failed"}};
        }
    }
    void save_attachments(json& event, const std::vector<Attachment>& attachments) {
        if (attachments.empty()) return;
        auto refs = json::array();
        size_t index = 0;
        for (const auto& item : attachments) {
            json ref = {{"id", item.id}, {"mime_type", item.mime_type}, {"metadata", item.metadata}};
            if (item.bytes.size() > artifact_limit_ - artifact_bytes_) {
                ref["availability"] = "omitted"; ref["reason"] = "artifact_budget";
            } else {
                try {
                    const auto hash = sha256(item.bytes.data(), item.bytes.size());
                    const auto path = "artifacts/artifact-" + std::to_string(event["seq"].get<uint64_t>()) + "-" + std::to_string(index) + (item.mime_type == "image/png" ? ".png" : ".bin");
                    save_bytes(path, item.bytes.data(), item.bytes.size());
                    artifact_bytes_ += item.bytes.size();
                    ref["availability"] = "available"; ref["path"] = path;
                    ref["size"] = item.bytes.size(); ref["sha256"] = hash;
                } catch (const std::exception& e) {
                    report_error(e.what());
                    ref["availability"] = "omitted"; ref["reason"] = "write_failed";
                }
            }
            refs.push_back(std::move(ref));
            ++index;
        }
        event["data"]["artifacts"] = std::move(refs);
    }
    void work() noexcept {
        while (true) {
            Item item;
            bool done = false;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this] { return closing_ || !queue_.empty() || gap_.count; });
                if (!queue_.empty()) {
                    item = std::move(queue_.front()); queue_.pop_front(); queue_bytes_ -= item.bytes;
                } else {
                    item.gap = gap_; gap_ = {};
                    done = closing_;
                }
            }
            try {
                write_gap(item.gap);
                if (item.seq) consume(item);
                if (item.barrier) {
                    output_.flush();
                    if (!output_) throw std::runtime_error("Cannot flush trace events");
                    item.barrier->set_value(errors == 0 && !truncated);
                }
            } catch (const std::exception& e) {
                report_error(e.what());
                if (item.barrier) item.barrier->set_value(false);
            } catch (...) {
                report_error("Unexpected writer error");
                if (item.barrier) item.barrier->set_value(false);
            }
            if (done) break;
        }
        try {
            output_.flush(); output_.close();
            if (!output_) report_error("Cannot finalize trace events");
            manifest_["state"] = truncated ? "truncated" : errors > 0 ? "error" : "closed";
            manifest_["finished_at"] = utc_now();
            manifest_["open_runs"] = runs_.size();
            manifest_["recording_complete"] = errors == 0 && dropped == 0 && !truncated && runs_.empty();
            manifest_["accepted"] = accepted.load(); manifest_["written"] = written.load(); manifest_["dropped"] = dropped.load();
            manifest_["recording_errors"] = errors.load();
            manifest_["artifact_bytes"] = artifact_bytes_; manifest_["config_bytes"] = config_bytes_;
            save_manifest();
        } catch (const std::exception& e) { report_error(e.what()); }
        catch (...) { report_error("Cannot finalize manifest"); }
    }
    Clock::time_point start_ = Clock::now();
    bool enabled_ = true, closing_ = false;
    uint64_t capacity_ = 8192, queue_limit_ = 0, max_file_ = 0, seq_ = 0, run_counter_ = 0, file_bytes_ = 0;
    uint64_t artifact_limit_ = 0, config_limit_ = 0, artifact_bytes_ = 0, config_bytes_ = 0;
    size_t queue_bytes_ = 0;
    std::string session_;
    std::filesystem::path directory_;
    std::ofstream output_;
    json manifest_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<Item> queue_;
    Gap gap_;
    std::thread worker_;
    std::map<uintptr_t, std::string> runs_;
    FlowTracker flow_;
    std::set<std::string> configs_;
};
Recorder* recorder = nullptr;
bool init_failed = false;
Recorder* get_recorder() {
    // Caller holds lifecycle. Construct lazily, outside DLL load initialization.
    if (!recorder && !init_failed && !stopped) {
        try {
            pin_module();
            recorder = new Recorder();
            initialized = true;
#ifndef _WIN32
            std::atexit([] { MaaTraceShutdown(); });
#endif
        } catch (...) { init_failed = true; throw; }
    }
    return recorder;
}
} // namespace
void report_error(const char* message) noexcept {
    if (++errors <= 5) std::fprintf(stderr, "[MaaTrace] %s\n", message ? message : "Unknown recording error");
}
void record(const char* source, uintptr_t owner, const char* message, const char* details, json enrichment, std::vector<Attachment> attachments) noexcept {
    try {
        if (stopped) return;
        Item item;
        item.owner = owner; item.source = source ? source : "unknown";
        item.message = message ? message : ""; item.details = details ? details : "";
        item.enrichment = std::move(enrichment);
        item.attachments = std::move(attachments);
        item.bytes = item.source.size() + item.message.size() + item.details.size() + item.enrichment.dump().size();
        for (const auto& attachment : item.attachments) item.bytes += attachment.bytes.size() + attachment.metadata.dump().size() + attachment.id.size() + attachment.mime_type.size();
        std::lock_guard<std::mutex> lock(lifecycle);
        auto* rec = get_recorder();
        if (rec) rec->submit(std::move(item));
    } catch (const std::exception& e) { report_error(e.what()); }
    catch (...) { report_error("Cannot capture event"); }
}
bool flush() noexcept {
    try {
        std::lock_guard<std::mutex> lock(lifecycle);
        return recorder ? recorder->flush() : errors == 0;
    } catch (const std::exception& e) { report_error(e.what()); return false; }
    catch (...) { report_error("Cannot flush trace"); return false; }
}
} // namespace maa_trace

extern "C" MAA_TRACE_API int MaaTraceFlush(void) { return maa_trace::flush() ? 1 : 0; }
extern "C" MAA_TRACE_API int MaaTraceShutdown(void) {
    try {
        std::lock_guard<std::mutex> lock(maa_trace::lifecycle);
        maa_trace::stopped = true;
        if (maa_trace::recorder) maa_trace::recorder->shutdown();
        return maa_trace::errors == 0 && !maa_trace::truncated ? 1 : 0;
    } catch (const std::exception& e) { maa_trace::report_error(e.what()); return 0; }
    catch (...) { maa_trace::report_error("Cannot shut down trace"); return 0; }
}
extern "C" MAA_TRACE_API const char* MaaTraceGetStatus(void) {
    try {
        thread_local std::string result;
        nlohmann::json status = {{"state", maa_trace::stopped ? "stopped" : maa_trace::errors > 0 ? "error"
                : maa_trace::truncated ? "truncated" : !maa_trace::capture_enabled ? "disabled" : maa_trace::initialized ? "recording" : "idle"},
            {"accepted", maa_trace::accepted.load()}, {"written", maa_trace::written.load()},
            {"dropped", maa_trace::dropped.load()}, {"recording_errors", maa_trace::errors.load()}, {"truncated", maa_trace::truncated.load()}};
        result = status.dump();
        return result.c_str();
    } catch (...) { return "{\"state\":\"error\"}"; }
}

extern "C" MAA_TRACE_API void MaaTraceEmit(void* tasker, const char* event_name, const char* details_json) {
    try {
        maa_trace::record("custom", reinterpret_cast<uintptr_t>(tasker), event_name, details_json);
    } catch (...) { maa_trace::report_error("Cannot capture custom event"); }
}
extern "C" MAA_TRACE_API void MaaTraceRecordStopRequest(void* tasker, const char* reason) {
    try {
        const auto details = nlohmann::json{{"reason", reason ? reason : "unknown"}}.dump();
        maa_trace::record("host", reinterpret_cast<uintptr_t>(tasker), "Host.Stop.Requested", details.c_str());
    } catch (...) { maa_trace::report_error("Cannot capture stop request"); }
}
