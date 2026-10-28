# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""maven_parser is pure — exercise it directly (§9.3/§9.4)."""
from app.agents.maven_parser import parse_maven_output

_MVN = """\
[INFO] Building network-core 2.0.0
[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) @ network-core ---
[ERROR] /ws/network-core/src/main/java/Foo.java:[12,34] cannot find symbol RefundStatus
[WARNING] /ws/network-core/src/main/java/Foo.java:[3] deprecated API
[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) @ IGW ---
[ERROR] /ws/IGW/src/Bar.java:[5,1] incompatible types
[INFO] BUILD FAILURE
"""


def test_parses_errors_with_file_line_col_and_module():
    r = parse_maven_output(_MVN)
    foo = next(d for d in r.diagnostics if d.file.endswith("Foo.java") and d.severity == "error")
    assert (foo.line, foo.col, foo.module) == (12, 34, "network-core")
    assert r.build_success is False


def test_warning_keeps_module_but_is_not_an_error():
    r = parse_maven_output(_MVN)
    warns = [d for d in r.diagnostics if d.severity == "warning"]
    assert len(warns) == 1 and warns[0].line == 3
    assert "network-core" in r.modules_with_errors
    assert r.error_count == 2  # Foo + Bar, before any soft-fail tagging


def test_soft_fail_downgrades_only_the_configured_module():
    r = parse_maven_output(_MVN, soft_fail_modules={"IGW"})
    bar = next(d for d in r.diagnostics if d.file.endswith("Bar.java"))
    assert bar.severity == "soft"                  # IGW error downgraded
    assert r.soft_count == 1 and r.error_count == 1  # only Foo remains a hard error
    assert "IGW" not in r.modules_with_errors      # soft errors don't gate


def test_plain_javac_form():
    r = parse_maven_output("/ws/Foo.java:12: error: cannot find symbol")
    assert len(r.diagnostics) == 1
    d = r.diagnostics[0]
    assert (d.severity, d.file, d.line) == ("error", "/ws/Foo.java", 12)


def test_build_success_when_no_failure_line():
    r = parse_maven_output("[INFO] BUILD SUCCESS")
    assert r.build_success is True
    assert r.error_count == 0


def test_empty_output_is_inconclusive():
    r = parse_maven_output("")
    assert r.build_success is None and r.diagnostics == []


def test_plugin_resolution_failure_is_environment():
    # Offline `mvn -o` with a cold ~/.m2 cache missing maven-source-plugin: this is
    # an ENVIRONMENT failure (warm the cache), NOT a code defect — must not drive code_change.
    out = ("[ERROR] Error resolving version for plugin "
           "'org.apache.maven.plugins:maven-source-plugin' from the repositories "
           "[local, nexus]: Plugin not found in any plugin repository -> [Help 1]")
    r = parse_maven_output(out)
    assert r.is_environment_failure and r.error_count == 0
    assert r.environment_errors


# ── Legacy "1.x" Java version scheme ──────────────────────────────────────────
# A module still pinned to `<maven.compiler.release>1.8</maven.compiler.release>`
# makes javac abort with "release version 1.8 not supported". The demanded major is
# 8 — capturing the "1" told the user to "install JDK 1", which does not exist.
_LEGACY_RELEASE = (
    "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.14.1:"
    "compile (default-compile) on project iupi-gateway-common: Fatal error compiling: "
    "error: release version 1.8 not supported -> [Help 1]")


def _skip_pats(spec):
    """Compile a skip spec the way verification_plan._skip_patterns() does."""
    import re
    star = re.escape("*")
    return [re.compile(re.escape(s.strip()).replace(star, ".*"), re.IGNORECASE)
            for s in spec.split(",") if s.strip()]


def test_legacy_1_x_release_reports_the_real_major():
    r = parse_maven_output(_LEGACY_RELEASE)
    assert r.required_java == 8, "'1.8' must normalise to 8, not 1"
    assert r.is_environment_failure and r.error_count == 0


def test_legacy_1_x_source_option_reports_the_real_major():
    r = parse_maven_output("[ERROR] Source option 1.6 is no longer supported. Use 1.8 or later.")
    assert r.required_java == 6


def test_modern_release_form_still_parses():
    r = parse_maven_output("[ERROR] invalid target release: 25")
    assert r.required_java == 25


def test_skip_modules_excludes_a_jdk_failure_in_an_out_of_scope_module():
    # AGENTIC_VERIFY_SKIP_MODULES must cover toolchain failures too: one legacy module
    # pinned to an old Java release otherwise condemns the whole reactor, because javac
    # dies before emitting any file:line diagnostic (error_count == 0 → environment
    # failure). Maven names the project on the failing line, so attribution works even
    # though the reactor aborted before `current_module` was set.
    r = parse_maven_output(_LEGACY_RELEASE, skip_patterns=_skip_pats("*iupi*"))
    assert not r.is_environment_failure
    assert r.environment_errors == []
    assert r.soft_count == 1


def test_skip_modules_does_not_mask_an_unrelated_module():
    r = parse_maven_output(_LEGACY_RELEASE, skip_patterns=_skip_pats("*hsm-proxy*"))
    assert r.is_environment_failure and r.required_java == 8
