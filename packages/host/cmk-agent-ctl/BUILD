load("@cargo_deps_host//:defs.bzl", "aliases", "all_crate_deps")
load("@rules_rust//rust:defs.bzl", "rust_binary", "rust_library", "rust_test", "rust_test_suite")

rust_library(
    name = "cmk-agent-ctl-lib",
    srcs = [
        "src/agent_receiver_api.rs",
        "src/certs.rs",
        "src/cli.rs",
        "src/configuration.rs",
        "src/configuration/config.rs",
        "src/configuration/migrate.rs",
        "src/constants.rs",
        "src/lib.rs",
        "src/log_ext.rs",
        "src/mailslot_transport.rs",
        "src/misc.rs",
        "src/modes.rs",
        "src/modes/daemon.rs",
        "src/modes/delete_connection.rs",
        "src/modes/dump.rs",
        "src/modes/import_connection.rs",
        "src/modes/pull.rs",
        "src/modes/push.rs",
        "src/modes/registration.rs",
        "src/modes/renew_certificate.rs",
        "src/modes/status.rs",
        "src/monitoring_data.rs",
        "src/monitoring_data/linux.rs",
        "src/monitoring_data/win.rs",
        "src/setup.rs",
        "src/site_spec.rs",
        "src/tls_server.rs",
        "src/types.rs",
    ],
    aliases = aliases(),
    crate_name = "cmk_agent_ctl",
    edition = "2021",
    proc_macro_deps = all_crate_deps(
        proc_macro = True,
    ),
    deps = all_crate_deps(),
)

rust_test(
    name = "cmk-agent-ctl-lib-internal",
    crate = ":cmk-agent-ctl-lib",
    deps = all_crate_deps() + [
        ":cmk-agent-ctl-lib",
        "@cargo_deps_host//:assert_cmd",
        "@cargo_deps_host//:lazy_static",
        "@cargo_deps_host//:predicates",
        "@cargo_deps_host//:tempfile",
    ],
)

rust_library(
    name = "cmk-agent-ctl-tests-common",
    testonly = 1,
    srcs = [
        "tests/common/agent/linux.rs",
        "tests/common/agent/mod.rs",
        "tests/common/agent/win.rs",
        "tests/common/certs.rs",
        "tests/common/mod.rs",
    ],
    crate_features = ["build_system_bazel"],
    crate_name = "common",
    crate_root = "tests/common/mod.rs",
    deps = all_crate_deps() + [
        ":cmk-agent-ctl-lib",
        "@cargo_deps_host//:assert_cmd",
        "@cargo_deps_host//:lazy_static",
        "@cargo_deps_host//:predicates",
        "@cargo_deps_host//:tempfile",
    ],
)

rust_test_suite(
    name = "cmk-agent-ctl-lib-external",
    srcs = [
        "tests/test_cli.rs",
        "tests/test_pull.rs",
        "tests/test_status.rs",
    ],
    crate_features = ["build_system_bazel"],
    env = {
        "CMK_AGENT_CTL_PATH": "$(execpaths :cmk-agent-ctl)",
        "ROOTPATH": "$(location :cmk-agent-ctl)",
        "RUST_BACKTRACE": "1",
    },
    env_inherit = [
        "CMK_AGENT_CTL_LOG_TO_FILE",
        "DEBUG_CONNECTION_TIMEOUT",
        "DEBUG_HOME_DIR",
        "DEBUG_MAX_CONNECTIONS",
        "ENV_PROGRAM_DATA",
        "MK_LOGDIR",
    ],
    deps = all_crate_deps() + [
        ":cmk-agent-ctl",
        ":cmk-agent-ctl-lib",
        ":cmk-agent-ctl-tests-common",
        "@cargo_deps_host//:assert_cmd",
        "@cargo_deps_host//:lazy_static",
        "@cargo_deps_host//:predicates",
        "@cargo_deps_host//:tempfile",
    ],
)

rust_binary(
    name = "cmk-agent-ctl",
    srcs = ["src/main.rs"],
    aliases = aliases(),
    crate_name = "cmk_agent_ctl",
    edition = "2021",
    proc_macro_deps = all_crate_deps(
        proc_macro = True,
    ),
    visibility = ["//visibility:public"],
    deps = all_crate_deps() + [":cmk-agent-ctl-lib"],
)
