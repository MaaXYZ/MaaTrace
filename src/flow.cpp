#include "flow.h"

#include <algorithm>
#include <map>
#include <optional>
#include <string>
#include <tuple>
#include <vector>

namespace maa_trace {
namespace {
using Json = nlohmann::json;

std::string text(const Json& value, const char* key)
{
    if (!value.is_object()) return {};
    const auto it = value.find(key);
    return it != value.end() && it->is_string() ? it->get<std::string>() : std::string();
}

std::string id(const Json& value, const char* key)
{
    if (!value.is_object()) return {};
    const auto it = value.find(key);
    if (it == value.end()) return {};
    std::string result;
    if (it->is_string()) result = it->get<std::string>();
    else if (it->is_number_integer() || it->is_number_unsigned()) result = it->dump();
    return result == "0" || result == "-1" ? std::string() : result;
}

uint64_t sequence(const Json& event)
{
    const auto it = event.find("seq");
    return it != event.end() && it->is_number_unsigned() ? it->get<uint64_t>()
        : it != event.end() && it->is_number_integer() && it->get<int64_t>() > 0 ? it->get<uint64_t>() : 0;
}

const Json& object(const Json& value, const char* key)
{
    static const auto* empty = new Json(Json::object());
    if (!value.is_object()) return *empty;
    const auto it = value.find(key);
    return it != value.end() && it->is_object() ? *it : *empty;
}

std::string ref(const std::string& run, const char* kind, const std::string& native_id)
{
    return native_id.empty() ? std::string() : run + ':' + kind + ':' + native_id;
}

void evidence(Json& flow, uint64_t seq)
{
    if (!seq) return;
    auto& values = flow["evidence_seqs"];
    if (!values.is_array()) values = Json::array();
    if (std::find(values.begin(), values.end(), Json(seq)) == values.end()) values.push_back(seq);
}

struct Operation {
    std::string span;
    std::string parent;
    std::string name;
    uint64_t seq = 0;
    uint64_t parent_seq = 0;
};

struct Attempt {
    Operation operation;
    std::optional<size_t> candidate;
    uint64_t finish_seq = 0;
    std::string outcome;
};

struct Decision {
    Operation operation;
    std::string list_type = "unknown";
    std::string list_type_source = "unavailable";
    Json candidates = Json::array();
    uint64_t round = 0;
    std::vector<Attempt> attempts;
    bool ambiguous = false;
};

struct Selection {
    std::string decision_span;
    std::string recognition_span;
    std::string name;
    std::optional<size_t> candidate;
    uint64_t seq = 0;
};

struct Scope {
    Operation node;
    std::string node_kind;
    Operation previous_node;
    std::optional<Decision> decision;
    std::optional<Selection> selection;
    std::map<std::string, Operation> recognitions;
    std::map<std::string, Operation> actions;
    std::map<std::string, Operation> waits;
    uint64_t round = 0;
    bool decision_pairing_ambiguous = false;
    std::map<std::string, Decision> native_decisions;
    size_t native_entries = 0;
};

void parent(Json& event, Json& flow, const Operation& operation)
{
    if (operation.parent.empty()) return;
    event["parent_span_id"] = operation.parent;
    flow["parent_source"] = "same_context_lifecycle";
    flow["provenance"] = "inferred";
    evidence(flow, operation.parent_seq);
}

std::vector<size_t> candidates_for(const Json& candidates, const Json& native)
{
    std::vector<size_t> result;
    const auto name = text(native, "name");
    const auto anchor = text(native, "anchor");
    for (size_t index = 0; index < candidates.size(); ++index) {
        const auto& candidate = candidates[index];
        if (candidate.is_string()) {
            if (anchor.empty() && candidate.get<std::string>() == name) result.push_back(index);
        } else if (candidate.is_object()) {
            const auto anchor_it = candidate.find("anchor");
            const bool is_anchor = anchor_it != candidate.end() && anchor_it->is_boolean() && anchor_it->get<bool>();
            if ((is_anchor && !anchor.empty() && text(candidate, "name") == anchor)
                || (!is_anchor && anchor.empty() && text(candidate, "name") == name)) result.push_back(index);
        }
    }
    return result;
}

Json canonical_list(const Json& list)
{
    if (!list.is_array()) return nullptr;
    Json result = Json::array();
    for (const auto& entry : list) {
        Json candidate;
        if (entry.is_string()) candidate = {{"name", entry}};
        else if (entry.is_object() && !text(entry, "name").empty()) candidate = entry;
        else return nullptr;
        if (!candidate.contains("anchor")) candidate["anchor"] = false;
        if (!candidate.contains("jump_back")) candidate["jump_back"] = false;
        result.push_back(std::move(candidate));
    }
    return result;
}

void list_origin(Decision& decision, const Json& native, const Json& enrichment)
{
    const auto explicit_type = text(native, "list_type");
    if (explicit_type == "next" || explicit_type == "on_error" || explicit_type == "entry") {
        decision.list_type = explicit_type;
        decision.list_type_source = "native_event";
        return;
    }
    const auto& snapshot = object(object(enrichment, "effective_node"), "value");
    const auto target = canonical_list(decision.candidates);
    if (target.is_null()) return;
    const bool next = snapshot.contains("next") && canonical_list(snapshot["next"]) == target;
    const bool error = snapshot.contains("on_error") && canonical_list(snapshot["on_error"]) == target;
    if (next || error) {
        decision.list_type = next && error ? "ambiguous" : next ? "next" : "on_error";
        decision.list_type_source = "effective_node_snapshot_comparison";
    }
}

Operation current_parent(const Scope& scope, bool recognition)
{
    if (recognition && scope.decision) return scope.decision->operation;
    if (!scope.waits.empty()) {
        // Multiple concurrently open waits cannot establish one parent.
        if (scope.waits.size() == 1) return scope.waits.begin()->second;
        return {};
    }
    return scope.node;
}

const Json& node_detail(const Json& native, const Json& enrichment)
{
    const auto& inline_detail = object(native, "node_details");
    if (!inline_detail.empty()) return inline_detail;
    const auto& snapshot = object(enrichment, "node");
    if (text(snapshot, "availability") == "available") return object(snapshot, "value");
    static const auto* empty = new Json(Json::object());
    return *empty;
}

std::optional<uint64_t> unsigned_value(const Json& value, const char* key)
{
    const auto it = value.find(key);
    if (it == value.end()) return std::nullopt;
    if (it->is_number_unsigned()) return it->get<uint64_t>();
    if (it->is_number_integer() && it->get<int64_t>() >= 0) return it->get<uint64_t>();
    return std::nullopt;
}

std::string operation_ref(const std::string& run, const Json& operation)
{
    const auto kind = text(operation, "kind");
    const auto number = id(operation, "id");
    if (kind == "list") return ref(run, "decision", number);
    if (kind == "wait_freezes") return ref(run, "wait", number);
    if (kind == "task" || kind == "node" || kind == "recognition" || kind == "action") {
        return ref(run, kind.c_str(), number);
    }
    return {};
}

void native_parent(Json& event, Json& flow, const Json& native, const std::string& run, bool subtask)
{
    if (native.contains("task_parent")) {
        const auto caller = operation_ref(run, native["task_parent"]);
        flow["task_parent_span_id"] = caller.empty() ? Json(nullptr) : Json(caller);
    }
    const char* key = subtask && native.contains("task_parent") ? "task_parent" : "parent";
    if (native.contains(key)) {
        const auto span = operation_ref(run, native[key]);
        event["parent_span_id"] = span.empty() ? Json(nullptr) : Json(span);
        flow["parent_source"] = "native_event";
    }
    const auto parent_task = ref(run, "task", id(native, "parent_task_id"));
    if (!parent_task.empty()) flow["parent_task_span_id"] = parent_task;
}

void native_node_result(Json& flow, const Json& native, const Json& enrichment, const std::string& run)
{
    const auto& detail = node_detail(native, enrichment);
    const auto selected = text(detail, "name");
    if (!selected.empty()) flow["executed_node_name"] = selected;
    if (detail.contains("jump_back") && detail["jump_back"].is_boolean()) flow["jump_back"] = detail["jump_back"];
    const auto reco = ref(run, "recognition", id(detail, "reco_id"));
    const auto action = ref(run, "action", id(detail, "action_id"));
    if (!reco.empty()) flow["selected_recognition_span_id"] = reco;
    if (!action.empty()) flow["action_span_id"] = action;
    if (!detail.empty()) flow["result_source"] = "native_node_detail";
}

void native_decision_metadata(Json& flow, const Json& native, const std::string& run)
{
    if (native.contains("list") && native["list"].is_array()) flow["candidates"] = native["list"];
    if (const auto round = unsigned_value(native, "round")) flow["round"] = *round;
    const auto list_type = text(native, "list_type");
    if (!list_type.empty()) {
        flow["list_type"] = list_type;
        flow["list_type_source"] = "native_event";
    }
    const auto node = ref(run, "node", id(native, "node_id"));
    flow["node_span_id"] = node.empty() ? Json(nullptr) : Json(node);
}

void native_attempts(Json& flow, const Decision& decision)
{
    flow["attempts"] = Json::array();
    for (const auto& attempt : decision.attempts) {
        flow["attempts"].push_back({{"recognition_span_id", attempt.operation.span}, {"node_name", attempt.operation.name},
            {"start_seq", attempt.operation.seq}, {"finish_seq", attempt.finish_seq ? Json(attempt.finish_seq) : Json(nullptr)},
            {"candidate_index", attempt.candidate ? Json(*attempt.candidate) : Json(nullptr)},
            {"outcome", attempt.outcome.empty() ? "unknown" : attempt.outcome}});
    }
}

void native_decision(Json& event, Json& flow, Scope& scope, const Json& native, const std::string& run, uint64_t seq, bool starting)
{
    const auto span = ref(run, "decision", id(native, "list_id"));
    if (span.empty()) return;
    event["span_id"] = span;
    native_decision_metadata(flow, native, run);
    if (starting) {
        Decision decision;
        decision.operation = {span, text(event, "parent_span_id"), text(native, "name"), seq, 0};
        if (native.contains("list") && native["list"].is_array()) decision.candidates = native["list"];
        const auto existing = scope.native_decisions.find(span);
        if (existing != scope.native_decisions.end()) {
            scope.native_entries -= 1 + existing->second.attempts.size();
            scope.native_decisions.erase(existing);
        }
        scope.native_decisions.emplace(span, std::move(decision));
        ++scope.native_entries;
        return;
    }
    const auto existing = scope.native_decisions.find(span);
    flow["selected_recognition_span_id"] = nullptr;
    flow["selected_candidate_index"] = nullptr;
    const auto& data = event["data"];
    const auto index = unsigned_value(native, "selected_index");
    const auto reco = ref(run, "recognition", id(native, "reco_id"));
    const bool selected = text(data, "native_status") == "Succeeded" && text(data, "outcome") != "cancelled"
        && index && native.contains("list") && native["list"].is_array() && *index < native["list"].size() && !reco.empty();
    if (selected) {
        flow["selected_recognition_span_id"] = reco;
        flow["selected_candidate_index"] = *index;
        flow["selection_source"] = "native_event";
    } else if (text(data, "native_status") == "Succeeded" && text(data, "outcome") != "cancelled") {
        flow["selection_source"] = "unavailable";
    }
    if (existing == scope.native_decisions.end()) return;
    auto& decision = existing->second;
    evidence(flow, decision.operation.seq);
    if (selected) {
        for (auto& attempt : decision.attempts) {
            if (attempt.operation.span == reco) attempt.candidate = static_cast<size_t>(*index);
        }
    }
    native_attempts(flow, decision);
    scope.native_entries -= 1 + decision.attempts.size();
    scope.native_decisions.erase(existing);
}

void native_recognition(Json& flow, Scope& scope, const Json& native, const std::string& run,
    const std::string& span, uint64_t seq, bool starting, const Json& data)
{
    const auto& enclosing = object(native, "parent");
    if (text(enclosing, "kind") != "list") return;
    const auto decision_span = operation_ref(run, enclosing);
    if (decision_span.empty()) return;
    flow["decision_span_id"] = decision_span;
    const auto existing = scope.native_decisions.find(decision_span);
    if (existing == scope.native_decisions.end() || span.empty()) return;
    auto& decision = existing->second;
    if (starting) {
        decision.attempts.push_back({{span, decision_span, text(native, "name"), seq, decision.operation.seq}, std::nullopt, 0, {}});
        ++scope.native_entries;
        return;
    }
    for (auto& attempt : decision.attempts) {
        if (attempt.operation.span != span) continue;
        attempt.finish_seq = seq;
        attempt.outcome = text(data, "outcome");
        evidence(flow, attempt.operation.seq);
        break;
    }
}

void native_enrich(Json& event, Json& flow, Scope& scope, const Json& native, const Json& enrichment,
    const std::string& run, const std::string& type, uint64_t seq)
{
    // Explicit identities remain usable after a missing start or association reset.
    // Do not replace an explicit null parent with a temporal guess.
    if (scope.native_entries >= 16384) {
        scope.native_decisions.clear();
        scope.native_entries = 0;
        flow["association_reset"] = "state_capacity";
    }
    const bool subtask = type == "subtask.started" || type == "subtask.finished";
    native_parent(event, flow, native, run, subtask);
    if (type == "node.transition") {
        const auto from = ref(run, "node", id(native, "from_node_id"));
        const auto to = ref(run, "node", id(native, "to_node_id"));
        flow["transition"] = {{"from_span_id", from.empty() ? Json(nullptr) : Json(from)},
            {"to_span_id", to.empty() ? Json(nullptr) : Json(to)}, {"from_node_name", text(native, "from")},
            {"to_node_name", text(native, "to")}, {"reason", text(native, "reason")},
            {"provenance", "observed"}, {"evidence_seqs", Json::array({seq})}};
    } else if (type == "decision.started" || type == "decision.finished") {
        native_decision(event, flow, scope, native, run, seq, type == "decision.started");
    } else if (type == "recognition.started" || type == "recognition.finished") {
        native_recognition(flow, scope, native, run, text(event, "span_id"), seq, type == "recognition.started", event["data"]);
    } else if (type == "action.started" || type == "action.finished") {
        const auto reco = ref(run, "recognition", id(native, "reco_id"));
        if (!reco.empty()) flow["selected_recognition_span_id"] = reco;
    } else if (type == "node.finished" || type == "recognition_node.finished" || type == "action_node.finished") {
        flow["source_node_name"] = text(native, "name");
        native_node_result(flow, native, enrichment, run);
    } else if (type == "node.started" || type == "recognition_node.started" || type == "action_node.started") {
        flow["source_node_name"] = text(native, "name");
    } else if (type == "subtask.finished") {
        scope = Scope{};
    }
    const auto reason = text(native, "reason");
    if (!reason.empty()) flow["reason"] = reason;
}
} // namespace

struct FlowTracker::State {
    // Context identifiers alone can repeat across native tasks and runs.
    using Key = std::tuple<std::string, std::string, std::string>;
    struct Root { std::string task_id; std::string span; uint64_t seq; };
    std::map<std::string, Root> roots;
    std::map<Key, Scope> scopes;
};

FlowTracker::FlowTracker() : state_(new State) {}
FlowTracker::~FlowTracker() = default;
void FlowTracker::reset() { state_->roots.clear(); state_->scopes.clear(); }

void FlowTracker::enrich(Json& event)
{
    const auto type = text(event, "type");
    if (type == "trace.gap" || type == "trace.truncated" || type == "trace.invalid_event") {
        reset();
        return;
    }
    const auto run = text(event, "run_id");
    if (run.empty()) return;
    const auto seq = sequence(event);
    const auto task = text(event, "task_id");
    const auto span = text(event, "span_id");
    if (type == "task.started") {
        state_->roots[run] = {task, span, seq};
        return;
    }
    if (type == "task.finished") {
        state_->roots.erase(run);
        for (auto it = state_->scopes.begin(); it != state_->scopes.end();) {
            if (std::get<0>(it->first) == run) it = state_->scopes.erase(it);
            else ++it;
        }
        return;
    }
    if (text(event, "source") != "context" || task.empty()) return;
    auto& data = event["data"];
    if (!data.is_object()) return;
    const auto& native = object(data, "native_details");
    const auto& enrichment = object(data, "enrichment");
    const auto native_context = id(native, "context_id");
    const auto context = native_context.empty() ? text(enrichment, "context_id") : native_context;
    if (context.empty() || context == "unavailable") return;
    const State::Key scope_key{run, task, context};
    const bool state_capacity = state_->scopes.size() >= 16384 && state_->scopes.find(scope_key) == state_->scopes.end();
    if (state_capacity) state_->scopes.clear();
    auto& scope = state_->scopes[scope_key];
    Json flow = {{"provenance", "observed"}, {"context_id", context}, {"evidence_seqs", Json::array({seq})}};
    if (state_capacity) flow["association_reset"] = "state_capacity";
    if (!native_context.empty() || native.contains("parent") || native.contains("task_parent")
        || native.contains("list_id") || type == "node.transition" || type == "subtask.started" || type == "subtask.finished") {
        native_enrich(event, flow, scope, native, enrichment, run, type, seq);
        data["flow"] = std::move(flow);
        return;
    }
    const auto name = text(native, "name");
    const auto root = state_->roots.find(run);
    const bool node_start = type == "node.started" || type == "recognition_node.started" || type == "action_node.started";
    const bool node_finish = type == "node.finished" || type == "recognition_node.finished" || type == "action_node.finished";

    if (node_start) {
        if (!scope.node.span.empty()) {
            // An unclosed previous operation makes ordering ambiguous. Keep the
            // native trace, but do not turn temporal overlap into a flow edge.
            scope = Scope{};
            flow["association_reset"] = "overlapping_node_lifecycle";
        }
        scope.node = {span, {}, name, seq, 0};
        scope.node_kind = type.substr(0, type.find('.'));
        scope.round = 0;
        scope.selection.reset();
        if (root != state_->roots.end() && root->second.task_id == task && !root->second.span.empty()) {
            scope.node.parent = root->second.span;
            scope.node.parent_seq = root->second.seq;
            parent(event, flow, scope.node);
        }
        flow["source_node_name"] = name;
    } else if (type == "decision.started") {
        if (scope.decision_pairing_ambiguous) return;
        if (scope.decision) {
            // Pairing of overlapping ID-less decisions is not reliable.
            scope.decision.reset();
            scope.selection.reset();
            scope.decision_pairing_ambiguous = true;
            flow["association_reset"] = "overlapping_decision_lifecycle";
            data["flow"] = std::move(flow);
            return;
        }
        scope.selection.reset();
        Decision decision;
        decision.operation = {run + ":decision:" + std::to_string(seq), scope.node.span, name, seq, scope.node.seq};
        decision.round = scope.node.span.empty() ? 0 : ++scope.round;
        if (native.contains("list") && native["list"].is_array()) decision.candidates = native["list"];
        list_origin(decision, native, enrichment);
        event["span_id"] = decision.operation.span;
        parent(event, flow, decision.operation);
        flow["list_type"] = decision.list_type;
        flow["list_type_source"] = decision.list_type_source;
        if (decision.list_type_source == "effective_node_snapshot_comparison") flow["provenance"] = "inferred";
        flow["candidates"] = decision.candidates;
        if (decision.round) flow["round"] = decision.round;
        flow["node_span_id"] = scope.node.span.empty() ? Json(nullptr) : Json(scope.node.span);
        scope.decision = std::move(decision);
    } else if (type == "decision.finished") {
        if (!scope.decision) return;
        auto decision = std::move(*scope.decision);
        scope.decision.reset();
        // The native list/name pair must agree; otherwise don't pair an ID-less
        // finish with a different operation's start.
        if (decision.operation.name != name || !native.contains("list") || native["list"] != decision.candidates) return;
        event["span_id"] = decision.operation.span;
        parent(event, flow, decision.operation);
        evidence(flow, decision.operation.seq);
        if (decision.round) flow["round"] = decision.round;
        flow["list_type"] = decision.list_type;
        flow["list_type_source"] = decision.list_type_source;
        if (decision.list_type_source == "effective_node_snapshot_comparison") flow["provenance"] = "inferred";
        flow["candidates"] = decision.candidates;
        flow["attempts"] = Json::array();
        std::vector<const Attempt*> matches;
        for (const auto& attempt : decision.attempts) {
            Json entry = {{"recognition_span_id", attempt.operation.span}, {"node_name", attempt.operation.name},
                {"start_seq", attempt.operation.seq}, {"finish_seq", attempt.finish_seq ? Json(attempt.finish_seq) : Json(nullptr)},
                {"candidate_index", attempt.candidate ? Json(*attempt.candidate) : Json(nullptr)},
                {"outcome", attempt.outcome.empty() ? "unknown" : attempt.outcome}};
            flow["attempts"].push_back(std::move(entry));
            if (attempt.outcome == "matched") matches.push_back(&attempt);
        }
        flow["selected_recognition_span_id"] = nullptr;
        flow["selected_candidate_index"] = nullptr;
        if (text(data, "native_status") == "Succeeded" && text(data, "outcome") != "cancelled" && !decision.ambiguous && matches.size() == 1) {
            const auto& hit = *matches.front();
            flow["selected_recognition_span_id"] = hit.operation.span;
            if (hit.candidate) flow["selected_candidate_index"] = *hit.candidate;
            flow["provenance"] = "inferred";
            flow["selection_source"] = "successful_round_with_single_observed_match";
            evidence(flow, hit.finish_seq);
            scope.selection = Selection{decision.operation.span, hit.operation.span, hit.operation.name, hit.candidate, seq};
        } else {
            scope.selection.reset();
            if (text(data, "native_status") == "Succeeded" && text(data, "outcome") != "cancelled") flow["selection_source"] = "unavailable";
        }
    } else if (type == "recognition.started") {
        if (span.empty()) return;
        auto enclosing = current_parent(scope, true);
        Operation operation{span, enclosing.span, name, seq, enclosing.seq};
        parent(event, flow, operation);
        if (scope.decision && !scope.recognitions.empty()) scope.decision->ambiguous = true;
        scope.recognitions[span] = operation;
        if (scope.decision) {
            auto& decision = *scope.decision;
            const auto candidates = candidates_for(decision.candidates, native);
            Attempt attempt{operation, std::nullopt, 0, {}};
            if (candidates.size() == 1) attempt.candidate = candidates.front();
            flow["decision_span_id"] = decision.operation.span;
            flow["candidate_index"] = attempt.candidate ? Json(*attempt.candidate) : Json(nullptr);
            flow["candidate_resolution"] = candidates.size() == 1 ? "unique_native_identity"
                : candidates.empty() ? "unavailable" : "ambiguous";
            if (decision.round) flow["round"] = decision.round;
            decision.attempts.push_back(std::move(attempt));
        }
    } else if (type == "recognition.finished") {
        const auto existing = scope.recognitions.find(span);
        if (existing == scope.recognitions.end()) return;
        parent(event, flow, existing->second);
        evidence(flow, existing->second.seq);
        scope.recognitions.erase(existing);
        if (scope.decision) {
            auto& decision = *scope.decision;
            auto attempt = std::find_if(decision.attempts.begin(), decision.attempts.end(),
                [&](const Attempt& value) { return value.operation.span == span; });
            if (attempt != decision.attempts.end()) {
                attempt->finish_seq = seq;
                attempt->outcome = text(data, "outcome");
                flow["decision_span_id"] = decision.operation.span;
                flow["candidate_index"] = attempt->candidate ? Json(*attempt->candidate) : Json(nullptr);
                if (decision.round) flow["round"] = decision.round;
            }
        }
    } else if (type == "action.started" || type == "wait.started") {
        if (span.empty()) return;
        Operation enclosing = scope.node;
        if (type == "wait.started" && scope.actions.size() == 1) enclosing = scope.actions.begin()->second;
        Operation operation{span, enclosing.span, name, seq, enclosing.seq};
        parent(event, flow, operation);
        if (type == "action.started") {
            scope.actions[span] = operation;
            if (scope.selection && scope.selection->name == name) {
                flow["selected_recognition_span_id"] = scope.selection->recognition_span;
                flow["decision_span_id"] = scope.selection->decision_span;
                flow["provenance"] = "inferred";
                evidence(flow, scope.selection->seq);
            }
        } else scope.waits[span] = operation;
    } else if (type == "action.finished" || type == "wait.finished") {
        auto& operations = type == "action.finished" ? scope.actions : scope.waits;
        auto existing = operations.find(span);
        if (existing == operations.end()) return;
        parent(event, flow, existing->second);
        evidence(flow, existing->second.seq);
        operations.erase(existing);
    } else if (node_finish) {
        const bool paired = !span.empty() && scope.node.span == span;
        if (paired) {
            parent(event, flow, scope.node);
            evidence(flow, scope.node.seq);
            flow["source_node_name"] = scope.node.name;
        }
        const auto& detail = node_detail(native, enrichment);
        const auto selected_name = text(detail, "name");
        const auto reco = id(detail, "reco_id");
        const auto action = id(detail, "action_id");
        if (!selected_name.empty()) flow["executed_node_name"] = selected_name;
        if (detail.contains("jump_back") && detail["jump_back"].is_boolean()) flow["jump_back"] = detail["jump_back"];
        if (!reco.empty()) flow["selected_recognition_span_id"] = ref(run, "recognition", reco);
        if (!action.empty()) flow["action_span_id"] = ref(run, "action", action);
        if (!detail.empty()) flow["result_source"] = "native_node_detail";
        if (paired && scope.selection && scope.selection->recognition_span == ref(run, "recognition", reco)) {
            flow["decision_span_id"] = scope.selection->decision_span;
            flow["selected_candidate_index"] = scope.selection->candidate ? Json(*scope.selection->candidate) : Json(nullptr);
            evidence(flow, scope.selection->seq);
        }
        // An execution edge is only inferable for consecutive completed pipeline
        // operations where native predecessor and previous executed name agree.
        // In particular, jump-back may restore a different predecessor: keep it
        // unresolved instead of attaching to the immediately preceding execution.
        if (paired && scope.node_kind == "node" && text(data, "outcome") != "cancelled" && !selected_name.empty() && !reco.empty()) {
            Json edge = {{"to_span_id", span}, {"to_node_name", selected_name},
                {"from_node_name", scope.node.name}, {"from_span_id", nullptr},
                {"provenance", "inferred"}, {"reason", "selected_node_observed"},
                {"evidence_seqs", Json::array({scope.node.seq, seq})}};
            if (!scope.previous_node.span.empty() && scope.previous_node.name == scope.node.name) {
                edge["from_span_id"] = scope.previous_node.span;
                edge["evidence_seqs"].push_back(scope.previous_node.seq);
            } else edge["source_instance_resolution"] = "unavailable";
            flow["transition"] = std::move(edge);
            scope.previous_node = {span, {}, selected_name, seq, 0};
        }
        if (paired) {
            auto previous = scope.previous_node;
            scope = Scope{};
            scope.previous_node = std::move(previous);
        } else if (!scope.node.span.empty()) {
            scope = Scope{};
            flow["association_reset"] = "unmatched_node_terminal";
        }
    } else return;

    data["flow"] = std::move(flow);
}

} // namespace maa_trace
