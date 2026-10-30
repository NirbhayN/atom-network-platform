# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Verification hard gates + plan (§9). Gate logic uses a fake executor (no mvn)."""
from types import SimpleNamespace

import pytest

from app.agents.platform_adapter import CommandResult, CommandNotAllowed
from app.agents import verification_plan as V
from app.agents.verification_plan import VerificationStep, run_plan, path_to_module
from app.core.config import settings as _settings

RUN = "run-1"


@pytest.fixture(autouse=True)
def _hermetic_verify_settings(monkeypatch):
    # The dev container sets AGENTIC_VERIFY_SKIP_MODULES (e.g. *igw*,*hsm-proxy*) which would
    # otherwise leak in and unconditionally skip IGW's errors — masking the touched-IGW gate. Reset
    # it so these tests exercise the soft-fail/touched logic hermetically, independent of the env.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", "")
    # The build-plan tests model the network's two-repo topology (core builds before
    # app), and build_plan reads repo_roles from the ACTIVE pack at call time —
    # pin the pack these fixtures describe rather than inheriting the shell's
    # (the F-1 shape: red on any NLLN-configured host).
    from pathlib import Path

    from app.agents import verification_plan as _vp
    from app.core.domain import registry as _registry

    monkeypatch.setenv("DOMAIN_PACK", str(
        Path(_vp.__file__).resolve().parents[1] / "packs" / "network" / "network.yaml"))
    _registry._load.cache_clear()
    yield
    _registry._load.cache_clear()


def _res(exit_code=0, stdout="", stderr="", timed_out=False):
    return CommandResult(argv=["mvn"], exit_code=exit_code, stdout=stdout, stderr=stderr,
                         timed_out=timed_out, duration_ms=1)


class _Exec:
    """Fake PlatformAdapter: maps a step (by argv keyword) to a CommandResult."""
    def __init__(self, responder):
        self.responder = responder

    def run_command(self, cwd, argv, timeout_s=None):
        return self.responder(argv)


def _verdict(steps, responder, **kw):
    return run_plan(None, RUN, steps, executor=_Exec(responder), **kw)


_COMPILE = VerificationStep("compile", "r1", ["mvn", "-o", "compile"], "refund-svc")
_TEST = VerificationStep("test", "r1", ["mvn", "-o", "test"])
_SMOKE = VerificationStep("smoke", "r1", ["mvn", "-o", "verify"])

_IGW_FAIL = ("[INFO] Building IGW 1.0\n"
              "[INFO] --- maven-compiler-plugin:3.11:compile (default) @ IGW ---\n"
              "[ERROR] /ws/IGW/src/Bar.java:[5,1] incompatible types\n"
              "[INFO] BUILD FAILURE\n")
_REAL_FAIL = ("[INFO] --- maven-compiler-plugin:3.11:compile (default) @ refund-svc ---\n"
              "[ERROR] /ws/refund-svc/src/Foo.java:[3,9] cannot find symbol\n"
              "[INFO] BUILD FAILURE\n")


def test_all_steps_pass_is_verified():
    out = _verdict([_COMPILE, _TEST], lambda a: _res(0), touched_modules={"refund-svc"})
    assert out.status == "verified" and all(out.gates.values())


def test_compile_hard_error_fails_the_run():
    out = _verdict([_COMPILE], lambda a: _res(1, stdout=_REAL_FAIL), touched_modules={"refund-svc"})
    assert out.status == "needs_fix" and out.gates["compile"] is False


def test_untouched_igw_soft_error_does_not_fail():
    # IGW is configured soft and NOT touched → its compile error is excluded.
    out = _verdict([_COMPILE], lambda a: _res(1, stdout=_IGW_FAIL),
                   touched_modules={"refund-svc"}, soft_fail_modules={"IGW"})
    assert out.status == "verified" and out.gates["compile"] is True


def test_touched_igw_is_gated_normally():
    # Same error, but the change EDITS IGW → it leaves the effective soft set → fails.
    out = _verdict([_COMPILE], lambda a: _res(1, stdout=_IGW_FAIL),
                   touched_modules={"IGW"}, soft_fail_modules={"IGW"})
    assert out.status == "needs_fix" and out.gates["compile"] is False


def test_nonzero_compile_with_no_parseable_errors_fails():
    # A build that fails for an UNRECOGNISED non-compiler reason (OOM, plugin crash)
    # exits non-zero with no [ERROR] file:[L,C] diagnostics and no known infra
    # signature. That must FAIL the gate — never silently pass.
    out = _verdict([_COMPILE],
                   lambda a: _res(1, stderr="java.lang.OutOfMemoryError: Java heap space"),
                   touched_modules={"refund-svc"})
    assert out.gates["compile"] is False and out.status == "needs_fix"


def test_dependency_resolution_failure_is_environment_not_code():
    # An infra failure (offline-cache miss / nexus 401 / unresolved deps) is NOT a
    # code defect — classify it as 'unverified' so the run fails fast instead of
    # looping code_change on a problem the agent can't fix by editing source.
    out = _verdict([_COMPILE],
                   lambda a: _res(1, stderr="[ERROR] Failed: Could not resolve dependencies for refund-svc"),
                   touched_modules={"refund-svc"})
    assert out.gates["environment"] is False and out.status == "unverified"
    assert "environment" in out.reason.lower()


