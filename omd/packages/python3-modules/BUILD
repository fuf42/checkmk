load(":build-python3-modules.bzl", "build_cmd", "build_python_module", "create_requirements_file")
load("@python_modules//:requirements.bzl", "all_requirements")
load("@omd_packages//:package_versions.bzl", "PYTHON_MAJOR_DOT_MINOR")
load("@omd_packages//:static_variables.bzl", "INTERNAL_PYPI_MIRROR")

config_setting(
    name = "git_ssl_no_verify",
    values = {"define": "git-ssl-no-verify=true"},
)

[create_requirements_file(
    name = module,
    outs = [
        "%s_requirements.txt" % module,
    ],
) for module in all_requirements]

[build_python_module(
    name = "%s_compile" % module,
    srcs = [
        "%s_requirements.txt" % module,
        "@freetds",
        "@omd_packages//omd/packages/omd:use_system_openssl",
        "@openssl",
        "@python",
    ],
    outs = [
        "compiled_%s" % module,
    ],
    cmd = select({
        "//conditions:default": build_cmd.format(
            git_ssl_no_verify = "",
            pyMajMin = PYTHON_MAJOR_DOT_MINOR,
            pypi_mirror = INTERNAL_PYPI_MIRROR,
            requirements = "-r $$HOME/$(execpath %s_requirements.txt);" % module,
        ),
        ":git_ssl_no_verify": build_cmd.format(
            git_ssl_no_verify = "GIT_SSL_NO_VERIFY=true",
            pyMajMin = PYTHON_MAJOR_DOT_MINOR,
            pypi_mirror = INTERNAL_PYPI_MIRROR,
            requirements = "-r $$HOME/$(execpath %s_requirements.txt);" % module,
        ),
    }),
) for module in all_requirements]

[build_python_module(
    name = "%s_compile" % module,
    srcs = [
        "@freetds",
        "@omd_packages//omd/packages/omd:use_system_openssl",
        "@omd_packages//packages:cmk-agent-based",
        "@omd_packages//packages:cmk-agent-receiver",
        "@omd_packages//packages:cmk-graphing",
        "@omd_packages//packages:cmk-mkp-tool",
        "@omd_packages//packages:cmk-server-side-calls",
        "@omd_packages//packages:cmk-werks",
        "@openssl",
        "@python",
    ],
    outs = [
        "compiled_%s" % module,
    ],
    cmd = select({
        "//conditions:default": build_cmd.format(
            git_ssl_no_verify = "",
            pyMajMin = PYTHON_MAJOR_DOT_MINOR,
            pypi_mirror = INTERNAL_PYPI_MIRROR,
            requirements = dep,
        ),
        ":git_ssl_no_verify": build_cmd.format(
            git_ssl_no_verify = "GIT_SSL_NO_VERIFY=true",
            pyMajMin = PYTHON_MAJOR_DOT_MINOR,
            pypi_mirror = INTERNAL_PYPI_MIRROR,
            requirements = dep,
        ),
    }),
) for module, dep in [
    ("agent-receiver", "$(execpath @omd_packages//packages:cmk-agent-receiver)"),
    ("graphing", "$(execpath @omd_packages//packages:cmk-graphing)"),
    ("server-side-calls", "$(execpath @omd_packages//packages:cmk-server-side-calls)"),
    ("werks", "$(execpath @omd_packages//packages:cmk-werks)"),
    ("agent-based", "$(execpath @omd_packages//packages:cmk-agent-based)"),
    ("mkp-tool", "$(execpath @omd_packages//packages:cmk-mkp-tool)"),
]]

genrule(
    name = "python3-modules-modify",
    srcs = [
        "%s_compile" % module
        for module in all_requirements + [
            "agent-based",
            "agent-receiver",
            "graphing",
            "mkp-tool",
            "server-side-calls",
            "werks",
        ]
    ],
    outs = [
        "python3-modules",
    ],
    cmd = """
        mkdir -p $(RULEDIR)/python3-modules
        for MODULE in $(SRCS); do
            if ls $$MODULE/* >/dev/null 2>&1; then
                rsync -a $$MODULE/* $(RULEDIR)/python3-modules/
            fi
        done

        # For some highly obscure unknown reason some files end up world-writable. Fix that!
        chmod -R o-w $(RULEDIR)/python3-modules/lib/python{pyMajMin}/site-packages

        # Cleanup some unwanted files (example scripts)
        find $(RULEDIR)/python3-modules/bin -name "*.py" ! -name snmpsimd.py -exec rm -f {{}} \\;

        # These files break the integration tests on the CI server. Don't know exactly
        # why this happens only there, but should be a working fix.
        rm -r -f $(RULEDIR)/python3-modules/snmpsim

        # Cleanup unneeded test files of numpy
        rm -r -f $(RULEDIR)/python3-modules/lib/python{pyMajMin}/site-packages/numpy/*/tests

        # Fix python interpreter for kept scripts
        sed -E -i '1s|^#!.*/python3?$$|#!/usr/bin/env python3|' $(RULEDIR)/python3-modules/bin/[!_]*

        # pip is using pip._vendor.distlib.scripts.ScriptMaker._build_shebang() to
        # build the shebang of the scripts installed to bin. When executed via our CI
        # containers, the shebang exceeds the max_shebang_length of 127 bytes. For this
        # case, it adds a #!/bin/sh wrapper in front of the python code o_O to make it
        # fit into the shebang. Let's also cleanup this case.
        sed -i -z "s|^#\\!/bin/sh\\n.*exec.*python3[^\\n]*\\n[^\\n]*|#\\!/usr/bin/env python3|" $(RULEDIR)/python3-modules/bin/[!_]*

        # Fix rpath for all .so libs
        cd $(RULEDIR)/python3-modules/lib/
        for F in $$(find -name "*.so");
            do patchelf --set-rpath \\
            $$(echo $$F | sed 's|[^/]*/|../|g' | sed 's|\\.\\./|\\$$ORIGIN/|' | sed 's|[^/]*$$||') $$F ;
        done
        cd -
    """.format(pyMajMin = PYTHON_MAJOR_DOT_MINOR),
)
