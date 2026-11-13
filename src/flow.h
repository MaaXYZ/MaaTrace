#pragma once

#include <memory>
#include <nlohmann/json.hpp>

namespace maa_trace {

// Worker-thread-only observer. Native events remain unchanged; associations are
// annotated with their provenance and supporting event sequence numbers.
class FlowTracker {
public:
    FlowTracker();
    ~FlowTracker();
    FlowTracker(const FlowTracker&) = delete;
    FlowTracker& operator=(const FlowTracker&) = delete;

    void enrich(nlohmann::json& event);
    void reset();

private:
    struct State;
    std::unique_ptr<State> state_;
};

} // namespace maa_trace