def test_java_version_mismatch_is_environment_with_required_version():
    # mvn aborts with "invalid target release: 25" when the active JDK is too old.
    # The agent must NOT treat this as a code bug — it's a toolchain problem.
    out = _verdict([_COMPILE],
                   lambda a: _res(1, stderr="[ERROR] Fatal error compiling: error: invalid target release: 25"),
                   touched_modules={"refund-svc"})
    assert out.gates["environment"] is False and out.status == "unverified"
    assert "Java 25" in out.reason


def test_module_report_built_failed_skipped():
    # Two changed modules: A fails to compile, B compiles clean. Both must be
    # ATTEMPTED (B isn't skipped just because A failed) and reported per-module.
    a = VerificationStep("install", "r1", ["mvn", "-o", "install"], "mod-a", subdir="mod-a")
    b = VerificationStep("install", "r1", ["mvn", "-o", "install"], "mod-b", subdir="mod-b")
    fail_a = ("[INFO] --- maven-compiler-plugin:3.11:compile (default) @ mod-a ---\n"
              "[ERROR] /ws/mod-a/src/Foo.java:[3,9] cannot find symbol\n[INFO] BUILD FAILURE\n")
    # Per-step responder: mod-a dir fails (first call), mod-b dir passes (second).
    calls = []

    def responder(argv):
        calls.append(argv)
        # both steps share argv; distinguish by call order: first=mod-a, second=mod-b
        return _res(1, stdout=fail_a) if len(calls) == 1 else _res(0)
    out = run_plan(None, RUN, [a, b], executor=_Exec(responder), touched_modules={"mod-a", "mod-b"})
    assert out.status == "needs_fix"
    assert out.module_results["mod-a"]["status"] == "failed"
    assert out.module_results["mod-b"]["status"] == "built", "B must build even though A failed"
    assert len(calls) == 2, "both modules attempted (no short-circuit across modules)"


def test_short_circuit_skips_steps_after_a_gating_failure():
    calls = []

    def responder(argv):
        calls.append(argv)
        return _res(1, stdout=_REAL_FAIL) if "compile" in argv else _res(0)
    out = run_plan(None, RUN, [_COMPILE, _TEST], executor=_Exec(responder),
                   touched_modules={"refund-svc"})
    assert out.status == "needs_fix"
    assert not any("test" in c for c in calls), "test must be skipped after compile fails"


def test_test_failure_fails_required_tests_gate():
    out = _verdict([_COMPILE, _TEST], lambda a: _res(0) if "compile" in a else _res(1),
                   touched_modules={"refund-svc"})
    assert out.gates["required_tests"] is False and out.status == "needs_fix"


def test_timeout_fails_the_timeout_gate():
    out = _verdict([_COMPILE], lambda a: _res(-1, timed_out=True), touched_modules={"refund-svc"})
    assert out.gates["timeout"] is False and out.status == "needs_fix"


def test_missing_toolchain_fails():
    def boom(argv):
        raise CommandNotAllowed("mvn not resolved")
    out = _verdict([_COMPILE], boom, touched_modules={"refund-svc"})
    assert out.gates["toolchain"] is False and out.status == "needs_fix"


def test_smoke_required_but_absent_fails():
    out = _verdict([_COMPILE], lambda a: _res(0), touched_modules={"refund-svc"}, smoke_required=True)
    assert out.gates["smoke"] is False


def test_smoke_present_and_passing():
    out = _verdict([_COMPILE, _SMOKE], lambda a: _res(0), touched_modules={"refund-svc"},
                   smoke_required=True)
    assert out.gates["smoke"] is True and out.status == "verified"


def test_path_to_module_reads_artifact_id(tmp_path):
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "pom.xml").write_text("<project><artifactId>refund-svc</artifactId></project>")
    (tmp_path / "mod" / "src").mkdir()
    (tmp_path / "mod" / "src" / "A.java").write_text("class A{}")
    assert path_to_module(tmp_path, "mod/src/A.java") == "refund-svc"
    assert path_to_module(tmp_path, "nopom/x.java") is None


def test_path_to_module_dir(tmp_path):
    from app.agents.verification_plan import path_to_module_dir
    (tmp_path / "network-parent" / "api-gateway" / "src").mkdir(parents=True)
    (tmp_path / "network-parent" / "api-gateway" / "pom.xml").write_text("<project/>")
    assert path_to_module_dir(tmp_path, "network-parent/api-gateway/src/A.java") == "network-parent/api-gateway"
    assert path_to_module_dir(tmp_path, "nopom/x.java") is None


