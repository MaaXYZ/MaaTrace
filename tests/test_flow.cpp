#include "flow.h"
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

using Json = nlohmann::json;

namespace {
void check(bool condition, const char* message)
{
    if (!condition) throw std::runtime_error(message);
}

struct Trace {
    maa_trace::FlowTracker tracker;
    uint64_t seq = 0;
    std::string run = "run-1";
    std::string task = "1";
    std::string context = "context-1";

    Json emit(const std::string& type, Json native, const std::string& span = {}, Json enrichment = Json::object())
    {
        const bool task_event = type.substr(0, 5) == "task.";
        if (!task_event && !context.empty()) enrichment["context_id"] = context;
        std::string native_status;
        if (type.size() > 9 && type.substr(type.size() - 9) == ".finished") {
            native_status = native.value("_status", "Succeeded");
            native.erase("_status");
        }
        Json data = {{"native_details", native}, {"enrichment", enrichment}};
        if (!native_status.empty()) {
            data["native_status"] = native_status;
            data["outcome"] = type == "recognition.finished" ? (native_status == "Succeeded" ? "matched" : "unknown")
                : (native_status == "Succeeded" ? "succeeded" : "failed");
        }
        Json event = {{"seq", ++seq}, {"type", type}, {"source", task_event ? "tasker" : "context"},
            {"run_id", run.empty() ? Json(nullptr) : Json(run)}, {"task_id", task},
            {"span_id", span.empty() ? Json(nullptr) : Json(span)}, {"parent_span_id", nullptr}, {"data", data}};
        tracker.enrich(event);
        return event;
    }
    std::string span(const std::string& kind, int id) { return run + ':' + kind + ':' + std::to_string(id); }
    Json begin_node(int number, const std::string& source)
    {
        return emit("node.started", {{"name", source}, {"node_id", number}}, span("node", number));
    }
    Json end_node(int number, const std::string& source, const std::string& executed, int reco, int action)
    {
        return emit("node.finished", {{"name", source}, {"node_id", number},
            {"node_details", {{"name", executed}, {"reco_id", reco}, {"action_id", action}}}}, span("node", number));
    }
    Json start_decision(const std::string& source, const Json& list)
    {
        return emit("decision.started", {{"name", source}, {"list", list}});
    }
    Json end_decision(const std::string& source, const Json& list, bool success = true)
    {
        return emit("decision.finished", {{"name", source}, {"list", list}, {"_status", success ? "Succeeded" : "Failed"}});
    }
    Json reco(int number, const std::string& name, bool matched, const std::string& anchor = {})
    {
        Json native = {{"name", name}, {"reco_id", number}};
        if (!anchor.empty()) native["anchor"] = anchor;
        emit("recognition.started", native, span("recognition", number));
        native["_status"] = matched ? "Succeeded" : "Failed";
        return emit("recognition.finished", native, span("recognition", number));
    }
    void start() { emit("task.started", {{"task_id", 1}}, span("task", 1)); }
};

const Json& flow(const Json& event) { return event.at("data").at("flow"); }

void actual_route_and_candidates()
{
    Trace t;
    t.start();
    const auto first = t.begin_node(101, "A");
    check(first["parent_span_id"] == t.span("task", 1), "top-level node belongs to observed task");
    const Json entry = Json::array({{{"name", "A"}}});
    const auto d1 = t.start_decision("A", entry);
    const auto r1 = t.reco(501, "A", true);
    check(r1["parent_span_id"] == d1["span_id"], "recognition must belong to decision");
    t.end_decision("A", entry);
    t.emit("action.started", {{"name", "A"}}, t.span("action", 601));
    t.emit("action.finished", {{"name", "A"}}, t.span("action", 601));
    t.end_node(101, "A", "A", 501, 601);

    t.begin_node(102, "A");
    const Json next = Json::array({{{"name", "Miss"}}, {{"name", "B"}}, {{"name", "Untested"}}});
    const auto decision = t.start_decision("A", next);
    t.reco(502, "Miss", false);
    const auto hit = t.reco(503, "B", true);
    const auto completed = t.end_decision("A", next);
    check(completed["span_id"] == decision["span_id"], "ID-less decision start/end must share observer ID");
    check(flow(completed)["attempts"].size() == 2, "unobserved candidate must not become fake attempt");
    check(flow(completed)["candidates"] == next, "preserve candidate order and attributes");
    check(flow(completed)["attempts"][0]["outcome"] == "unknown", "do not turn native failure into proven miss");
    check(flow(completed)["selected_candidate_index"] == 1, "select uniquely observed B");
    check(flow(completed)["selected_recognition_span_id"] == hit["span_id"], "selection links the actual hit");
    const auto action = t.emit("action.started", {{"name", "B"}}, t.span("action", 602));
    check(action["parent_span_id"] == t.span("node", 102), "action is under current node, not closed decision");
    t.emit("action.finished", {{"name", "B"}}, t.span("action", 602));
    const auto end = t.end_node(102, "A", "B", 503, 602);
    check(flow(end)["executed_node_name"] == "B", "terminal details determine executed name");
    check(flow(end)["source_node_name"] == "A", "starting source name remains distinct");
    check(flow(end)["transition"]["from_span_id"] == t.span("node", 101), "route connects execution instances");
    check(flow(end)["transition"]["to_span_id"] == t.span("node", 102), "route target matches selected execution");
    check(flow(end)["transition"]["provenance"] == "inferred", "derived transition must declare inference");
}

void rounds_and_duplicate_candidates()
{
    Trace t;
    t.start();
    t.begin_node(101, "Poll");
    const Json list = Json::array({{{"name", "B"}}, {{"name", "B"}}});
    const auto first = t.start_decision("Poll", list);
    t.reco(501, "B", false);
    t.reco(502, "B", false);
    const auto miss = t.end_decision("Poll", list, false);
    check(flow(miss)["selected_recognition_span_id"].is_null(), "failed round has no selected recognition");
    const auto second = t.start_decision("Poll", list);
    t.reco(503, "B", true);
    const auto hit = t.end_decision("Poll", list);
    check(flow(first)["round"] == 1 && flow(second)["round"] == 2, "recognition retries are separate rounds");
    check(first["span_id"] != second["span_id"], "each polling round owns unique identity");
    check(flow(hit)["selected_recognition_span_id"] == t.span("recognition", 503), "hit ID can be known with ambiguous list position");
    check(flow(hit)["selected_candidate_index"].is_null(), "duplicate names must not fabricate candidate position");
}

void anchors_and_jumpback()
{
    Trace t;
    t.start();
    t.begin_node(101, "Root");
    t.end_node(101, "Root", "Handler", 501, 601);
    t.begin_node(102, "Root");
    const Json list = Json::array({{{"name", "LastHandler"}, {"anchor", true}, {"jump_back", true}}});
    t.start_decision("Root", list);
    t.reco(502, "Handler", true, "LastHandler");
    const auto decision = t.end_decision("Root", list);
    check(flow(decision)["selected_candidate_index"] == 0, "anchor identifies reference rather than resolved node name");
    const auto node = t.end_node(102, "Root", "Handler", 502, 602);
    check(flow(node)["transition"]["from_span_id"].is_null(), "jumpback source cannot be mistaken for last completed handler");
}

void nested_scopes_and_waits()
{
    Trace t;
    t.start();
    t.begin_node(101, "Outer");
    const auto action = t.emit("action.started", {{"name", "Outer"}}, t.span("action", 601));
    auto waiting = t.emit("wait.started", {{"name", "Outer"}, {"phase", "repeat"}}, t.span("wait", 701));
    check(waiting["parent_span_id"] == action["span_id"], "wait during action belongs to that action");
    t.emit("wait.finished", {{"name", "Outer"}}, t.span("wait", 701));
    t.task = "2";
    // Even an observer context ID reused in a different native task is isolated.
    const auto child = t.begin_node(102, "Child");
    check(child["parent_span_id"].is_null(), "child call relationship is unknown without native parent evidence");
    const Json list = Json::array({{{"name", "Child"}}});
    const auto d = t.start_decision("Child", list);
    const auto r = t.reco(502, "Child", true);
    check(r["parent_span_id"] == d["span_id"], "child recognition has its own decision");
    t.end_decision("Child", list);
    t.end_node(102, "Child", "Child", 502, 602);
    t.task = "1";
    const auto end = t.emit("action.finished", {{"name", "Outer"}}, t.span("action", 601));
    check(end["parent_span_id"] == t.span("node", 101), "nested events must not overwrite outer active operation");
}

void gaps_and_missing_information()
{
    Trace t;
    t.start();
    t.begin_node(101, "A");
    const Json list = Json::array({{{"name", "B"}}});
    t.start_decision("A", list);
    t.emit("recognition.started", {{"name", "B"}}, t.span("recognition", 501));
    Json gap = {{"type", "trace.gap"}};
    t.tracker.enrich(gap);
    auto end = t.emit("recognition.finished", {{"name", "B"}}, t.span("recognition", 501));
    check(end["parent_span_id"].is_null(), "gap must invalidate association state");
    check(!end["data"].contains("flow"), "unpaired terminal cannot gain stale provenance");
    const auto decision_end = t.end_decision("A", list);
    check(decision_end["span_id"].is_null(), "missing decision start remains unknown");
    t.context.clear();
    const auto missing = t.begin_node(102, "A");
    check(!missing["data"].contains("flow"), "missing context is not inferred from other active nodes");
    t.context = "context-1";
    t.run.clear();
    const auto missing_run = t.begin_node(103, "A");
    check(!missing_run["data"].contains("flow"), "missing run stays unassociated");
}

void list_origin_and_overlap()
{
    Trace t;
    t.start();
    t.begin_node(101, "A");
    const Json list = Json::array({{{"name", "Recover"}, {"anchor", false}, {"jump_back", false}}});
    const Json extra = {{"effective_node", {{"value", {{"next", Json::array({"B"})}, {"on_error", Json::array({"Recover"})}}}}}};
    auto start = t.emit("decision.started", {{"name", "A"}, {"list", list}}, {}, extra);
    check(flow(start)["list_type"] == "on_error", "actual ordered list may be compared to effective error configuration");
    check(flow(start)["list_type_source"] == "effective_node_snapshot_comparison", "list classification must declare inference source");
    t.reco(501, "Recover", true);
    const auto end = t.end_decision("A", list);
    check(flow(end)["list_type"] == "on_error", "list classification survives to terminal summary");
    t.start_decision("A", list);
    const auto overlap = t.start_decision("A", list);
    check(flow(overlap)["association_reset"] == "overlapping_decision_lifecycle", "overlapping ID-less starts must invalidate pairing");
    auto ambiguous_end = t.end_decision("A", list);
    check(ambiguous_end["span_id"].is_null(), "ambiguous finish must not be assigned a decision");
    auto third_start = t.start_decision("A", list);
    check(third_start["span_id"].is_null(), "pairing stays disabled until enclosing node ends");
}

void loop_identity_and_task_cleanup()
{
    Trace t;
    t.start();
    t.begin_node(101, "A");
    t.end_node(101, "A", "A", 501, 601);
    t.begin_node(102, "A");
    auto again = t.end_node(102, "A", "A", 502, 602);
    check(flow(again)["transition"]["from_span_id"] != flow(again)["transition"]["to_span_id"], "loop repeats must be distinct executions");
    t.begin_node(103, "A"); // Deliberately never completes: user stop.
    t.emit("task.finished", {{"task_id", 1}}, t.span("task", 1));
    t.run = "run-2";
    t.start();
    t.begin_node(104, "A");
    auto fresh = t.end_node(104, "A", "A", 503, 603);
    check(flow(fresh)["transition"]["from_span_id"].is_null(), "new task cannot inherit stale loop execution");
}
} // namespace

int main()
{
    try {
        actual_route_and_candidates();
        rounds_and_duplicate_candidates();
        anchors_and_jumpback();
        nested_scopes_and_waits();
        gaps_and_missing_information();
        loop_identity_and_task_cleanup();
        list_origin_and_overlap();
        std::cout << "PASS flow associations (7 scenarios)\n";
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "FAIL flow associations: " << e.what() << '\n';
        return 1;
    }
}
