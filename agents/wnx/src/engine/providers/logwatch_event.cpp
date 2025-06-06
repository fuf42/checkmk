// Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
// This file is part of Checkmk (https://checkmk.com). It is subject to the
// terms and conditions defined in the file COPYING, which is part of this
// source code package.

#include "stdafx.h"

#include "providers/logwatch_event.h"

#include <fmt/format.h>

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <ranges>
#include <regex>
#include <string>

#include "common/wtools.h"
#include "eventlog/eventlogbase.h"
#include "eventlog/eventlogvista.h"
#include "providers/logwatch_event_details.h"
#include "wnx/cfg.h"
#include "wnx/cfg_engine.h"
#include "wnx/logger.h"
namespace fs = std::filesystem;
namespace rs = std::ranges;

namespace cma::provider {

// kOff if LevelValue is not valid safe for nullptr and mixed case
cfg::EventLevels LabelToEventLevel(std::string_view required_level) {
    using cfg::EventLevels;
    if (required_level.data() == nullptr) {
        XLOG::l(XLOG_FUNC + " parameter set to nullptr ");
        return EventLevels::kOff;
    }

    std::string val(required_level);
    tools::StringLower(val);

    constexpr std::array levels = {EventLevels::kIgnore, EventLevels::kOff,
                                   EventLevels::kAll, EventLevels::kWarn,
                                   EventLevels::kCrit};

    for (const auto level : levels) {
        if (val == ConvertLogWatchLevelToString(level)) {
            return level;
        }
    }

    XLOG::d("Key '{}' is not allowed, switching level to 'off'", val);
    return EventLevels::kOff;
}

void LogWatchEntry::init(std::string_view name, std::string_view level_value,
                         cfg::EventContext context) {
    name_ = name;
    context_ = context;
    level_ = LabelToEventLevel(level_value);

    loaded_ = true;
}

namespace {
std::pair<std::string, std::string> ParseLine(std::string_view line) {
    auto name_body = tools::SplitString(std::string(line), ":");
    if (name_body.empty()) {
        XLOG::l("Bad entry '{}' in logwatch section ", line);
        return {};
    }

    auto name = name_body[0];
    tools::AllTrim(name);
    if (name.empty()) {
        return {};
    }

    if (name.back() == '\"' || name.back() == '\'') {
        name.pop_back();
    }
    if (name.empty()) {
        return {};
    }

    if (name.front() == '\"' || name.front() == '\'') {
        name.erase(name.begin());
    }
    tools::AllTrim(name);  // this is intended
    if (name.empty()) {
        XLOG::d("Skipping empty entry '{}'", line);
        return {};
    }

    auto body = name_body.size() > 1 ? name_body[1] : "";
    tools::AllTrim(body);
    return {name, body};
}
}  // namespace

bool LogWatchEntry::loadFromMapNode(const YAML::Node &node) {
    if (node.IsNull() || !node.IsDefined() || !node.IsMap()) {
        return false;
    }
    try {
        YAML::Emitter emit;
        emit << node;
        return loadFrom(emit.c_str());
    } catch (const std::exception &e) {
        XLOG::l(
            "Failed to load logwatch entry from Node exception: '{}' in file '{}'",
            e.what(), wtools::ToUtf8(cfg::GetPathOfLoadedConfig()));
        return false;
    }
}

// For one-line encoding, example:
// - 'Application' : crit context
bool LogWatchEntry::loadFrom(std::string_view line) {
    using cfg::EventLevels;
    if (line.data() == nullptr || line.empty()) {
        XLOG::t("Skipping logwatch entry with empty name");
        return false;
    }

    try {
        auto context = cfg::EventContext::hide;
        auto [name, body] = ParseLine(line);
        if (name.empty()) {
            return false;
        }

        auto table = tools::SplitString(std::string(body), " ");
        std::string level_string{cfg::vars::kLogWatchEvent_ParamDefault};
        if (!table.empty()) {
            level_string = table[0];
            tools::AllTrim(level_string);
            if (table.size() > 1) {
                auto context_value = table[1];
                tools::AllTrim(context_value);
                context = tools::IsEqual(context_value, "context")
                              ? cfg::EventContext::with
                              : cfg::EventContext::hide;
            }
        } else {
            XLOG::d("logwatch entry '{}' has no data, this is not normal",
                    name);
        }

        init(name, level_string, context);
        return true;
    } catch (const std::exception &e) {
        XLOG::l(
            "Failed to load logwatch entry '{}' exception: '{}' in file '{}'",
            std::string(line), e.what(),
            wtools::ToUtf8(cfg::GetPathOfLoadedConfig()));
        return false;
    }
}

void LogWatchEvent::loadConfig() {
    loadSectionParameters();
    size_t count = 0;
    try {
        auto log_array = readLogEntryArray();
        if (!log_array.has_value()) {
            return;
        }
        count = processLogEntryArray(*log_array);
        setupDefaultEntry();
        XLOG::d.t("Loaded [{}] entries in LogWatch", count);

    } catch (const std::exception &e) {
        XLOG::l(
            "CONFIG for '{}.{}' is seriously not valid, skipping. Exception {}. Loaded {} entries",
            cfg::groups::kLogWatchEvent, cfg::vars::kLogWatchEventLogFile,
            e.what(), count);
    }
}

void LogWatchEvent::loadSectionParameters() {
    using cfg::GetVal;
    send_all_ = GetVal(cfg::groups::kLogWatchEvent,
                       cfg::vars::kLogWatchEventSendall, true);
    evl_type_ = GetVal(cfg::groups::kLogWatchEvent,
                       cfg::vars::kLogWatchEventVistaApi, true)
                    ? EvlType::vista
                    : EvlType::classic;

    skip_ = GetVal(cfg::groups::kLogWatchEvent, cfg::vars::kLogWatchEventSkip,
                   false)
                ? evl::SkipDuplicatedRecords::yes
                : evl::SkipDuplicatedRecords::no;

    max_size_ =
        GetVal(cfg::groups::kLogWatchEvent, cfg::vars::kLogWatchEventMaxSize,
               cfg::logwatch::kMaxSize);
    max_entries_ =
        GetVal(cfg::groups::kLogWatchEvent, cfg::vars::kLogWatchEventMaxEntries,
               cfg::logwatch::kMaxEntries);
    max_line_length_ = GetVal(cfg::groups::kLogWatchEvent,
                              cfg::vars::kLogWatchEventMaxLineLength,
                              cfg::logwatch::kMaxLineLength);
    timeout_ =
        GetVal(cfg::groups::kLogWatchEvent, cfg::vars::kLogWatchEventTimeout,
               cfg::logwatch::kTimeout);

    if (!evl::IsEvtApiAvailable()) {
        XLOG::d(
            "Vista API requested in config, but support in OS is absent. Disabling...");
        evl_type_ = EvlType::classic;
    }
}

std::optional<YAML::Node> LogWatchEvent::readLogEntryArray() {
    const auto cfg = cfg::GetLoadedConfig();
    const auto section = cfg[cfg::groups::kLogWatchEvent];

    // sanity checks:
    if (!section) {
        XLOG::t("'{}' section absent", cfg::groups::kLogWatchEvent);
        return {};
    }

    if (!section.IsMap()) {
        XLOG::l("'{}' is not correct", cfg::groups::kLogWatchEvent);
        return {};
    }

    // get array, on success, return it
    const auto log_array = section[cfg::vars::kLogWatchEventLogFile];
    if (!log_array) {
        XLOG::t("'{}' section has no '{}' member", cfg::groups::kLogWatchEvent,
                cfg::vars::kLogWatchEventLogFile);
        return {};
    }

    if (!log_array.IsSequence()) {
        XLOG::t("'{}' section has no '{}' member", cfg::groups::kLogWatchEvent,
                cfg::vars::kLogWatchEventLogFile);
        return {};
    }
    return log_array;
}

size_t LogWatchEvent::processLogEntryArray(const YAML::Node &log_array) {
    size_t count{0U};
    entries_.clear();
    for (const auto &l : log_array) {
        LogWatchEntry lwe;
        lwe.loadFromMapNode(l);
        if (lwe.loaded()) {
            ++count;
            entries_.emplace_back(lwe);
        }
    }

    return count;
}

namespace {
std::optional<size_t> FindLastEntryWithName(const LogWatchEntryVector &entries,
                                            std::string_view name) {
    auto found = rs::find_if(entries.rbegin(), entries.rend(),
                             [name](auto e) { return e.name() == name; });
    return found == entries.rend()
               ? std::optional<size_t>{}
               : entries.size() - 1 - std::distance(entries.rbegin(), found);
}
}  // namespace

void LogWatchEvent::setupDefaultEntry() {
    auto offset = FindLastEntryWithName(entries_, "*");
    default_entry_ = offset.has_value() ? *offset : addDefaultEntry();
}

size_t LogWatchEvent::addDefaultEntry() {
    entries_.emplace_back(LogWatchEntry());
    entries_.back().init("*", "off", cfg::EventContext::hide);
    return entries_.size() - 1;
}

namespace details {
// Example: line = "System|1234" provides {"System", 1234}
State ParseStateLine(const std::string &line) {
    auto tbl = tools::SplitString(line, "|");

    if (tbl.size() != 2 || tbl[0].empty() || tbl[1].empty()) {
        XLOG::l("State Line is not valid {}", line);
        return {};
    }

    auto pos = tools::ConvertToUint64(tbl[1]);
    if (pos.has_value()) {
        return {tbl[0], pos.value(), false};
    }

    XLOG::l("State Line has no valid pos {}", line);
    return {};
}

// build big common state
StateVector LoadEventlogOffsets(const PathVector &state_files,
                                bool reset_pos_to_null) {
    for (const auto &fname : state_files) {
        StateVector states;
        std::ifstream ifs(fname);
        std::string line;

        while (std::getline(ifs, line)) {
            if (line.empty()) {
                continue;
            }
            // remove trailing carriage return
            if (line.back() == '\n') {
                line.pop_back();
            }

            // build state from the text
            auto state = ParseStateLine(line);

            // check status
            if (state.name_.empty()) {
                continue;
            }

            if (reset_pos_to_null) {
                state.pos_ = 0;
            }

            states.push_back(state);
        }

        rs::sort(states, [](const auto &s1, const auto &s2) {
            return tools::IsLess(s1.name_, s2.name_);
        });

        if (!states.empty()) {
            return states;
        }
    }

    return {};
}

auto x() {
    std::string_view s1 = "a";
    std::string_view s2 = "a";
    return s1 < s2;
}

void SaveEventlogOffsets(const std::string &file_name,
                         const StateVector &states) {
    {
        std::ofstream ofs(file_name);

        if (!ofs) {
            XLOG::l("Can't open file '{}' error [{}]", file_name,
                    ::GetLastError());
            return;
        }

        for (const auto &state : states) {
            if (state.name_ == "*") {
                continue;
            }
            ofs << state.name_ << "|" << state.pos_ << std::endl;
        }
    }
}
}  // namespace details

constexpr const char *g_event_log_reg_path =
    R"(SYSTEM\CurrentControlSet\Services\Eventlog)";

// updates presented flag or add to the States
void AddLogState(StateVector &states, bool from_config,
                 const std::string &log_name, SendMode send_mode) {
    for (auto &state : states) {
        if (tools::IsEqual(state.name_, log_name)) {
            XLOG::t("Old event log '{}' found", log_name);

            state.setDefaults();
            state.in_config_ = from_config;
            state.presented_ = true;
            return;
        }
    }

    // new added
    uint64_t pos = send_mode == SendMode::all ? 0 : cfg::kFromBegin;
    states.emplace_back(log_name, pos, true);
    states.back().in_config_ = from_config;
    XLOG::t("New event log '{}' added with pos {}", log_name, pos);
}

// main API to add config entries to the engine
void AddConfigEntry(StateVector &states, const LogWatchEntry &log_entry,
                    bool reset_to_null) {
    auto found = rs::find_if(states, [&](auto s) {
        return tools::IsEqual(s.name_, log_entry.name());
    });
    if (found != states.end()) {
        XLOG::t("Old event log '{}' found", log_entry.name());
        found->setDefaults();
        found->context_ = log_entry.context();
        found->level_ = log_entry.level();
        found->in_config_ = true;
        found->presented_ = true;
        return;
    }

    // new added
    uint64_t pos = reset_to_null ? 0 : cfg::kFromBegin;
    states.emplace_back(log_entry.name(), pos, true);
    states.back().in_config_ = true;
    states.back().level_ = log_entry.level();
    states.back().context_ = log_entry.context();
    XLOG::t("New event log '{}' added with pos {}", log_entry.name(), pos);
}

// Update States vector with log entries and Send All flags
// event logs are available
// returns count of processed Logs entries
int UpdateEventLogStates(StateVector &states,
                         const std::vector<std::string> &logs,
                         SendMode send_mode) {
    for (const auto &log : logs) {
        AddLogState(states, false, log, send_mode);
    }

    return static_cast<int>(logs.size());
}

std::vector<std::string> GatherEventLogEntriesFromRegistry() {
    return wtools::EnumerateAllRegistryKeys(g_event_log_reg_path);
}

bool IsEventLogInRegistry(std::string_view name) {
    auto regs = GatherEventLogEntriesFromRegistry();
    return std::ranges::any_of(
        regs, [name](const std::string &r) { return r == name; });
}

std::optional<uint64_t> GetLastPos(EvlType type, std::string_view name) {
    if (type == EvlType::classic && !IsEventLogInRegistry(name)) return {};

    auto log =
        evl::OpenEvl(wtools::ConvertToUtf16(name), type == EvlType::vista);

    if (log && log->isLogValid()) {
        return log->getLastRecordId();
    }

    return {};
}

std::pair<uint64_t, std::string> DumpEventLog(evl::EventLogBase &log,
                                              const State &state,
                                              LogWatchLimits lwl) {
    std::string out;
    int64_t count = 0;
    auto start = std::chrono::steady_clock::now();
    auto pos = evl::PrintEventLog(
        log, state.pos_, state.level_, state.context_, lwl.skip,
        [&out, lwl, &count, start](const std::string &str) {
            if (lwl.max_line_length > 0 &&
                static_cast<int64_t>(str.length()) >= lwl.max_line_length) {
                out += str.substr(0, static_cast<size_t>(lwl.max_line_length));
                out += '\n';
            } else {
                out += str;
            }
            if (lwl.max_size > 0 &&
                static_cast<int64_t>(out.length()) >= lwl.max_size) {
                return false;
            }
            ++count;
            if (lwl.max_entries > 0 && count >= lwl.max_entries) {
                return false;
            }
            if (lwl.timeout > 0) {
                auto p = std::chrono::steady_clock::now();
                auto span =
                    std::chrono::duration_cast<std::chrono::seconds>(p - start);
                if (span.count() > lwl.timeout) {
                    return false;
                }
            }
            return true;
        }

    );

    return {pos, out};
}

std::optional<std::string> ReadDataFromLog(EvlType type, State &state,
                                           LogWatchLimits lwl) {
    if (type == EvlType::classic && !IsEventLogInRegistry(state.name_)) {
        // we have to check registry, Windows always return success for
        // OpenLog for any even not existent log, but opens Application
        XLOG::d("Log '{}' not found in registry, try VistaApi ", state.name_);
        return {};
    }

    auto log = evl::OpenEvl(wtools::ConvertToUtf16(state.name_),
                            type == EvlType::vista);

    if (!log || !log->isLogValid()) {
        return {};
    }

    if (state.pos_ == cfg::kFromBegin) {
        // We just started monitoring this log.
        state.pos_ = log->getLastRecordId();
        return "";
    }

    // The last processed eventlog record will serve as previous state
    // (= saved offset) for the next call.
    auto [last_pos, worst_state] =
        evl::ScanEventLog(*log, state.pos_, state.level_);

    if (worst_state < state.level_) {
        // nothing to report
        state.pos_ = last_pos;
        return "";
    }

    auto [pos, out] = DumpEventLog(*log, state, lwl);

    if (provider::config::g_set_logwatch_pos_to_end && last_pos > pos) {
        XLOG::d.t("Skipping logwatch pos from [{}] to [{}]", pos, last_pos);
        pos = last_pos;
    }

    state.pos_ = pos;
    return out;
}

LogWatchEntry GenerateDefaultValue() { return LogWatchEntry().withDefault(); }

bool UpdateState(State &state, const LogWatchEntryVector &entries) noexcept {
    for (const auto &config_entry : entries) {
        if (tools::IsEqual(state.name_, config_entry.name())) {
            state.context_ = config_entry.context();
            state.level_ = config_entry.level();
            state.in_config_ = true;
            return true;
        }
    }

    return false;
}

void UpdateStates(StateVector &states, const LogWatchEntryVector &entries,
                  const LogWatchEntry *dflt) {
    LogWatchEntry default_entry =
        dflt != nullptr ? *dflt : GenerateDefaultValue();

    // filtering states
    for (auto &s : states) {
        if (UpdateState(s, entries)) {
            continue;
        }

        // not found - attempting to load default value
        s.context_ = default_entry.context();
        s.level_ = default_entry.level();

        // if default level isn't off, then we set entry as configured
        if (s.level_ != cfg::EventLevels::kOff) {
            s.in_config_ = true;
        }
    }
}

LogWatchLimits LogWatchEvent::getLogWatchLimits() const noexcept {
    return {.max_size = max_size_,
            .max_line_length = max_line_length_,
            .max_entries = max_entries_,
            .timeout = timeout_,
            .skip = skip_};
}

std::vector<fs::path> LogWatchEvent::makeStateFilesTable() const {
    namespace fs = std::filesystem;
    std::vector<fs::path> statefiles;
    fs::path state_dir = cfg::GetStateDir();
    auto ip_addr = ip();
    if (!ip_addr.empty()) {
        auto ip_fname = MakeStateFileName(kLogWatchEventStateFileName,
                                          kLogWatchEventStateFileExt, ip_addr);
        if (!ip_fname.empty()) {
            statefiles.push_back(state_dir / ip_fname);
        }
    }

    auto normal_fname = MakeStateFileName(kLogWatchEventStateFileName,
                                          kLogWatchEventStateFileExt);

    statefiles.push_back(state_dir / normal_fname);
    return statefiles;
}

std::string GenerateOutputFromStates(EvlType type, StateVector &states,
                                     LogWatchLimits lwl) {
    std::string out;
    for (auto &state : states) {
        switch (state.level_) {
            case cfg::EventLevels::kOff:
                // updates position in state file for disabled log too
                state.pos_ = GetLastPos(type, state.name_).value_or(0);
                [[fallthrough]];
            case cfg::EventLevels::kIgnore:
                // this is NOT log, just stupid entries in registry
                continue;

            case cfg::EventLevels::kAll:
            case cfg::EventLevels::kWarn:
            case cfg::EventLevels::kCrit:
                if (state.in_config_) {
                    auto log_data = ReadDataFromLog(type, state, lwl);
                    if (log_data.has_value()) {
                        out += "[[[" + state.name_ + "]]]\n" + *log_data;
                    } else
                        out += "[[[" + state.name_ + ":missing]]]\n";
                } else {
                    // skipping
                    XLOG::d("Skipping log {}", state.name_);
                }
        }
    }

    return out;
}

std::string LogWatchEvent::makeBody() {
    XLOG::t(XLOG_FUNC + " entering");

    // The agent reads from a state file the record numbers
    // of the event logs up to which messages have
    // been processed. When no state information is available,
    // the eventlog is skipped to the end (unless the sendall config
    // option is used).
    auto statefiles = makeStateFilesTable();

    // creates states table from the file
    auto states =
        details::LoadEventlogOffsets(statefiles, send_all_);  // offsets stored

    // check by registry, which logs are presented
    auto logs = GatherEventLogEntriesFromRegistry();
    if (logs.empty()) {
        XLOG::l("Registry has nothing to logwatch. This is STRANGE");
    }
    UpdateEventLogStates(states, logs,
                         send_all_ ? SendMode::all : SendMode::normal);

    // 2) Register additional, configured logs that are not in registry.
    //    Note: only supported with vista API enabled.
    if (evl_type_ == EvlType::vista) {
        for (const auto &e : entries_) {
            AddConfigEntry(states, e, send_all_);
        }
    }

    // now we have states list and want to mark all registered sources
    UpdateStates(states, entries_, defaultEntry());

    // make string
    auto out = GenerateOutputFromStates(evl_type_, states, getLogWatchLimits());

    // The offsets are persisted in a statefile.
    // Always use the first available statefile name. In case of a
    // TCP/IP connection, this is the host-IP-specific statefile, and in
    // case of non-TCP (test / debug run etc.) the general
    // eventstate.txt.
    const auto &statefile = statefiles.front();
    details::SaveEventlogOffsets(wtools::ToUtf8(statefile.wstring()), states);

    return out;
}

}  // namespace cma::provider