def test_build_plan_builds_each_module_from_its_own_dir(tmp_path, monkeypatch):
    """Broken-root-safe: every step runs OFFLINE from the module's own directory
    (subdir), never `-pl :artifact` off the (possibly broken) root reactor."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run, rid = "run1", "r1"
    rdir = tmp_path / run / rid
    for d in ("network-dependencies/network-domain-xsd", "network-parent/api-gateway"):
        (rdir / d / "src").mkdir(parents=True)
        (rdir / d / "pom.xml").write_text("<project/>")
    ops = [
        FileOp("modify", rid, "network-parent/api-gateway/src/A.java", "x", None),
        FileOp("add", rid, "network-dependencies/network-domain-xsd/src/main/resources/R.xsd", "<xsd/>", None),
    ]
    steps, _touched = VP.build_plan(None, run, SimpleNamespace(operations=ops))

    installs = {s.subdir for s in steps if s.kind == "install"}
    assert installs == {"network-dependencies/network-domain-xsd", "network-parent/api-gateway"}
    assert all("-pl" not in s.argv for s in steps)              # never the broken root reactor
    assert all(s.argv[0] == "mvn" for s in steps)               # mvn from the module dir (the -o/offline flag is config-driven)
    assert any(s.kind == "generate_sources" and s.subdir == "network-dependencies/network-domain-xsd" for s in steps)


def test_build_plan_aggregator_root_builds_module_with_also_make(tmp_path, monkeypatch):
    """Intra-repo dependency fix: when the app repo's root pom AGGREGATES (declares
    <module>), the touched submodule is built off the root with `-pl <module> -am` so its
    in-repo sibling dependencies land in ~/.m2 first — preventing a phantom
    'package … does not exist' for a sibling the change never touched (which the agent can
    only "fix" by inventing a pom dependency). The non-aggregating-root case keeps the
    broken-root-safe build-from-dir path (see test_build_plan_builds_each_module_from_its_own_dir)."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp
    from app.models.agentic import AgenticRun

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run, app_id = "run1", "network-20"
    rdir = tmp_path / run / app_id
    # aggregator root (declares modules) + touched submodule + an untouched sibling dep
    for mod in ("gateway-web", "tcp-controller"):
        (rdir / mod / "src").mkdir(parents=True)
        (rdir / mod / "pom.xml").write_text("<project/>")
    (rdir / "pom.xml").write_text(
        "<project><module>gateway-web</module><module>tcp-controller</module></project>")
    ops = [FileOp("modify", app_id, "gateway-web/src/A.java", "x", None)]

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[app_id])
            return SimpleNamespace(role="app", depends_on=None, label=key)

    steps, touched = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    installs = [s for s in steps if s.kind == "install"]
    assert len(installs) == 1
    s = installs[0]
    assert s.subdir is None, "built off the aggregator root, not the module's own dir"
    assert "-am" in s.argv and "-pl" in s.argv
    assert s.argv[s.argv.index("-pl") + 1] == "gateway-web", "-pl scopes to the touched module"
    assert "gateway-web" in touched


def test_build_plan_two_repos_core_builds_before_app(tmp_path, monkeypatch):
    """The production topology: network-core (framework + XSDs, role='core') must be
    built — mvn install into the shared ~/.m2 — BEFORE network-2.0 (business logic,
    role='app') compiles against its artifacts. Repo `role` drives the order,
    regardless of repo-id sort order."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run = "run1"
    # repo ids chosen so naive id-sorting would build the APP first.
    core_id, app_id = "z-network-core", "a-network-20"
    for rid, mod in ((core_id, "domain-xsd"), (app_id, "txn-service")):
        d = tmp_path / run / rid / mod
        (d / "src").mkdir(parents=True)
        (d / "pom.xml").write_text("<project/>")
    ops = [
        FileOp("modify", app_id, "txn-service/src/A.java", "x", None),
        FileOp("add", core_id, "domain-xsd/src/main/resources/R.xsd", "<xsd/>", None),
    ]

    from app.models.agentic import AgenticRun

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[core_id, app_id])
            return SimpleNamespace(role="core" if key == core_id else "app", depends_on=None, label=key)

    steps, touched = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    repo_order = [s.repo_id for s in steps]
    # Every core step precedes every app step → core's install hits ~/.m2 first.
    assert repo_order.index(core_id) < repo_order.index(app_id)
    assert max(i for i, r in enumerate(repo_order) if r == core_id) \
        < min(i for i, r in enumerate(repo_order) if r == app_id)
    # Core is built as a FULL reactor (from its root, subdir=None) so EVERY core
    # module — not just the changed one — lands in ~/.m2 for network-2.0 to resolve.
    core_steps = [s for s in steps if s.repo_id == core_id]
    assert core_steps and all(s.subdir is None for s in core_steps)


def test_build_plan_untouched_core_still_full_builds_for_app_change(tmp_path, monkeypatch):
    """The reported bug: a network-2.0-ONLY change has no ops in network-core, yet network-core
    must still be built (full reactor → ~/.m2) FIRST, or network-2.0 fails to resolve
    core artifacts it didn't itself change. Core is in scope (selected) but untouched."""
    from types import SimpleNamespace
    from app.core.config import settings
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp
    from app.models.agentic import AgenticRun

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    run = "run1"
    core_id, app_id = "network-core", "network-20"
    d = tmp_path / run / app_id / "txn-service"
    (d / "src").mkdir(parents=True)
    (d / "pom.xml").write_text("<project/>")
    # ONLY a network-2.0 file changes — nothing in core.
    ops = [FileOp("modify", app_id, "txn-service/src/A.java", "x", None)]

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[core_id, app_id])
            return SimpleNamespace(role="core" if key == core_id else "app", depends_on=None, label=key)

    steps, _touched = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    repo_order = [s.repo_id for s in steps]
    assert core_id in repo_order, "untouched core repo must still be built"
    # full core build precedes the app module build
    assert max(i for i, r in enumerate(repo_order) if r == core_id) \
        < min(i for i, r in enumerate(repo_order) if r == app_id)
    # Untouched core → a single full reactor install from the root (no per-module
    # steps, no separate generate-sources: `mvn install` runs the full lifecycle).
    core_steps = [s for s in steps if s.repo_id == core_id]
    assert [s.kind for s in core_steps] == ["install"]
    assert all(s.subdir is None for s in core_steps)
    assert "txn-service" in _touched


def test_build_plan_without_declared_roles_keeps_core_dependency_default(tmp_path, monkeypatch):
    """No topology declaration keeps the historical role='core' bootstrap."""
    from app.agents import verification_plan as VP
    from app.agents.agentic_tools import FileOp
    from app.core.config import settings
    from app.models.agentic import AgenticRun

    monkeypatch.setattr(settings, "agentic_workspace_root", str(tmp_path))
    monkeypatch.setattr(VP, "get_active_pack", lambda: object())
    run, core_id, app_id = "run1", "shared", "service"
    app_dir = tmp_path / run / app_id / "txn-service"
    (app_dir / "src").mkdir(parents=True)
    (app_dir / "pom.xml").write_text("<project/>")
    ops = [FileOp("modify", app_id, "txn-service/src/A.java", "x", None)]

    class _DB:
        def get(self, model, key):
            if model is AgenticRun:
                return SimpleNamespace(selected_repo_ids=[core_id, app_id])
            return SimpleNamespace(
                role="core" if key == core_id else "app",
                depends_on=None,
                label=key,
            )

    steps, _ = VP.build_plan(_DB(), run, SimpleNamespace(operations=ops))
    assert any(step.repo_id == core_id and step.kind == "install" for step in steps)


def test_mvn_test_tolerates_unmatched_upstream_reactor_modules():
    # `-pl <module> -am` pulls dependency siblings into the reactor; Surefire 3 hard-fails
    # any of them whose tests don't match -Dtest unless failIfNoSpecifiedTests is off
    # (a DIFFERENT flag from failIfNoTests). Regression: run 74a785ac looped forever on
    # "No tests matching pattern" thrown by an upstream module with zero matching tests.
    argv = V._mvn_test("dataaccessor", ["MandateRegistryServiceTest"])
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in argv
    assert "-DfailIfNoTests=false" in argv
    assert argv[-1] == "test"


def test_unparseable_failure_surfaces_gate_command_and_mojo_line():
    # No path:line diagnostics to parse → the fallback must hand the agent (1) the exact
    # command the gate ran (it cannot see it any other way) and (2) the mojo-failure
    # sentence, not just Maven's boilerplate footer tail.
    step = VerificationStep(
        "test", "r1",
        ["mvn", "-B", "-pl", "dataaccessor", "-am", "-Dtest=FooTest",
         "-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false", "test"])
    tail = (
        "[INFO] Building network-domain-xsd 1.0-SNAPSHOT\n"
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.5.4:test "
        '(default-test) on project network-domain-xsd: No tests matching pattern "FooTest" were executed! '
        "(Set -Dsurefire.failIfNoSpecifiedTests=false to ignore this error.) -> [Help 1]\n"
        "[ERROR] \n"
        "[ERROR] For more information about the errors and possible solutions, please read the following articles:\n"
        "[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException\n")
    sr = V.StepResult(step, exit_code=1, timed_out=False, gate_pass=False, output_tail=tail)
    errs = V.format_step_errors(sr)
    assert any(e.startswith("test command: $ mvn -B -pl dataaccessor -am") for e in errs)
    assert any("No tests matching pattern" in e for e in errs)


# ── _skip_patterns: CWE-1333 ReDoS / CWE-1286 regex injection ────────────────

def test_skip_patterns_wildcard_still_matches_as_substring(monkeypatch):
    # Existing dev-container config (*igw*,*hsm-proxy*) must keep working
    # identically after switching to the escape-then-restore-wildcard build.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", "*igw*,*hsm-proxy*")
    pats = V._skip_patterns()
    assert any(p.search("module-igw-core failed") for p in pats)
    assert any(p.search("hsm-proxy-adapter") for p in pats)
    assert not any(p.search("unrelated-module") for p in pats)


def test_skip_patterns_no_wildcard_still_matches_substring(monkeypatch):
    # A spec with no wildcards used to still substring-match via bare
    # re.compile(spec) — must be preserved by the escape step.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", "igw")
    pats = V._skip_patterns()
    assert any(p.search("some-module-igw-core failed") for p in pats)


def test_skip_patterns_regex_metacharacters_are_neutralised(monkeypatch):
    # The actual finding: a spec containing regex metacharacters other than
    # `*` must be treated as a LITERAL string, not compiled as regex syntax.
    # Before the fix, `(a+)+$` compiled as a nested-quantifier pattern with
    # catastrophic-backtracking potential; after the fix it only matches
    # itself literally.
    monkeypatch.setattr(_settings, "agentic_verify_skip_modules", r"(a+)+$")
    pats = V._skip_patterns()
    assert len(pats) == 1
    assert pats[0].search(r"(a+)+$")
    assert not pats[0].search("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!")


# ── Per-module Java detection (legacy "1.8" ≡ modern "8") + JDK-mismatch retry ──────

def _pom(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_java_major_normalises_both_version_schemes():
    # "java 8" and "java 1.8" are the SAME major — never two different answers.
    assert V._java_major_of("1.8") == 8
    assert V._java_major_of("8") == 8
    assert V._java_major_of("1.8.0_292") == 8
    assert V._java_major_of("17") == 17
    assert V._java_major_of("11") == 11
    assert V._java_major_of("1") is None                     # never a real release
    assert V._java_major_of("notaversion") is None
    assert V._java_major_of("${java.version}", {"java.version": "1.8"}) == 8
    assert V._java_major_of("${jdk}", {"jdk": "${java.version}", "java.version": "8"}) == 8
    assert V._java_major_of("${missing}", {}) is None


def test_detect_module_java_mixed_major_repo(tmp_path):
    # Root parent on 17 (via ${java.version}); one legacy module pinned to 1.8.
    # Each module resolves to its OWN major; the repo-wide max stays 17.
    _pom(tmp_path / "pom.xml",
         "<project><properties><java.version>17</java.version></properties>"
         "<maven.compiler.source>${java.version}</maven.compiler.source></project>")
    _pom(tmp_path / "legacy-igw" / "pom.xml",
         "<project><maven.compiler.source>1.8</maven.compiler.source>"
         "<maven.compiler.target>1.8</maven.compiler.target></project>")
    _pom(tmp_path / "modern" / "pom.xml", "<project></project>")
    assert V.detect_module_java(tmp_path, "legacy-igw") == 8   # own pom wins; 1.8 ≡ 8
    assert V.detect_module_java(tmp_path, "modern") == 17      # inherits the parent
    assert V.detect_required_java(tmp_path) == 17              # repo max unchanged


def test_detect_module_java_property_override_beats_parent(tmp_path):
    # Parent declares <source>${java.version}</source> with 17; the module overrides
    # the PROPERTY to 1.8 → the module's effective major is 8 (Maven's effective-pom
    # rule), not the parent's literal.
    _pom(tmp_path / "pom.xml",
         "<project><properties><java.version>17</java.version></properties>"
         "<maven.compiler.source>${java.version}</maven.compiler.source></project>")
    _pom(tmp_path / "legacy" / "pom.xml",
         "<project><properties><java.version>1.8</java.version></properties></project>")
    assert V.detect_module_java(tmp_path, "legacy") == 8


class _EnvExec:
    """Fake adapter that accepts env_overrides: builds fail on the default JVM; a
    build under any matching /fake/jdkN JAVA_HOME passes — unless `stubborn`, where
    even the right JDK doesn't save it (a real code failure in a legacy module)."""
    def __init__(self, fail_out, stubborn=False):
        self.fail_out = fail_out
        self.stubborn = stubborn
        self.calls = []
        self.argvs = []

    def run_command(self, cwd, argv, timeout_s=None, env_overrides=None):
        self.calls.append((str(cwd), env_overrides))
        self.argvs.append(list(argv))
        if (not self.stubborn and env_overrides
                and "/fake/jdk" in (env_overrides.get("JAVA_HOME") or "")):
            return _res(0)
        return _res(1, stdout=self.fail_out)


def _mixed_repo(tmp_path):
    _pom(tmp_path / "pom.xml",
         "<project><properties><java.version>17</java.version></properties>"
         "<maven.compiler.source>${java.version}</maven.compiler.source>"
         "<module>legacy-igw</module></project>")
    _pom(tmp_path / "legacy-igw" / "pom.xml",
         "<project><maven.compiler.source>1.8</maven.compiler.source></project>")
    return tmp_path


def _patch_jdk_env(monkeypatch, tmp_path, *, installed=(8,)):
    from app.agents import jdk_discovery, workspace_local
    monkeypatch.setattr(workspace_local, "repo_dir", lambda run, rid: tmp_path)
    monkeypatch.setattr(jdk_discovery, "select_jdk_home",
                        lambda req, **kw: f"/fake/jdk{req}" if req in installed else None)
    monkeypatch.setattr(V, "_active_jdk_major", lambda: 17)


def test_jdk_mismatch_retry_builds_legacy_java8_module(monkeypatch, tmp_path):
    # Mixed-major reactor: the full build fails ONLY inside the module whose own pom
    # declares 1.8, while the step ran on JDK 17. The runtime must rebuild that module
    # from its own dir with the installed JDK 8 and lift it out of the verdict —
    # BUILT, not skipped.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8,))      # JDK 8 installed, 17 not
    fail = ("[INFO] Building legacy-igw 1.0\n"
            "[INFO] --- maven-compiler-plugin:3.11:compile (default) @ legacy-igw ---\n"
            f"[ERROR] {repo}/legacy-igw/src/Bar.java:[5,1] incompatible types\n"
            "[INFO] BUILD FAILURE\n")
    step = VerificationStep("install", "r1", ["mvn", "-B", "install"],
                            module="core (full build)", subdir=None)
    ex = _EnvExec(fail)
    out = run_plan(None, RUN, [step], touched_modules=set(), soft_fail_modules=set(), executor=ex)
    assert out.status == "verified" and out.gates["compile"] is True
    assert out.module_results["core (full build)"]["status"] == "built"
    # The rescue is never a silent green — the rebuild note names module + JDK.
    assert any("JDK 8" in n for n in out.module_results["core (full build)"]["errors"])
    # Retry ran FROM the module dir WITH the matching JAVA_HOME.
    retry_calls = [c for c in ex.calls if c[1]]
    assert retry_calls and retry_calls[0][0].endswith("legacy-igw")
    assert retry_calls[0][1]["JAVA_HOME"] == "/fake/jdk8"


def test_no_retry_when_module_major_matches_the_jvm(monkeypatch, tmp_path):
    # The failing module already built on its own declared major → a real code
    # failure; the retry must NOT fire and the verdict stays needs_fix.
    _pom(tmp_path / "pom.xml", "<project></project>")
    _pom(tmp_path / "legacy-igw" / "pom.xml",
         "<project><maven.compiler.source>17</maven.compiler.source></project>")
    _patch_jdk_env(monkeypatch, tmp_path, installed=())
    fail = (f"[ERROR] {tmp_path}/legacy-igw/src/Bar.java:[5,1] cannot find symbol\n"
            "[INFO] BUILD FAILURE\n")
    step = VerificationStep("install", "r1", ["mvn", "-B", "install"],
                            module="core (full build)", subdir=None)
    ex = _EnvExec(fail)
    out = run_plan(None, RUN, [step], touched_modules=set(), soft_fail_modules=set(), executor=ex)
    assert out.status == "needs_fix" and out.gates["compile"] is False
    assert not [c for c in ex.calls if c[1]], "no JAVA_HOME retry for a matching major"


# ── Detection edge cases ────────────────────────────────────────────────────────────

def test_java_major_edge_values():
    assert V._java_major_of("  1.8  ") == 8                  # whitespace tolerated
    assert V._java_major_of("") is None
    assert V._java_major_of(None) is None
    assert V._java_major_of("21-ea") == 21                   # early-access suffix
    # A property CYCLE must terminate and answer "undeclared", never hang.
    assert V._java_major_of("${a}", {"a": "${b}", "b": "${a}"}) is None


def test_detect_module_java_nested_module_nearest_ancestor_wins(tmp_path):
    # repo(17) > mid(11) > leaf(no decl): leaf resolves to the NEAREST declaring
    # ancestor (11), not the repo root; a leaf with its own 1.8 resolves to 8.
    _pom(tmp_path / "pom.xml",
         "<project><maven.compiler.source>17</maven.compiler.source></project>")
    _pom(tmp_path / "mid" / "pom.xml",
         "<project><maven.compiler.source>11</maven.compiler.source></project>")
    _pom(tmp_path / "mid" / "leaf" / "pom.xml", "<project></project>")
    _pom(tmp_path / "mid" / "pinned" / "pom.xml",
         "<project><java.version>1.8</java.version></project>")
    assert V.detect_module_java(tmp_path, "mid/leaf") == 11
    assert V.detect_module_java(tmp_path, "mid/pinned") == 8
    assert V.detect_module_java(tmp_path, None) == 17        # root/reactor question
    assert V.detect_module_java(tmp_path, ".") == 17


def test_detect_module_java_source_target_max_and_no_pom(tmp_path):
    # Within ONE pom, source=8 target=11 → the higher bound wins (the JVM must
    # satisfy both); a tree with no pom anywhere answers None (don't guess).
    _pom(tmp_path / "m" / "pom.xml",
         "<project><maven.compiler.source>1.8</maven.compiler.source>"
         "<maven.compiler.target>11</maven.compiler.target></project>")
    assert V.detect_module_java(tmp_path, "m") == 11
    assert V.detect_module_java(tmp_path / "empty", "x") is None


def test_detect_required_java_resolves_root_property_in_child(tmp_path):
    # Child pom consumes a property DECLARED ONLY at the root: the repo scan must
    # resolve it (the pre-fix numeric-only regex skipped ${...} values entirely).
    _pom(tmp_path / "pom.xml",
         "<project><properties><java.version>21</java.version></properties></project>")
    _pom(tmp_path / "child" / "pom.xml",
         "<project><maven.compiler.release>${java.version}</maven.compiler.release></project>")
    assert V.detect_required_java(tmp_path) == 21


# ── Upfront per-step JDK selection ──────────────────────────────────────────────────

def _step(argv=None, module="core (full build)", subdir=None, kind="install"):
    return VerificationStep(kind, "r1", argv or ["mvn", "-B", "install"],
                            module=module, subdir=subdir)


def test_build_from_dir_step_selects_module_jdk_upfront(monkeypatch, tmp_path):
    # A build-from-dir step for the 1.8 module runs on JDK 8 from the FIRST attempt —
    # no failure, no retry needed.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8, 17))
    ex = _EnvExec("unused")
    out = run_plan(None, RUN, [_step(module="legacy-igw", subdir="legacy-igw")],
                   touched_modules=set(), soft_fail_modules=set(), executor=ex)
    assert out.status == "verified"
    assert ex.calls[0][0].endswith("legacy-igw")
    assert ex.calls[0][1] == {"JAVA_HOME": "/fake/jdk8"} and len(ex.calls) == 1


def test_reactor_step_falls_back_to_repo_max_jdk(monkeypatch, tmp_path):
    # A reactor-root step (subdir None) keeps the repo-wide max (17 here).
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8, 17))
    ex = _EnvExec("unused")
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "verified"
    assert ex.calls[0][1] == {"JAVA_HOME": "/fake/jdk17"}


# ── Retry scenarios ────────────────────────────────────────────────────────────────

def _fail_lines(repo, *rel_files):
    lines = "".join(f"[ERROR] {repo}/{f}:[5,1] incompatible types\n" for f in rel_files)
    return f"{lines}[INFO] BUILD FAILURE\n"


def test_retry_surfaces_missing_jdk_as_actionable_note(monkeypatch, tmp_path):
    # Mismatch detected but JDK 8 is NOT installed: the failure stays, and the
    # module report LEADS with "install JDK 8", not a cryptic compile error.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=())
    ex = _EnvExec(_fail_lines(repo, "legacy-igw/src/Bar.java"))
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "needs_fix"
    errs = out.module_results["core (full build)"]["errors"]
    assert errs and "no JDK 8 is installed" in errs[0]
    assert not [c for c in ex.calls if c[1]], "no retry without a matching JDK"


def test_retry_that_still_fails_keeps_the_failure(monkeypatch, tmp_path):
    # Right JDK, still red → a REAL code failure in the legacy module: verdict stands.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8,))
    ex = _EnvExec(_fail_lines(repo, "legacy-igw/src/Bar.java"), stubborn=True)
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "needs_fix" and out.gates["compile"] is False
    assert out.module_results["core (full build)"]["status"] == "failed"


def test_retry_rescues_multiple_mismatched_modules(monkeypatch, tmp_path):
    # TWO legacy 1.8 modules fail in one reactor pass → both rebuilt on JDK 8.
    _pom(tmp_path / "pom.xml",
         "<project><maven.compiler.source>17</maven.compiler.source></project>")
    for m in ("legacy-a", "legacy-b"):
        _pom(tmp_path / m / "pom.xml",
             "<project><maven.compiler.source>1.8</maven.compiler.source></project>")
    _patch_jdk_env(monkeypatch, tmp_path, installed=(8,))
    ex = _EnvExec(_fail_lines(tmp_path, "legacy-a/src/A.java", "legacy-b/src/B.java"))
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "verified"
    notes = out.module_results["core (full build)"]["errors"]
    assert len(notes) == 2 and all("JDK 8" in n for n in notes)
    retry_dirs = sorted(c[0] for c in ex.calls if c[1])
    assert retry_dirs[0].endswith("legacy-a") and retry_dirs[1].endswith("legacy-b")


def test_retry_refuses_when_any_failing_module_is_not_a_mismatch(monkeypatch, tmp_path):
    # One failing module is 1.8 (mismatch) but another failed ON its declared major —
    # the breakage is not purely toolchain, so the verdict must stand.
    _pom(tmp_path / "pom.xml", "<project></project>")
    _pom(tmp_path / "legacy-igw" / "pom.xml",
         "<project><maven.compiler.source>1.8</maven.compiler.source></project>")
    _pom(tmp_path / "ok17" / "pom.xml",
         "<project><maven.compiler.source>17</maven.compiler.source></project>")
    # 17 deliberately NOT installed: the reactor runs on the active JDK 17 anyway
    # (ok17 fails ON its declared major → a real code failure, not a mismatch).
    _patch_jdk_env(monkeypatch, tmp_path, installed=(8,))
    ex = _EnvExec(_fail_lines(tmp_path, "legacy-igw/src/A.java", "ok17/src/B.java"))
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "needs_fix" and out.gates["compile"] is False


def test_retry_cap_stops_systemic_breakage(monkeypatch, tmp_path):
    # More than _JDK_RETRY_CAP failing modules = systemic, not a toolchain mismatch.
    # Root pins 17 (not installed) so the reactor runs on the active JDK and fails.
    _pom(tmp_path / "pom.xml",
         "<project><maven.compiler.source>17</maven.compiler.source></project>")
    mods = [f"m{i}" for i in range(1, 7)]
    for m in mods:
        _pom(tmp_path / m / "pom.xml",
             "<project><maven.compiler.source>1.8</maven.compiler.source></project>")
    _patch_jdk_env(monkeypatch, tmp_path, installed=(8,))
    ex = _EnvExec(_fail_lines(tmp_path, *[f"{m}/src/X.java" for m in mods]))
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "needs_fix"
    assert not [c for c in ex.calls if c[1]], "no per-module retries above the cap"


def test_retry_strips_reactor_selectors_from_argv(monkeypatch, tmp_path):
    # `-pl <module> -am` resolves against the ROOT reactor; the retry runs FROM the
    # module dir, so those selectors must be stripped or Maven fails spuriously.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8,))
    ex = _EnvExec(_fail_lines(repo, "legacy-igw/src/Bar.java"))
    step = _step(argv=["mvn", "-pl", "legacy-igw", "-am", "-B", "clean", "install"])
    out = run_plan(None, RUN, [step], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "verified"
    retry_argv = [a for c, a in zip(ex.calls, ex.argvs) if c[1]][0]
    assert "-pl" not in retry_argv and "-am" not in retry_argv
    assert "install" in retry_argv and "clean" in retry_argv


def test_retry_never_targets_the_repo_root(monkeypatch, tmp_path):
    # A failing file directly under the repo root resolves to module "." — rebuilding
    # the whole reactor is NOT a targeted retry, even when the root declares 1.8 and
    # JDK 8 is installed (a full-reactor rerun on jdk8 would wrongly go green here).
    _pom(tmp_path / "pom.xml",
         "<project><maven.compiler.source>1.8</maven.compiler.source></project>")
    _pom(tmp_path / "modern" / "pom.xml",
         "<project><maven.compiler.source>17</maven.compiler.source></project>")
    _patch_jdk_env(monkeypatch, tmp_path, installed=(8,))
    ex = _EnvExec(_fail_lines(tmp_path, "src/Root.java"))
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules=set(), executor=ex)
    assert out.status == "needs_fix"
    assert not [c for c in ex.calls if c[1]]


def test_retry_tolerates_executors_without_env_overrides(monkeypatch, tmp_path):
    # An adapter that doesn't accept env_overrides (older backends / fakes) must not
    # crash the gate — the retry degrades to "no rescue", verdict unchanged.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8,))
    fail = _fail_lines(repo, "legacy-igw/src/Bar.java")
    out = run_plan(None, RUN, [_step()], touched_modules=set(), soft_fail_modules=set(),
                   executor=_Exec(lambda a: _res(1, stdout=fail)))
    assert out.status == "needs_fix" and out.gates["compile"] is False


def _module_attributed_fail(repo, module):
    # Soft-fail attribution needs the module markers in the reactor log, not just
    # a bare [ERROR] path line.
    return (f"[INFO] Building {module} 1.0\n"
            f"[INFO] --- maven-compiler-plugin:3.11:compile (default) @ {module} ---\n"
            f"[ERROR] {repo}/{module}/src/Bar.java:[5,1] incompatible types\n"
            "[INFO] BUILD FAILURE\n")


def test_soft_listed_module_is_rescued_not_excluded(monkeypatch, tmp_path):
    # A module on the soft list that fails on a JDK mismatch must be BUILT by the
    # rescue, not merely excluded — soft-fail is the fallback, never the first answer.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8,))
    ex = _EnvExec(_module_attributed_fail(repo, "legacy-igw"))
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules={"legacy-igw"}, executor=ex)
    assert out.status == "verified"
    notes = out.module_results["core (full build)"]["errors"]
    assert notes and "JDK 8" in notes[0], "rescued (built), not silently excluded"
    assert any(c[1] == {"JAVA_HOME": "/fake/jdk8"} for c in ex.calls)


def test_soft_exclusion_still_covers_a_genuinely_broken_module(monkeypatch, tmp_path):
    # Right JDK, still red (rotten legacy code): the soft exclusion keeps an
    # UNTOUCHED out-of-scope module from condemning the run — the operator's
    # last-resort escape hatch, applied only after the rescue failed.
    repo = _mixed_repo(tmp_path)
    _patch_jdk_env(monkeypatch, repo, installed=(8,))
    ex = _EnvExec(_module_attributed_fail(repo, "legacy-igw"), stubborn=True)
    out = run_plan(None, RUN, [_step()], touched_modules=set(),
                   soft_fail_modules={"legacy-igw"}, executor=ex)
    assert out.status == "verified" and out.gates["compile"] is True
    assert any(c[1] for c in ex.calls), "the rescue was attempted first"
    assert out.module_results["core (full build)"]["errors"] == [], "plain soft-pass"
